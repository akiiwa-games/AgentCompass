<div align="center">

# 🧭 AgentCompass

**Route sub-agent tasks across your coding subscriptions by what each task needs and what each quota window has left.**

[English](#english) · [简体中文](#简体中文) · [Technical Reference](docs/technical-reference.md) · [`llms.txt`](llms.txt)

![Python](https://img.shields.io/badge/python-3.11%2B-3776AB)
![Router](https://img.shields.io/badge/router-Python%20%2B%20JSON-555)
![Tray](https://img.shields.io/badge/tray-Windows%2011-0078D4)
![License](https://img.shields.io/badge/license-MIT-green)

<img src="docs/hero.gif" width="880" alt="The Decision Compass replaying one synthetic decision: task, kind, candidate routes, then the winner and the best-ranked other model opening as two sheets of tinted glass on five axes, and the winner under that radar">

</div>

---

## English

### What it is for

If you delegate coding work to sub-agents and pay for more than one subscription, four things are decided by hand on every task:

| Problem | What AgentCompass does |
| :-- | :-- |
| Each subscription has its own limits and reset times: 5-hour and weekly windows, on different clocks. | Reads the current-period usage of every signed-in account from local snapshots and follows each window on its own clock. |
| Quota expires unused on one account while another runs dry. | Spends first the quota that is about to expire, and holds back a window that is being used faster than it recovers. |
| The right model and effort differ from task to task. | A small classifier states what the task needs; every available `model × effort × account` is scored against that need. |
| You do not know what a delegated worker will cost until it has run. | Estimates hours and API-equivalent USD for each candidate before launch and writes the decision to a local ledger. |

It works with plans you already have: Claude Max / Pro, ChatGPT / Codex, Google AI Ultra / Pro, Grok, and OpenAI-compatible pools. When the main session wants to delegate a task or ask for a second opinion, it runs one command:

```bash
agentcompass roll --task "<what the worker should do>"
```

### How a task is routed

1. **Profile.** The classifier estimates expected hours and a demand level (`0–3`) across four capability categories: **creativity** (`divergence`: writing, brainstorming, research synthesis, data analysis), **coding** (`execution` and `faithfulness` for code, tests, reviews, and audits), **aesthetics** (web/GUI layout, 3D, and SVG), and **tool operation** (responsiveness and, at level 3, image recognition). Single-thread work stays with the main session; multi-task briefs return `split` so each independent deliverable gets its own worker.
2. **Candidates.** Every eligible `model × effort × account` is checked against hard gates (sign-in health, host policy, modality, `local_only`, data coverage, and per-window safety reserves). Capability scores come from cited public leaderboards on a shared panel standard-score scale, with missing facets estimated and marked explicitly.
3. **Score.** `N = F + continuity + boost − C`: capability fit `F` is capped at the task's target (routine tasks do not over-spend on frontier effort), `continuity` rewards resuming an active coupled worker within its context rotation limit, `boost` applies any temporary user priority, and `C` charges only the part of the task's spend that lands over the account's quota recovery line. Exact formulas and panel calibration live in [`docs/technical-reference.md`](docs/technical-reference.md#1-how-scoring--allocation-work).
4. **Ties.** Equal `N` goes to the candidate that can start now, then to the one that finishes sooner, then to the cheaper one. Expiring quota is not a tie-break: it already lowers `C`.
5. **Launch.** The winner starts as a worker, and the ledger records every candidate's breakdown for replay in the Decision Compass (shown above with synthetic data).

### Quota: the recovery line

Treat a window's quota as recovering evenly over its period. A window with capacity `C` has recovered `C × elapsed / period` so far; that is its recovery line.

- **Usage under the line (`Used first`).** The gap is quota that has recovered and will expire at the reset if nobody spends it. Spending under the line adds zero quota cost.
- **Usage over the line (`Held back`).** The window is being drained faster than it recovers. Each dollar over the line is borrowed from the rest of the period and priced into `C`.

<div align="center">
<img src="docs/quota-line.gif" width="560" alt="The Quota line tab replaying one task twice, each quota window drawn as a small chart of its quota line and its rising usage line: first a window that is over its line is held back and the task runs on one that is under, then two hours before its reset the remaining quota is used first in fast mode">
</div>

In the animation above (synthetic data), each chart tracks one window on its own reset clock: the pale diagonal is the recovery line, the green/red curve is current-period usage, indigo shows the chosen task's spend, and the dotted line closes at the reset:
- **Spared when ahead of pace:** When one account is already above its recovery line while another sits below its own line, the over-line spend raises `C` on the first account, shifting work to the under-line account automatically—even when their reset clocks do not line up.
- **Used first before reset:** Shortly before a window resets with unused allowance still under its line, its whole spend (including fast mode when it stays under the line) incurs zero over-line cost, so equal-`N` ties go straight to the faster run instead of letting recovered quota expire.

Three rules keep this predictable:
- Multi-window accounts (such as 5-hour + weekly) are checked against every applicable window; the tightest one binds.
- Non-resetting starter allowances never recover, so every dollar spent is over the line rather than treated as unlimited.
- Only current-period usage is read: the demand other work will place on a window before its reset is projected from that window's own usage in the current period, never from earlier periods or task history.

### Staying in control

- Name a model or an effort in your prompt and the main session passes it through as `--model ... --effort ... --basis "<your words>"`. The router does not override it.
- `agentcompass plan --input examples/task.json --dry-run` prints the ranking for a task without launching anything or writing to the ledger.
- An optional `PreToolUse` hook blocks sub-agent launches that did not go through the router.
- A worker whose cumulative context reaches the configured context rotation threshold (`rotate_after_carried_tokens`, set in the tray) is replaced by a new worker with a handoff brief instead of being compacted again.

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
- **Reference**: scoring formulas, quota collection, Windows tray console, host rollout, checkout migration, and test suites are in **[`docs/technical-reference.md`](docs/technical-reference.md)**; module ownership and contributor invariants are in **[`AGENTS.md`](AGENTS.md)**.

---

## 简体中文

### 它解决什么问题

如果你把编码任务派给子 Agent，同时又订阅了不止一家服务，那么每个任务都有四件事要靠人来定：

| 问题 | AgentCompass 的做法 |
| :-- | :-- |
| 每个订阅的限额和重置时间各不相同：5 小时窗口、周窗口，各走各的时钟。 | 从本地快照读取每个已登录账号本期的用量，每个窗口按自己的时钟计算。 |
| 一个账号的额度没用完就作废，另一个账号却已经见底。 | 先花快要作废的额度；消耗速度超过恢复速度的窗口先留着。 |
| 不同任务适合的模型和档位不同。 | 小模型分类器给出任务的需求；每个可用的 `模型 × 档位 × 账号` 都按这份需求打分。 |
| 派出去的 worker 要花多少，跑完才知道。 | 启动前先估算每个候选的工时和按 API 价格折算的 USD，并把这次决策写入本地台账。 |

支持你已经在用的订阅：Claude Max / Pro、ChatGPT / Codex、Google AI Ultra / Pro、Grok，以及 OpenAI 兼容的号池。主会话要派发任务或找顾问时，只运行一条命令：

```bash
agentcompass roll --task "<交给子 Agent 的完整任务>"
```

### 一个任务如何被路由

1. **画像。** 分类器给出任务的预计工时，以及四个能力类别上的需求等级（`0–3`）：**创意**（`divergence`：写作、头脑风暴、调研综合、数据分析）、**编码能力**（`coding`：代码实现、测试与审查审计）、**审美能力**（`aesthetics`：界面交互、排版、3D 与 SVG）与**工具操作**（`tool_operation`：单轮响应速度及等级 3 的图像识别）。单线任务交还主会话自己做；包含多个独立交付物的任务书退回主会话拆分。
2. **候选。** 所有满足硬门禁（登录健康、宿主策略、模态、`local_only`、数据覆盖与窗口安全余量）的 `模型 × 档位 × 账号` 进入评分。模型能力统一由公开引证榜单折算到固定面板标准分，缺失子维度显式标注估算来源。
3. **打分。** `N = F + continuity + boost − C`：契合度 `F` 在满足任务目标后封顶（简单改动不会浪费最高档旗舰模型），`continuity` 奖励在上下文轮换阈值内续接强耦合 worker，`boost` 叠加用户临时加权，`C` 只对越过账号额度恢复线的花费计价。完整公式与校准细节见 [`docs/technical-reference.md`](docs/technical-reference.md#1-how-scoring--allocation-work)。
4. **平分时。** `N` 相等，先选现在就能开始的候选，再选完成更快的，最后选更便宜的。额度临期不参与平分比较：它已经体现在更低的 `C` 里。
5. **启动。** 胜出的候选作为 worker 启动，台账保留全部候选得分明细供托盘“决策罗盘”重放（页首动画为合成数据）。

### 额度：恢复线

把一个窗口的额度看成在周期内匀速恢复。容量为 `C` 的窗口，到现在为止已经恢复了 `C × 已过时间 / 周期`，这就是它的恢复线。

- **用量在线下（`优先使用`）。** 差额是已经恢复、但到重置时没人用就会作废的额度。在线下花掉它不计额度成本。
- **用量在线上（`降权`）。** 这个窗口的消耗快于恢复。越过恢复线的每一美元都是向本期后面借的，计入 `C`。

<div align="center">
<img src="docs/quota-line.gif" width="560" alt="“额度恢复线”页签把同一个任务重放两次，每个额度窗口画成一张小图，一根额度线和一根不断上涨的实际用量线：先是用量已经超过额度线的窗口被降权，任务派到线下的窗口；然后在重置前两小时，剩余额度被优先使用，用快速模式花掉">
</div>

上方动画（合成数据）里，每张小图按各自重置时钟跟踪一个窗口：浅色斜线是恢复线，绿/红实线是本期用量，靛蓝色是本次任务的预计消耗，点线在重置时刻闭合：
- **超前消耗自动避让**：当某个账号已跑到恢复线上方而另一账号仍在线下时，越线花费会抬高前者的 `C`，无需重置时间对齐即可自动把任务分流到线下账号。
- **临期余量优先消纳**：临近重置且仍有大量线下余量时，整笔花费（包括仍在线下的快速模式）越线成本为 `0`，同分时按完成速度优先选用更快方案，避免已恢复额度白白作废。

三点规则保证行为可预期：
- 同时拥有 5 小时窗口和周窗口的账号会同时检查所有适用窗口，由最紧的窗口约束。
- 永不重置的入门赠额不会随时间恢复，因此每一美元花费都计为越线，不会被当作无限额度。
- 只读取各账号本周期的当前用量：其他任务在重置前还会用掉多少，只按该窗口本周期已发生的用量推算，不读以前的周期，也不读任务历史。

### 你始终可以做主

- 在对话里点名模型或档位，主会话会以 `--model ... --effort ... --basis "<你的原话>"` 原样透传，路由器不会改。
- `agentcompass plan --input examples/task.json --dry-run` 只打印某个任务的候选排名，不启动任何 worker，也不写台账。
- 可选的 `PreToolUse` 门禁会拦截没有经过路由器的子 Agent 启动。
- worker 的历史累计上下文达到配置的上下文轮换阈值（`rotate_after_carried_tokens`，可在托盘策略页设置）时，会带着交接任务书换一个新 worker，而不是继续压缩。

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

也可以直接把仓库交给你的主 Agent：
*“读一下这个仓库，先跑 `agentcompass setup --dry-run` 给我看会改哪些配置，确认后执行安装并帮我路由这个任务：……”*

### 支持的宿主与技术文档

- **直接支持的宿主**：Claude Code、Codex CLI、Google Antigravity (`agy`)、Paseo、Pi、OpenCode、Orca、T3 Code、Herdr core (`hc`)。
- **配套插件**：[cindy-agentcompass-plugin](https://github.com/akiiwa-games/cindy-agentcompass-plugin)（Cindy 任务路由）、[magpie-agentcompass-router](https://github.com/akiiwa-games/magpie-agentcompass-router)（Magpie 网关路由）、[usage-float](https://github.com/akiiwa-games/usage-float)（桌面额度悬浮窗）。
- **参考文档**：打分公式、多账号额度采集、Windows 托盘控制台、各宿主全局规则同步、从源码目录迁移及测试命令见 **[`docs/technical-reference.md`](docs/technical-reference.md)**；模块职责地图与开发红线见 **[`AGENTS.md`](AGENTS.md)**。

## License

[MIT](LICENSE) · Copyright (c) 2026 akiiwa-games.
