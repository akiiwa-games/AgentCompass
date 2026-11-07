"""Rule 6 (static AST/symbol scanner) and Rule 7 (phrasing-invariance) tests for NO-HARDCODE classification."""
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
import dispatch_registry as registry
import roll
import route_judge as router
from router_test_support import isolated_router_state, usage_windows
import router_data


def setUpModule():
    isolated_router_state(unittest.addModuleCleanup)


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
    # Category 6.7: Roll title regexes & review-title classifier
    '_REVIEW_RE',
    '_AUTHORING_RE',
    'is_review_title',
    # Realtime loop legacy symbols
    'infer_realtime_loop',
    'infer_realtime_gui',
    'realtime_loop_needs_gui',
    'REALTIME_LOOP_CONTINUOUS_WORDS',
    'REALTIME_LOOP_TARGET_WORDS',
    'REALTIME_LOOP_PRESSURE_WORDS',
})

# Explicitly audited functions in routing/classification modules that may touch task text ONLY for
# non-classification purposes (forwarding to System-One scorer, ledger/UI truncation, reroute-lock identity,
# or worker-continuity Jaccard token matching).
ALLOWED_TEXT_TOUCHING_FUNCTIONS = {
    'skills/agent-compass/scripts/allocator.py': {
        'validate_profile': 'caps optional profile.reason to 300 chars without inspecting content',
        'normalize_task': 'passes task["cwd"] through to normalized dict for account cwd_regex gate',
        'cwd_allowed': 'matches structured task["cwd"] against account cwd_regex / allowed_cwd config',
        'decision_snapshot': 'truncates objective[:200] for compact ledger snapshot metadata',
    },
    'skills/agent-compass/scripts/route_judge.py': {
        'scorer_task_fields': 'extracts objective/context/environment/expected_hours/needs to send to System-One scorer',
        'scorer_evidence': 'serializes task fields into the System-One scorer prompt',
        'scorer_prompt': 'builds the System-One scorer prompt',
        'build_jev_request': 'passes scorer_task_fields(task) to TypeSafe System-One API',
        'score_task': 'checks whether task["objective"] is non-empty before invoking System-One scorer',
        'recent_route_lock': 'compares objective[:180] equality across recent ledger rows for reroute guard',
        'scorer_unavailable_result': 'truncates objective[:200] for ledger record',
        'split_result': 'truncates objective[:200] for ledger record',
        'plan_stage': 'checks bool((task.get("objective") or "").strip()) to decide whether to call System-One scorer',
        'finalize': 'records objective[:200] in ledger entry',
        'scorer_test': 'passes sample objective to run_single_scorer for CLI scorer self-test',
        'main': 'registers --objective CLI argument for scorer test',
    },
    'skills/agent-compass/scripts/roll.py': {
        'title_of': 'extracts first Markdown line as display title for launched worker',
        'build_task': 'populates task["objective"] and needs["local_only_reason"] from CLI arguments',
        '_match_tokens': 'tokenizes brief/title for idle-worker continuity Jaccard matching (not task classification)',
        'match_reusable_worker': 'computes Jaccard overlap between follow-up brief and parent idle workers',
        'paseo': 'passes title_of(text) as worker display title',
        'run': 'passes task text to task["objective"] and match_reusable_worker',
    },
    'tray/router_data.py': {
        'ledger_summary': 'formats and truncates objective[:48] for tray table display',
        'build_task': 'stores caller objective and local_only_reason strings on task dict',
        'replay': 'passes recorded objective and local_only_reason to build_task',
    },
}

TASK_TEXT_CORPUS = (
    'Complete the assigned unit of work.',
    '请尽快完成单模块接口修复，比较急，加急，开启fast，use fast mode immediately asap pre-reset sprint!',
    'Do NOT use fast mode; no fast mode; not urgent; 不用开启fast，不着急，慢慢做。',
    'Brainstorm creative novel scheme and roadmap; 制定方案、设计方案、新方案、发散、架构创新、提出假设。',
    '### 交付物 1\nFirst part\n### 交付物 2\nSecond part\n### Deliverable 3\nThird part\n#### Task 4\nFourth part',
    'Click the Save button with the mouse and keyboard in editor.py on localhost 127.0.0.1 pytest sqlite daemon worktree C:\\Users\\tanwei\\repo',
    '用鼠标点击桌面设置里的保存并截图，检查截图对比视觉差异，搜索官网公开文档完成厂商调研并翻译文档。',
    'Independent security & invariant review (read-only) of commit a81f3c2; do not modify repo; 只读代码审查、独立审查、不修改仓库代码。',
    'Fix the review findings and address audit comments in dispatch_flow.py; 任务：修复"代码包增量更新"审查发现的问题。',
    '实现实时刷新页面、修复页面轮询逻辑、设计盯盘看板前端、排查客户端循环等待、编写抢票脚本与挂机工具。',
)


class StaticNoHardcodeScannerTests(unittest.TestCase):
    """Rule 6: Static scanner verifying zero hardcoded text classification in production code."""

    def production_python_files(self):
        files = []
        for folder in (REPO_ROOT / 'skills' / 'agent-compass' / 'scripts', REPO_ROOT / 'tray', REPO_ROOT / 'agentcompass'):
            if not folder.exists():
                continue
            for path in sorted(folder.glob('*.py')):
                if path.name.startswith('test_') or path.name in ('conftest.py', 'router_test_support.py', 'tk_test_support.py'):
                    continue
                files.append(path)
        return files

    def test_no_banned_classification_symbols_exist_in_production_files(self):
        violations = []
        for path in self.production_python_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=rel)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if node.name in BANNED_CLASSIFICATION_SYMBOLS:
                        violations.append(f'{rel}:{node.lineno}: banned definition {node.name}')
                elif isinstance(node, ast.Name) and node.id in BANNED_CLASSIFICATION_SYMBOLS:
                    violations.append(f'{rel}:{node.lineno}: banned symbol reference {node.id}')
                elif isinstance(node, ast.Attribute) and node.attr in BANNED_CLASSIFICATION_SYMBOLS:
                    violations.append(f'{rel}:{node.lineno}: banned attribute {node.attr}')
        self.assertEqual(violations, [], 'Banned text-classification symbols found:\n' + '\n'.join(violations))

    def test_no_route_keywords_in_accounts_or_capability_catalogs(self):
        for rel in (
            'skills/agent-compass/references/accounts.json',
            'skills/agent-compass/references/model-capabilities.json',
            'skills/agent-compass/references/reference-fleet.json',
        ):
            path = REPO_ROOT / rel
            if not path.exists():
                continue
            doc = json.loads(path.read_text(encoding='utf-8-sig'))
            routes = ((doc.get('capability_policy') or {}).get('routes') or doc.get('routes') or {})
            for route_name, spec in routes.items():
                if isinstance(spec, dict):
                    self.assertNotIn('keywords', spec, f'{rel} route {route_name} must not define keywords')

    def test_ast_no_unauthorized_regex_or_keyword_matching_on_task_text_fields(self):
        text_fields = {'objective', 'local_only_reason', 'titles'}
        violations = []
        for rel, allowed_funcs in ALLOWED_TEXT_TOUCHING_FUNCTIONS.items():
            path = REPO_ROOT / rel
            tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=rel)
            for node in tree.body:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                touches_text = False
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and sub.value in text_fields:
                        touches_text = True
                        break
                if touches_text and node.name not in allowed_funcs:
                    violations.append(
                        f'{rel}:{node.lineno}: function {node.name!r} accesses task text field {text_fields} '
                        'without an audited non-classification exception'
                    )
        self.assertEqual(violations, [], 'Unauthorized task-text access found:\n' + '\n'.join(violations))


class PhrasingInvarianceAndBlockedFallbackTests(unittest.TestCase):
    """Rule 7: Changing task text wording must never alter routing for a fixed TaskProfile."""

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

    def test_all_entrypoints_return_blocked_when_classifier_unavailable(self):
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


if __name__ == '__main__':
    unittest.main()
