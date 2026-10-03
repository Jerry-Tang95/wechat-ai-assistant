# -*- coding: utf-8 -*-
"""本地 Web 控制台：REST API + 托管单文件控制台页面"""
import json
import os
import threading

from flask import Flask, jsonify, request, Response

import storage

_DIR = os.path.dirname(os.path.abspath(__file__))


def create_app(engine, adapter, mock_mode=False):
    app = Flask(__name__, static_folder=None)

    def save_config(cfg):
        with open(os.path.join(_DIR, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        engine.reload_config(cfg)

    @app.get("/")
    def index():
        with open(os.path.join(_DIR, "console.html"), encoding="utf-8") as f:
            return Response(f.read(), mimetype="text/html",
                            headers={"Cache-Control": "no-store"})

    @app.get("/api/state")
    def state():
        cfg = engine.config
        return jsonify({
            "status": {**adapter.status(), "mock": mock_mode},
            "stats": storage.stats_today(),
            "messages": storage.get_messages(200),
            "drafts": storage.get_drafts("pending"),
            "alerts": storage.get_alerts(30),
            "suspended": sorted(engine.suspended),
            "llm_enabled": engine.llm.enabled,
            "config": cfg,
        })

    @app.post("/api/draft/send")
    def draft_send():
        d = request.get_json(force=True)
        text = (d.get("content") or "").strip()
        if not text:
            return jsonify({"ok": False, "error": "内容为空"}), 400
        storage.update_draft(d["id"], "sent", text)
        engine.send_queue.put((d["chat"], text, "人工审核发送"))
        return jsonify({"ok": True})

    @app.post("/api/draft/reject")
    def draft_reject():
        d = request.get_json(force=True)
        storage.update_draft(d["id"], "rejected")
        return jsonify({"ok": True})

    @app.post("/api/chat/resume")
    def chat_resume():
        d = request.get_json(force=True)
        engine.resume_chat(d["chat"])
        return jsonify({"ok": True})

    @app.post("/api/alerts/read")
    def alerts_read():
        storage.mark_alerts_read()
        return jsonify({"ok": True})

    @app.post("/api/config")
    def config_save():
        try:
            cfg = request.get_json(force=True)
            json.dumps(cfg)  # 校验可序列化
            save_config(cfg)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

    @app.post("/api/test/inject")
    def test_inject():
        if not mock_mode:
            return jsonify({"ok": False, "error": "仅 Mock 模式可用"}), 400
        d = request.get_json(force=True)
        adapter.inject(d.get("chat", "测试用户"), d.get("content", ""),
                       sender=d.get("sender"), mtype=d.get("mtype", "文本"))
        return jsonify({"ok": True})

    return app


def run_app(engine, adapter, mock_mode, port=None):
    app = create_app(engine, adapter, mock_mode)
    port = port or int(engine.config.get("web", {}).get("port", 8765))
    t = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True)
    t.start()
    return f"http://127.0.0.1:{port}"
