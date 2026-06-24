# Claude Code 权限审批卡片

和 Claude Code 对话时，如果你切去处理别的事、没盯着终端，遇到需要人工审批的命令（或 `AskUserQuestion`）只会在终端里干等。本项目给 CC 装一个 **Windows 桌面悬浮卡片**：权限请求一到，屏幕右下角弹出卡片，直接点 **Allow / Deny** 代答；要是你先在终端回答了，卡片会自动消失，两条路互不冲突。

> 仅 Windows、仅标准库、无需 `pip install`。

## 文件说明

| 文件 | 作用 |
|---|---|
| `permission_bubble.py` | 核心：常驻 daemon + tkinter 气泡 UI + HTTP server（监听 `127.0.0.1:23333`） |
| `permission_cleanup.py` | 辅助：PostToolUse / Stop 时关闭残留气泡（CC 不会主动关 keep-alive 连接） |
| `settings.example.json` | 最小可用的 hook 配置示例|

## 工作机制

1. **SessionStart** 拉起 `permission_bubble.py --daemon`（幂等，已在运行则跳过）。
2. Claude 触发权限请求 → CC 把请求 POST 到 `http://127.0.0.1:23333/permission`。
3. daemon 弹出 tkinter 卡片：
   - 普通权限（Bash / Edit / Write / ...）：显示命令 + Allow / Deny
   - `AskUserQuestion`：逐题展示选项（支持单选 / 多选），提交时回传 `answers` 代答
4. 用户点按钮 → 通过 HTTP 响应回传决策（Deny 带 `interrupt` 中止模型重试）。
5. 若终端先回答，CC 关闭连接，daemon 探测到对端 FIN 自动关卡片、回退终端。
6. **PostToolUse / Stop** 触发 `permission_cleanup.py`，按指纹或全量关闭残留卡片。

## 安装

### 方式一：让 Agent 自动安装（推荐）

把下面这段话直接发给你的 coding agent，让它自己装：

```text
[ 仓库地址 ]，帮我把这个项目的权限卡片 hook 装到 Claude Code：
1. 把 permission_bubble.py 和 permission_cleanup.py 复制到 ~/.claude/hooks/。
2. 读取 settings.example.json 里的 hooks 块，把它合并进 ~/.claude/settings.json
   的 hooks 字段（深合并，不要覆盖我已有的其它 hook / 配置）。需要保留这 4 个 hook：
   SessionStart / PermissionRequest / PostToolUse / Stop。
3. 装完告诉我，我重启 CC 会话验证。
```

Agent 完成后，重启 Claude Code 会话即可——首次 SessionStart 会自动拉起 daemon。

### 方式二：手动安装

1. 把两个 `.py` 放到 `~/.claude/hooks/`：
   ```bash
   mkdir -p ~/.claude/hooks
   cp permission_bubble.py permission_cleanup.py ~/.claude/hooks/
   ```
2. 把 `settings.example.json` 里的 `hooks` 块合并进你的 `~/.claude/settings.json`
   （只需这 4 个 hook 块：`SessionStart` / `PermissionRequest` / `PostToolUse` / `Stop`，
   不要覆盖你已有的其它配置）。
3. 重启 Claude Code 会话。首次 SessionStart 会自动拉起 daemon。

## 依赖

- Windows 10 / 11（用到 tkinter、Win11 圆角 `DwmSetWindowAttribute`、注册表判暗色模式）
- Python 3.10+（用了 `str | None` 类型注解语法）
- 仅标准库，无需 pip 安装任何包

## 日志

默认**关闭**文件日志（`log()` / `_log()` 函数体已注释）。
需要排查问题时，打开 `permission_bubble.py` 的 `log()` 函数体即可写 `permission_bubble.log`。

## 单实例与端口

- daemon 通过 bind `127.0.0.1:23333` 作为单实例锁；已运行则 `--daemon` 直接退出。
- 如端口被占用，改 `permission_bubble.py` 顶部的 `HOST` / `PORT`，并同步改 `settings.example.json` 里的 url 和 `permission_cleanup.py` 的 `URL`。

## 已知行为

- CC 串行处理权限请求：新请求到达视为上一请求已解决，会先清旧气泡。
- `AskUserQuestion` 终端回答后触发 PostToolUse，因 `tool_input` 差异指纹匹配不可靠，统一走 `dismiss-all`。
- Deny 决策带 `interrupt: true`，避免模型把"被拒"当成可绕过限制而换方式重试导致死循环弹卡。
