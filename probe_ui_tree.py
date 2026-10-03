# -*- coding: utf-8 -*-
"""只读探测：微信 4.x 账号 UI 树是否可见（不做任何点击/发送操作）"""
import sys
from pyweixin.WeChatTools import Tools
from pywinauto import Desktop

def main():
    if not Tools.is_weixin_running():
        print("[X] 微信未运行"); return 1
    hwnd = Tools.get_weixin_hwnd()
    if not hwnd:
        print("[X] 未找到微信主窗口句柄"); return 1
    desktop = Desktop(backend="uia")
    win = desktop.window(handle=hwnd)
    cls = win.class_name()
    print(f"[i] 微信主窗口 class={cls}, title={win.window_text()!r}")
    if cls == "mmui::LoginWindow":
        print("[X] 微信未登录（停在登录窗口）"); return 1

    # 枚举整棵 UI 树（只读）
    try:
        all_desc = win.descendants()
        print(f"[i] 主窗口后代控件总数: {len(all_desc)}")
    except Exception as e:
        print(f"[X] 枚举 UI 树失败: {type(e).__name__}: {e}"); return 2

    if len(all_desc) < 30:
        print("[结论] UI 树几乎为空 => 你的账号 UI 自动化不可见（新号无障碍策略屏蔽）")
        return 2

    # 尝试读取会话列表条目（只读）
    from collections import Counter
    ctrl_types = Counter(d.element_info.control_type for d in all_desc)
    tops = ", ".join(f"{k}={v}" for k, v in ctrl_types.most_common(8))
    print(f"[i] 控件类型分布(前8): {tops}")
    lists = [d for d in all_desc if d.element_info.control_type == "List"]
    print(f"[i] List 容器数量: {len(lists)}")
    for li in lists[:6]:
        try:
            items = li.children(control_type="ListItem")
            names = [it.window_text()[:12] for it in items[:5]]
            print(f"    - List({li.class_name()}) 子项 {len(items)} 个, 示例: {names}")
        except Exception as e:
            print(f"    - List({li.class_name()}) 读取失败: {type(e).__name__}")
    print("[结论] UI 树可见 => UI 自动化路线可行 ✓")
    return 0

if __name__ == "__main__":
    sys.exit(main())
