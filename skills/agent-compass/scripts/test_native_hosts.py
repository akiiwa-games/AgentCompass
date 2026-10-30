"""Orca and T3 Code as parent hosts: session detection, addressing and the native hand-off text."""
import argparse
import os
import unittest
from unittest.mock import patch

import roll
import worker_context as wc


def parent(env, **flags):
    args = argparse.Namespace(parent_id=None, parent_host=None, parent_address=None)
    vars(args).update(flags)
    with patch.dict(os.environ, env, clear=True):
        return wc.resolve_parent(args)


class OrcaParentTests(unittest.TestCase):
    def test_plain_orca_agent_is_addressed_by_its_session_prefix(self):
        env = {'ORCA_AGENT_SESSION_ID': 'sess-1', 'ORCA_TERMINAL_HANDLE': 'term_abc', 'ORCA_PANE_KEY': 'p'}
        self.assertEqual(parent(env), ('sess-1', 'orca', 'orca_session_id:sess-1'))

    def test_dispatched_orca_worker_keeps_its_minted_handle(self):
        env = {'ORCA_AGENT_SESSION_ID': 'sess-2', 'ORCA_TERMINAL_HANDLE': 'structworker_9'}
        self.assertEqual(parent(env), ('sess-2', 'orca', 'structworker_9'))

    def test_terminal_handle_alone_is_enough(self):
        self.assertEqual(parent({'ORCA_TERMINAL_HANDLE': 'term_abc'}), ('term_abc', 'orca', 'term_abc'))

    def test_pane_key_without_an_address_is_not_orca(self):
        with self.assertRaisesRegex(ValueError, 'addressable parent'):
            parent({'ORCA_PANE_KEY': 'p'})

    def test_orca_beats_the_provider_session_inside_it(self):
        env = {'ORCA_AGENT_SESSION_ID': 'sess-1', 'CLAUDE_CODE_SESSION_ID': 'claude-9'}
        self.assertEqual(parent(env)[:2], ('sess-1', 'orca'))

    def test_explicit_parent_in_orca_derives_the_address(self):
        self.assertEqual(parent({}, parent_host='orca', parent_id='sess-3'), ('sess-3', 'orca', 'orca_session_id:sess-3'))
        self.assertEqual(parent({'ORCA_TERMINAL_HANDLE': 'structworker_1'}, parent_host='orca', parent_id='sess-3'),
                         ('sess-3', 'orca', 'structworker_1'))
        self.assertEqual(parent({}, parent_host='orca', parent_id='sess-3', parent_address='term_x')[2], 'term_x')

    def test_paseo_still_wins_and_never_turns_into_orca(self):
        env = {'PASEO_AGENT_ID': 'agent-1', 'ORCA_AGENT_SESSION_ID': 'sess-1'}
        self.assertEqual(parent(env), ('agent-1', 'paseo', 'agent-1'))
        with self.assertRaisesRegex(ValueError, 'actual PASEO_AGENT_ID'):
            parent(env, parent_host='orca')

    def test_orca_variables_do_not_leak_into_an_explicit_other_host(self):
        env = {'ORCA_AGENT_SESSION_ID': 'sess-1'}
        self.assertEqual(parent(env, parent_host='native', parent_id='c', parent_address='run:r'), ('c', 'native', 'run:r'))


class T3ParentTests(unittest.TestCase):
    def test_pi_provider_env_marks_t3_and_the_session_is_the_parent_thread(self):
        env = {'T3_MCP_URL': 'http://127.0.0.1:1/mcp', 'T3_MCP_BEARER_TOKEN': 't', 'PI_SESSION_ID': 'thread-1'}
        self.assertEqual(parent(env), ('thread-1', 't3', 'thread-1'))

    def test_claude_and_codex_threads_need_the_explicit_flag_or_env(self):
        env = {'CODEX_THREAD_ID': 'thread-2'}
        self.assertEqual(parent(env), ('thread-2', 'paseo', 'thread-2'))
        self.assertEqual(parent(env, parent_host='t3'), ('thread-2', 't3', 'thread-2'))
        self.assertEqual(parent(dict(env, ROUTER_PARENT_HOST='t3')), ('thread-2', 't3', 'thread-2'))

    def test_half_a_t3_credential_is_not_t3(self):
        self.assertEqual(parent({'T3_MCP_URL': 'u', 'CODEX_THREAD_ID': 'x'})[1], 'paseo')

    def test_unknown_host_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'paseo[|]native[|]orca[|]t3'):
            parent({'CODEX_THREAD_ID': 'x'}, parent_host='cursor')


class NativeHandOffTests(unittest.TestCase):
    bundle = {'provider': 'codex', 'model': 'gpt-6-astra', 'effort': 'high'}

    def test_orca_gets_a_worker_start_command_for_the_routed_bundle(self):
        text = roll.native_next('orca', self.bundle, 'orca_session_id:s')
        self.assertIn('orca orchestration worker-start', text)
        self.assertIn('--agent codex', text)
        self.assertIn('--model gpt-6-astra --effort high', text)
        self.assertIn('orca_session_id:s', text)

    def test_t3_gets_delegate_task_and_no_polling(self):
        text = roll.native_next('t3', self.bundle, 'thread-1')
        self.assertIn('delegate_task', text)
        self.assertIn('do not poll', text)

    def test_generic_native_text_is_unchanged(self):
        self.assertIn('--spec', roll.native_next('native', self.bundle, 'run:r'))
        self.assertEqual(roll.NATIVE_HOSTS, wc.NATIVE_HOSTS)


if __name__ == '__main__':
    unittest.main()
