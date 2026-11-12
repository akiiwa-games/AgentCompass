<div align="center">

# 🧭 AgentCompass

**Route sub-agent tasks across your coding subscriptions by what each task needs and what each quota window has left.**

[English](#english) · [简体中文](#简体中文) · [Releases](https://github.com/akiiwa-games/AgentCompass/releases) · [Technical Reference](docs/technical-reference.md) · [`llms.txt`](llms.txt)

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![Router](https://img.shields.io/badge/router-Python%20%2B%20JSON-555)
![Tray](https://img.shields.io/badge/tray-Windows%2011-0078D4)
![License](https://img.shields.io/badge/license-MIT-green)

<table>
  <tr>
    <td align="center" width="62%">
      <img src="docs/hero.gif" width="520" alt="Decision Compass replaying a synthetic decision across five capability axes against the best-ranked other model">
      <br>
      <sub><b>Decision Compass · 决策罗盘</b> — 5-axis capability fit vs. runner-up</sub>
    </td>
    <td align="center" width="38%">
      <img src="docs/quota-line.gif" width="320" alt="Quota Recovery Line holding back an over-line window and spending under-line quota in fast mode before reset">
      <br>
      <sub><b>Quota Recovery Line · 额度恢复线</b> — spare over-pace windows, spend expiring surplus first</sub>
    </td>
  </tr>
</table>

</div>

---

## English

### Why AgentCompass

Even when you funnel all your work through a single main session or one GrokBot, the mental load is no lighter than managing a team of specialists in your head:

> *"Hand this new showcase UI straight to Opus 5.5 to design and build. Turn on `xhigh`—I want the result to really stand out."*
>
> *"Ask Argon whether this caching strategy can be pushed any further. Proposal only, don't touch the code."*
>
> *"Have Astra run the code review and end-to-end tests. If Plus quota runs low, let Sol or DeepSeek step in—and burn that Gemini weekly allowance before tonight's reset."*

Every model shines at different work, routine tasks burn through flagship quota if left on max effort, and 5-hour / weekly limits reset on separate clocks—leaving one account throttled mid-afternoon while another expires unused at midnight.

**AgentCompass** takes that scheduling off your hands across the plans you already pay for (**Claude Max / Pro**, **ChatGPT / Codex**, **Google AI Ultra / Pro**, **Grok**, and OpenAI-compatible pools). Your main session delegates or asks for a second opinion with one command:

```bash
agentcompass roll --task "<what the worker should do>"
```

| Real-world situation (illustrative) | What `agentcompass roll` does automatically |
| :-- | :-- |
| *"Design an interactive showcase page with polished motion"* | Sees high **aesthetics** demand → picks a visual-leading frontier route (such as `Opus 5.5 xhigh`) where extra effort actually pays off. |
| *"Brainstorm better ways to structure this retry pipeline, read-only"* | Sees high **creativity** (`divergence`) and read-only scope → dispatches an advisor strong at open-ended synthesis (such as `Argon high`) without editing files. |
| *"Add `--timeout` to a Python CLI and run local `pytest`"* (`--demo`) | Sees routine **coding** + `local_only` → drops remote `Dot` and an `Astra xhigh` run that would eat ~85% of a weekly window, picking a target-meeting `Sol medium` (~6% spend) instead of over-spending. |
| *Claude was hammered early in the week; Gemini resets in 2 hours* | Compares each window against its **quota recovery line** → holds back the over-pace Claude window and spends Gemini's expiring under-line allowance at `0` quota cost first (even in fast mode, as in the right-hand animation above). |

### How it stays predictable & under your control

- **Calibrated capability fit, capped at target** — Scores healthy `model × effort × account` candidates across **creativity**, **coding**, **aesthetics**, and **tool operation** (`0–3`) using public-leaderboard standard scores; fit caps at the task target so simple work never wastes frontier effort (single-thread tasks stay in the main session; multi-deliverable briefs return `split`).
- **Per-window quota recovery line** — Each 5h / weekly window recovers linearly on its own reset clock: spend **under** the line is about-to-expire surplus (`0` quota cost, used first); spend **over** the line borrows from the rest of the period and raises quota cost (held back). Pre-reset demand from other work is projected solely from that window's current-period usage—never from past periods or task history. Full math lives in [`docs/technical-reference.md`](docs/technical-reference.md#1-how-scoring--allocation-work).
- **Explicit pins & offline preview** — Name a model or effort in chat and `--model ... --effort ... --basis "<your words>"` passes straight through untouched; `agentcompass plan --demo --dry-run` (or `--input examples/task.json --dry-run`) prints every candidate's score and exclusion reason without launching anything.
- **Spawn gate & context rotation** — An optional `PreToolUse` hook blocks sub-agents that bypass the router, and workers reaching `rotate_after_carried_tokens` (configurable in the tray) hand off with a structured brief instead of compacting until they lose context.

### Quickstart

**Windows (installer, no Python or admin rights needed):** download `AgentCompass-tray-setup.exe` from [Releases](https://github.com/akiiwa-games/AgentCompass/releases), or run silently:

```powershell
AgentCompass-tray-setup.exe /VERYSILENT /SETUP=hooks      # /SETUP=nohooks to skip the spawn gate
```

**macOS / Linux, or CLI-only (Python 3.11+):**

```bash
uv tool install "git+https://github.com/akiiwa-games/AgentCompass"
agentcompass plan --demo --dry-run                        # offline demo, no credentials needed
agentcompass setup --dry-run                              # preview host wiring (omit --dry-run to apply; `agentcompass uninstall` undoes it)
agentcompass doctor                                       # check active install and signed-in providers
agentcompass roll --task "<what the worker should do>"    # route and launch a worker
```

Or hand setup to your coding agent: *"Read this repository, run `agentcompass setup --dry-run` to show what it changes, then apply it and route this task: …"*

### Hosts & Ecosystem

- **Hosts**: Claude Code, Codex CLI, Google Antigravity (`agy`), Paseo, Pi, OpenCode, Orca, T3 Code, and Herdr core (`hc`).
- **Plugins & HUD**: [cindy-agentcompass-plugin](https://github.com/akiiwa-games/cindy-agentcompass-plugin) (Cindy) · [magpie-agentcompass-router](https://github.com/akiiwa-games/magpie-agentcompass-router) (Magpie) · [usage-float](https://github.com/akiiwa-games/usage-float) (desktop quota HUD).
- **Reference**: [`docs/technical-reference.md`](docs/technical-reference.md) (scoring formulas, quota polling, Windows tray, host rollout, tests) · [`AGENTS.md`](AGENTS.md) (module map & invariants).

---

## 简体中文

### 为什么需要 AgentCompass

即使你把所有工作都收口到一个主会话或一个 GrokBot，脑子里的排班负担一点也没比亲手管十几个 Agent 轻：

> *“新加这个展柜界面，你直接派给 Opus 5.5 来设计落地。我需要惊艳一点的效果，开 `xhigh` 吧。”*
>
> *“你找 Argon 看看这套缓存策略还有没有什么优化空间，只出方案，别改代码。”*
>
> *“让 Astra 做代码 review 和端到端测试；Plus 额度快见底的话让 Sol 或 DeepSeek 顶上——对了，Gemini 今晚周重置还剩小半管额度，能塞的活儿先塞过去花掉。”*

每个模型各有所长，常规小活儿忘了关高档位几轮就把旗舰号烧穿，而各家的 5 小时窗、周窗口又各走各的重置时钟——常常一边下午刚刷爆限流，另一边半夜到期白白清零。

**AgentCompass** 直接复用你现有的订阅（**Claude Max / Pro**、**ChatGPT / Codex**、**Google AI Ultra / Pro**、**Grok** 及各类 OpenAI 兼容号池），把这套“看活选人 + 盯表算账”收口成主会话的一条命令：

```bash
agentcompass roll --task "<交给子 Agent 的完整任务>"
```

| 你日常派出的真实场景（示例） | `agentcompass roll` 在背后自动做什么 |
| :-- | :-- |
| *“给展柜页做一套交互惊艳的动效改版”* | 识别高**审美**需求 → 启用视觉表现拔尖的旗舰高档位（如 `Opus 5.5 xhigh`），把好钢用在刀刃上。 |
| *“评估这套重试管线还能怎么优化，只给方案别改代码”* | 识别高**创意发散** + 只读范围 → 派发散推演强的顾问（如 `Argon high`）出方案，不碰仓库代码。 |
| *“给 Python CLI 补个 `--timeout` 参数并跑通本地 `pytest`”*（`--demo`） | 识别常规**编码** + `local_only` → 排除远端 `Dot` 与会吃掉 ~85% 周额度的 `Astra xhigh`，选刚好达标、仅花 ~6% 额度的 `Sol medium`，到标即止不烧旗舰。 |
| *Claude 前两天刷太猛超速了，而 Gemini 离周重置只剩 2 小时* | 对比各窗口**额度恢复线** → 自动避让透支的 Claude 窗口，把 Gemini 恢复线以下不用就作废的余量按 `0` 额度成本优先花掉（甚至直接开快档消纳，见页首右图）。 |

### 关键规则与控制权

- **公开榜单标准分，到标封顶** — 按**创意、编码、审美、工具操作**四类需求（`0–3`）对比各健康 `模型 × 档位 × 账号` 的公开榜单校准分；能力满足任务目标后不再加分（单线任务留在主会话自己做，多独立交付物退回拆分）。
- **单窗口独立额度恢复线** — 每个 5h / 周窗口按各自重置时钟匀速恢复：**线下**差额是重置时不用即作废的已恢复额度（计 `0` 成本，优先使用）；**线上**花费是向本期后半段预支（抬高成本自动避让）。重置前其他任务的预计占用仅由该窗口本周期已发生的用量推算，不读历史周期或任务记录。完整公式见 [`docs/technical-reference.md`](docs/technical-reference.md#1-how-scoring--allocation-work)。
- **点名即透传，离线可预览** — 对话里明确指定模型或档位时，`--model ... --effort ... --basis "<你的原话>"` 原样执行绝不篡改；`agentcompass plan --demo --dry-run`（或 `--input examples/task.json --dry-run`）可随时离线查看完整候选打分与淘汰原因。
- **派工门禁与上下文轮换** — 可选 `PreToolUse` 门禁拦截绕过路由器的私自派工；worker 累计上下文达到 `rotate_after_carried_tokens`（可在托盘调节）时带着交接任务书换新 worker，避免反复压缩后失忆跑偏。

### 快速开始

**Windows（独立安装包，无需预装 Python 或管理员权限）：** 从 [Releases](https://github.com/akiiwa-games/AgentCompass/releases) 下载运行 `AgentCompass-tray-setup.exe`，或静默安装：

```powershell
AgentCompass-tray-setup.exe /VERYSILENT /SETUP=hooks      # 不装门禁可用 /SETUP=nohooks
```

**macOS / Linux 或仅安装 CLI（Python 3.11+）：**

```bash
uv tool install "git+https://github.com/akiiwa-games/AgentCompass"
agentcompass plan --demo --dry-run                        # 离线演示，无需登录账号
agentcompass setup --dry-run                              # 预览宿主配置改动（去掉 --dry-run 执行；agentcompass uninstall 可回滚）
agentcompass doctor                                       # 查看安装状态与已识别账号
agentcompass roll --task "<交给子 Agent 的完整任务>"       # 自动分类、选型并启动 worker
```

也可以直接交给主 Agent：*“读一下这个仓库，先跑 `agentcompass setup --dry-run` 给我看会改哪些配置，确认后执行安装并帮我路由这个任务：……”*

### 支持宿主与生态

- **直接支持宿主**：Claude Code、Codex CLI、Google Antigravity (`agy`)、Paseo、Pi、OpenCode、Orca、T3 Code、Herdr core (`hc`)。
- **插件与悬浮窗**：[cindy-agentcompass-plugin](https://github.com/akiiwa-games/cindy-agentcompass-plugin)（Cindy）· [magpie-agentcompass-router](https://github.com/akiiwa-games/magpie-agentcompass-router)（Magpie）· [usage-float](https://github.com/akiiwa-games/usage-float)（桌面额度悬浮窗）。
- **参考文档**：[`docs/technical-reference.md`](docs/technical-reference.md)（打分公式、额度采集、Windows 托盘、宿主规则同步与测试）· [`AGENTS.md`](AGENTS.md)（模块地图与架构红线）。

## License

[MIT](LICENSE) · Copyright (c) 2026 akiiwa-games.
