#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code 权限审批气泡（方案 A）

机制：作为 PermissionRequest 的 HTTP hook server。Claude Code 把权限请求 POST 到
http://127.0.0.1:23333/permission，本进程弹 tkinter 气泡，点 Allow/Deny 后通过
HTTP 响应回传决策（代答）。终端先回答时 Claude Code 关闭连接，本进程探测到对端
FIN 后自动关气泡、不代答（回退终端）。

响应格式（已对照 Clawd src/permission.js 确认）：
  {"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"|"deny"}}}
不返回 hookSpecificOutput → Claude Code 视为未决策，回退终端内置确认。

用法：
  python permission_bubble.py            # 前台启动（server + 气泡 UI）
  python permission_bubble.py --daemon   # 幂等：已在运行则退出，否则脱离后台拉起
"""

import http.server
import json
import logging
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
import queue

HOST = "127.0.0.1"
PORT = 23333
TIMEOUT = 600  # 秒，与 Clawd HTTP hook timeout 对齐
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "permission_bubble.log")

# ── 配色（对照 Clawd bubble.css 的 CSS 变量还原，亮/暗双套）──
CARD_W = 340          # 对齐 Clawd BUBBLE_BASE_WIDTH
CARD_PAD = 16         # 对齐 .card padding 上下
CARD_HPAD = 20        # 对齐 .card padding 左右
CARD_RADIUS = 16      # 对齐 .card border-radius

THEME_LIGHT = {
    "card_bg": "#ffffff",
    "card_border": "#e7e7eb",          # rgba(0,0,0,0.08) 近似
    "text_primary": "#18181b",
    "header_color": "#374151",
    "cmd_bg": "#f4f4f5",
    "cmd_border": "#eeeeef",
    "cmd_color": "#374151",
    "deny_bg": "#ffffff",
    "deny_color": "#52525b",
    "deny_border": "#d1d5db",
    "deny_hover_bg": "#f9fafb",
    "deny_hover_border": "#9ca3af",
}
THEME_DARK = {
    "card_bg": "#18181b",
    "card_border": "#2a2a30",           # rgba(255,255,255,0.1) 近似
    "text_primary": "#f4f4f5",
    "header_color": "#e4e4e7",
    "cmd_bg": "#09090b",
    "cmd_border": "#1c1c20",
    "cmd_color": "#a1a1aa",
    "deny_bg": "#232328",               # rgba(255,255,255,0.05) 近似
    "deny_color": "#e4e4e7",
    "deny_border": "#2e2e35",
    "deny_hover_bg": "#2e2e35",
    "deny_hover_border": "#3a3a42",
}
ALLOW_BG = "#d97757"        # Clawd 主橙
ALLOW_HOVER = "#c4684a"

# 工具 pill 颜色（对照 .tool-pill[data-tool]）
TOOL_PILL_COLORS = {
    "Bash": "#d97757",
    "Edit": "#5b8dd9",
    "Write": "#8b7ec7",
    "Read": "#5a9e6f",
    "Glob": "#5a9eab",
    "Grep": "#5a9eab",
    "Agent": "#c47a9a",
}


def detect_dark():
    """Windows: 读注册表判断系统是否暗色模式。失败默认亮色。"""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return val == 0  # 0 = 暗色
    except OSError:
        return False


def apply_rounded_corners(top):
    """Win11: 用 DwmSetWindowAttribute 给窗口加圆角 + 暗色边框阴影。非 Win11 静默失败。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        hwnd = top.winfo_id()
        # 顶层窗口的 hwnd 需取 root
        hwnd = ctypes.windll.user32.GetParent(hwnd) or hwnd
        dwm = ctypes.windll.dwmapi
        # DWMWA_WINDOW_CORNER_PREFERENCE = 33; 2 = round
        pref = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20（暗色标题栏，无标题栏窗口影响边框色）
        if detect_dark():
            dark = ctypes.c_int(1)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))
    except (OSError, AttributeError):
        pass

_logger = None
_log_lock = threading.Lock()


def logger():
    global _logger
    if _logger is None:
        with _log_lock:
            if _logger is None:
                lg = logging.getLogger("pb")
                lg.setLevel(logging.INFO)
                lg.propagate = False
                try:
                    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
                    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
                    lg.addHandler(handler)
                except OSError:
                    lg.addHandler(logging.NullHandler())
                _logger = lg
    return _logger


def log(msg):
    # 日志功能已关闭：卡片通知稳定，暂不需要写文件日志
    # try:
    #     logger().info(msg)
    # except Exception:
    #     pass
    pass

# ---------- 单实例锁 ----------
# bind 端口即锁；bind 失败说明已有实例在跑。
def try_acquire_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((HOST, PORT))
    except OSError:
        s.close()
        return None
    return s


def is_already_running():
    """探测端口是否已被实例占用（用于 --daemon 幂等检查）。"""
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


# ---------- 气泡控制器（主线程）----------
class BubbleController:
    """主线程持有。HTTP 线程只 put 消息到 queue，主线程 after 轮询消费并操作 widget。"""

    SHOW = "show"
    CLOSE = "close"
    CLOSE_ALL = "close_all"

    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.bubble = None  # 当前气泡 Toplevel
        self.current_req_id = None
        root.after(100, self._poll)

    def _poll(self):
        try:
            while True:
                self._handle(self.queue.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == self.SHOW:
            _, req_id, tool_name, summary, tool_input = msg
            self._show(req_id, tool_name, summary, tool_input)
        elif kind == self.CLOSE:
            _, req_id = msg
            if req_id == self.current_req_id:
                self._destroy_bubble()
        elif kind == self.CLOSE_ALL:
            self._destroy_bubble()

    def _destroy_bubble(self):
        if self.bubble is not None:
            try:
                self.bubble.destroy()
            except tk.TclError:
                pass
            self.bubble = None
            self.current_req_id = None

    def _show(self, req_id, tool_name, summary, tool_input=None):
        self._destroy_bubble()
        self.current_req_id = req_id

        theme = THEME_DARK if detect_dark() else THEME_LIGHT
        is_elicitation = (tool_name == "AskUserQuestion"
                          and isinstance(tool_input, dict)
                          and isinstance(tool_input.get("questions"), list)
                          and tool_input["questions"])

        top = tk.Toplevel(self.root)
        top.overrideredirect(True)  # 无标题栏
        top.attributes("-topmost", True)
        top.configure(background=theme["card_bg"],
                      highlightbackground=theme["card_border"], highlightthickness=1)

        # 内容容器：内边距对照 Clawd (padding:16px 20px)
        # 宽度由后续 geometry 强制锁定为 CARD_W；高度随内容自适应
        content = tk.Frame(top, bg=theme["card_bg"], padx=CARD_HPAD, pady=CARD_PAD)
        content.pack(fill="both", expand=True)

        # ── header：标题 + 工具 pill ──
        header = tk.Frame(content, bg=theme["card_bg"])
        header.pack(fill="x")
        title_text = "需要回答" if is_elicitation else "权限请求"
        tk.Label(header, text=title_text, fg=theme["header_color"], bg=theme["card_bg"],
                 font=("Segoe UI", 11, "bold")).pack(side="left")
        pill_bg = TOOL_PILL_COLORS.get(tool_name, "#52525b")
        tk.Label(header, text=tool_name.upper(), fg="#ffffff", bg=pill_bg,
                 font=("Segoe UI", 8, "bold"), padx=8, pady=2).pack(side="left", padx=(8, 0))

        body_wrap = CARD_W - CARD_HPAD * 2 - 12 * 2

        # relayout：测量 content 真实高度并定位右下角。elicitation 的 Other 展开/
        # 问题切换会改变内容高度，需重新调用。content.winfo_reqheight() 不含自身
        # pady（上下各16=32），补上避免按钮贴底被裁。
        def relayout():
            content.pack_propagate(True)
            top.update_idletasks()
            h = content.winfo_reqheight() + CARD_PAD * 2
            if h < 50:
                top.geometry(f"{CARD_W}x{400}+0+0")
                top.update_idletasks()
                h = content.winfo_reqheight() + CARD_PAD * 2
            sw = top.winfo_screenwidth()
            sh = top.winfo_screenheight()
            top.geometry(f"{CARD_W}x{h}+{sw - CARD_W - 20}+{sh - h - 60}")
            content.pack_propagate(False)
            content.configure(width=CARD_W)
            top.update_idletasks()
            apply_rounded_corners(top)

        if is_elicitation:
            deny_btn, allow_btn = self._build_elicitation(
                content, theme, req_id, tool_input, body_wrap, relayout)
        else:
            deny_btn, allow_btn = self._build_permission(
                content, theme, req_id, tool_input, body_wrap)

        # 首次测量：先让按钮重绘一次（确保 Canvas 按钮的 reqheight 被正确计入）
        deny_btn.redraw()
        allow_btn.redraw()
        relayout()
        deny_btn.redraw()
        allow_btn.redraw()

        self.bubble = top

    def _make_btn(self, parent, theme, text, bg, fg, hover_bg, border, on_click):
        """构造一个 Canvas 圆角按钮。支持 set_text/set_enabled 动态更新。"""
        btn = tk.Canvas(parent, bg=theme["card_bg"], bd=0, highlightthickness=0,
                        height=32, width=1, highlightbackground=theme["card_bg"])
        state = {"hover": False, "enabled": True, "text": text}

        def redraw():
            btn.delete("all")
            w = max(btn.winfo_width(), 1)
            enabled = state["enabled"]
            cur_bg = (hover_bg if state["hover"] else bg) if enabled else theme["deny_bg"]
            cur_fg = fg if enabled else theme["deny_color"]
            self._round_rect(btn, 1, 1, w - 1, 31, 8,
                             fill=cur_bg, outline=border, width=1)
            btn.create_text(w // 2, 16, text=state["text"], fill=cur_fg,
                            font=("Segoe UI", 10, "bold"))
        btn.bind("<Enter>", lambda e: (state.__setitem__("hover", True), redraw()) if state["enabled"] else None)
        btn.bind("<Leave>", lambda e: (state.__setitem__("hover", False), redraw()) if state["enabled"] else None)
        btn.bind("<Button-1>", lambda e: on_click() if state["enabled"] else None)
        btn.bind("<Configure>", lambda e: redraw())

        def set_text(t):
            state["text"] = t
            redraw()
        def set_enabled(en):
            state["enabled"] = en
            redraw()
        btn.redraw = redraw
        btn.set_text = set_text
        btn.set_enabled = set_enabled
        return btn

    def _build_permission(self, content, theme, req_id, tool_input, wrap):
        """普通权限请求：命令块（description 上 + command 下，命令单行截断）+ Allow/Deny。
        返回 (deny_btn, allow_btn)。"""
        cmd = tool_input.get("command") or ""
        desc = tool_input.get("description") or ""
        # 无 command 时退回 file_path/pattern/整体 JSON
        if not cmd:
            cmd = (tool_input.get("file_path") or tool_input.get("pattern")
                   or json.dumps(tool_input, ensure_ascii=False))
        cmd_frame = tk.Frame(content, bg=theme["cmd_bg"], bd=0, highlightthickness=1,
                             highlightbackground=theme["cmd_border"])
        cmd_frame.pack(fill="x", pady=(8, 0))
        # 描述在上（可换行），有 description 时顶部留边距
        if desc:
            tk.Label(cmd_frame, text=desc, fg=theme["header_color"], bg=theme["cmd_bg"],
                     font=("Segoe UI", 9), justify="left", anchor="w",
                     wraplength=wrap).pack(fill="x", padx=12, pady=(10, 4))
            cmd_pady = (0, 10)
        else:
            cmd_pady = (10, 10)
        # 命令在下（等宽、可滚动，对照 Clawd：max-height + overflow-y:auto）
        # 用 Text + Scrollbar：短命令自适应高度，长命令限高 5 行滚动，不截断
        cmd_font = ("Cascadia Code", 10)
        cmd_box = tk.Frame(cmd_frame, bg=theme["cmd_bg"])
        cmd_box.pack(fill="x", padx=12, pady=cmd_pady)
        cmd_text = tk.Text(cmd_box, bg=theme["cmd_bg"], fg=theme["cmd_color"],
                           font=cmd_font, wrap="word", relief="flat", bd=0,
                           highlightthickness=0, padx=0, pady=0,
                           height=1,  # 初始 1 行，后续按内容调整
                           cursor="arrow")
        # 自动隐藏的滚动条
        sb = tk.Scrollbar(cmd_box, orient="vertical", command=cmd_text.yview,
                          troughcolor=theme["cmd_bg"], borderwidth=0,
                          activebackground=theme["cmd_border"])
        cmd_text.configure(yscrollcommand=lambda lo, hi: (
            sb.set(lo, hi), sb.pack_forget() if hi == "1.0" else sb.pack(side="right", fill="y")))
        cmd_text.insert("1.0", cmd)
        cmd_text.configure(state="disabled")  # 只读
        cmd_text.pack(side="left", fill="x", expand=True)
        # 按自动换行后的显示行数限高（最多 5 行，超出滚动）
        # 需先布局让 Text 有宽度，count displaylines 才能算 word-wrap 行数
        content.update_idletasks()
        try:
            info = cmd_text.count("1.0", "end -1c", "displaylines")
            lines = (info[0] if info else 1) or 1
            cmd_text.configure(height=min(max(lines, 1), 5))
        except Exception:
            cmd_text.configure(height=1)

        actions = tk.Frame(content, bg=theme["card_bg"])
        actions.pack(fill="x", pady=(6, 0))
        actions.grid_columnconfigure(0, weight=1, uniform="btn")
        actions.grid_columnconfigure(1, weight=1, uniform="btn")
        allow_btn = self._make_btn(actions, theme, "Allow", ALLOW_BG, "#ffffff",
                                   ALLOW_HOVER, ALLOW_BG,
                                   lambda: self._on_click(req_id, "allow"))
        deny_btn = self._make_btn(actions, theme, "Deny", theme["deny_bg"],
                                  theme["deny_color"], theme["deny_hover_bg"],
                                  theme["deny_border"], lambda: self._on_click(req_id, "deny"))
        allow_btn.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        deny_btn.grid(row=0, column=1, sticky="nsew")
        return deny_btn, allow_btn

    def _build_elicitation(self, content, theme, req_id, tool_input, wrap, relayout):
        """AskUserQuestion：逐问题展示选项（含 Other 自定义输入），提交代答。
        返回 (back_btn, next_btn)。
        回传格式（对照 Clawd buildElicitationUpdatedInput）：
          updatedInput = {**tool_input, answers: {问题文本: 答案文本, ...}}
        通过 event.payload = ("elicitation", updated_input) 传给 handler。"""
        questions = tool_input.get("questions") or []
        multi_flags = {}       # 问题文本 -> bool
        # answers[qtext] = {"selected": set|None, "other": bool, "other_text": str}
        answers = {}
        for q in questions:
            qtext = q.get("question", "")
            multi_flags[qtext] = bool(q.get("multiSelect"))
            answers[qtext] = {"selected": set() if multi_flags[qtext] else None,
                              "other": False, "other_text": ""}

        # 问题展示区（每次只显示一个问题，用 pack_forget 切换）
        qhost = tk.Frame(content, bg=theme["card_bg"])
        qhost.pack(fill="x", pady=(8, 0))
        progress = tk.Label(content, text="", fg=theme["cmd_color"], bg=theme["card_bg"],
                            font=("Segoe UI", 8))
        progress.pack(anchor="w", pady=(6, 0))
        state = {"idx": 0, "cards": []}

        def render_question(i):
            for c in state["cards"]:
                c.destroy()
            state["cards"] = []
            q = questions[i]
            qtext = q.get("question", "")
            qheader = q.get("header") or f"问题 {i+1}"
            multi = multi_flags[qtext]
            card = tk.Frame(qhost, bg=theme["cmd_bg"], bd=0, highlightthickness=1,
                            highlightbackground=theme["cmd_border"])
            card.pack(fill="x")
            state["cards"].append(card)
            # card 有 highlightthickness=1 两侧各 1px，wraplength 留余量避免末字被切
            qwrap = wrap - 8
            tk.Label(card, text=qheader.upper(), fg=theme["cmd_color"], bg=theme["cmd_bg"],
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12, pady=(10, 2))
            tk.Label(card, text=qtext, fg=theme["text_primary"], bg=theme["cmd_bg"],
                     font=("Segoe UI", 10), justify="left", anchor="w",
                     wraplength=qwrap).pack(fill="x", padx=12, pady=(0, 6))
            hint = "可多选" if multi else "请选择一项"
            tk.Label(card, text=hint, fg=theme["cmd_color"], bg=theme["cmd_bg"],
                     font=("Segoe UI", 8)).pack(anchor="w", padx=12, pady=(0, 6))

            # 收集本问题所有选项：(row_frame, dot, is_selected_fn, set_visual)
            opts_ui = []

            def make_option(opt_label, is_other=False, desc=""):
                # 选项卡片：圆角 frame + 1px 边框，选中时边框变橙
                row = tk.Frame(card, bg=theme["card_bg"], cursor="hand2",
                               highlightthickness=1,
                               highlightbackground=theme["cmd_border"])
                row.pack(fill="x", padx=6, pady=3)
                # radio 圆点：未选空心○，选中实心● 橙色
                dot = tk.Label(row, text="○", fg=theme["cmd_color"], bg=theme["card_bg"],
                               font=("Segoe UI", 11))
                dot.pack(side="left", padx=(10, 8), pady=8)
                lbl_text = "自定义输入…" if is_other else opt_label
                copy = tk.Frame(row, bg=theme["card_bg"])
                copy.pack(side="left", fill="x", expand=True, pady=8)
                txt = tk.Label(copy, text=lbl_text, fg=theme["text_primary"],
                               bg=theme["card_bg"], font=("Segoe UI", 10), anchor="w",
                               justify="left")
                txt.pack(fill="x", anchor="w")
                desc_lbl = None
                if desc:
                    desc_lbl = tk.Label(copy, text=desc, fg=theme["cmd_color"],
                                        bg=theme["card_bg"], font=("Segoe UI", 8),
                                        anchor="w", justify="left", wraplength=qwrap - 30)
                    desc_lbl.pack(fill="x", anchor="w", pady=(2, 0))

                def set_visual(on_):
                    dot.configure(text="●" if on_ else "○",
                                  fg=ALLOW_BG if on_ else theme["cmd_color"])
                    row.configure(highlightbackground=ALLOW_BG if on_ else theme["cmd_border"])

                opts_ui.append(set_visual)

                def toggle():
                    a = answers[qtext]
                    multi = multi_flags[qtext]
                    if multi:
                        sel = a["selected"]
                        if is_other:
                            a["other"] = not a["other"]; on_ = a["other"]
                        else:
                            if opt_label in sel:
                                sel.discard(opt_label); on_ = False
                            else:
                                sel.add(opt_label); on_ = True
                        set_visual(on_)
                    else:
                        # 单选：清除本问题所有视觉
                        for sv in opts_ui:
                            sv(False)
                        set_visual(True)
                        if is_other:
                            a["other"] = True; a["selected"] = None
                        else:
                            a["other"] = False; a["selected"] = opt_label
                    sync_other_entry()
                    update_buttons()

                widgets = [row, dot, txt, copy] + ([desc_lbl] if desc_lbl else [])
                for w in widgets:
                    w.bind("<Button-1>", lambda e, fn=toggle: fn())

            for opt in (q.get("options") or []):
                make_option(opt.get("label", ""), desc=opt.get("description", ""))
            # Other 自定义输入（CC 终端会自动提供，options 不含，客户端注入）
            make_option(None, is_other=True)

            # Other 输入框：Canvas 圆角灰边框容器 + 嵌入 Entry（聚焦边框变橙）
            entry_host = tk.Frame(card, bg=theme["card_bg"])
            entry_canvas = tk.Canvas(entry_host, bg=theme["card_bg"], bd=0,
                                     highlightthickness=0, height=30)
            entry_canvas.pack(fill="x")
            entry_inner = tk.Entry(entry_canvas, bg=theme["card_bg"],
                                   fg=theme["text_primary"], insertbackground=ALLOW_BG,
                                   relief="flat", bd=0, highlightthickness=0,
                                   font=("Segoe UI", 10))
            entry_win = entry_canvas.create_window(10, 15, window=entry_inner, anchor="w")

            def draw_entry_border(focused=False):
                entry_canvas.delete("border")
                w = max(entry_canvas.winfo_width(), 1)
                color = ALLOW_BG if focused else theme["cmd_border"]
                self._round_rect(entry_canvas, 1, 1, w - 1, 29, 6,
                                 fill=theme["card_bg"], outline=color, width=1,
                                 tags="border")
                entry_canvas.tag_lower("border")

            def on_entry_configure(_=None):
                entry_canvas.itemconfigure(entry_win, width=max(entry_canvas.winfo_width() - 20, 1))
                draw_entry_border(entry_inner is card.focus_get())
            entry_canvas.bind("<Configure>", on_entry_configure)
            entry_inner.bind("<FocusIn>", lambda e: draw_entry_border(True))
            entry_inner.bind("<FocusOut>", lambda e: draw_entry_border(False))

            other_entry = entry_inner  # 兼容 sync_other_entry / on_other_input

            def sync_other_entry():
                a = answers[qtext]
                if a["other"]:
                    entry_host.pack(fill="x", padx=12, pady=(4, 8))
                    entry_inner.delete(0, "end")
                    entry_inner.insert(0, a["other_text"])
                    card.update_idletasks()
                    draw_entry_border(False)
                    # 自动聚焦输入框
                    try:
                        entry_inner.focus_set()
                    except Exception:
                        pass
                else:
                    entry_host.pack_forget()
                relayout()

            def on_other_input(_=None):
                answers[qtext]["other_text"] = other_entry.get()
                update_buttons()
            other_entry.bind("<KeyRelease>", on_other_input)

            # 恢复已选状态（切回此问题时回显）
            a0 = answers[qtext]
            preset_opts = q.get("options") or []
            if a0["other"]:
                opts_ui[-1](True)  # Other 是最后一个
                sync_other_entry()
            elif a0["selected"]:
                sel = a0["selected"]
                sel_set = sel if isinstance(sel, set) else {sel}
                for idx, opt in enumerate(preset_opts):
                    if opt.get("label", "") in sel_set:
                        opts_ui[idx](True)

            progress.configure(text=f"问题 {i+1} / {len(questions)}")
            update_buttons()
            relayout()  # 切题后内容高度变了，重新测量自适应

        def answer_text(qtext):
            a = answers.get(qtext)
            if not a:
                return ""
            parts = []
            if a["selected"]:
                parts.extend(sorted(a["selected"]) if isinstance(a["selected"], set)
                             else [a["selected"]])
            if a["other"]:
                t = a["other_text"].strip()
                if t:
                    parts.append(t)
            return ", ".join(parts)

        def all_answered():
            for q in questions:
                if not answer_text(q.get("question", "")):
                    return False
            return True

        def current_answered():
            qtext = questions[state["idx"]].get("question", "")
            return bool(answer_text(qtext))

        # 按钮区：左=上一步（首题禁用），右=下一题/提交（答完才可点）
        actions = tk.Frame(content, bg=theme["card_bg"])
        actions.pack(fill="x", pady=(6, 0))
        actions.grid_columnconfigure(0, weight=1, uniform="btn")
        actions.grid_columnconfigure(1, weight=1, uniform="btn")

        def go_next_or_submit():
            i = state["idx"]
            if i < len(questions) - 1:
                state["idx"] = i + 1
                render_question(state["idx"])
            else:
                # 最后一题 → 提交
                if all_answered():
                    updated_input = dict(tool_input)
                    updated_input["answers"] = {
                        q.get("question", ""): answer_text(q.get("question", ""))
                        for q in questions
                    }
                    self._on_click_payload(req_id, ("elicitation", updated_input))

        def go_back():
            i = state["idx"]
            if i > 0:
                state["idx"] = i - 1
                render_question(state["idx"])

        back_btn = self._make_btn(actions, theme, "上一步", theme["deny_bg"],
                                  theme["deny_color"], theme["deny_hover_bg"],
                                  theme["deny_border"], go_back)
        next_btn = self._make_btn(actions, theme, "下一步", ALLOW_BG, "#ffffff",
                                  ALLOW_HOVER, ALLOW_BG, go_next_or_submit)
        back_btn.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        next_btn.grid(row=0, column=1, sticky="nsew")

        def update_buttons():
            i = state["idx"]
            is_last = i >= len(questions) - 1
            back_btn.set_enabled(i > 0)
            # 右按钮：最后一题=提交（需全答完），否则=下一步（需当前答完）
            next_btn.set_text("提交" if is_last else "下一步")
            next_btn.set_enabled(all_answered() if is_last else current_answered())

        render_question(0)
        return back_btn, next_btn

    @staticmethod
    def _round_rect(canvas, x1, y1, x2, y2, r, **kw):
        """在 Canvas 上画圆角矩形（tkinter 无原生圆角，用 smooth polygon 近似）。"""
        return canvas.create_polygon(
            x1 + r, y1, x2 - r, y1,
            x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r,
            x1, y1 + r, x1, y1,
            smooth=True, **kw
        )

    def _on_click(self, req_id, behavior):
        # 统一走 resolve_event（防与 dismiss 竞态）
        resolve_event(req_id, behavior)
        self._destroy_bubble()

    def _on_click_payload(self, req_id, payload):
        """elicitation 提交：payload = ("elicitation", updated_input)。"""
        resolve_event(req_id, payload)
        self._destroy_bubble()


# 全局：req_id -> threading.Event，handler 注册、按钮/清理消费
pending_events = {}            # req_id -> event
fingerprint_index = {}         # fingerprint -> req_id（用于 PostToolUse 匹配清理）
pending_lock = threading.Lock()
controller = None  # 主线程设置


def make_fingerprint(tool_name, tool_input):
    """tool_name + 规范化 tool_input 的指纹。同一工具调用在 PermissionRequest 与
    PostToolUse 中 tool_input 一致，据此匹配。"""
    try:
        body = json.dumps(tool_input, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        body = str(tool_input)
    return f"{tool_name}|{body}"


def register_event(req_id, event, fingerprint):
    with pending_lock:
        pending_events[req_id] = event
        if fingerprint:
            fingerprint_index[fingerprint] = req_id


def unregister_event(req_id, fingerprint=None):
    with pending_lock:
        pending_events.pop(req_id, None)
        if fingerprint and fingerprint_index.get(fingerprint) == req_id:
            fingerprint_index.pop(fingerprint, None)


def resolve_event(req_id, payload):
    """统一入口：设置决策事件。已 resolved 则不覆盖（防竞态）。
    返回是否由本次调用完成 resolved。"""
    with pending_lock:
        ev = pending_events.get(req_id)
        if ev is None or ev.is_set():
            return False
        ev.payload = payload
        ev.set()
        return True


def dismiss_by_fingerprint(fingerprint):
    """PostToolUse 信号：工具已执行 → 用户在终端放行 → 关闭对应气泡。"""
    with pending_lock:
        req_id = fingerprint_index.get(fingerprint)
    if req_id is None:
        return False
    resolved = resolve_event(req_id, "no-decision")
    if controller is not None:
        controller.queue.put((BubbleController.CLOSE, req_id))
    log(f"DISMISS fp={fingerprint[:80]} req_id={req_id} resolved={resolved}")
    return resolved


def dismiss_all_pending(reason):
    """Stop / 新请求到达：关闭所有残留气泡（CC 串行，新事件意味着旧请求已解决）。"""
    with pending_lock:
        req_ids = list(pending_events.keys())
    for rid in req_ids:
        resolve_event(rid, "no-decision")
    if controller is not None:
        controller.queue.put((BubbleController.CLOSE_ALL, None))
    log(f"DISMISS_ALL reason={reason} count={len(req_ids)}")


# ---------- 连接关闭探测 ----------
def wait_decision_or_disconnect(handler_sock, event, timeout):
    """
    阻塞等待三种结果之一：
      - ("decision", behavior)  用户点了按钮（event 被 set，payload 为 allow/deny）
      - ("disconnect", None)    Claude Code 关闭了连接（终端先回答）
      - ("timeout", None)       超时
    用非阻塞 recv(MSG_PEEK) 探测对端 FIN：BlockingIOError=无数据连接活着；
    recv 返回 b''=对端关闭；其余 OSError=连接异常。
    （注：Windows 上 select 对空闲 TCP 连接会误报可读，故不用 select。）
    """
    deadline = time.monotonic() + timeout
    was_blocking = handler_sock.getblocking()
    try:
        handler_sock.setblocking(False)
    except OSError:
        pass
    try:
        while True:
            if event.is_set():
                return ("decision", getattr(event, "payload", None))
            try:
                data = handler_sock.recv(1, socket.MSG_PEEK)
            except BlockingIOError:
                pass  # 无数据，连接仍活着
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                return ("disconnect", None)
            else:
                if data == b"":
                    return ("disconnect", None)  # 对端 FIN
                # 罕见：有待读数据但非关闭，忽略继续等
            if time.monotonic() >= deadline:
                return ("timeout", None)
            event.wait(timeout=0.5)
    finally:
        try:
            handler_sock.setblocking(was_blocking)
        except OSError:
            pass


# ---------- HTTP handler ----------
class PermissionHandler(http.server.BaseHTTPRequestHandler):
    # 关闭默认日志刷屏
    def log_message(self, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # 连接已断，写不了就算

    def _handle_dismiss(self):
        """PostToolUse 清理：工具已执行 → 终端放行 → 关闭匹配气泡。body 是完整 hook payload。"""
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "bad json"}, 400)
            return
        tool_name = data.get("tool_name") or "unknown"
        tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        fp = make_fingerprint(tool_name, tool_input)
        hit = dismiss_by_fingerprint(fp)
        self._send_json({"ok": True, "hit": hit})

    def do_POST(self):
        if self.path == "/dismiss":
            self._handle_dismiss()
            return
        if self.path == "/dismiss-all":
            dismiss_all_pending("stop-hook")
            self._send_json({"ok": True})
            return
        if self.path != "/permission":
            self._send_json({"error": "not found"}, 404)
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "bad json"}, 400)
            return

        tool_name = data.get("tool_name") or "unknown"
        tool_input = data.get("tool_input") if isinstance(data.get("tool_input"), dict) else {}
        # 摘要优先级（对照 Clawd bubble-format.js formatDetail）：
        #   description > Bash.command > Edit/Write/Read.file_path > Glob/Grep.pattern > 整体 JSON
        summary = (tool_input.get("description")
                   or tool_input.get("command")
                   or tool_input.get("file_path")
                   or tool_input.get("pattern")
                   or json.dumps(tool_input, ensure_ascii=False))
        if len(summary) > 120:
            summary = summary[:120] + "…"
        req_id = data.get("tool_use_id") or f"{time.monotonic_ns()}"
        fingerprint = make_fingerprint(tool_name, tool_input)
        conn_hdr = self.headers.get("Connection", "")
        log(f"REQ req_id={req_id} tool={tool_name} conn={conn_hdr} "
            f"keepalive={self.close_connection is False}")

        # CC 串行：新权限请求到达意味着上一个已解决 → 清旧 pending
        dismiss_all_pending("new-request")

        event = threading.Event()
        event.payload = None
        register_event(req_id, event, fingerprint)

        # 通知主线程弹气泡
        if controller is not None:
            controller.queue.put((BubbleController.SHOW, req_id, tool_name, summary, tool_input))

        # 等待：决策（按钮/PostToolUse dismiss）/ 连接关闭 / 超时
        t0 = time.monotonic()
        result = wait_decision_or_disconnect(self.request, event, TIMEOUT)
        dt = time.monotonic() - t0
        log(f"WAIT req_id={req_id} result={result[0]} dt={dt:.2f}s")
        unregister_event(req_id, fingerprint)

        # 通知主线程关气泡（如果还开着）
        if controller is not None:
            controller.queue.put((BubbleController.CLOSE, req_id))

        kind = result[0]
        payload = result[1] if len(result) > 1 else None
        if kind == "decision" and isinstance(payload, tuple) and payload and payload[0] == "elicitation":
            # AskUserQuestion 提交 → 代答，回传 updatedInput.answers
            updated_input = payload[1] if len(payload) > 1 else {}
            log(f"ELICIT_SUBMIT req_id={req_id} answers={updated_input.get('answers')}")
            self._send_json({
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {"behavior": "allow", "updatedInput": updated_input},
                }
            })
        elif kind == "decision" and payload in ("allow", "deny"):
            # 用户点了气泡按钮 → 代答
            decision = {"behavior": payload}
            if payload == "deny":
                # 明确拒绝 + interrupt 终止 Claude 的重试循环
                # （裸 deny 会让模型误以为是可绕过的限制而换方式重试，导致死循环弹卡片）
                decision["message"] = "用户在权限卡片上点击了 Deny，主动拒绝此操作。"
                decision["interrupt"] = True
            self._send_json({
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": decision,
                }
            })
        else:
            # disconnect（CC 关连接）/ timeout / no-decision（终端已处理，PostToolUse/Stop 清理）
            # 不返回 hookSpecificOutput → Claude Code 非阻塞忽略，回退终端内置确认
            self._send_json({})


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


# ---------- 启动 ----------
def run_foreground(lock_sock):
    """前台：起 server 线程 + tkinter mainloop。"""
    global controller

    httpd = ThreadingHTTPServer((HOST, PORT), PermissionHandler)
    httpd.timeout = None
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    root = tk.Tk()
    root.withdraw()  # 不显示主窗口，只用 Toplevel 气泡
    controller = BubbleController(root)

    def on_quit():
        httpd.shutdown()
        lock_sock.close()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_quit)
    root.mainloop()


def run_daemon():
    """--daemon：幂等拉起。已在运行则退出；否则脱离后台启动一个前台实例。"""
    if is_already_running():
        return 0
    # 脱离当前进程后台启动
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.abspath(__file__)], **kwargs)
    return 0


def main():
    if "--daemon" in sys.argv:
        return run_daemon()
    lock_sock = try_acquire_lock()
    if lock_sock is None:
        # 已有实例在跑
        return 0
    try:
        run_foreground(lock_sock)
    finally:
        try:
            lock_sock.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
