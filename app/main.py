# -*- coding: utf-8 -*-
"""微信AI助手入口：监听线程 + UI 工作队列 + Web 控制台
用法: python main.py [--mock] [--no-browser] [--port N]
日志: app/run.log（滚动保留2MB×3）"""
import json
import logging
import os
import queue
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

import storage
from adapter import PyWeixinAdapter, MockAdapter
from engine import Engine
from llm import ZhipuLLM, MockLLM
from webapp import run_app

_DIR = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(_DIR, "config.json")

# ---------- 日志（进程级保命：任何线程的异常都要留痕） ----------
LOG = logging.getLogger("wechatai")
LOG.setLevel(logging.INFO)
_h = RotatingFileHandler(os.path.join(_DIR, "run.log"),
                         maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s [%(threadName)s] %(levelname)s %(message)s"))
LOG.addHandler(_h)


def _thread_exc_hook(args):
    LOG.error("线程异常 %s: %s: %s", args.thread.name,
              args.exc_type.__name__, args.exc_value,
              exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


threading.excepthook = _thread_exc_hook


def load_config():
    path = CFG_PATH
    if not os.path.exists(path):
        example = os.path.join(_DIR, "config.example.json")
        if os.path.exists(example):
            import shutil
            shutil.copyfile(example, path)
            print(f"已从 config.example.json 生成默认配置: {path}（请填入 llm.api_key）")
        else:
            raise FileNotFoundError(f"找不到配置文件: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    mock = "--mock" in sys.argv
    port_override = None
    if "--port" in sys.argv:
        port_override = int(sys.argv[sys.argv.index("--port") + 1])
    cfg = load_config()
    port = port_override or int(cfg.get("web", {}).get("port", 8765))
    storage.init(os.path.join(_DIR, "data.db"))

    adapter = MockAdapter() if mock else PyWeixinAdapter()
    llm = MockLLM() if mock else ZhipuLLM(
        cfg["llm"].get("api_key", ""), cfg["llm"].get("model", "glm-4-flash"),
        cfg["llm"].get("base_url", "https://open.bigmodel.cn/api/paas/v4"))

    send_queue = queue.Queue()
    engine = Engine(adapter, llm, cfg, send_queue)

    state = {"running": True, "last_poll": "", "last_error": ""}

    def ui_worker():
        """唯一的 UI 操作线程：串行处理发送任务和消息轮询。任何异常都不允许杀死本线程。"""
        LOG.info("轮询线程启动 mode=%s interval=%ss",
                 "mock" if mock else "real",
                 engine.config["monitor"].get("poll_interval_sec", 3))
        interval = float(engine.config["monitor"].get("poll_interval_sec", 3))
        while state["running"]:
            try:
                # 1) 发送队列
                while True:
                    chat, text, action = send_queue.get_nowait()
                    try:
                        adapter.send(chat, text)
                        storage.add_message(chat, "我", text, "文本", "out", action)
                        engine.record_sent(chat)
                        LOG.info("[发送] %s: %s… (%s)", chat, text[:30], action)
                    except Exception as e:
                        LOG.error("[发送失败] %s: %s", chat, e)
                        storage.add_message(chat, "系统", f"发送失败: {e}",
                                            "系统", "sys", "send_error")
            except queue.Empty:
                pass
            except Exception as e:
                LOG.error("发送队列处理异常: %s", e, exc_info=True)
            # 2) 轮询新消息
            try:
                unread = adapter.scan_unread()
                state["last_poll"] = time.strftime("%H:%M:%S")
                state["last_error"] = ""
                if unread:
                    LOG.info("[轮询] 未读: %s",
                             {k: v["count"] for k, v in unread.items()})
                for chat, info in unread.items():
                    if not engine.is_monitored(chat):
                        continue
                    engine.handle_incoming(chat, info.get("preview", ""),
                                           int(info.get("count", 1)),
                                           info.get("time", ""))
            except Exception as e:
                state["last_error"] = f"{type(e).__name__}: {e}"
                LOG.error("轮询异常: %s", e, exc_info=True)
            time.sleep(interval)

    threading.Thread(target=ui_worker, daemon=True, name="ui-worker").start()
    url = run_app(engine, adapter, mock, port)
    LOG.info("启动完成 mode=%s 控制台=%s", "mock" if mock else "real", url)
    print("=" * 50)
    print(f"  微信AI助手已启动 (模式: {'Mock' if mock else '真实微信'})")
    print(f"  控制台: {url}")
    print(f"  日志: {os.path.join(_DIR, 'run.log')}")
    print("=" * 50)
    # 页面打开交给启动脚本（bat）负责；看门狗复活实例时不再弹新标签页
    try:
        while True:
            time.sleep(1)
    except Exception:
        LOG.exception("主线程异常退出")
        state["running"] = False


if __name__ == "__main__":
    main()
