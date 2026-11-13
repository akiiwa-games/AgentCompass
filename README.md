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

Even with a single main session, delegating work across multiple subscriptions requires manual routing on every task:

> *"Assign the new showcase UI to Opus 5.5 on `xhigh`."*
>
> *"Have Argon review this caching strategy for improvements—proposal only, no code changes."*
>
> *"Run code review and E2E tests on Astra; fall back to Sol or DeepSeek if Plus quota is low, and use the remaining Gemini weekly quota before tonight's reset."*

Model names such as Argon, Sol, Astra, Dot and DeepSeek in this README come from the public catalogue: [`model-capabilities.md`](skills/agent-compass/references/model-capabilities.md).

Models differ by strength, routine tasks quickly drain flagship quota on high effort, and 5-hour / weekly windows reset on independent clocks—leaving one account rate-limited while another expires unused.

**AgentCompass** schedules across your existing subscriptions (**Claude Max / Pro**, **ChatGPT / Codex**, **Google AI Ultra / Pro**, **Grok**, and OpenAI-compatible pools) through one command:

```bash
agentcompass roll --task "<what the worker should do>"
```

| Scenario (illustrative) | How `agentcompass roll` routes it |
| :-- | :-- |
| *"Design layout and motion for the showcase page"* | High **aesthetics** demand → selects a visually strong frontier route (e.g., `Opus 5.5 xhigh`). |
| *"Propose improvements to the retry pipeline, read-only"* | High **creativity** (`divergence`) + read-only scope → dispatches an analytical advisor (e.g., `Argon high`) without modifying files. |
| *"Add `--timeout` to a Python CLI and run local `pytest`"* (`--demo`) | Routine **coding** + `local_only` → excludes remote `Dot` and `Astra xhigh` (~85% of a weekly window), selecting target-meeting `Sol medium` (~6% spend). |
| *Claude is ahead of pace this week; Gemini resets in 2 hours* | Per-window **quota recovery line** → holds back the over-pace Claude window and spends Gemini's under-line surplus first at `0` quota cost (right-hand animation above). |

### Key Rules & Controls

- **Calibrated capability fit, capped by demand level** — Scores healthy `model × effort × account` candidates across **creativity**, **coding**, **aesthetics**, and **tool operation** (`0–3`) on public-leaderboard standard scores; routine demands cap the fit at their target so routine tasks do not over-spend on frontier effort, while the hardest demands keep crediting ability up to the best measured model (single-thread work stays in the main session; multi-deliverable briefs return `split`).
- **Per-window quota recovery line** — Each 5h / weekly window recovers linearly along its own reset clock: spend **under** the line is expiring surplus (`0` quota cost, used first); spend **over** the line borrows from the remainder of the period and raises quota cost (held back). Pre-reset demand from other work is projected solely from that window's current-period usage, never from past periods or task history. Full formulas live in [`docs/technical-reference.md`](docs/technical-reference.md#1-how-scoring--allocation-work).
- **Task profile from a small model** — The task text is sent to the configured scorer (the System-One classifier), which runs on your own subscription CLI or a configured API, and returns the task profile. Allocation is deterministic for a given profile.
- **Explicit pins & offline preview** — Specify a model or effort in chat and `--model ... --effort ... --basis "<your words>"` passes straight through; `agentcompass plan --demo --dry-run` (or `--input examples/task.json --dry-run`) prints candidate scores and exclusion reasons offline.
- **Spawn gate & context rotation** — An optional `PreToolUse` hook blocks sub-agent launches that bypass the router, and workers reaching `rotate_after_carried_tokens` (configurable in the tray) hand off via a structured brief rather than degrading through repeated compactions.

### Quickstart

**Windows (installer, no Python or admin rights needed):** download `AgentCompass-tray-setup.exe` from [Releases](https://github.com/akiiwa-games/AgentCompass/releases), or install silently:

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

Or delegate setup to your coding agent: *"Read this repository, run `agentcompass setup --dry-run` to preview changes, then apply it and route this task: …"*

**Upgrading:** After an update, run `agentcompass doctor` to flag any old-shape local state and apply the listed fix (`re-run "agentcompass setup"`, `re-toggle in the tray`, or `delete "<file>"`; see [`docs/technical-reference.md`](docs/technical-reference.md#after-an-update)).

### Hosts & Ecosystem

- **Hosts**: Claude Code, Codex CLI, Google Antigravity (`agy`), Paseo, Pi, OpenCode, Orca, T3 Code, and Herdr core (`hc`).
- **Plugins & HUD**: [cindy-agentcompass-plugin](https://github.com/akiiwa-games/cindy-agentcompass-plugin) (Cindy) · [magpie-agentcompass-router](https://github.com/akiiwa-games/magpie-agentcompass-router) (Magpie) · [usage-float](https://github.com/akiiwa-games/usage-float) (desktop quota HUD).
- **Reference**: [`docs/technical-reference.md`](docs/technical-reference.md) (scoring formulas, quota polling, Windows tray, host rollout, tests) · [`AGENTS.md`](AGENTS.md) (module map & invariants).

---

## 简体中文

### 为什么需要 AgentCompass

即便所有工作都经由单一主会话分发，跨订阅派活时仍需逐单人工权衡：

> *“新加的展柜界面派给 Opus 5.5 设计落地，开 `xhigh`。”*
>
> *“让 Argon 评估这套缓存策略还有没有优化空间，只出方案，不改代码。”*
>
> *“让 Astra 做代码审查和端到端测试；Plus 额度不足时切到 Sol 或 DeepSeek，另外把今晚重置前剩余的 Gemini 周额度先用掉。”*

本文中的 Argon、Sol、Astra、Dot、DeepSeek 等模型名取自公开模型目录：[`model-capabilities.md`](skills/agent-compass/references/model-capabilities.md)。

不同模型长板各异，常规任务误开高档位会迅速消耗旗舰配额；各家 5 小时窗与周窗口又按各自时钟重置，容易出现主力账号提前限流、另一账号到期作废。

**AgentCompass** 接入你现有的订阅（**Claude Max / Pro**、**ChatGPT / Codex**、**Google AI Ultra / Pro**、**Grok** 及各类 OpenAI 兼容号池），主会话派发任务或征求顾问意见时通过一条命令完成调度：

```bash
agentcompass roll --task "<交给子 Agent 的完整任务>"
```

| 典型场景（示例） | `agentcompass roll` 的处理 |
| :-- | :-- |
| *“设计展柜页面的布局与交互动效”* | 识别高**审美**需求 → 选用视觉长板的旗舰高档位（如 `Opus 5.5 xhigh`）。 |
| *“评估重试管线的优化方案，只读不改代码”* | 识别高**创意**（发散推演）与只读范围 → 派发分析见长的顾问（如 `Argon high`），不修改工作区。 |
| *“为 Python CLI 增加 `--timeout` 参数并运行本地 `pytest`”*（`--demo`） | 识别常规**编码** + `local_only` → 排除远端 `Dot` 及需占用约 85% 周额度的 `Astra xhigh`，选择达标且仅占约 6% 额度的 `Sol medium`（到标封顶）。 |
| *Claude 本周消耗过快，而 Gemini 距周重置仅剩 2 小时* | 对照各窗口**额度恢复线** → 降权超速消耗的 Claude 窗口，将 Gemini 恢复线以下的临期余量按 `0` 额度成本优先使用（见页首右图）。 |

### 关键规则与控制权

- **公开榜单标准分，按需求档位封顶** — 按**创意、编码、审美、工具操作**四类需求（`0–3`）评估各健康 `模型 × 档位 × 账号` 的公开榜单校准分；常规需求（1、2 级）满足目标后不再加分，最难的 3 级需求继续计分到实测最强模型（单线任务留在主会话执行，多独立交付物退回拆分）。
- **单窗口独立额度恢复线** — 每个 5h / 周窗口按各自重置时钟线性恢复：**线下**差额为重置前未用即作废的已恢复余量（计 `0` 成本，优先使用）；**线上**花费为向本期剩余时间预支（计入成本自动降权）。重置前其他任务的预计占用仅由该窗口本周期当前用量推算，不依赖历史周期或任务记录。完整公式见 [`docs/technical-reference.md`](docs/technical-reference.md#1-how-scoring--allocation-work)。
- **任务画像来自小模型** — 任务文本会发送给已配置的打分器（System-One 分类器）；它运行在你自己的订阅 CLI 或已配置的 API 上，返回任务画像。给定画像后，分配结果是确定的。
- **点名透传与离线预览** — 对话中指定模型或档位时，`--model ... --effort ... --basis "<原话>"` 原样透传；`agentcompass plan --demo --dry-run`（或 `--input examples/task.json --dry-run`）可离线查看候选得分与排除原因。
- **派工门禁与上下文轮换** — 可选 `PreToolUse` 门禁拦截未经路由器的子 Agent 启动；worker 累计上下文达到 `rotate_after_carried_tokens`（可在托盘配置）时附带交接摘要轮换新实例，避免多轮压缩导致上下文退化。

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

或交由主 Agent 执行：*“阅读本仓库，先运行 `agentcompass setup --dry-run` 展示拟修改项，确认后完成安装并路由以下任务：……”*

**升级说明：** 更新版本后运行 `agentcompass doctor` 检查本地是否有旧格式状态文件，并按提示执行对应修复（重跑 `agentcompass setup`、在托盘重新切换开关、或删除指定缓存文件；详见 [`docs/technical-reference.md`](docs/technical-reference.md#after-an-update)）。

### 支持宿主与生态

- **直接支持宿主**：Claude Code、Codex CLI、Google Antigravity (`agy`)、Paseo、Pi、OpenCode、Orca、T3 Code、Herdr core (`hc`)。
- **插件与悬浮窗**：[cindy-agentcompass-plugin](https://github.com/akiiwa-games/cindy-agentcompass-plugin)（Cindy）· [magpie-agentcompass-router](https://github.com/akiiwa-games/magpie-agentcompass-router)（Magpie）· [usage-float](https://github.com/akiiwa-games/usage-float)（桌面额度悬浮窗）。
- **参考文档**：[`docs/technical-reference.md`](docs/technical-reference.md)（打分公式、额度采集、Windows 托盘、宿主规则同步与测试）· [`AGENTS.md`](AGENTS.md)（模块地图与架构红线）。

## License

[MIT](LICENSE) · Copyright (c) 2026 akiiwa-games.
