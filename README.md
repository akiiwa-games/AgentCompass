<div align="center">

# 🧭 AgentCompass

**Route sub-agent tasks across your coding subscriptions by model strengths and live quota.**

[English](#english) · [简体中文](#简体中文) · [Technical Reference](docs/technical-reference.md) · [`llms.txt`](llms.txt)

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![Router](https://img.shields.io/badge/router-Python%20%2B%20JSON-555)
![Tray](https://img.shields.io/badge/tray-Windows%2011-0078D4)
![License](https://img.shields.io/badge/license-MIT-green)

<img src="docs/hero.gif" width="880" alt="AgentCompass Decision Compass replaying a synthetic decision">

</div>

---

## English

### Why AgentCompass

Even when you funnel all your work through a single main session or one GrokBot, the mental load is no lighter than managing multiple agents yourself:

> *"Hand this new showcase UI straight to Opus 5.5 to design and build. Turn on `xhigh`—I want the result to really stand out."*
>
> *"Ask Argon whether this strategy can be optimized any further. Proposal only, don't write code."*
>
> *"Have Astra run the code review and end-to-end tests. If Plus quota runs out, have DeepSeek step in."*

Each agent is good at different things, and subscription quotas are always moving. It feels like managing a team of dozens of people in your head—remembering who does what best, which effort level to turn on, and which account still has quota left today.

**AgentCompass** takes that scheduling off your hands for the plans and pools you already use (Claude Max / Pro, ChatGPT / Codex, Google AI Ultra / Pro, Grok, and OpenAI-compatible fallbacks). When your main session needs to delegate a task or get a second opinion, it calls `agentcompass roll`:

- **Matches the model to the job** — profiles whether the task is UI design, open-ended strategy advice, repo-wide refactoring, or strict review & E2E testing, and picks a fitting `model × effort` worker (or tells the main session to do single-thread work itself).
- **Schedules around live 5h / 7d quotas** — reads local quota snapshots across your signed-in accounts, falls back automatically when an account runs dry or is paused, and spends expiring surplus before weekly resets.
- **Follows explicit instructions when you give them** — if you name a model or effort in your prompt, the main agent passes `--model ... --effort ... --basis "<your words>"` straight through.
- **Keeps sub-agents on the rails** — an optional `PreToolUse` hook blocks unrouted background spawns, and workers that reach 2M tokens of cumulative context rotate with a handoff brief instead of compacting until they lose the plot.

```text
$ agentcompass plan --input examples/task.json --dry-run
task      other_or_mixed · routine · 1.5 h · local_only (runs pytest)
winner    argon_high   score 0.2589
          gemini-weekly: 63% left ($158.19), this work ~2.4% (~$6.00),
          other work wants ~48% before its reset in 89h → opportunity cost 0.0211
runner-up sol_high     score 0.2528   (codex: 83% left, but observed demand ~84% before reset in 144h)
excluded  dot          local_only: needs this machine
```

### Quickstart

**Windows (installer, no Python or admin rights needed):**

Download `AgentCompass-tray-setup.exe` from [Releases](https://github.com/akiiwa-games/AgentCompass/releases) and run it, or install silently from PowerShell:

```powershell
AgentCompass-tray-setup.exe /VERYSILENT /SETUP=hooks      # /SETUP=nohooks to skip the spawn gate
```

**macOS / Linux, or CLI-only (Python 3.11+):**

```bash
uv tool install "git+https://github.com/akiiwa-games/AgentCompass"
agentcompass plan --demo --dry-run                        # offline demo, no credentials needed
agentcompass setup                                        # --dry-run to preview; `agentcompass uninstall` undoes it
agentcompass doctor                                       # check active install and signed-in providers
agentcompass roll --task "<what the worker should do>"    # route and launch a worker
```

Or hand setup to your coding agent:
*"Read this repository. Run `agentcompass setup --dry-run`, show me what it would change, then run it and route this task: …"*

### Supported Hosts & Docs

- **Hosts**: Claude Code, Codex CLI, Google Antigravity (`agy`), Paseo, Pi, OpenCode, Orca, T3 Code, and Herdr core (`hc`).
- **Host & gateway plugins**: [cindy-agentcompass-plugin](https://github.com/akiiwa-games/cindy-agentcompass-plugin) (Cindy tasks), [magpie-agentcompass-router](https://github.com/akiiwa-games/magpie-agentcompass-router) (Magpie gateway), [usage-float](https://github.com/akiiwa-games/usage-float) (floating quota HUD).
- **Deep dive**: Scoring formula, quota collection, Windows tray console, host rollout internals, checkout migration, and test suites live in **[`docs/technical-reference.md`](docs/technical-reference.md)**.

---

## 简体中文

### 为什么需要 AgentCompass

即使聚焦到一个主会话或一个 GrokBot，你的负担并不比直接管理多个 Agent 轻：

> *“新加这个展柜界面，你直接派给 opus5.5 来设计落地。我需要惊艳一点的修改所以开 xhigh 吧。”*
>
> *“你找 argon 看看这个策略还有没有什么优化的可能性，只出方案，别写代码。”*
>
> *“让 astra 做代码 review 和端到端测试。plus 额度没了的话，再让 deepseek 顶上。”*

Agent 各擅所长，额度动态变化，仿佛在脑子里管理一支数十人的团队——既要记着谁审美在线、谁适合发散出方案、谁审查严谨，又要盘算哪个任务该开 `xhigh`、哪个账号的 5 小时或周额度还没见底。

**AgentCompass** 把这套排班与额度调度从你的脑子里接管过来（支持 Claude Max / Pro、ChatGPT / Codex、Google AI Ultra / Pro、Grok 及各类兼容号池）。当主会话需要派发子任务或找顾问征求意见时，只需调用 `agentcompass roll`：

- **按任务长板与难度选人定档** — 自动识别任务属于界面设计、只读方案推演、跨模块重构还是代码审查与测试，匹配合适的 `model × effort` 组合（单线任务留在主会话自己做，多独立交付物提示拆分）。
- **跟随 5h / 7d 动态额度自动轮转** — 本地读取已登录账号的实时余量，某个账号额度耗尽或暂停时自动切换替补，并在周重置前优先消耗富余额度。
- **点名即透传，不点名免操心** — 你在对话里明确指定了模型或档位时，主会话会通过 `--model ... --effort ... --basis "<你的原话>"` 原样执行；没指定时由路由器自动拍板。
- **派工门禁与 2M 上下文轮换** — 可选挂载 `PreToolUse` 门禁拦截主会话私自起子 Agent；worker 历史累计未压缩上下文达到 2M tokens 时自动换人交接，防止反复压缩后失忆。

### 快速开始

**Windows（独立安装包，无需管理员权限或预装 Python）：**

从 [Releases](https://github.com/akiiwa-games/AgentCompass/releases) 下载运行 `AgentCompass-tray-setup.exe`，或在终端静默安装：

```powershell
AgentCompass-tray-setup.exe /VERYSILENT /SETUP=hooks      # 不装门禁可用 /SETUP=nohooks
```

**macOS / Linux 或仅安装 CLI（Python 3.11+）：**

```bash
uv tool install "git+https://github.com/akiiwa-games/AgentCompass"
agentcompass plan --demo --dry-run                        # 离线演示，无需登录账号
agentcompass setup                                        # --dry-run 预览改动；agentcompass uninstall 可完整回滚
agentcompass doctor                                       # 查看安装状态与已识别账号
agentcompass roll --task "<交给子 Agent 的完整任务>"       # 自动分类、选型并启动 worker
```

也可以直接把仓库丢给你的主 Agent：
*“读一下这个仓库，先跑 `agentcompass setup --dry-run` 给我看会改哪些配置，确认后执行安装并帮我路由这个任务：……”*

### 支持的宿主与技术文档

- **直接支持的宿主**：Claude Code、Codex CLI、Google Antigravity (`agy`)、Paseo、Pi、OpenCode、Orca、T3 Code、Herdr core (`hc`)。
- **配套插件**：[cindy-agentcompass-plugin](https://github.com/akiiwa-games/cindy-agentcompass-plugin)（Cindy 任务路由）、[magpie-agentcompass-router](https://github.com/akiiwa-games/magpie-agentcompass-router)（Magpie 网关路由）、[usage-float](https://github.com/akiiwa-games/usage-float)（桌面额度悬浮窗）。
- **技术与运维细节**：评分公式、多账号额度采集、Windows 托盘控制台、各宿主全局规则同步、从源码目录迁移及测试命令，详见 **[`docs/technical-reference.md`](docs/technical-reference.md)**。

## License

[MIT](LICENSE) · Copyright (c) 2026 akiiwa-games.
