"""Rule 6 (static AST taint/sink scanner) and Rule 7 (phrasing-invariance) tests for NO-HARDCODE classification."""
import argparse
import ast
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
TRAY_DIR = REPO_ROOT / 'tray'
for _p in (str(SCRIPT_DIR), str(TRAY_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import allocator
import dispatch_flow as flow
import dispatch_registry as registry
import roll
import route_judge as router
from router_test_support import isolated_router_state, usage_windows
import router_data


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


PRODUCTION_DIRS = (
    'skills/agent-compass/scripts',
    'skills/agent-compass/grok-bot',
    'tray',
    'agentcompass',
    'setup',
    'audit',
    'packaging',
)

NON_PRODUCTION_FILES = frozenset({
    'conftest.py',
    'router_test_support.py',
    'tk_test_support.py',
    'guest_check.py',
    'sandbox_check.py',
    'windows_sandbox_check.py',
    'foreign_machine_check.py',
    'zero_dispatch_check.py',
    'zero_fake_cli.py',
    'f10_evidence_probe.py',
    'f11_omp_probe.py',
    'f13_product_layout_probe.py',
    'verify_release.py',
    'check_payload.py',
    'check_router_contract.py',
})

BANNED_CLASSIFICATION_SYMBOLS = frozenset({
    # Category 6.1: Fast mode keyword list & helper
    'FAST_REQUEST_WORDS',
    'infer_fast_preference',
    # Category 6.2: Divergent keyword list & helper
    'DIVERGENT_WORDS',
    'infer_divergence',
    # Category 6.3: Multi-task heading regex & helper
    'MULTI_TASK_MARK_RE',
    'multi_task_marks',
    # Category 6.4: Offline heuristic profile & route/GUI/local regexes
    'GUI_WORDS',
    'COMPUTER_USE_PHRASES',
    'CODE_FILE_RE',
    'OBJECTIVE_LOCAL_RUNTIME_RE',
    'LOCAL_MACHINE_RE',
    'infer_kind_from_objective',
    'infer_task_profile',
    # Category 6.5: Read-only review keyword phrases & helper
    'READONLY_REVIEW_PHRASES',
    'is_readonly_review_task',
    # Category 6.6: Bogus local_only reason regexes & validator
    'BOGUS_LOCAL_ONLY_RE',
    'LOCAL_UI_OR_HOST_ROUTES',
    'valid_local_only_reason',
    # Category 6.7: Roll title regexes, review-title classifier & token overlap matcher (B1)
    '_REVIEW_RE',
    '_AUTHORING_RE',
    'is_review_title',
    '_MATCH_STOP_WORDS',
    '_match_tokens',
    # Audit reason regex (m8)
    'FIXED_WORDS',
    # Realtime loop legacy symbols
    'infer_realtime_loop',
    'infer_realtime_gui',
    'realtime_loop_needs_gui',
    'REALTIME_LOOP_CONTINUOUS_WORDS',
    'REALTIME_LOOP_TARGET_WORDS',
    'REALTIME_LOOP_PRESSURE_WORDS',
})

BANNED_ROUTE_CONFIG_KEYS = frozenset({
    'keywords',
    'keyword',
    'phrases',
    'patterns',
    'trigger_words',
    'stop_words',
})

BASE_TASK_TEXT_KEYS = frozenset({
    'objective',
    'context',
    'environment',
    'titles',
    'local_only_reason',
    'brief_path',
    'brief_paths',
    'keep_whole',
})

ROUTING_MODULES = frozenset({
    'allocator.py',
    'route_judge.py',
    'roll.py',
    'dispatch_registry.py',
    'router_data.py',
})

SINK_STRING_METHODS = frozenset({
    'lower',
    'upper',
    'casefold',
    'find',
    'rfind',
    'index',
    'rindex',
    'startswith',
    'endswith',
    'count',
    'split',
    'rsplit',
    'splitlines',
    'partition',
    'rpartition',
    'replace',
    'strip',
    'lstrip',
    'rstrip',
})

SINK_REGEX_METHODS = frozenset({
    'search',
    'match',
    'fullmatch',
    'findall',
    'finditer',
    'sub',
    'subn',
    'split',
})

# Exact per-statement allowlist: (relative_path, function_name, exact_source_line).
# Whole functions are NEVER allowlisted. Every entry here is verified against the live AST in
# `test_allowlist_entries_all_match_live_ast_statements`.
ALLOWED_TAINT_SINK_STATEMENTS = {
    (
        'skills/agent-compass/scripts/allocator.py',
        'validate_profile',
        'if isinstance(reason, str) and reason.strip():',
    ): 'checks non-empty optional classifier reason string before capping length',
    (
        'skills/agent-compass/scripts/allocator.py',
        'validate_profile',
        "profile['reason'] = reason.strip()[:300]",
    ): 'trims and caps optional classifier reason string to 300 chars',
    (
        'skills/agent-compass/scripts/dispatch_registry.py',
        'launch_request',
        "if not isinstance(launch.get('title'), str) or not 0 < len(launch['title']) <= 60:",
    ): 'enforces Paseo launch title length bound (1..60 chars)',
    (
        'skills/agent-compass/scripts/roll.py',
        'title_of',
        'for line in text.splitlines():',
    ): 'splits brief lines to extract first Markdown heading as display title',
    (
        'skills/agent-compass/scripts/roll.py',
        'title_of',
        "line = line.strip().lstrip('#').strip()",
    ): 'strips Markdown heading hashes from worker display title',
    (
        'skills/agent-compass/scripts/roll.py',
        'match_reusable_worker',
        "if (task_id and task_id in (w.get('task_ids') or ())) or (brief_path and brief_path in (w.get('brief_paths') or ())):",
    ): 'exact brief_path / task_id equality check for worker reuse (B1)',
    (
        'skills/agent-compass/scripts/roll.py',
        'strip_injected_contract',
        "cleaned = _INJECTED_FOOTER_RE.sub('', text)",
    ): 'strips previously injected Parent session agent ID footer to prevent duplicate injection',
    (
        'skills/agent-compass/scripts/roll.py',
        '_run',
        'if not text.strip():',
    ): 'rejects empty brief/task input before routing',
    (
        'skills/agent-compass/scripts/route_judge.py',
        'scorer_test',
        "task = {'objective': (objective or '').strip() or SCORER_TEST_OBJECTIVE}",
    ): 'falls back to default self-test sample objective when --objective is blank',
    (
        'skills/agent-compass/scripts/route_judge.py',
        'plan_stage',
        "or (not args.dry_run and bool((task.get('objective') or '').strip()) and not isinstance(task.get('profile'), dict)",
    ): 'checks whether task has non-empty objective to invoke System-One classifier',
    (
        'tray/decision_theatre.py',
        'scene_from_result',
        'title = next((s.strip().lstrip("# ") for s in title.splitlines() if s.strip()), "")',
    ): 'formats task title for Decision Theatre canvas animation',
    (
        'tray/router_data.py',
        'ledger_summary',
        'objective = " ".join(str(row.get("objective") or row.get("task_id") or "").split())',
    ): 'collapses whitespace in ledger objective for tray table display',
    (
        'tray/router_data.py',
        'ledger_summary',
        'if objective.startswith("#"):',
    ): 'checks leading Markdown hash in ledger summary for tray table display',
    (
        'tray/router_data.py',
        'ledger_summary',
        'objective = objective.lstrip("# ")',
    ): 'strips leading Markdown hash in ledger summary for tray table display',
    (
        'audit/router_audit.py',
        'render',
        'f"{r.get(\'task_count\') or \'-\'} | {\' \'.join(str(text).split())[:120].replace(\'|\', \'/\')} |")',
    ): 'escapes Markdown table pipes in daily audit report rendering',
}

TASK_TEXT_CORPUS = (
    'Complete the assigned unit of work.',
    'hurry up 赶紧 请尽快完成单模块接口修复，比较急，加急，开启fast，use fast mode immediately asap pre-reset sprint!',
    'Do NOT use fast mode; no fast mode; not urgent; 不用开启fast，不着急，慢慢做。',
    'Brainstorm creative novel scheme and roadmap; 制定方案、设计方案、新方案、发散、架构创新、提出假设。',
    '### 交付物 1\nFirst part\n### 交付物 2\nSecond part\n### Deliverable 3\nThird part\n#### Task 4\nFourth part',
    'Click the Save button with the mouse and keyboard in editor.py on localhost 127.0.0.1 pytest sqlite daemon worktree C:\\Users\\tanwei\\repo',
    '用鼠标点击桌面设置里的保存并截图，检查截图对比视觉差异，搜索官网公开文档完成厂商调研并翻译文档。',
    'Independent security & invariant review (read-only) of commit a81f3c2; do not modify repo; 只读代码审查、独立审查、不修改仓库代码。',
    'Fix the review findings and address audit comments in dispatch_flow.py; 任务：修复"代码包增量更新"审查发现的问题。',
    '实现实时刷新页面、修复页面轮询逻辑、设计盯盘看板前端、排查客户端循环等待、编写抢票脚本与挂机工具。',
    'short',
)


def iter_production_python_files(root: Path = REPO_ROOT) -> list[Path]:
    files = []
    for rel_dir in PRODUCTION_DIRS:
        folder = root / rel_dir
        if not folder.exists():
            continue
        for path in sorted(folder.rglob('*.py')):
            if path.name.startswith('test_') or path.name in NON_PRODUCTION_FILES:
                continue
            files.append(path)
    return files


def _eval_static_str(node: ast.AST | None, const_map: dict) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in const_map:
        val = const_map[node.id]
        return val if isinstance(val, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _eval_static_str(node.left, const_map)
        right = _eval_static_str(node.right, const_map)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'join':
        sep = _eval_static_str(node.func.value, const_map)
        if sep is not None and len(node.args) == 1 and isinstance(node.args[0], (ast.List, ast.Tuple)):
            parts = [_eval_static_str(elt, const_map) for elt in node.args[0].elts]
            if all(p is not None for p in parts):
                return sep.join(parts)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in const_map:
        mapping = const_map[node.value.id]
        key = _eval_static_str(node.slice, const_map)
        if isinstance(mapping, dict) and key in mapping:
            return mapping[key]
    return None


def scan_python_source_for_violations(rel: str, src: str) -> tuple[list[str], set[tuple[str, str, str]]]:
    """Scan a single Python source file for banned symbols, dynamic symbol tricks, and task-text sink hits.
    Returns (violation_messages, matched_allowlist_keys)."""
    violations = []
    matched_allowlist = set()
    path_name = Path(rel).name
    is_routing_file = 'skills/agent-compass/scripts' in rel or path_name in ROUTING_MODULES
    text_keys = (BASE_TASK_TEXT_KEYS | {'reason', 'title'}) if is_routing_file else BASE_TASK_TEXT_KEYS

    tree = ast.parse(src, filename=rel)
    lines = src.splitlines()

    # 1. Check banned symbols, dynamic globals()/vars()/locals() tricks, and banned route config lookups
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in BANNED_CLASSIFICATION_SYMBOLS:
                violations.append(f'{rel}:{node.lineno}: banned definition {node.name}')
        elif isinstance(node, ast.Name) and node.id in BANNED_CLASSIFICATION_SYMBOLS:
            violations.append(f'{rel}:{node.lineno}: banned symbol reference {node.id}')
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_CLASSIFICATION_SYMBOLS:
            violations.append(f'{rel}:{node.lineno}: banned attribute {node.attr}')
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in BANNED_CLASSIFICATION_SYMBOLS:
            violations.append(f'{rel}:{node.lineno}: banned symbol string literal {node.value!r}')
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in ('globals', 'locals', 'eval', 'exec') and is_routing_file:
                violations.append(f'{rel}:{node.lineno}: dynamic symbol reflection {node.func.id}() is forbidden in routing modules')
        if is_routing_file and isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get':
            if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in BANNED_ROUTE_CONFIG_KEYS:
                violations.append(f'{rel}:{node.lineno}: route word-list lookup .get({node.args[0].value!r}) is forbidden')
        if is_routing_file and isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if node.slice.value in BANNED_ROUTE_CONFIG_KEYS:
                violations.append(f'{rel}:{node.lineno}: route word-list subscript [{node.slice.value!r}] is forbidden')

    # 2. Build module-level constant string & dict map
    const_map = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            s_val = _eval_static_str(node.value, const_map)
            if s_val is not None:
                const_map[name] = s_val
                if s_val in BANNED_CLASSIFICATION_SYMBOLS:
                    violations.append(f'{rel}:{node.lineno}: constant {name} constructs banned symbol {s_val!r}')
            elif isinstance(node.value, ast.Dict):
                d_val = {}
                for k_node, v_node in zip(node.value.keys, node.value.values):
                    ks = _eval_static_str(k_node, const_map)
                    vs = _eval_static_str(v_node, const_map)
                    if ks is not None and vs is not None:
                        d_val[ks] = vs
                if d_val:
                    const_map[name] = d_val

    # 3. Collect all scopes: every FunctionDef / AsyncFunctionDef (including class methods & nested functions)
    scopes: list[tuple[str, ast.AST, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            param_names = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
            scopes.append((node.name, node, param_names))

    tainted_params: dict[str, set[str]] = {name: set() for name, _, _ in scopes}
    scope_const_maps: dict[str, dict] = {name: dict(const_map) for name, _, _ in scopes}
    if path_name == 'roll.py':
        for fn in ('title_of', 'build_task', 'prepare_worker_prompt', 'paseo', '_run', 'run',
                   'match_reusable_worker', 'strip_injected_contract'):
            if fn in tainted_params:
                tainted_params[fn].add('text')

    def is_tainted(expr: ast.AST | None, tainted: set[str], tainted_dict_keys: set[tuple[str, str]], cmap: dict) -> bool:
        if expr is None or isinstance(expr, ast.Dict):
            return False
        if isinstance(expr, ast.Name):
            return expr.id in tainted
        if isinstance(expr, ast.Attribute):
            if expr.attr in text_keys and isinstance(expr.value, ast.Name) and expr.value.id in ('args', 'task', 'row', 'record', 'r'):
                return True
            if expr.attr in ('values', 'items') and isinstance(expr.value, ast.Name) and expr.value.id in ('task', 'raw', 'profile', 'needs', 'worker', 'w'):
                return True
            return is_tainted(expr.value, tainted, tainted_dict_keys, cmap)
        if isinstance(expr, ast.Subscript):
            if isinstance(expr.slice, ast.Slice):
                return is_tainted(expr.value, tainted, tainted_dict_keys, cmap)
            k = _eval_static_str(expr.slice, cmap)
            if k in text_keys:
                if k == 'reason' and isinstance(expr.value, ast.Name) and expr.value.id in (
                    'decision', 'result', 'row', 'r', 's', 'd', 'event', 'alert', 'a', 'gap'
                ):
                    return False
                return True
            if isinstance(expr.value, ast.Name) and k and (expr.value.id, k) in tainted_dict_keys:
                return True
            return False
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Attribute):
                attr = expr.func.attr
                if attr == 'get' and expr.args:
                    k = _eval_static_str(expr.args[0], cmap)
                    if k in text_keys:
                        if k == 'reason' and isinstance(expr.func.value, ast.Name) and expr.func.value.id in (
                            'decision', 'result', 'row', 'r', 's', 'd', 'event', 'alert', 'a', 'gap'
                        ):
                            return False
                        return True
                    if len(expr.args) > 1 and is_tainted(expr.args[1], tainted, tainted_dict_keys, cmap):
                        return True
                    return False
                if attr in ('values', 'items') and isinstance(expr.func.value, ast.Name) and expr.func.value.id in (
                    'task', 'raw', 'profile', 'needs', 'worker', 'w'
                ):
                    return True
                if is_tainted(expr.func.value, tainted, tainted_dict_keys, cmap):
                    return True
                if attr == 'join' and any(is_tainted(a, tainted, tainted_dict_keys, cmap) for a in expr.args):
                    return True
                return False
            if isinstance(expr.func, ast.Name):
                fname = expr.func.id
                if path_name == 'roll.py' and fname in ('title_of', 'read_brief', 'strip_injected_contract'):
                    return True
                if fname in ('str', 'repr', 'next', 'ord', 'chr') and any(
                    is_tainted(a, tainted, tainted_dict_keys, cmap) for a in expr.args
                ):
                    return True
            if isinstance(expr.func, ast.Call) and isinstance(expr.func.func, ast.Name) and expr.func.func.id == 'getattr':
                if any(is_tainted(a, tainted, tainted_dict_keys, cmap) for a in expr.args):
                    return True
            return False
        if isinstance(expr, (ast.BoolOp, ast.JoinedStr, ast.List, ast.Tuple, ast.Set)):
            children = expr.values if hasattr(expr, 'values') else expr.elts
            return any(is_tainted(c, tainted, tainted_dict_keys, cmap) for c in children)
        if isinstance(expr, ast.FormattedValue):
            return is_tainted(expr.value, tainted, tainted_dict_keys, cmap)
        if isinstance(expr, ast.BinOp):
            return is_tainted(expr.left, tainted, tainted_dict_keys, cmap) or is_tainted(expr.right, tainted, tainted_dict_keys, cmap)
        if isinstance(expr, ast.UnaryOp):
            return is_tainted(expr.operand, tainted, tainted_dict_keys, cmap)
        if isinstance(expr, ast.IfExp):
            return is_tainted(expr.body, tainted, tainted_dict_keys, cmap) or is_tainted(expr.orelse, tainted, tainted_dict_keys, cmap)
        if isinstance(expr, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            return is_tainted(expr.elt, tainted, tainted_dict_keys, cmap) or any(
                is_tainted(g.iter, tainted, tainted_dict_keys, cmap) for g in expr.generators
            )
        return False

    def compute_scope_taint(scope_node: ast.AST, seed_vars: set[str], cmap: dict) -> tuple[set[str], set[tuple[str, str]]]:
        tainted = set(seed_vars)
        tainted_dict_keys: set[tuple[str, str]] = set()
        changed = True
        while changed:
            changed = False
            for sub in ast.walk(scope_node):
                if isinstance(sub, ast.Assign):
                    if isinstance(sub.value, ast.Dict):
                        for t in sub.targets:
                            if isinstance(t, ast.Name):
                                for k_node, v_node in zip(sub.value.keys, sub.value.values):
                                    ks = _eval_static_str(k_node, cmap) if k_node else None
                                    if ks and is_tainted(v_node, tainted, tainted_dict_keys, cmap):
                                        if (t.id, ks) not in tainted_dict_keys:
                                            tainted_dict_keys.add((t.id, ks))
                                            changed = True
                    elif is_tainted(sub.value, tainted, tainted_dict_keys, cmap):
                        for t in sub.targets:
                            if isinstance(t, ast.Name):
                                if t.id not in tainted:
                                    tainted.add(t.id)
                                    changed = True
                            elif isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name):
                                k = _eval_static_str(t.slice, cmap)
                                if k and (t.value.id, k) not in tainted_dict_keys:
                                    tainted_dict_keys.add((t.value.id, k))
                                    changed = True
                elif isinstance(sub, ast.NamedExpr):
                    if is_tainted(sub.value, tainted, tainted_dict_keys, cmap) and isinstance(sub.target, ast.Name):
                        if sub.target.id not in tainted:
                            tainted.add(sub.target.id)
                            changed = True
                elif isinstance(sub, (ast.For, ast.comprehension)):
                    if is_tainted(sub.iter, tainted, tainted_dict_keys, cmap) and isinstance(sub.target, ast.Name):
                        if sub.target.id not in tainted:
                            tainted.add(sub.target.id)
                            changed = True
        return tainted, tainted_dict_keys

    # Inter-procedural propagation of tainted arguments & constant field-name arguments across functions in the module
    scope_by_name = {name: (node, params) for name, node, params in scopes}
    for _ in range(4):
        for scope_name, scope_node, _ in scopes:
            cmap = scope_const_maps[scope_name]
            t_vars, t_dkeys = compute_scope_taint(scope_node, tainted_params[scope_name], cmap)
            for sub in ast.walk(scope_node):
                if isinstance(sub, ast.Call):
                    callee = (
                        sub.func.id if isinstance(sub.func, ast.Name)
                        else sub.func.attr if isinstance(sub.func, ast.Attribute)
                        else None
                    )
                    if callee in scope_by_name:
                        _, target_params = scope_by_name[callee]
                        for idx, arg in enumerate(sub.args):
                            if idx < len(target_params):
                                pname = target_params[idx]
                                if is_tainted(arg, t_vars, t_dkeys, cmap):
                                    tainted_params[callee].add(pname)
                                s_val = _eval_static_str(arg, cmap)
                                if s_val is not None:
                                    scope_const_maps[callee][pname] = s_val

    # 4. Check sinks in every scope
    for scope_name, scope_node, _ in scopes:
        if scope_name == '<module>':
            continue
        cmap = scope_const_maps[scope_name]
        t_vars, t_dkeys = compute_scope_taint(scope_node, tainted_params[scope_name], cmap)
        for sub in ast.walk(scope_node):
            hit_kind = None
            if isinstance(sub, ast.Call):
                if isinstance(sub.func, ast.Attribute):
                    if sub.func.attr in SINK_STRING_METHODS and is_tainted(sub.func.value, t_vars, t_dkeys, cmap):
                        hit_kind = f'str.{sub.func.attr}'
                    elif sub.func.attr in SINK_REGEX_METHODS and any(is_tainted(a, t_vars, t_dkeys, cmap) for a in sub.args):
                        hit_kind = f'regex.{sub.func.attr}'
                elif isinstance(sub.func, ast.Name):
                    if sub.func.id == 'len' and sub.args and is_tainted(sub.args[0], t_vars, t_dkeys, cmap):
                        hit_kind = 'len()'
                    elif sub.func.id == 'getattr' and (
                        any(is_tainted(a, t_vars, t_dkeys, cmap) for a in sub.args)
                        or (sub.args and isinstance(sub.args[0], ast.Name) and sub.args[0].id == 'str')
                    ):
                        hit_kind = 'getattr()'
                elif isinstance(sub.func, ast.Call) and isinstance(sub.func.func, ast.Name) and sub.func.func.id == 'getattr':
                    if any(is_tainted(a, t_vars, t_dkeys, cmap) for a in sub.args):
                        hit_kind = 'getattr_call()'
            elif isinstance(sub, ast.Compare):
                if any(isinstance(op, (ast.In, ast.NotIn)) for op in sub.ops):
                    operands = [sub.left] + list(sub.comparators)
                    if any(is_tainted(o, t_vars, t_dkeys, cmap) for o in operands):
                        hit_kind = 'in/not_in'
            if hit_kind:
                stmt_line = lines[sub.lineno - 1].strip()
                allow_key = (rel, scope_name, stmt_line)
                if allow_key in ALLOWED_TAINT_SINK_STATEMENTS:
                    matched_allowlist.add(allow_key)
                else:
                    violations.append(
                        f'{rel}:{sub.lineno} in {scope_name}(): tainted task text reaches {hit_kind} sink: {stmt_line}'
                    )
    return violations, matched_allowlist


class StaticNoHardcodeScannerTests(unittest.TestCase):
    """Rule 6 & B2: AST taint/sink static scanner across all production Python & JSON files."""

    def test_no_banned_symbols_or_unauthorized_taint_sinks_in_production_files(self):
        all_violations = []
        all_matched_allowlist = set()
        for path in iter_production_python_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            src = path.read_text(encoding='utf-8-sig')
            v, matched = scan_python_source_for_violations(rel, src)
            all_violations.extend(v)
            all_matched_allowlist.update(matched)
        self.assertEqual(
            all_violations,
            [],
            'Hardcoded task-text classification or banned symbols found:\n' + '\n'.join(all_violations),
        )
        # m7: Every allowlisted statement must match a live AST sink hit; stale entries fail immediately.
        stale = set(ALLOWED_TAINT_SINK_STATEMENTS) - all_matched_allowlist
        self.assertEqual(
            stale,
            set(),
            f'Stale allowlist entries do not match any live AST statement: {sorted(stale)}',
        )

    def test_no_route_keywords_or_phrases_anywhere_in_reference_json_files(self):
        def walk_json(obj, path_prefix):
            found = []
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k in BANNED_ROUTE_CONFIG_KEYS:
                        found.append(f'{path_prefix}.{k}')
                    found.extend(walk_json(v, f'{path_prefix}.{k}'))
            elif isinstance(obj, list):
                for idx, item in enumerate(obj):
                    found.extend(walk_json(item, f'{path_prefix}[{idx}]'))
            return found

        violations = []
        for rel in (
            'skills/agent-compass/references/accounts.json',
            'skills/agent-compass/references/model-capabilities.json',
            'skills/agent-compass/references/reference-fleet.json',
            'skills/agent-compass/references/public-data.json',
        ):
            path = REPO_ROOT / rel
            if not path.exists():
                continue
            doc = json.loads(path.read_text(encoding='utf-8-sig'))
            violations.extend(walk_json(doc, rel))
        self.assertEqual(violations, [], 'Banned keyword/phrase config keys found in JSON:\n' + '\n'.join(violations))

    def test_all_17_review_mutants_and_controls_are_caught_by_static_scanner(self):
        a_rel = 'skills/agent-compass/scripts/allocator.py'
        j_rel = 'skills/agent-compass/scripts/route_judge.py'
        r_rel = 'skills/agent-compass/scripts/roll.py'
        s_rel = 'setup/router_gate_hook.py'
        a_src = (REPO_ROOT / a_rel).read_text(encoding='utf-8-sig').replace('\r\n', '\n')
        j_src = (REPO_ROOT / j_rel).read_text(encoding='utf-8-sig').replace('\r\n', '\n')
        r_src = (REPO_ROOT / r_rel).read_text(encoding='utf-8-sig').replace('\r\n', '\n')
        s_src = (REPO_ROOT / s_rel).read_text(encoding='utf-8-sig').replace('\r\n', '\n')

        anchor_mod = "GENERIC_CALLER_KINDS = (None, 'general_implementation', 'tool_execution', 'debug_review', 'other_or_mixed')\n"
        anchor_fast = "    if _flag_present(needs, 'read_only_review'):\n"

        def patch_src(base, old, new):
            self.assertIn(old, base, f'Anchor missing: {old!r}')
            return base.replace(old, new, 1)

        def mut_a(mod_code='', fast_expr=''):
            out = a_src
            if mod_code:
                out = patch_src(out, anchor_mod, anchor_mod + mod_code + '\n')
            if fast_expr:
                out = patch_src(out, anchor_fast, f'    wants_fast = wants_fast or {fast_expr}\n' + anchor_fast)
            return a_rel, out

        cases = {
            'C1_banned_symbol': mut_a("FAST_REQUEST_WORDS = ('asap',)", "any(w in (task.get('objective') or '') for w in FAST_REQUEST_WORDS)"),
            'C2_new_func_objective': mut_a("def _hurry(task):\n    return 'hurry up' in (task.get('objective') or '')", '_hurry(task)'),
            'C3_in_normalize_task': mut_a('', "'加急' in (task.get('objective') or '')"),
            'M1_renamed_word_list': mut_a("SPEED_HINTS = ('hurry up', '赶紧')", "any(w in (task.get('objective') or '') for w in SPEED_HINTS)"),
            'M2_getattr_str_find_concat_key': mut_a(
                "def _h(task, name):\n    return getattr(str, 'find')(str(task.get(name) or ''), 'hurry up') >= 0",
                "_h(task, 'obj' + 'ective')",
            ),
            'M3_regex_on_context': mut_a(
                "_HURRY_RX = re.compile(r'hurry\\s+up|赶紧')\ndef _h(task):\n    return bool(_HURRY_RX.search(task.get('context') or ''))",
                '_h(task)',
            ),
            'M4_module_const_key_find': mut_a(
                "_TEXT_KEY = 'objective'\ndef _h(task):\n    return (task.get(_TEXT_KEY) or '').find('hurry up') >= 0",
                '_h(task)',
            ),
            'M5_in_roll_build_task': (
                r_rel,
                patch_src(
                    r_src,
                    "    if getattr(args, 'fast', False):\n        task.setdefault('needs', {})['fast'] = True\n",
                    "    if getattr(args, 'fast', False) or 'hurry up' in text:\n        task.setdefault('needs', {})['fast'] = True\n",
                ),
            ),
            'M6_casefold_helper_from_plan_stage': (
                j_rel,
                patch_src(
                    patch_src(
                        j_src,
                        'def recent_route_lock(task, records, now, bundle_id, window_seconds):',
                        "def _pressing(text):\n    return 'hurry up' in (text or '').casefold()\n\n\ndef recent_route_lock(task, records, now, bundle_id, window_seconds):",
                    ),
                    "    if getattr(args, 'fast', False):\n        needs['fast'] = True\n",
                    "    if getattr(args, 'fast', False) or _pressing(task.get('objective')):\n        needs['fast'] = True\n",
                ),
            ),
            'M7_dict_indirection_field_name': mut_a(
                "_FIELDS = {'t': 'objective'}\ndef _h(task):\n    return 'hurry up' in (task.get(_FIELDS['t']) or '')",
                '_h(task)',
            ),
            'M8_new_module_in_scripts': (
                'skills/agent-compass/scripts/task_hints.py',
                "def hurry(task):\n    return 'hurry up' in (task.get('objective') or '')\n",
            ),
            'M9_class_method': mut_a(
                "class _Hints:\n    @staticmethod\n    def fast(task):\n        return 'hurry up' in (task.get('objective') or '')",
                '_Hints.fast(task)',
            ),
            'M10_profile_reason_keyword': mut_a(
                "def _h(profile):\n    return 'hurry up' in ((profile or {}).get('reason') or '')",
                '_h(profile)',
            ),
            'M11_globals_banned_symbol': mut_a(
                "globals()['FAST_' + 'REQUEST_WORDS'] = ('hurry up',)",
                "any(w in (task.get('objective') or '') for w in globals()['FAST_' + 'REQUEST_WORDS'])",
            ),
            'M12_route_phrases_lookup': mut_a(
                '',
                "any(p in (task.get('objective') or '') for p in (capabilities['routes'].get('general_implementation') or {}).get('phrases', ('hurry up',)))",
            ),
            'M13_banned_symbol_in_setup': (
                s_rel,
                patch_src(s_src, 'UUID_RE = re.compile(', "FAST_REQUEST_WORDS = ('asap',)\nUUID_RE = re.compile("),
            ),
            'M14_title_regex_in_roll_paseo': (
                r_rel,
                patch_src(
                    r_src,
                    'def is_readonly_review_profile(profile):',
                    "_RV_RX = re.compile(r'review|audit|审查')\n\ndef _bad_title_check(args, text):\n    return bool(_RV_RX.search(args.title or title_of(text)))\n\ndef is_readonly_review_profile(profile):",
                ),
            ),
            'M15_local_only_reason_regex_in_plan_stage': (
                j_rel,
                patch_src(
                    patch_src(
                        j_src,
                        'def recent_route_lock(task, records, now, bundle_id, window_seconds):',
                        "_EDIT_ONLY_RX = re.compile(r'^\\s*patch(ing)? files?\\s*$', re.I)\n\n\ndef recent_route_lock(task, records, now, bundle_id, window_seconds):",
                    ),
                    '    kind = allocator.task_kind(task, capabilities, policy)\n    guard = capabilities',
                    "    if needs.get('local_only') and _EDIT_ONLY_RX.match(str(needs.get('local_only_reason') or '')):\n        needs['local_only'] = False\n        task = {**task, 'needs': needs}\n    kind = allocator.task_kind(task, capabilities, policy)\n    guard = capabilities",
                ),
            ),
            'M16_startswith_on_brief_path_in_match_reusable_worker': (
                r_rel,
                patch_src(
                    r_src,
                    "    brief_path = task.get('brief_path')\n",
                    "    brief_path = task.get('brief_path')\n    if (brief_path or '').lower().startswith('review'):\n        return None\n",
                ),
            ),
            'M17_length_heuristic_in_plan_stage': (
                j_rel,
                patch_src(
                    j_src,
                    "    if getattr(args, 'fast', False):\n        needs['fast'] = True\n",
                    "    if getattr(args, 'fast', False) or len(task.get('objective') or '') < 12:\n        needs['fast'] = True\n",
                ),
            ),
        }

        for name, (rel, mutated_src) in cases.items():
            with self.subTest(mutant=name):
                violations, _ = scan_python_source_for_violations(rel, mutated_src)
                self.assertTrue(violations, f'Mutant {name} escaped static scanner!')


class PhrasingInvarianceAndBlockedFallbackTests(unittest.TestCase):
    """Rule 7: Changing task text wording must never alter routing across allocator.plan, plan_stage, or roll.run."""

    def setUp(self):
        self.now = datetime(2026, 10, 9, 3, 0, tzinfo=timezone.utc)
        self.accounts = allocator.load_accounts()
        self.capabilities = allocator.load_capabilities(accounts=self.accounts)
        self.policy = self.accounts['scoring']
        self.usage = usage_windows(self.now, {
            'claude': [('five_hour', 90, 3), ('seven_day', 80, 100), ('weekly_fable', 100, 100)],
            'claude-lab': [('five_hour', 90, 3), ('seven_day', 80, 100), ('weekly_fable', 100, 100)],
            'codex': [('usage', 80, 100)],
            'antigravity': [('gemini-5h', 90, 3), ('gemini-weekly', 80, 100), ('3p-5h', 90, 3), ('3p-weekly', 80, 100)],
        })

    def make_profile(self, **overrides):
        base = {
            'work_kind': 'general_implementation',
            'reasoning_depth': 2,
            'code_complexity': 2,
            'context_scale': 1,
            'divergence': 1,
            'hallucination_risk': 2,
            'error_cost': 2,
            'needs_visual': False,
            'needs_gui': False,
            'local_only': True,
            'realtime_loop': False,
            'wants_fast': False,
            'read_only_review': False,
            'interaction_style': 'engineering',
            'expected_hours': 1.0,
            'hours_confidence': 'high',
            'urgency': 'unknown',
            'multi_task': False,
            'task_count': '1',
        }
        base.update(overrides)
        return allocator.validate_profile(base)

    def test_allocator_plan_is_invariant_across_task_text_corpus(self):
        profiles = [
            self.make_profile(work_kind='general_implementation', reasoning_depth=2, code_complexity=2),
            self.make_profile(work_kind='debug_review', reasoning_depth=2, code_complexity=0, hallucination_risk=3,
                              read_only_review=True, interaction_style='exploration'),
            self.make_profile(work_kind='debug_review', reasoning_depth=2, code_complexity=2, wants_fast=True),
            self.make_profile(work_kind='web_research', reasoning_depth=2, code_complexity=0, divergence=3,
                              local_only=False, interaction_style='exploration'),
            self.make_profile(work_kind='computer_use', reasoning_depth=1, code_complexity=0,
                              needs_visual=True, needs_gui=True, realtime_loop=True),
            self.make_profile(work_kind='direction_reasoning', reasoning_depth=4, code_complexity=0,
                              divergence=3, local_only=False, interaction_style='advisory'),
        ]
        for prof in profiles:
            baseline_norm = None
            baseline_choice = None
            baseline_scores = None
            for text in TASK_TEXT_CORPUS:
                task = {
                    'objective': text,
                    'context': text,
                    'environment': text,
                    'expected_hours': 1.0,
                    'profile': dict(prof, reason=text[:200]),
                }
                norm = allocator.normalize_task(task, self.policy, self.capabilities, self.accounts)
                plan = allocator.plan(task, self.usage, self.now, accounts=self.accounts, capabilities=self.capabilities)
                scores = [(r['id'], r['score'], r['excluded']) for r in plan['ranking']]
                norm_key = (
                    norm['kind'], norm['difficulty'], norm['divergence'], norm['wants_fast'],
                    norm['read_only_review'], norm['local_only'], norm['needs_gui'],
                    norm['needs_visual'], norm['realtime_loop'],
                )
                if baseline_norm is None:
                    baseline_norm = norm_key
                    baseline_choice = (plan['choice'], plan.get('selected_bundle'))
                    baseline_scores = scores
                else:
                    self.assertEqual(norm_key, baseline_norm, f'normalize_task changed for text={text!r}')
                    self.assertEqual((plan['choice'], plan.get('selected_bundle')), baseline_choice,
                                     f'allocator.plan choice changed for text={text!r}')
                    self.assertEqual(scores, baseline_scores, f'candidate ranking changed for text={text!r}')

    def test_plan_stage_is_invariant_across_task_text_corpus(self):
        prof = self.make_profile(work_kind='general_implementation', reasoning_depth=2, code_complexity=2, local_only=True)
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'usage.json'
            cache.write_text(json.dumps(self.usage), encoding='utf-8')
            args = argparse.Namespace(
                stage='plan', hours=1.0, demo=False, dry_run=False, usage_cache=cache,
                fast=False, no_score=False, score=True, score_timeout=5, keep_whole=None,
            )
            baseline = None
            for idx, text in enumerate(TASK_TEXT_CORPUS):
                scored_prof = dict(prof, reason=text[:200])
                with patch.object(router, 'score_task', return_value=(scored_prof, {'source': 'system_one', 'model': 'm'})), \
                        patch.object(router, 'read_ledger', return_value=[]), \
                        patch.object(router, 'append_ledger'):
                    doc = {
                        'task': {
                            'task_id': f'invariance-{idx}',
                            'objective': text,
                            'context': text,
                            'environment': text,
                            'expected_hours': 1.0,
                            'needs': {'local_only': True, 'local_only_reason': text[:80] or 'local'},
                        }
                    }
                    res, code = router.plan_stage(doc, args)
                self.assertEqual(code, 0)
                dec_task = dict(res['decision']['task'])
                dec_task.pop('profile', None)
                sig = (
                    res['choice'],
                    res.get('selected_bundle'),
                    dec_task,
                    [(r['id'], r['score'], r['excluded']) for r in res['ranking']],
                )
                if baseline is None:
                    baseline = sig
                else:
                    self.assertEqual(sig, baseline, f'plan_stage output changed for text={text!r}')

    def test_roll_run_is_invariant_across_task_text_corpus(self):
        prof = self.make_profile(work_kind='general_implementation', reasoning_depth=2, code_complexity=2, local_only=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'usage.json'
            cache.write_text(json.dumps(self.usage), encoding='utf-8')
            idle_worker = {
                'agent_id': 'w-idle-1',
                'task_ids': ['other-task'],
                'brief_paths': [str(root / 'other.md')],
                'chain_ids': [],
                'titles': ['Fix telegram review findings and urgent parser bug'],
                'profiles': [prof],
                'read_only_review': False,
                'updated_at': self.now.isoformat(),
            }
            baseline = None
            for idx, text in enumerate(TASK_TEXT_CORPUS):
                brief_file = root / ('review-step1.md' if idx % 2 == 0 else 'implement-step1.md')
                brief_file.write_text(text, encoding='utf-8')
                scored_prof = dict(prof, reason=text[:200])
                ns = argparse.Namespace(
                    brief=brief_file, task=None, file=[], apply_to=None,
                    local_only=text[:60].strip() or 'local dependency',
                    keep_whole=None, hours=1.0, title=text[:40], workspace=None,
                    task_id='fixed-task-id-for-invariance', wait=0,
                    parent_id='parent-a', parent_host='antigravity', parent_address='parent-a',
                    provider=None, account=None, model=None, effort=None, basis=None,
                    usage_cache=cache, dry_run=False, verbose=True, fast=False,
                    resume=None, new_worker=False, no_score=False, score=True, score_timeout=5,
                )
                with patch.object(router, 'score_task', return_value=(scored_prof, {'source': 'system_one', 'model': 'm'})), \
                        patch.object(router, 'read_ledger', return_value=[]), \
                        patch.object(router, 'append_ledger'), \
                        patch.object(roll, 'resolve_parent', return_value=('parent-a', 'native', 'parent-a')), \
                        patch.object(registry, 'pending', return_value=[]), \
                        patch.object(registry, 'reusable_workers', return_value=[idle_worker]), \
                        patch.object(roll, 'WORKER_PROMPTS_DIR', root / 'wp'), \
                        patch.object(roll, 'RESULTS_DIR', root / 'rr'):
                    res, code = roll.run(ns, router)
                self.assertEqual(code, 0)
                sig = (
                    res['status'],
                    res['route']['choice'],
                    res['route']['bundle'],
                    (res.get('worker') or {}).get('agent_id'),
                )
                if baseline is None:
                    baseline = sig
                else:
                    self.assertEqual(sig, baseline, f'roll.run decision changed for text={text!r}')

    def test_local_only_reason_text_never_overrides_explicit_local_only_flag(self):
        prof = self.make_profile(work_kind='self_contained_code', local_only=False)
        for reason in ('', 'in-place edits of workspace files', 'modify local files', 'needs local pytest and sqlite'):
            with tempfile.TemporaryDirectory() as directory:
                cache = Path(directory) / 'usage.json'
                cache.write_text(json.dumps(self.usage), encoding='utf-8')
                args = argparse.Namespace(
                    stage='plan', hours=1.0, demo=False, dry_run=True, usage_cache=cache,
                    fast=False, no_score=True, score=False, score_timeout=5, keep_whole=None,
                )
                doc = {
                    'task': {
                        'objective': 'Implement standalone parser',
                        'expected_hours': 1.0,
                        'profile': prof,
                        'needs': {'local_only': True, 'local_only_reason': reason},
                    }
                }
                res, code = router.plan_stage(doc, args)
                self.assertEqual(code, 0)
                self.assertNotEqual(res['choice'], 'dot_text', f'local_only=True was ignored for reason={reason!r}')
                codes = {a['code'] for a in res.get('alerts') or ()}
                self.assertNotIn('local_only_ignored_without_reason', codes)

    def test_independent_reviewer_gate_depends_on_profile_not_title_keywords(self):
        review_prof = self.make_profile(work_kind='debug_review', code_complexity=0, hallucination_risk=3,
                                         read_only_review=True, interaction_style='exploration')
        author_prof = self.make_profile(work_kind='debug_review', code_complexity=2, read_only_review=False)
        for title in TASK_TEXT_CORPUS:
            workers = [
                {'agent_id': 'w-author', 'titles': [title], 'profiles': [author_prof], 'read_only_review': False},
                {'agent_id': 'w-reviewer', 'titles': [title], 'profiles': [review_prof], 'read_only_review': True},
            ]
            self.assertEqual([w['agent_id'] for w in roll.independent_reviewers(workers)], ['w-reviewer'])

    def test_all_entrypoints_return_blocked_when_classifier_unavailable_or_no_score_without_profile(self):
        for text in TASK_TEXT_CORPUS[:4]:
            with tempfile.TemporaryDirectory() as directory:
                cache = Path(directory) / 'usage.json'
                cache.write_text(json.dumps(self.usage), encoding='utf-8')
                args = argparse.Namespace(
                    stage='plan', hours=1.0, demo=False, dry_run=False, usage_cache=cache,
                    fast=False, no_score=False, score=True, score_timeout=5, keep_whole=None,
                )
                with patch.object(router, 'score_task', return_value=(
                    None, {'source': 'scorer_unavailable', 'fallback_reason': 'all_scorers_failed'}
                )), patch.object(router_data, 'modules', return_value=(allocator, router)):
                    plan_res, plan_code = router.plan_stage({'task': {'objective': text, 'expected_hours': 1.0}}, args)
                    self.assertEqual(plan_code, 2)
                    self.assertEqual(plan_res['status'], 'blocked')
                    self.assertEqual(plan_res['choice'], 'none')
                    self.assertEqual(plan_res['error'], 'scorer_unavailable')

                    sim_res = router_data.simulate(text, 1.0, live_scorer=True)
                    self.assertEqual(sim_res['status'], 'blocked')
                    self.assertEqual(sim_res['choice'], 'none')
                    self.assertEqual(sim_res['error'], 'scorer_unavailable')

                # M2: roll --no-score without caller profile is blocked; with caller profile it succeeds
                roll_ns = argparse.Namespace(
                    brief=None, task=text, file=[], apply_to=None, local_only='local daemon',
                    keep_whole=None, hours=1.0, title=None, workspace=None, task_id='t-noscore',
                    wait=0, parent_id='parent-a', parent_host=None, parent_address=None,
                    provider=None, account=None, model=None, effort=None, basis=None,
                    usage_cache=cache, dry_run=False, verbose=True, fast=False,
                    resume=None, new_worker=False, no_score=True, score=False, score_timeout=5,
                )
                roll_res, roll_code = roll.run(roll_ns, router)
                self.assertEqual(roll_code, 2)
                self.assertEqual(roll_res['status'], 'blocked')
                self.assertEqual(roll_res['error'], 'scorer_unavailable')

                # roll --dry-run marks unclassified: True
                dry_ns = argparse.Namespace(**{**vars(roll_ns), 'dry_run': True, 'no_score': False})
                dry_res, dry_code = roll.run(dry_ns, router)
                self.assertEqual(dry_code, 0)
                self.assertTrue(dry_res.get('unclassified'))
                self.assertEqual(dry_res.get('unclassified_reason'), 'dry_run')


if __name__ == '__main__':
    unittest.main()
