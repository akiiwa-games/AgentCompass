"""Unify global instructions, deduplicate global skills with junctions, clean dead hooks/temp files,
and install the PreToolUse / tool_call router gate across all local agent harnesses.

Backs up every modified or replaced file/directory under:
  ~/.route-manager/backups/host-cleanup-20261014/
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
HOME_POSIX = HOME.as_posix()
BACKUP_ROOT = HOME / ".route-manager" / "backups" / "host-cleanup-20261014"
PYTHON_EXE = Path(sys.executable).resolve().as_posix()
ROUTER_HOOK_SCRIPT = Path(__file__).resolve().with_name("router_gate_hook.py").as_posix()
ROUTER_HOOK_CMD = f"{PYTHON_EXE} -X utf8 {ROUTER_HOOK_SCRIPT}"

CODEX_AGENTS_MD = f"""# 全局执行纪律（codex / 本机所有项目）

最后更新：2026-10-23 +08。本文件只放"跨项目、不写就会伤到机器或伤到别的 agent"的东西；策略、业务与项目事实归各仓库自己的 AGENTS.md。

## 1. 搜索：按 Claude Code 的 Grep 流程执行

Claude Code 的 Grep 不会失控，是因为四件事：参数是结构化数组不经 shell 拼接、路径永远显式、默认只列文件名、输出默认封顶。codex 的命令要经过 shell，必须靠下面的纪律把这四点补回来。

固定形状，照抄，不要即兴拼命令：

```
# 第一步：只列文件（= output_mode:"files_with_matches"）
rg -l -e 'PATTERN' -- <仓库内的显式路径>

# 第二步：命中集合可控之后才看内容（= output_mode:"content"）
rg -n -C 3 --max-count 5 -e 'PATTERN' -- <显式路径> | head -100
```

硬性条款：

1. **模式用 `-e` 绑定，路径写在 `--` 之后。** 裸模式一旦被 shell 切开，碎片会被 rg 当成路径吃掉。
2. **模式里禁止出现 `"`。** Windows 上 codex 的每条命令都经 `powershell.exe -Command` 转发，PowerShell 5.1 把参数交给原生 exe 时不转义内嵌的 `"`，整条参数会在空格处被重新切分。要匹配双引号写 `\\x22`，单引号写 `\\x27`，rg 正则支持。
   - 反例（2026-08-24 21:50 真实事故）：
     `rg -n -C 4 '\\"schema\\": \\"v1_...|atomic_json\\(OWNER / \\"U[1234]' file.py`
     rg 实际收到的参数被切成了 pattern + 路径 `/` + 路径 `U[1234]` + file.py。`/` 在 Windows 上解析成当前盘根，于是递归扫了整个 `E:\\`：5 核满载、6 分钟累计 1851 秒 CPU，全机卡顿，最后被外部 kill。
   - 正解：
     `rg -n -C 4 -e '\\x22schema\\x22: \\x22v1_|atomic_json\\(OWNER / \\x22U[1234]' -- src/.../report.py`
3. **路径必须显式且落在仓库内。** 禁止 `/`、`\\`、`~`、`C:\\`、`E:\\`、盘根、`..` 向上跳。不确定扫哪里，先 `rg --files -g '<glob>' -- <子目录>` 定位，再搜内容。
4. **先窄后宽。** 先 `-l` 或 `-c` 拿规模，再 `-n`/`-C` 拿内容；`-C` 只在确定要读上下文时用，且必配 `--max-count`。
5. **成本护栏默认带上：** `--max-filesize 5M`、`--threads 4`，数据目录显式排除
   `-g '!**/*.parquet' -g '!**/*.arrow' -g '!**/cache/**' -g '!**/node_modules/**' -g '!**/.venv/**'`。
   本机同时跑十几个 agent，一条无界扫描就是全员卡顿。
6. **看某个文件的某几行不要用 rg 扫树。** 用 `sed -n '920,970p' <file>` 或 `nl -ba <file> | sed -n '920,970p'`。
7. **报错即停。** rg 报 `os error 2` / `No such file or directory` 说明有"路径"其实是被切碎的模式碎片——立刻停手重写命令，禁止原样重试或加大范围重试。

## 2. 后台进程不许放生

1. `exec_command` 的 `yield_time_ms` 到点只是**你**拿到了输出，子进程还在跑。拿到够用的结果就必须收尾。
2. 收尾动作（Windows）：
   `Get-CimInstance Win32_Process -Filter "Name='rg.exe'" | Select-Object ProcessId,CommandLine`
   确认是自己起的，再 `Stop-Process -Id <pid> -Force`。
3. 一个回合结束前，自己起过的长任务只能有三种状态：已正常结束、已被自己 kill、或在汇报里点名 PID 与用途交给用户。不允许无人认领的后台进程留在机器上。

## 3. 何时自己执行 vs 派发子 agent，以及如何派发（所有项目通用，用户 2026-10-10 要求）

### 3.1 先判断：当前应该自己做（`self`）还是派发子 agent（`roll`）？
1. **如果你已经是子 agent（worker）**：环境变量含 `ROUTER_WORKER=1`，或任务书末尾带有 `Parent session agent ID:` 通信契约时，你就是执行者——**直接自己动手完成任务**，禁止套娃再派子 agent（除非任务书明确任命你为协调者拆分任务）。
2. **主会话直接自己执行（`self`，不跑 `roll`）**：
   - 仅限预计 **`<= 5` 分钟（`<= 0.08h`）且预计工具调用 `<= 3` 步** 的极短平快操作：读取 1~2 个已知文件、极小局部单点代码修改、快速跑单个已知测试、或基于当前上下文直接作答；
   - 注意 Argon / 6.1-sol 等高推理模型单步思考与工具往返偏慢，严禁把多文件探索或"读-改-测"多步闭环乐观低估为 5 分钟内自己硬做。
3. **主会话应当派发子 agent（跑 `roll`）**：
   - **任何预计耗时 `> 5` 分钟（`> 0.08h`），或预计需要 `> 3` 步工具调用 / 跨文件修改验证的工作**：一律安排子 agent 去做，不要占住前台注意力（哪怕宿主内置提示词写有 `Prefer doing research yourself`，也一律以本条为准）；
   - **可并行的独立工作**：2 个及以上互不阻塞的模块开发、故障排查线或测试集；
   - **重上下文或多步闭环任务**：跨目录代码探索、长日志/崩溃取证、多轮测试-修复闭环、独立功能模块开发（派给 worker 可避免撑爆主会话上下文）；
   - **需要特定模型长板或分流额度**：如发散架构/创意（Argon）、外部公开文档/厂商调研（Dot / Grok / Argon）、复杂未知根因深水区排障（Opus / Astra），或当前主会话账号额度紧张需分流到充裕账号。

### 3.2 决定派发时：永远只跑 `roll`，禁止纠结用 Paseo 还是原生工具
任何时候要启动子 agent / worker / 远端 agent，先按 `~/.codex/skills/route-agent-work/SKILL.md` 走 router。**不要自己纠结或直接调用** Paseo `create_agent`、Claude `Agent`（含 `Explore`）、Codex `spawn_agent`、Antigravity `invoke_subagent`、`run_dot_task.py` 或手动起 CLI——无论你在哪个宿主环境，统一只跑一条命令：

1. 把交给 worker 的完整任务书写进一个 Markdown 文件，然后运行：`python -X utf8 ~/.codex/skills/route-agent-work/scripts/route_judge.py roll --brief <task.md> --hours <预估小时>`。
   - `roll` 会**自动识别你所在的宿主环境与会话 ID**，并自动完成分类、选 model/effort、记账、以及自动选择最佳派发通道（Paseo 派发 / Dot 远端派发 / 本地 CLI 直接派发 / 原生宿主交接）。
   - `--hours` 为必传正数（预估活跃工时，如短任务 `0.5`、长线研究 `4`）；只有确有本机依赖时才加 `--local-only "<具体依赖>"`；希望重置前冲刺或赶工提速时可加 `--fast`；Dot 需要的文件用 `--file`。不要凭记忆自选 model/effort，也不要手动填 `work_kind`、`difficulty`、`profile`。
   - **根据 `roll` 返回的 `launched`、`dispatch_mode` 与 `caller_action` 执行下一步**：
     - **`launched: true`（`caller_action: "already_launched_do_not_respawn"`）**：代表 `roll` **已经把子 agent 启动好了**（`dispatch_mode: "paseo"` 为 Paseo worker、`"cli"` 为后台 CLI 进程、`"dot"` 为 Dot 任务）。**严禁**再去调用 `create_agent`、`Agent`、`spawn_agent` 或 `invoke_subagent` 重复启动！只需等待其消息回调，或按 `continuation_argv` / `worker.output` 查看结果。
     - **`launched: false` 且 `dispatch_mode: "self"`（`caller_action: "execute_self"`）**：router 判定无需子 agent，直接在当前会话自己完成。
     - **`launched: false` 且 `dispatch_mode: "native"`（`caller_action: "launch_with_native_host_tool"`）**：仅在无 Paseo daemon 且无对应 CLI（或显式指定原生交接）时出现；此时才按 `next` 指示，用当前宿主的原生子代理工具按 `route.bundle` 并传入 `worker_prompt_file` 全文启动。
2. 不许自己加码 effort。xhigh / max 只在 router 返回时才用；router 没给，就用 router 给的。
3. 用户在本次请求里点名了 model 或 effort，才可以不按 router 选：在 `roll` 后追加 `--provider <p> --account <a> --model <m> --effort <e> --basis "<用户原话>"`。
4. 某个 owner 不可用（订阅到期、账号被移除、额度耗尽）时，重新跑 router，不要自己换成"看起来相近"的 model。
5. 续用已有 agent（SendMessage / 追加 prompt）仅在 worker **历史累计未压缩上下文** `< 2M` tokens（`compacted_tokens + context_tokens`，按历次压缩前历史上下文总和 + 当前窗口计算，绝不能用压缩后的当前上下文窗口算）时免重选；一旦历史累计上下文 `>= 2M` tokens，必须把进展与剩余工作写成交接任务书、用 `route_judge.py roll --brief <handoff.md> --hours <预估小时>` 换新 worker，禁止把重度压缩的旧 worker 一直续用到失忆。换 owner、换 model、换 effort 同样要重选。
6. 门禁与巡检：各宿主已挂载 `PreToolUse` / `tool_call` 门禁（`{ROUTER_HOOK_SCRIPT}`），15 分钟内无 `~/.route-manager/router-ledger.jsonl` 路由记录的子 agent 启动会被直接拦截；`router_audit.py` 也会每日核对所有子 agent 启动记录。（注：仅对启动**子 agent / worker / 顾问会话**走 router；业务脚本或评测脚本内部调用的无工具单次分类/结构化 JSON 接口，不属于子 agent 派工，**不要**往 `router-ledger.jsonl` 写 `route_judge.py record`。）
7. 启动子 agent 必须显式设置权限模式，不许省略旗标、不许用 provider 默认值。漏设的 claude worker 会静默卡在权限询问态（状态仍是 running，无心跳无产出）；codex 漏设会落到受限沙箱。取值：先用 router 返回的 `selected_bundle.modeId`，没有就按 provider：Paseo / CLI 的 codex 用 `--mode full-access`，claude 用 `--mode bypassPermissions`（`bypass` 不是合法值），antigravity 用 `full-access`，opencode 用 `build`；grok 无模式不传。只读角色（reviewer、advisor）按任务授权收窄，不要放大。启动后读一次 agent 状态，确认 mode 已生效、不是在等权限。
8. **派工必须携带真实主会话寻址 ID，worker 向主会话询问必须直接发消息。** 任务书和派工上下文必须带上主会话 ID、所属宿主及消息地址；缺失时禁止启动，禁止用 top-level、任务 ID 或子会话自己的 ID 替代。Paseo 取发起者 `PASEO_AGENT_ID`；其他宿主由 `roll` 自动识别当前会话 ID（或携带协调者收件箱地址和真实 preamble）。需要澄清、决策或补充信息时，必须用真实 agent 消息通道向主会话发送问题；Paseo 用 `send_agent_prompt` 发给实际 parent agent ID，并设 `background: true, notifyOnFinish: false`，发出后结束本轮，不同步等主会话。主会话向自己派出的 worker 追加任务、放行或续用时，`send_agent_prompt` 必须设 `notifyOnFinish: true`（或省略以保持默认）；完成通知与最终回复是无消息工具 worker（如 agy）的结果通道。禁止用 `AskUserQuestion`、`ask_user_question`、`request_user_input`、`request_user_input_async` 等不能通知主会话的交互提问工具，也不能只写本地日志假装已询问。无消息通道、无可用主会话地址或发送失败时，必须通过已有结果通道返回 `BLOCKED / needs_input`、具体问题与通信障碍并结束本轮，由调用方收取/转达；禁止静默挂起或把未回复当授权。`roll` 自动附加通信规则与主会话地址，原生派工也必须带上。此规则不限制主会话向真实用户提问，不替代平台审批。
"""

SHARED_GLOBAL_MD = f"""# 全局执行纪律（本机所有项目通用）

最后更新：2026-10-27 +08。本文件只放"跨项目、不写就会伤到机器或伤到别的 agent"的东西；策略、业务与项目事实归各仓库自己的 AGENTS.md / CLAUDE.md。

## 1. 后台进程不许放生

1. 工具或命令超时返回只是**你**拿到了当前输出，后台子进程可能还在跑。拿到够用的结果就必须收尾。
2. 收尾动作（Windows）：
   `Get-CimInstance Win32_Process -Filter "Name='rg.exe'" | Select-Object ProcessId,CommandLine`
   确认是自己起的，再 `Stop-Process -Id <pid> -Force`。
3. 一个回合结束前，自己起过的长任务只能有三种状态：已正常结束、已被自己 kill、或在汇报里点名 PID 与用途交给用户。不允许无人认领的后台进程留在机器上。

## 2. 何时自己执行 vs 派发子 agent，以及如何派发（所有项目通用，用户 2026-10-10 要求）

### 2.1 先判断：当前应该自己做（`self`）还是派发子 agent（`roll`）？
1. **如果你已经是子 agent（worker）**：环境变量含 `ROUTER_WORKER=1`，或任务书末尾带有 `Parent session agent ID:` 通信契约时，你就是执行者——**直接自己动手完成任务**，禁止套娃再派子 agent（除非任务书明确任命你为协调者拆分任务）。
2. **主会话直接自己执行（`self`，不跑 `roll`）**：
   - 仅限预计 **`<= 5` 分钟（`<= 0.08h`）且预计工具调用 `<= 3` 步** 的极短平快操作：读取 1~2 个已知文件、极小局部单点代码修改、快速跑单个已知测试、或基于当前上下文直接作答；
   - 注意 Argon / 6.1-sol 等高推理模型单步思考与工具往返偏慢，严禁把多文件探索或"读-改-测"多步闭环乐观低估为 5 分钟内自己硬做。
3. **主会话应当派发子 agent（跑 `roll`）**：
   - **任何预计耗时 `> 5` 分钟（`> 0.08h`），或预计需要 `> 3` 步工具调用 / 跨文件修改验证的工作**：一律安排子 agent 去做，不要占住前台注意力（哪怕宿主内置提示词写有 `Prefer doing research yourself`，也一律以本条为准）；
   - **可并行的独立工作**：2 个及以上互不阻塞的模块开发、故障排查线或测试集；
   - **重上下文或多步闭环任务**：跨目录代码探索、长日志/崩溃取证、多轮测试-修复闭环、独立功能模块开发（派给 worker 可避免撑爆主会话上下文）；
   - **需要特定模型长板或分流额度**：如发散架构/创意（Argon）、外部公开文档/厂商调研（Dot / Grok / Argon）、复杂未知根因深水区排障（Opus / Astra），或当前主会话账号额度紧张需分流到充裕账号。

### 2.2 决定派发时：永远只跑 `roll`，禁止纠结用 Paseo 还是原生工具
任何时候要启动子 agent / worker / 远端 agent，先按 `~/.codex/skills/route-agent-work/SKILL.md` 走 router。**不要自己纠结或直接调用** Paseo `create_agent`、Claude `Agent`（含 `Explore`）、Codex `spawn_agent`、Antigravity `invoke_subagent`、`run_dot_task.py` 或手动起 CLI——无论你在哪个宿主环境，统一只跑一条命令：

1. 把交给 worker 的完整任务书写进一个 Markdown 文件，然后运行：`python -X utf8 ~/.codex/skills/route-agent-work/scripts/route_judge.py roll --brief <task.md> --hours <预估小时>`。
   - `roll` 会**自动识别你所在的宿主环境与会话 ID**，并自动完成分类、选 model/effort、记账、以及自动选择最佳派发通道（Paseo 派发 / Dot 远端派发 / 本地 CLI 直接派发 / 原生宿主交接）。
   - `--hours` 为必传正数（预估活跃工时，如短任务 `0.5`、长线研究 `4`）；只有确有本机依赖时才加 `--local-only "<具体依赖>"`；希望重置前冲刺或赶工提速时可加 `--fast`；Dot 需要的文件用 `--file`。不要凭记忆自选 model/effort，也不要手动填 `work_kind`、`difficulty`、`profile`。
   - **根据 `roll` 返回的 `launched`、`dispatch_mode` 与 `caller_action` 执行下一步**：
     - **`launched: true`（`caller_action: "already_launched_do_not_respawn"`）**：代表 `roll` **已经把子 agent 启动好了**（`dispatch_mode: "paseo"` 为 Paseo worker、`"cli"` 为后台 CLI 进程、`"dot"` 为 Dot 任务）。**严禁**再去调用 `create_agent`、`Agent`、`spawn_agent` 或 `invoke_subagent` 重复启动！只需等待其消息回调，或按 `continuation_argv` / `worker.output` 查看结果。
     - **`launched: false` 且 `dispatch_mode: "self"`（`caller_action: "execute_self"`）**：router 判定无需子 agent，直接在当前会话自己完成。
     - **`launched: false` 且 `dispatch_mode: "native"`（`caller_action: "launch_with_native_host_tool"`）**：仅在无 Paseo daemon 且无对应 CLI（或显式指定原生交接）时出现；此时才按 `next` 指示，用当前宿主的原生子代理工具按 `route.bundle` 并传入 `worker_prompt_file` 全文启动。
2. 不许自己加码 effort。xhigh / max 只在 router 返回时才用；router 没给，就用 router 给的。
3. 用户在本次请求里点名了 model 或 effort，才可以不按 router 选：在 `roll` 后追加 `--provider <p> --account <a> --model <m> --effort <e> --basis "<用户原话>"`。
4. 某个 owner 不可用（订阅到期、账号被移除、额度耗尽）时，重新跑 router，不要自己换成"看起来相近"的 model。
5. 续用已有 agent（SendMessage / 追加 prompt）仅在 worker **历史累计未压缩上下文** `< 2M` tokens（`compacted_tokens + context_tokens`，按历次压缩前历史上下文总和 + 当前窗口计算，绝不能用压缩后的当前上下文窗口算）时免重选；一旦历史累计上下文 `>= 2M` tokens，必须把进展与剩余工作写成交接任务书、用 `route_judge.py roll --brief <handoff.md> --hours <预估小时>` 换新 worker，禁止把重度压缩的旧 worker 一直续用到失忆。换 owner、换 model、换 effort 同样要重选。
6. 门禁与巡检：各宿主已挂载 `PreToolUse` / `tool_call` 门禁（`{ROUTER_HOOK_SCRIPT}`），15 分钟内无 `~/.route-manager/router-ledger.jsonl` 路由记录的子 agent 启动会被直接拦截；`router_audit.py` 也会每日核对所有子 agent 启动记录。（注：仅对启动**子 agent / worker / 顾问会话**走 router；业务脚本或评测脚本内部调用的无工具单次分类/结构化 JSON 接口，不属于子 agent 派工，**不要**往 `router-ledger.jsonl` 写 `route_judge.py record`。）
7. 启动子 agent 必须显式设置权限模式，不许省略旗标、不许用 provider 默认值。漏设的 claude worker 会静默卡在权限询问态（状态仍是 running，无心跳无产出）；codex 漏设会落到受限沙箱。取值：先用 router 返回的 `selected_bundle.modeId`，没有就按 provider：Paseo / CLI 的 codex 用 `--mode full-access`，claude 用 `--mode bypassPermissions`（`bypass` 不是合法值），antigravity 用 `full-access`，opencode 用 `build`；grok 无模式不传。只读角色（reviewer、advisor）按任务授权收窄，不要放大。启动后读一次 agent 状态，确认 mode 已生效、不是在等权限。
8. **派工必须携带真实主会话寻址 ID，worker 向主会话询问必须直接发消息。** 任务书和派工上下文必须带上主会话 ID、所属宿主及消息地址；缺失时禁止启动，禁止用 top-level、任务 ID 或子会话自己的 ID 替代。Paseo 取发起者 `PASEO_AGENT_ID`；其他宿主由 `roll` 自动识别当前会话 ID（或携带协调者收件箱地址和真实 preamble）。需要澄清、决策或补充信息时，必须用真实 agent 消息通道向主会话发送问题；Paseo 用 `send_agent_prompt` 发给实际 parent agent ID，并设 `background: true, notifyOnFinish: false`，发出后结束本轮，不同步等主会话。主会话向自己派出的 worker 追加任务、放行或续用时，`send_agent_prompt` 必须设 `notifyOnFinish: true`（或省略以保持默认）；完成通知与最终回复是无消息工具 worker（如 agy）的结果通道。禁止用 `AskUserQuestion`、`ask_user_question`、`request_user_input`、`request_user_input_async` 等不能通知主会话的交互提问工具，也不能只写本地日志假装已询问。无消息通道、无可用主会话地址或发送失败时，必须通过已有结果通道返回 `BLOCKED / needs_input`、具体问题与通信障碍并结束本轮，由调用方收取/转达；禁止静默挂起或把未回复当授权。`roll` 自动附加通信规则与主会话地址，原生派工也必须带上。此规则不限制主会话向真实用户提问，不替代平台审批。
"""

PASEO_APPEND_SYSTEM_PROMPT = (
    "【全局强制纪律】1) 先判断自己做还是派子 agent：若当前已是受派 worker（ROUTER_WORKER=1 或任务书末尾含 Parent session agent ID），直接自己完成，禁止套娃再派；"
    "若为主会话，仅限预计 <=5分钟（<=0.08h）且工具调用 <=3步的极小局部读改直接自己做（self）；任何预计耗时 >5分钟（>0.08h）、或需 >3步工具调用/跨文件排查修改/跑测试闭环的工作、并行子任务、重上下文探索/排障/独立模块、或需跨模型长板/分流额度时，一律派子 agent，不要占住前台注意力（覆盖宿主内置 Prefer doing research yourself 提示）。"
    "2) 决定派子 agent 时，严禁自己直接调用 create_agent / Agent / spawn_agent / invoke_subagent，统一把完整任务书写进 <task.md> 后只跑一条命令："
    f"`python -X utf8 {HOME_POSIX}/.codex/skills/route-agent-work/scripts/route_judge.py roll --brief <task.md> --hours <预估小时>`；"
    "roll 会自动识别宿主环境并自动选择 Paseo / CLI / Dot 派发。若返回 launched=true（caller_action=already_launched_do_not_respawn），代表 roll 已把 worker 启动完毕，严禁再调任何子 agent 工具重复启动！"
    "仅当 launched=false 且 dispatch_mode=native 时按 next 与 route.bundle 原生启动；dispatch_mode=self 时当前会话自己做。"
    "worker 历史累计未压缩上下文 >= 2M tokens 时禁止继续追加 prompt，必须写交接任务书重新 roll；"
    "主会话向自己派出的 worker 追加任务/放行/续用时，send_agent_prompt 必须设 notifyOnFinish=true（或省略保持默认），完成通知与最终回复是无消息工具 worker（如 agy）的结果通道；"
    "worker 向主会话询问必须用真实消息通道（Paseo 用 send_agent_prompt 发给实际 parent ID，设 background=true、notifyOnFinish=false，发出后结束本轮），禁止 request_user_input_async / AskUserQuestion / ask_user_question / request_user_input 等交互工具；无法发送时返回 BLOCKED / needs_input 并结束本轮。"
)

ROUTER_GATE_MATCHER = (
    ".*request_user_input_async|.*request_user_input|.*AskUserQuestion|.*ask_user_question|"
    "Agent|Task|dispatch_agent|spawn_agent|invoke_subagent|create_agent|mcp__paseo__create_agent|"
    "send_agent_prompt|mcp__paseo__send_agent_prompt|run_turn|mcp__paseo__run_turn|send_message|mcp__paseo__send_message|"
    "codemode|execute_code|mcp__paseo__execute_code|Bash|bash|PowerShell|powershell|exec|exec_command|run_command|shell"
)

PI_ROUTER_GATE_TS = """import { spawnSync } from "node:child_process";

const PYTHON_EXE = __PYTHON_EXE__;
const HOOK_SCRIPT = __HOOK_SCRIPT__;
const SUBAGENT_TOOLS = new Set([
  "request_user_input_async",
  "request_user_input",
  "AskUserQuestion",
  "ask_user_question",
  "Agent",
  "Task",
  "dispatch_agent",
  "spawn_agent",
  "invoke_subagent",
  "create_agent",
  "mcp__paseo__create_agent",
  "send_agent_prompt",
  "mcp__paseo__send_agent_prompt",
  "run_turn",
  "mcp__paseo__run_turn",
  "send_message",
  "mcp__paseo__send_message",
  "codemode",
  "execute_code",
  "mcp__paseo__execute_code",
  "Bash",
  "bash",
  "PowerShell",
  "powershell",
  "exec",
  "exec_command",
  "run_command",
  "shell",
]);

export default function (pi: any) {
  pi.on("tool_call", async (event: any) => {
    const toolName = String(event?.toolName || "");
    if (!SUBAGENT_TOOLS.has(toolName) && !/request_user_input|AskUserQuestion|ask_user_question/i.test(toolName)) {
      return undefined;
    }
    try {
      const payload = JSON.stringify({
        toolName,
        input: event?.input || {},
        cwd: process.cwd(),
      });
      const res = spawnSync(PYTHON_EXE, ["-X", "utf8", HOOK_SCRIPT], {
        input: payload,
        encoding: "utf-8",
        timeout: 8000,
        windowsHide: true,
      });
      if (res.status === 0 && res.stdout) {
        const parsed = JSON.parse(res.stdout.trim());
        if (parsed && parsed.block) {
          return { block: true, reason: parsed.reason || "Blocked by Router Gate" };
        }
      }
    } catch {
      // Fail-open if python is unexpectedly unavailable
    }
    return undefined;
  });
}
""".replace("__PYTHON_EXE__", json.dumps(PYTHON_EXE)).replace("__HOOK_SCRIPT__", json.dumps(ROUTER_HOOK_SCRIPT))


def backup_path(src: Path, rel_name: str) -> None:
    if not src.exists() and not src.is_symlink():
        return
    dst = BACKUP_ROOT / rel_name
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir() and not is_reparse_point(src):
        shutil.copytree(src, dst)
    elif src.is_file():
        shutil.copy2(src, dst)


def is_reparse_point(path: Path) -> bool:
    try:
        return bool(os.lstat(path).st_file_attributes & 0x400)
    except (OSError, AttributeError):
        return path.is_symlink()


def remove_dir_or_junction(path: Path) -> None:
    if not path.exists() and not is_reparse_point(path):
        return
    if is_reparse_point(path):
        subprocess.run(["cmd", "/c", "rmdir", str(path)], check=True, capture_output=True)
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def create_junction(link: Path, target: Path) -> None:
    if is_reparse_point(link) or link.exists():
        remove_dir_or_junction(link)
    link.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        encoding="mbcs",
        errors="replace",
    )
    if res.returncode != 0:
        raise RuntimeError(f"mklink /J failed for {link} -> {target}: {res.stderr or res.stdout}")


def _secondary_claude_dirs() -> list[Path]:
    found: dict[str, Path] = {}
    primary = (HOME / ".claude").resolve()
    for p in sorted(HOME.glob(".claude-*")):
        if p.is_dir() and p.resolve() != primary:
            found[str(p.resolve())] = p
    try:
        paseo_cfg = json.loads((HOME / ".paseo" / "config.json").read_text(encoding="utf-8-sig"))
        providers = ((paseo_cfg.get("agents") or {}).get("providers") or {})
        for name, row in providers.items():
            if not isinstance(row, dict) or not (name == "claude" or name.startswith("claude-") or row.get("extends") == "claude"):
                continue
            cfg_dir = (row.get("env") or {}).get("CLAUDE_CONFIG_DIR")
            if isinstance(cfg_dir, str) and cfg_dir.strip():
                p = Path(cfg_dir.strip()).expanduser()
                if p.is_dir() and p.resolve() != primary:
                    found[str(p.resolve())] = p
    except (OSError, ValueError):
        pass
    return list(found.values())


def _secondary_codex_dirs() -> list[Path]:
    found: dict[str, Path] = {}
    primary = (HOME / ".codex").resolve()
    for p in sorted(HOME.glob(".codex-*")):
        if not p.is_dir() or p.resolve() == primary:
            continue
        if p.name.startswith((".codex-chatgpt-web", ".codex-pro-consult")):
            continue
        found[str(p.resolve())] = p
    try:
        paseo_cfg = json.loads((HOME / ".paseo" / "config.json").read_text(encoding="utf-8-sig"))
        providers = ((paseo_cfg.get("agents") or {}).get("providers") or {})
        for name, row in providers.items():
            if not isinstance(row, dict) or not (name == "codex" or name.startswith("codex-") or row.get("extends") == "codex"):
                continue
            cfg_dir = (row.get("env") or {}).get("CODEX_HOME")
            if isinstance(cfg_dir, str) and cfg_dir.strip():
                p = Path(cfg_dir.strip()).expanduser()
                if p.is_dir() and p.resolve() != primary and not p.name.startswith((".codex-chatgpt-web", ".codex-pro-consult")):
                    found[str(p.resolve())] = p
    except (OSError, ValueError):
        pass
    return list(found.values())


def unify_global_docs() -> None:
    targets = [
        (HOME / ".codex" / "AGENTS.md", "codex-AGENTS.md", CODEX_AGENTS_MD, True),
        *[(d / "AGENTS.md", f"{d.name.lstrip('.')}-AGENTS.md", CODEX_AGENTS_MD, True) for d in _secondary_codex_dirs()],
        (HOME / ".claude" / "CLAUDE.md", "claude-CLAUDE.md", SHARED_GLOBAL_MD, True),
        *[(d / "CLAUDE.md", f"{d.name.lstrip('.')}-CLAUDE.md", SHARED_GLOBAL_MD, True) for d in _secondary_claude_dirs()],
        (HOME / ".pi" / "agent" / "AGENTS.md", "pi-agent-AGENTS.md", SHARED_GLOBAL_MD, True),
        (HOME / ".gemini" / "GEMINI.md", "gemini-GEMINI.md", SHARED_GLOBAL_MD, True),
        (HOME / ".gemini" / "config" / "GEMINI.md", "gemini-config-GEMINI.md", SHARED_GLOBAL_MD, True),
        (HOME / ".config" / "opencode" / "AGENTS.md", "opencode-AGENTS.md", SHARED_GLOBAL_MD, True),
    ]
    for path, backup_name, content, enabled in targets:
        if not enabled:
            continue
        backup_path(path, f"docs/{backup_name}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"[docs] Updated {path}")


def update_paseo_config() -> None:
    cfg_path = HOME / ".paseo" / "config.json"
    if not cfg_path.is_file():
        return
    backup_path(cfg_path, "configs/paseo-config.json")
    data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    daemon = data.setdefault("daemon", {})
    daemon["appendSystemPrompt"] = PASEO_APPEND_SYSTEM_PROMPT

    agy_env = data.get("agents", {}).get("providers", {}).get("antigravity", {}).get("env", {})
    raw_path = agy_env.get("PATH")
    if isinstance(raw_path, str) and raw_path:
        cleaned = []
        seen = set()
        for entry in raw_path.split(";"):
            item = entry.strip()
            if not item:
                continue
            low = item.lower().rstrip("\\/")
            if "codex-arg0" in low or "grok-subagent" in low:
                continue
            if low in seen:
                continue
            seen.add(low)
            cleaned.append(item)
        agy_env["PATH"] = ";".join(cleaned)

    cfg_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"[paseo] Updated {cfg_path} (appendSystemPrompt + deduplicated antigravity PATH)")


def deduplicate_and_link_skills() -> None:
    codex_skills = HOME / ".codex" / "skills"
    agents_skills = HOME / ".agents" / "skills"
    claude_skills = HOME / ".claude" / "skills"

    # 1. Replace duplicate/pointer skills in ~/.claude/skills with junctions to ~/.codex/skills
    to_codex = [
        "route-agent-work",
        "dot-subagent",
        "chatgpt-pro-consult",
        "gemini-web",
        "gate-design-reflection",
    ]
    for name in to_codex:
        src = codex_skills / name
        dst = claude_skills / name
        if src.is_dir():
            if dst.exists() and not is_reparse_point(dst):
                backup_path(dst, f"skills/claude-{name}")
            create_junction(dst, src)
            print(f"[skills] Linked {dst} -> {src}")

    # 2. Replace duplicate paseo* skills in ~/.claude/skills with junctions to ~/.agents/skills
    to_agents = [
        "paseo",
        "paseo-advisor",
        "paseo-committee",
        "paseo-handoff",
        "paseo-help",
        "paseo-plugin",
    ]
    for name in to_agents:
        src = agents_skills / name
        dst = claude_skills / name
        if src.is_dir():
            if dst.exists() and not is_reparse_point(dst):
                backup_path(dst, f"skills/claude-{name}")
            create_junction(dst, src)
            print(f"[skills] Linked {dst} -> {src}")

    # 3. Link <secondary-claude>/skills -> ~/.claude/skills for all discovered secondary Claude homes
    for alt_dir in _secondary_claude_dirs():
        alt_skills = alt_dir / "skills"
        create_junction(alt_skills, claude_skills)
        print(f"[skills] Linked {alt_skills} -> {claude_skills}")

    # 4. Populate ~/.pi/agent/skills with junctions to all shared skills
    pi_skills = HOME / ".pi" / "agent" / "skills"
    pi_skills.mkdir(parents=True, exist_ok=True)
    skill_sources: dict[str, Path] = {}
    for root in (codex_skills, agents_skills):
        if not root.is_dir():
            continue
        for item in sorted(root.iterdir()):
            if item.name.startswith(".") or not item.is_dir():
                continue
            if (item / "SKILL.md").is_file():
                skill_sources[item.name] = item
    claude_only_names: list[str] = []
    if claude_skills.is_dir():
        for item in sorted(claude_skills.iterdir()):
            if item.name.startswith(".") or not item.is_dir() or is_reparse_point(item):
                continue
            if (item / "SKILL.md").is_file() and item.name not in skill_sources:
                skill_sources[item.name] = item
                claude_only_names.append(item.name)
    for name, target in sorted(skill_sources.items()):
        create_junction(pi_skills / name, target)
    print(f"[skills] Populated {pi_skills} with {len(skill_sources)} skill junctions")

    # 5. Create ~/.gemini/config/skills.json for Antigravity
    gemini_skills_json = HOME / ".gemini" / "config" / "skills.json"
    backup_path(gemini_skills_json, "configs/gemini-skills.json")
    gemini_skills_cfg = {
        "entries": [
            {"path": (HOME / ".codex" / "skills").as_posix(), "exclude": ["\\..*"]},
            {"path": (HOME / ".agents" / "skills").as_posix(), "exclude": ["\\..*"]},
            {"path": (HOME / ".claude" / "skills").as_posix(), "exclude": ["\\..*"]},
        ]
    }
    gemini_skills_json.parent.mkdir(parents=True, exist_ok=True)
    gemini_skills_json.write_text(json.dumps(gemini_skills_cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[skills] Wrote {gemini_skills_json}")

    # 6. Materialize real top-level skill directories in ~/.gemini/skills for any skill in
    # ~/.codex/skills that is a Windows directory junction (ReparsePoint), because
    # Antigravity's Go os.ReadDir 1-level scanner skips ReparsePoint directories.
    # Inside each real skill directory, copy top-level files (SKILL.md) and link
    # subdirectories (scripts/, references/, etc.) via junctions so code stays live.
    gemini_skills_dir = HOME / ".gemini" / "skills"
    gemini_skills_dir.mkdir(parents=True, exist_ok=True)
    for name in to_codex:
        src = codex_skills / name
        if not src.is_dir() or not is_reparse_point(src):
            continue
        real_src = src.resolve()
        dst = gemini_skills_dir / name
        if is_reparse_point(dst) or dst.exists():
            remove_dir_or_junction(dst)
        dst.mkdir(parents=True, exist_ok=True)
        for child in sorted(real_src.iterdir()):
            if child.name.startswith(".") or child.name == "__pycache__":
                continue
            dst_child = dst / child.name
            if child.is_dir():
                create_junction(dst_child, child)
            elif child.is_file():
                shutil.copy2(child, dst_child)
        print(f"[skills] Materialized {dst} -> {real_src} (real dir + live subdir junctions for Antigravity)")


def clean_dead_files() -> None:
    for empty_dir in (
        HOME / ".codex" / "skills" / ".git",
        HOME / ".codex" / "skills" / ".agents",
        HOME / ".codex" / ".agents",
    ):
        if empty_dir.is_dir() and not any(empty_dir.iterdir()):
            empty_dir.rmdir()
            print(f"[clean] Removed empty directory {empty_dir}")

    codex_dir = HOME / ".codex"
    removed_tmp = 0
    for tmp_file in codex_dir.glob("..codex-global-state.json*.tmp-*"):
        if tmp_file.is_file():
            backup_path(tmp_file, f"codex-tmp/{tmp_file.name}")
            tmp_file.unlink()
            removed_tmp += 1
    if removed_tmp:
        print(f"[clean] Removed {removed_tmp} stale ..codex-global-state.json*.tmp-* files")


def ensure_hook_in_list(hook_entries: list, command_str: str, matcher: str | None = ROUTER_GATE_MATCHER) -> list:
    """Ensure a command hook entry with command_str exists in hook_entries (updating its matcher) without duplicating."""
    filtered = []
    for group in hook_entries:
        other_hooks = [
            h for h in group.get("hooks", [])
            if h.get("command") != command_str and "router_gate_hook.py" not in str(h.get("command", ""))
        ]
        if other_hooks:
            new_group = dict(group)
            new_group["hooks"] = other_hooks
            filtered.append(new_group)
    entry: dict = {
        "hooks": [
            {
                "type": "command",
                "command": command_str,
                "timeout": 10,
            }
        ]
    }
    if matcher is not None:
        entry["matcher"] = matcher
    # Put router gate first so blocked tool calls don't even proceed
    return [entry] + filtered


def configure_hooks() -> None:
    # 1. Claude (& Claude-2 if present) settings.json
    claude_settings_path = HOME / ".claude" / "settings.json"
    backup_path(claude_settings_path, "configs/claude-settings.json")
    claude_settings = (
        json.loads(claude_settings_path.read_text(encoding="utf-8-sig"))
        if claude_settings_path.is_file()
        else {}
    )
    hooks = claude_settings.setdefault("hooks", {})
    pre_tool = hooks.get("PreToolUse", [])
    hooks["PreToolUse"] = ensure_hook_in_list(pre_tool, ROUTER_HOOK_CMD, matcher=ROUTER_GATE_MATCHER)
    text = json.dumps(claude_settings, indent=2, ensure_ascii=False) + "\n"
    claude_settings_path.parent.mkdir(parents=True, exist_ok=True)
    claude_settings_path.write_text(text, encoding="utf-8")
    print(f"[hooks] Updated {claude_settings_path}")

    for alt_dir in _secondary_claude_dirs():
        alt_settings_path = alt_dir / "settings.json"
        backup_path(alt_settings_path, f"configs/{alt_dir.name.lstrip('.')}-settings.json")
        existing_alt = (
            json.loads(alt_settings_path.read_text(encoding="utf-8-sig"))
            if alt_settings_path.is_file()
            else {}
        )
        alt_settings = json.loads(text)
        alt_env = dict(alt_settings.get("env") or {})
        for k, v in (existing_alt.get("env") or {}).items():
            if v is not None and v != "":
                alt_env[k] = v
        alt_settings["env"] = alt_env
        alt_settings_path.write_text(json.dumps(alt_settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[hooks] Synced {alt_settings_path}")

    # 2. Codex (& any secondary Codex homes) hooks.json
    codex_hooks_path = HOME / ".codex" / "hooks.json"
    backup_path(codex_hooks_path, "configs/codex-hooks.json")
    codex_data = (
        json.loads(codex_hooks_path.read_text(encoding="utf-8-sig"))
        if codex_hooks_path.is_file()
        else {}
    )
    chooks = codex_data.setdefault("hooks", {})
    chooks["PreToolUse"] = ensure_hook_in_list(chooks.get("PreToolUse", []), ROUTER_HOOK_CMD, matcher=None)
    codex_text = json.dumps(codex_data, indent=2, ensure_ascii=False) + "\n"
    codex_hooks_path.parent.mkdir(parents=True, exist_ok=True)
    codex_hooks_path.write_text(codex_text, encoding="utf-8")
    print(f"[hooks] Updated {codex_hooks_path} (added router gate)")

    for alt_codex in _secondary_codex_dirs():
        alt_hooks_path = alt_codex / "hooks.json"
        backup_path(alt_hooks_path, f"configs/{alt_codex.name.lstrip('.')}-hooks.json")
        alt_hooks_path.write_text(codex_text, encoding="utf-8")
        print(f"[hooks] Synced {alt_hooks_path}")

    # 3. Antigravity ~/.gemini/config/hooks.json
    gemini_hooks_path = HOME / ".gemini" / "config" / "hooks.json"
    backup_path(gemini_hooks_path, "configs/gemini-hooks.json")
    gemini_data = json.loads(gemini_hooks_path.read_text(encoding="utf-8-sig")) if gemini_hooks_path.is_file() else {}
    # Place router-gate first in PreToolUse evaluation order
    new_gemini_data = {
        "router-gate": {
            "PreToolUse": [
                {
                    "matcher": ROUTER_GATE_MATCHER,
                    "hooks": [
                        {
                            "type": "command",
                            "command": ROUTER_HOOK_CMD,
                            "timeout": 10,
                        }
                    ],
                }
            ]
        }
    }
    for k, v in gemini_data.items():
        if k != "router-gate":
            new_gemini_data[k] = v
    gemini_hooks_path.parent.mkdir(parents=True, exist_ok=True)
    gemini_hooks_path.write_text(json.dumps(new_gemini_data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[hooks] Updated {gemini_hooks_path}")

    # 4. Pi ~/.pi/agent/extensions/router-gate.ts
    pi_ext_path = HOME / ".pi" / "agent" / "extensions" / "router-gate.ts"
    backup_path(pi_ext_path, "configs/pi-router-gate.ts")
    pi_ext_path.parent.mkdir(parents=True, exist_ok=True)
    pi_ext_path.write_text(PI_ROUTER_GATE_TS, encoding="utf-8")
    print(f"[hooks] Wrote {pi_ext_path}")


def ensure_paseo_bridge_dependencies() -> None:
    """Prepare the optional Paseo dispatcher without requiring Node for other hosts."""
    if not (HOME / ".paseo" / "config.json").is_file() and not shutil.which("paseo"):
        return
    bridge = Path(__file__).resolve().parent / "paseo-bridge"
    if (bridge / "node_modules").is_dir():
        return
    hint = "Install Node.js with npm, then run npm --prefix setup/paseo-bridge ci from the repository root (only needed for Paseo worker dispatch via roll)."
    npm = shutil.which("npm")
    if not npm:
        print(f"[paseo-bridge] {hint} Continuing other host setup.")
        return
    print("[paseo-bridge] Installing optional Paseo worker dispatch dependencies...")
    subprocess.run([npm, "ci"], cwd=bridge, check=True)


def main() -> int:
    ensure_paseo_bridge_dependencies()
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    print(f"Backups directory: {BACKUP_ROOT}")
    unify_global_docs()
    update_paseo_config()
    deduplicate_and_link_skills()
    clean_dead_files()
    configure_hooks()
    print("All host globals, skills, and router gate hooks synchronized successfully.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
