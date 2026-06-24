#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""权限气泡清理钩子（PostToolUse / Stop）。

为什么需要：Claude Code 在终端回答权限后不会关闭 PermissionRequest 的挂起 HTTP 连接
（Node fetch keep-alive），导致气泡无法靠"连接关闭"自行消失。本钩子提供可靠的
"已解决"信号：

  PostToolUse（Bash|Edit|Write）→ 工具已执行 = 用户在终端放行 → POST /dismiss
      daemon 按 tool_name+tool_input 指纹匹配，关闭对应气泡。
  Stop                          → 会话停止，残留气泡无意义 → POST /dismiss-all

fire-and-forget，失败静默，不阻塞 Claude Code。
"""
import json
import os
import sys
import urllib.request

URL = "http://127.0.0.1:23333"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "permission_cleanup.log")


def _log(msg):
    # 日志功能已关闭：卡片通知稳定，暂不需要写文件日志
    # try:
    #     with open(LOG_PATH, "a", encoding="utf-8") as f:
    #         import time
    #         f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    # except OSError:
    #     pass
    pass


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    try:
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}

    event = data.get("hook_event_name")
    tool = data.get("tool_name", "")
    _log(f"called event={event} tool={tool}")
    # AskUserQuestion 终端回答后触发 PostToolUse；它是交互收集，回答即解决，
    # 且 CC 串行不会有并发 pending 气泡 → 直接 dismiss-all 最可靠（指纹因
    # tool_input 差异常匹配不上）。
    if event == "Stop" or tool == "AskUserQuestion":
        target = URL + "/dismiss-all"
        body = b"{}"
    else:
        # PostToolUse（Bash|Edit|Write）：转发完整 payload，daemon 按指纹匹配
        target = URL + "/dismiss"
        body = raw.encode("utf-8") if raw.strip() else b"{}"

    try:
        req = urllib.request.Request(
            target, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass  # daemon 未运行或超时，静默


if __name__ == "__main__":
    main()
