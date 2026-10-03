# -*- coding: utf-8 -*-
"""大模型客户端：智谱 GLM 免费档，带每会话上下文记忆
网络策略：读超时 90s、瞬时失败自动重试 2 次、4xx 不重试
风格策略：上下文只存摘要（防长文风格传染）、系统级微信体输出纪律、回复去 Markdown"""
import re
import threading
import time

import requests


class LLMError(Exception):
    pass


# 系统级输出纪律：放在 system 消息最末尾（最后一条指令权重最高）
CHAT_DISCIPLINE = (
    "【微信聊天输出纪律】只回答当前问题本身，像真人发微信一样口语化、简短（一般不超过80字）；"
    "不要展开无关话题、背景科普或分类罗列；禁止使用任何Markdown符号（#、*、列表序号）；"
    "一次说不完就引导对方到院面诊或留联系方式。")


def _strip_markdown(text):
    text = re.sub(r"#{1,6}\s*", "", text)
    text = text.replace("**", "").replace("*", "").replace("`", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class ZhipuLLM:
    def __init__(self, api_key, model="glm-4-flash", base_url="https://open.bigmodel.cn/api/paas/v4"):
        self.api_key = (api_key or "").strip()
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.contexts = {}          # chat -> list[(role, content)]
        self._ctx_lock = threading.Lock()
        self.max_context = 8        # 每会话保留最近8条（更少token、更快、更少风格传染）
        self.timeout = (10, 90)     # 连接, 读取
        self.retries = 2            # 超时/5xx 自动重试次数
        self.max_tokens = 350       # 控制回复长度与生成耗时

    @property
    def enabled(self):
        return bool(self.api_key)

    def _remember(self, chat, role, content):
        # 助手长回复只存前80字：保住话题连续性，防止长文风格污染后续回复
        if role == "assistant" and len(content) > 80:
            content = content[:80] + "…"
        with self._ctx_lock:
            ctx = self.contexts.setdefault(chat, [])
            ctx.append((role, content))
            if len(ctx) > self.max_context:
                del ctx[: len(ctx) - self.max_context]

    def reset_context(self, chat=None):
        with self._ctx_lock:
            if chat:
                self.contexts.pop(chat, None)
            else:
                self.contexts.clear()

    def generate(self, chat, persona, incoming, extra_rules=""):
        """调 GLM 生成回复（含重试）；失败抛 LLMError。同时把本次对话记入上下文。"""
        if not self.enabled:
            raise LLMError("未配置 API key")
        self._remember(chat, "user", incoming)
        with self._ctx_lock:
            history = list(self.contexts.get(chat, []))
        system = persona
        if extra_rules:
            system += "\n" + extra_rules
        system += "\n" + CHAT_DISCIPLINE
        msgs = [{"role": "system", "content": system}]
        msgs += [{"role": r, "content": c} for r, c in history]

        last_err = None
        for attempt in range(self.retries + 1):
            try:
                resp = requests.post(
                    self.url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": self.model, "messages": msgs,
                          "temperature": 0.7, "max_tokens": self.max_tokens},
                    timeout=self.timeout)
                resp.raise_for_status()
                reply = _strip_markdown(resp.json()["choices"][0]["message"]["content"].strip())
                self._remember(chat, "assistant", reply)
                return reply
            except requests.exceptions.HTTPError as e:
                status = getattr(e.response, "status_code", None)
                if status and status < 500:   # key/参数问题，重试无意义
                    raise LLMError(f"GLM HTTP {status}: {getattr(e.response, 'text', '')[:120]}")
                last_err = e
            except Exception as e:
                last_err = e
            if attempt < self.retries:
                time.sleep(2 * (attempt + 1))
        raise LLMError(f"GLM 调用失败（已重试{self.retries}次）: {last_err}")


    def scope_check(self, persona, incoming):
        """人设范围门禁：判断消息是否属于人设业务范围（工具层用）。
        返回 True=在范围内；检查失败按 True 处理（fail-open，保持原行为）"""
        msgs = [
            {"role": "system",
             "content": "你是意图分类器。根据给定人设判断用户消息是否属于其职责/业务范围，"
                        "只输出一个字：是 或 否。判断规则：\n"
                        "1. 若人设明确限定了业务范围（如'只做颌面外科咨询'、'与XX无关的不要作答'），"
                        "超出该范围的话题（天气、时间、新闻、闲聊等）输出 否；\n"
                        "2. 若人设是通用助手、个人分身、日常聊天角色（没有限定范围），"
                        "天气、时间、闲聊等日常话题都算在范围内，输出 是。\n"
                        "不要过度拒绝。"},
            {"role": "user",
             "content": f"人设：{persona}\n\n用户消息：{incoming}\n\n属于职责范围？只答：是/否"},
        ]
        try:
            resp = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "messages": msgs,
                      "temperature": 0, "max_tokens": 4},
                timeout=(10, 30))
            resp.raise_for_status()
            return "是" in resp.json()["choices"][0]["message"]["content"]
        except Exception:
            return True


class MockLLM:
    """Mock 模式：不联网，直接回固定文案"""
    enabled = True

    def reset_context(self, chat=None):
        pass

    def scope_check(self, persona, incoming):
        return True  # mock 下工具始终放行

    def generate(self, chat, persona, incoming, extra_rules=""):
        return f"[Mock回复] 已收到「{incoming[:20]}」，persona={persona[:12]}…"
