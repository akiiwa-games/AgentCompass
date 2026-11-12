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

Works with the subscriptions you already pay for—**Claude Max / Pro**, **ChatGPT / Codex**, **Google AI Ultra / Pro**, **Grok**, and OpenAI-compatible pools—through one command when delegating work or asking for a second opinion:

```bash
agentcompass roll --task "<what the worker should do>"
```

| Problem | What AgentCompass does |
| :-- | :-- |
| 5-hour and weekly limits reset on separate clocks across accounts. | Reads local current-period snapshots and tracks every window on its own reset clock. |
| Quota expires unused on one account while another runs dry. | Spends under-line quota first (`0` cost) and holds back windows draining faster than they recover. |
| Every task wants a different `model × effort`. | Classifies what the task needs and scores every healthy `model × effort × account` against it. |
| Delegated cost is invisible until after the worker finishes. | Estimates hours and API-equivalent USD upfront and logs every candidate's breakdown to a local ledger. |

### How it works

- **4-category profile, capped at target** — Classifies expected hours and `0–3` demand across **creativity** (`divergence`), **coding**, **aesthetics**, and **tool operation** (keeping single-thread tasks in the main session and returning `split` on multi-deliverable briefs). Candidates are scored on public-leaderboard standard scores (`N = F + continuity + boost − C`), with capability fit `F` capped at the task target so routine work does not burn frontier effort.
- **Quota recovery line** — A window of capacity `C` has recovered `C × elapsed / period` so far. Spend **under** that line would otherwise expire at reset (`0` quota cost, used first); spend **over** the line borrows from the rest of the period and raises `C` (held back automatically). Multi-window accounts bind on their tightest window, non-resetting allowances never recover, and pre-reset demand from other work is projected solely from that window's current-period usage—never from past periods or task history.
- **Deterministic tie-breaks** — Equal `N` goes to the candidate that can start now, then finishes sooner, then costs less. Full formulas and calibration details live in [`docs/technical-reference.md`](docs/technical-reference.md#1-how-scoring--allocation-work).
- **You stay in control** — Explicit `--model` / `--effort` pins (`--basis "<your words>"`) pass straight through untouched; `agentcompass plan --input examples/task.json --dry-run` previews rankings without launching; an optional `PreToolUse` gate blocks unrouted spawns; workers reaching `rotate_after_carried_tokens` hand off with a brief instead of compacting again.

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

直接复用你现有的订阅——**Claude Max / Pro**、**ChatGPT / Codex**、**Google AI Ultra / Pro**、**Grok** 及各类 OpenAI 兼容号池——主会话派发子任务或征求第二意见时只需一条命令：

```bash
agentcompass roll --task "<交给子 Agent 的完整任务>"
```

| 痛点 | AgentCompass 的做法 |
| :-- | :-- |
| 各家 5 小时窗口与周窗口限额不同、各走各的重置时钟。 | 从本地快照读取各已登录账号的本期用量，每个窗口按自己的重置时钟独立追踪。 |
| 一个账号的额度没用完就作废，另一个账号却提前见底。 | 优先花掉恢复线以下即将作废的余量（额度成本为 `0`），对超速消耗的窗口自动降权。 |
| 不同任务适合的 `模型 × 档位` 各不相同。 | 小模型分类器提取任务需求，对所有健康的 `模型 × 档位 × 账号` 组合打分选优。 |
| 派出去的 worker 要跑多久、折合多少成本事先未知。 | 启动前估算工时与按 API 折算的 USD，并将全部候选得分明细写入本地台账。 |

### 核心机制

- **四类能力画像，到标封顶** — 分类器给出预计工时与 **创意**（`divergence`）、**编码**、**审美**、**工具操作** 四类需求等级（`0–3`；单线任务留在主会话执行，多独立交付物退回拆分）。候选按公开引证榜单折算的标准分计算净效用 `N = F + continuity + boost − C`，契合度 `F` 在满足任务目标后封顶，简单任务不会浪费高档旗舰额度。
- **额度恢复线** — 容量为 `C` 的窗口当前已线性恢复 `C × 已过时间 / 周期`。**线下**差额是重置前不用就会作废的已恢复额度（计 `0` 成本，优先使用）；**线上**花费是向本期剩余时间预支（计入 `C`，自动避让）。多窗口账号受最紧窗口约束，不重置的赠额永不恢复；重置前其他任务的预计占用仅由该窗口本周期已发生的用量推算，不读历史周期或任务记录。
- **确定性平分决胜** — `N` 相等时依次优选立即可启动、更快完成、成本更低的候选。完整公式与面板校准见 [`docs/technical-reference.md`](docs/technical-reference.md#1-how-scoring--allocation-work)。
- **始终由你做主** — 对话里点名模型或档位（`--model ... --effort ... --basis "<你的原话>"`）原样透传；`agentcompass plan --input examples/task.json --dry-run` 仅预览排名；可选 `PreToolUse` 门禁拦截绕过路由器的私自派工；累计上下文达 `rotate_after_carried_tokens` 时自动交接轮换，避免反复压缩失忆。

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
