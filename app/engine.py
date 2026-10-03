# -*- coding: utf-8 -*-
"""消息引擎：三大角色分流 + 规则优先 + LLM 兜底 + 安全兜底

角色路由（adapter.pull 返回的每条消息按序判断）：
1. 白名单客户（monitor.auto_reply 配置）→ 规则命中直发话术 / LLM 生成直发
2. 群监控（monitor.group_monitor 配置）→ 关键词命中发提醒；keyword_replies 可配自动回复
3. AI 分身（monitor.avatar，只对私聊）→ 按分身模式：review 生成草稿待审 / auto 直发
非文本消息（图片/动画表情/文件等）只留痕不回复。
"""
import re
import threading
from datetime import datetime, time as dtime

import storage
from adapter import is_non_text
from llm import LLMError
import tools as realtime_tools


class Engine:
    def __init__(self, adapter, llm, config, send_queue):
        self.adapter = adapter
        self.llm = llm
        self.config = config
        self.send_queue = send_queue        # 队列元素: (chat, text, action)
        self.suspended = set()              # 已转人工的会话
        self._last_sent = {}                # chat -> timestamp（频控）
        self._last_seen = {}                # chat -> (count, preview) 去重

    # ---------- 配置热加载 ----------
    def reload_config(self, new_cfg=None):
        if new_cfg is not None:
            self.config = new_cfg
        self.llm.api_key = (self.config["llm"].get("api_key") or "").strip()
        self.llm.model = self.config["llm"].get("model", "glm-4-flash")

    def _auto_map(self):
        return {e["chat"]: e for e in self.config["monitor"].get("auto_reply", [])}

    def _group_map(self):
        return {e["chat"]: e for e in self.config["monitor"].get("group_monitor", [])}

    # ---------- 状态查询（控制台用） ----------
    def is_monitored(self, chat):
        if chat in self._auto_map() or chat in self._group_map():
            return True
        av = self.config["monitor"].get("avatar", {})
        if not av.get("enabled"):
            return False
        return chat not in av.get("exclude", [])

    # ---------- 消息入口 ----------
    def handle_incoming(self, chat, preview, count=1, row_time=""):
        """处理一轮扫描到的未读会话。preview 即对方最新一条消息。
        去重键 = (会话行内时间, 预览)：新消息必然更新行内时间，用户手动点开
        聊天清零徽标不会改变它——彻底避免"看了微信之后再来的消息被吞"。
        同一分钟内重发相同内容（时间/预览都不变）用未读数增加来辅助触发。"""
        preview = (preview or "").strip()
        state = (row_time, preview)
        last = self._last_seen.get(chat)          # (row_time, preview, count)
        if last is not None:
            if state == (last[0], last[1]) and count <= last[2]:
                return
        self._last_seen[chat] = (row_time, preview, count)
        storage.add_message(chat, chat, preview, "文本", "in", "received")
        if count > 1:
            storage.add_message(chat, "系统",
                                f"对方连发{count}条，本轮按最新一条处理", "系统", "sys", "logged")
        if not preview or is_non_text(preview):
            return
        if count > 1:
            storage.add_message(chat, "系统",
                                f"对方连发{count}条，本轮按最新一条处理", "系统", "sys", "logged")
        if not preview or is_non_text(preview):
            return
        safe = self.config.get("safety", {})
        if any(w in preview for w in safe.get("suspend_keywords", [])):
            self.suspended.add(chat)
            storage.add_message(chat, "系统", "已暂停该会话自动回复（触发转人工关键词）",
                                "系统", "sys", "suspended")
            return
        if chat in self._auto_map():
            self._handle_auto(chat, self._auto_map()[chat], preview)
        elif chat in self._group_map():
            self._handle_group(chat, self._group_map()[chat], preview)
        else:
            self._handle_avatar(chat, preview)

    # ---------- 角色1：白名单客户自动回复 ----------
    def _handle_auto(self, chat, cfg, content):
        for rule in cfg.get("static_rules", []):
            if re.search(rule["match"], content):
                self._dispatch(chat, rule["reply"], "规则直发", mode="auto")
                return
        persona = cfg.get("persona", "你是一个友好的客服助手，简洁回复。")
        self._llm_reply(chat, persona, content, mode=cfg.get("mode", "auto"), reason="白名单客户")

    # ---------- 角色2：群消息监控 ----------
    def _handle_group(self, chat, cfg, content):
        hit = next((k for k in cfg.get("keywords", []) if k in content), None)
        if hit:
            storage.add_alert(chat, hit, content)
        for k, v in cfg.get("keyword_replies", {}).items():
            if k in content:
                self._dispatch(chat, v, "群关键词回复", mode="auto")
                return

    # ---------- 角色3：AI 分身（其余私聊） ----------
    def _handle_avatar(self, chat, content):
        av = self.config["monitor"].get("avatar", {})
        if not av.get("enabled"):
            return
        if chat in av.get("exclude", []):
            return
        try:
            if self.adapter.is_group(chat):
                storage.add_message(chat, "系统", "未配置的群聊有新消息（仅记录）",
                                    "系统", "sys", "logged")
                return
        except Exception:
            pass  # 判断失败就当私聊处理
        persona = av.get("persona", "你是账号主人本人，口语化简短回复。")
        self._llm_reply(chat, persona, content, mode=av.get("mode", "review"), reason="AI分身")

    # ---------- LLM 生成（放后台线程，避免阻塞轮询） ----------
    def _llm_reply(self, chat, persona, content, mode, reason):
        safe = self.config.get("safety", {})
        sensitive = next((w for w in safe.get("sensitive_words", []) if w in content), None)
        extra = "对方消息涉及资金/密码等敏感内容，务必提醒对方注意安全，不要索要任何账号信息。" if sensitive else ""

        def worker():
            extra_rules = extra
            # 实时数据工具：命中的先拉真实数据注入，LLM 据此作答
            tname = realtime_tools.match_tool(content)
            if tname and tname not in realtime_tools.DEFAULT_DISABLED:
                pass
            elif tname and self.config.get("tools", {}).get(tname, {}).get("enabled"):
                pass
            else:
                tname = None
            if tname:
                # 人设范围门禁：超出人设业务范围的问题不调工具，改为礼貌拒答
                try:
                    in_scope = self.llm.scope_check(persona, content)
                except Exception as e:
                    in_scope = True
                    storage.add_message(chat, "系统", f"范围门禁异常(按在范围处理): {e}",
                                        "系统", "sys", "tool_error")
                if in_scope:
                    try:
                        ctx = realtime_tools.run_tool(tname, content)
                        if ctx:
                            extra_rules = (extra_rules + "\n" + ctx).strip()
                            storage.add_message(chat, "系统", f"已调用实时工具：{tname}",
                                                "系统", "sys", "tool")
                    except Exception as e:
                        storage.add_message(chat, "系统", f"工具 {tname} 调用失败: {e}",
                                            "系统", "sys", "tool_error")
                else:
                    extra_rules = (extra_rules + "\n【范围边界】对方的问题超出你人设的业务范围："
                                   "不要回答该问题的任何具体内容，用一句话礼貌说明你只负责人设相关的咨询，"
                                   "并自然引导对方提出相关方面的问题。").strip()
                    storage.add_message(chat, "系统", f"范围门禁拦截：{tname}（超出人设业务）",
                                        "系统", "sys", "tool")
            if not self.llm.enabled:
                self._make_draft(chat, content, "未配置LLM-API key，仅生成占位草稿")
                return
            try:
                reply = self.llm.generate(chat, persona, content, extra_rules=extra_rules)
            except LLMError as e:
                storage.add_message(chat, "系统", f"LLM 生成失败: {e}", "系统", "sys", "llm_error")
                # 兜底：生成一条待发草稿，客户不被晾着，主人一键补发
                self._make_draft(chat, "稍等哈，我确认一下再详细回复你",
                                 f"LLM失败兜底（{reason}）")
                return
            if mode == "auto":
                self._dispatch(chat, reply, f"{reason}-LLM", mode="auto", sensitive=sensitive)
            else:
                if sensitive:
                    storage.add_alert(chat, f"敏感词:{sensitive}", content)
                self._make_draft(chat, reply, reason)

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 动作执行 ----------
    def _dispatch(self, chat, text, action, mode="auto", sensitive=None):
        """auto=排队直发；其他=生成草稿"""
        if mode != "auto" or sensitive:
            if sensitive:
                self._make_draft(chat, text, f"{action}-含敏感词转人工")
            else:
                self._make_draft(chat, text, action)
            return
        if not self._safety_ok(chat):
            self._make_draft(chat, text, f"{action}-触发频控/时段限制")
            return
        self.send_queue.put((chat, text, action))

    def _make_draft(self, chat, content, reason, force_draft=False):
        storage.add_draft(chat, content, reason)

    def _safety_ok(self, chat):
        safe = self.config.get("safety", {})
        # 工作时段
        try:
            now = datetime.now().time()
            start = dtime(*map(int, safe.get("work_start", "08:00").split(":")))
            end = dtime(*map(int, safe.get("work_end", "23:00").split(":")))
            if not (start <= now <= end):
                return False
        except Exception:
            pass
        # 频控：同一会话最小间隔
        import time as _t
        last = self._last_sent.get(chat, 0)
        if _t.time() - last < float(safe.get("min_interval_sec", 5)):
            return False
        # 每日上限
        if storage.today_sent_count(chat) >= int(safe.get("per_chat_daily_limit", 30)):
            return False
        return True

    def record_sent(self, chat):
        import time as _t
        self._last_sent[chat] = _t.time()

    def resume_chat(self, chat):
        self.suspended.discard(chat)
