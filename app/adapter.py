# -*- coding: utf-8 -*-
"""微信适配层：业务代码只依赖 BaseAdapter 接口，底层可替换。
PyWeixinAdapter 基于 pyweixin（pywinauto UI 自动化，适配微信 4.1.15 实测）。

设计要点（2026-10-03 实测结论）：
- 监控不打开聊天：只读会话列表，未读徽标只在对方来讯时增加（发送人归属天然正确），
  预览行即最新一条消息文本；聊天气泡区（RecyclerListView）懒加载极不稳定，不依赖。
- 发送前先点击会话定位并校验顶部聊天名，再走库的发送（复用当前聊天，绝不发错人）。
"""
import re
import threading


class AdapterError(Exception):
    pass


# 非文本消息（不自动回复）：气泡/预览里出现这些形态就跳过
NON_TEXT = {"图片", "动画表情", "视频", "语音", "文件", "音乐", "卡片链接",
            "[图片]", "[动画表情]", "[视频]", "[语音]", "[文件]", "[链接]", "[名片]"}

_BADGE_RE = re.compile(r"^\[(\d+)条\]$")
_META_LINE = re.compile(
    r"^\d{4}-\d{1,2}-\d{1,2}$|^\d{1,2}:\d{2}(:\d{2})?$|^(昨天|今天|明天)$|"
    r"^\d{1,2}月\d{1,2}日$|^星期[一二三四五六日天]$|^(周一|周二|周三|周四|周五|周六|周日)$")
_FLAGS = {"消息免打扰", "免打扰", "Muted", "置顶"}


def is_non_text(content):
    return (not content) or content in NON_TEXT or \
        (content.startswith("[") and content.endswith("]"))


class BaseAdapter:
    def status(self) -> dict:
        raise NotImplementedError

    def scan_unread(self) -> dict:
        """只读扫描会话列表：{会话名: {'count': 未读数, 'preview': 最新消息预览}}"""
        raise NotImplementedError

    def is_group(self, chat) -> bool:
        raise NotImplementedError

    def send(self, chat, text):
        raise NotImplementedError


class PyWeixinAdapter(BaseAdapter):
    def __init__(self):
        from pyweixin.Config import GlobalConfig
        GlobalConfig.close_weixin = False
        GlobalConfig.is_maximize = False
        from pyweixin import Messages, Tools
        from pyweixin.WeChatTools import Navigator
        from pyweixin.Uielements import Main_window, Special_Labels, Regex_Patterns, Texts
        from pywinauto import Desktop
        self._Messages, self._Tools, self._Navigator = Messages, Tools, Navigator
        self._Main_window, self._Special_Labels, self._Regex_Patterns = (
            Main_window, Special_Labels, Regex_Patterns)
        self._Texts = Texts
        self._Desktop = Desktop
        self._group_cache = {}          # 会话名 -> 是否群聊（只判一次）
        self._lock = threading.Lock()   # 所有 UI 操作串行化

    # ---------- 内部 ----------
    def _main_window(self):
        hwnd = self._Tools.get_weixin_hwnd()
        if not hwnd:
            raise AdapterError("找不到微信主窗口，请确认微信已登录")
        win = self._Desktop(backend="uia").window(handle=hwnd)
        if win.class_name() == "mmui::LoginWindow":
            raise AdapterError("微信未登录")
        return win

    def _open_chat(self, chat):
        """在会话列表点击目标会话并校验顶部聊天名。返回 (main_window, 是否成功)"""
        import time as _t
        win = self._Navigator.open_weixin(is_maximize=False)
        lst = win.child_window(**self._Main_window.SessionList)
        if not lst.exists(timeout=0.5):
            return win, False
        for item in lst.children(control_type="ListItem"):
            if item.window_text().split("\n")[0].strip() == chat:
                item.click_input()
                spec = dict(self._Texts.CurrentChatNameText)
                spec["title"] = chat
                ok = win.child_window(**spec).exists(timeout=2.5)
                if not ok:
                    _t.sleep(1)
                    ok = win.child_window(**spec).exists(timeout=1)
                return win, ok
        return win, False

    def _parse_row(self, text):
        """解析会话行文本 → (未读数, 预览, 行内时间)。
        行结构: 名称\\n[已置顶\\n][N条\\n]预览\\n时间\\n[消息免打扰]
        行内时间是最后一条消息的时间戳，是去重的关键（手动清徽标不会改变它）"""
        badge = 0
        preview = ""
        row_time = ""
        for ln in text.split("\n")[1:]:
            s = ln.strip()
            if not s:
                continue
            if s == "已置顶":
                continue
            m = _BADGE_RE.match(s)
            if m:
                badge = int(m.group(1))
                continue
            if _META_LINE.match(s):
                row_time = s
                break  # 预览之后的行（时间/标志），结束
            if s in _FLAGS:
                break
            if not preview:
                preview = s
        return badge, preview, row_time

    # ---------- 对外 ----------
    def status(self):
        try:
            win = self._main_window()
            ok = win.child_window(**self._Main_window.SessionList).exists(timeout=0.5)
            return {"connected": True, "chat_page": bool(ok),
                    "hint": "" if ok else "微信窗口请停留在「聊天」标签页"}
        except AdapterError as e:
            return {"connected": False, "chat_page": False, "hint": str(e)}
        except Exception as e:
            return {"connected": False, "chat_page": False, "hint": f"{type(e).__name__}: {e}"}

    def scan_unread(self):
        """只读扫描（不点击不按键不打扰），徽标只在对方来讯时存在"""
        with self._lock:
            win = self._main_window()
            lst = win.child_window(**self._Main_window.SessionList)
            if not lst.exists(timeout=0.5):
                return {}
            unread = {}
            for item in lst.children(control_type="ListItem"):
                text = item.window_text()
                if self._Special_Labels.MuteNotifications in text:
                    continue  # 免打扰会话不监控（群监控请勿开免打扰）
                m = self._Regex_Patterns.newMessage_pattern.search(text)
                if m:
                    name = text.split("\n")[0].strip()
                    badge, preview, row_time = self._parse_row(text)
                    unread[name] = {"count": int(m.group(1)),
                                    "preview": preview, "time": row_time}
            return unread

    def is_group(self, chat):
        """判断会话是否群聊（打开一次并缓存；仅 AI 分身路径对未配置会话调用）"""
        if chat in self._group_cache:
            return self._group_cache[chat]
        group = False
        with self._lock:
            try:
                win, ok = self._open_chat(chat)
                if ok:
                    group = bool(self._Tools.is_group_chat(win))
            except Exception:
                group = False
        self._group_cache[chat] = group
        return group

    def send(self, chat, text):
        """发送：先点击定位会话（复用当前聊天防发错人），失败自动换路由重试。
        路由0=搜索框直达；路由5=会话列表滚动查找。全失败才抛错（由调用方留痕）。"""
        import time as _t
        last_err = None
        with self._lock:
            for route in (0, 5):
                try:
                    self._open_chat(chat)
                    self._Messages.send_messages_to_friend(
                        friend=chat, messages=[text], search_pages=route)
                    return
                except Exception as e:
                    last_err = e
                    _t.sleep(1)
        raise last_err


class MockAdapter(BaseAdapter):
    """Mock 模式：内存里模拟微信，配合控制台「注入消息」测试全流程"""

    def __init__(self):
        self._inbox = []      # 待处理 {chat, content}
        self._unread = {}     # chat -> {'count','preview','time'}
        self._groups = {"示例群": True}
        self._tick = 0
        self._lock = threading.Lock()

    def inject(self, chat, content, sender=None, mtype="文本"):
        with self._lock:
            self._inbox.append({"chat": chat, "content": content})
            self._tick += 1
            cur = self._unread.get(chat, {"count": 0, "preview": "", "time": ""})
            self._unread[chat] = {"count": cur["count"] + 1,
                                  "preview": content, "time": f"t{self._tick}"}

    def status(self):
        return {"connected": True, "chat_page": True, "hint": "Mock 模式"}

    def scan_unread(self):
        with self._lock:
            return {k: dict(v) for k, v in self._unread.items()}

    def consume(self, chat):
        with self._lock:
            self._unread.pop(chat, None)
            rest = [i for i in self._inbox if i["chat"] != chat]
            self._inbox = rest

    def is_group(self, chat):
        return self._groups.get(chat, False)

    def send(self, chat, text):
        pass  # 发送记录由 main.py 写入 storage 消息流
