# -*- coding: utf-8 -*-
"""SQLite 存储：消息日志 / 待审草稿 / 群提醒 / 统计"""
import sqlite3
import threading
import time
from datetime import datetime

_lock = threading.RLock()  # 可重入：stats_today 内部会调 unread_alert_count
_conn = None


def init(db_path):
    global _conn
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.execute("""CREATE TABLE IF NOT EXISTS messages(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, chat TEXT, sender TEXT, content TEXT,
        mtype TEXT, direction TEXT, action TEXT)""")
    _conn.execute("""CREATE TABLE IF NOT EXISTS drafts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, chat TEXT, content TEXT, reason TEXT, status TEXT)""")
    _conn.execute("""CREATE TABLE IF NOT EXISTS alerts(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT, chat TEXT, keyword TEXT, content TEXT, read INTEGER DEFAULT 0)""")
    _conn.commit()


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def add_message(chat, sender, content, mtype="文本", direction="in", action=""):
    with _lock:
        _conn.execute(
            "INSERT INTO messages(ts,chat,sender,content,mtype,direction,action) VALUES(?,?,?,?,?,?,?)",
            (_now(), chat, sender, content, mtype, direction, action))
        _conn.commit()


def get_messages(limit=200):
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(zip(["id", "ts", "chat", "sender", "content", "mtype", "direction", "action"], r))
            for r in reversed(rows)]


def add_draft(chat, content, reason=""):
    with _lock:
        cur = _conn.execute(
            "INSERT INTO drafts(ts,chat,content,reason,status) VALUES(?,?,?,?,?)",
            (_now(), chat, content, reason, "pending"))
        _conn.commit()
        return cur.lastrowid


def get_drafts(status="pending"):
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM drafts WHERE status=? ORDER BY id DESC", (status,)).fetchall()
    return [dict(zip(["id", "ts", "chat", "content", "reason", "status"], r)) for r in rows]


def update_draft(draft_id, status, content=None):
    with _lock:
        if content is not None:
            _conn.execute("UPDATE drafts SET status=?, content=? WHERE id=?",
                          (status, content, draft_id))
        else:
            _conn.execute("UPDATE drafts SET status=? WHERE id=?", (status, draft_id))
        _conn.commit()


def add_alert(chat, keyword, content):
    with _lock:
        _conn.execute("INSERT INTO alerts(ts,chat,keyword,content,read) VALUES(?,?,?,?,0)",
                      (_now(), chat, keyword, content))
        _conn.commit()


def get_alerts(limit=50):
    with _lock:
        rows = _conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(zip(["id", "ts", "chat", "keyword", "content", "read"], r)) for r in rows]


def mark_alerts_read():
    with _lock:
        _conn.execute("UPDATE alerts SET read=1")
        _conn.commit()


def unread_alert_count():
    with _lock:
        return _conn.execute("SELECT COUNT(*) FROM alerts WHERE read=0").fetchone()[0]


def today_sent_count(chat):
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat=? AND direction='out' AND ts LIKE ?",
            (chat, today + "%")).fetchone()
    return row[0]


def stats_today():
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        c_in = _conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='in' AND ts LIKE ?",
            (today + "%",)).fetchone()[0]
        c_out = _conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction='out' AND ts LIKE ?",
            (today + "%",)).fetchone()[0]
        c_pending = _conn.execute(
            "SELECT COUNT(*) FROM drafts WHERE status='pending'").fetchone()[0]
        c_alert = unread_alert_count()
    return {"in": c_in, "out": c_out, "pending": c_pending, "alerts": c_alert}
