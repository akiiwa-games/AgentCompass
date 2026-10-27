import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import consult_bridge
from consult_bridge import receive, answer_text


class CaptureTests(unittest.TestCase):
    def test_cli_sends_one_pinned_pro_request_and_captures_answer(self):
        response = {'id': 'resp_pinned', 'status': 'completed', 'model': 'chatgpt-web/gpt-6-pro',
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '测试完成'}]}]}
        wire = ('data: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n\n').encode()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prompt = root / 'question.txt'
            prompt.write_text('无敏感信息的测试', encoding='utf-8')
            out = root / 'run'
            with patch('sys.argv', ['consult_bridge.py', '--prompt', str(prompt), '--out', str(out)]), \
                 patch.object(consult_bridge, 'bridge_home', return_value=root), \
                 patch.object(consult_bridge, 'ensure_connection', return_value=('http://127.0.0.1:17843', {})), \
                 patch.object(consult_bridge.urllib.request, 'urlopen', return_value=io.BytesIO(wire)) as send, \
                 patch('sys.stdout', new_callable=io.StringIO):
                consult_bridge.main()
            send.assert_called_once()
            payload = json.loads(send.call_args.args[0].data)
            self.assertEqual(payload['model'], 'chatgpt-web/gpt-6-pro')
            self.assertEqual(payload['reasoning'], {'effort': 'max'})
            self.assertEqual(payload['tools'], [])
            self.assertEqual(payload['tool_choice'], 'none')
            self.assertEqual((out / 'response.md').read_text(encoding='utf-8'), '测试完成')
            state = json.loads((out / 'state.json').read_text(encoding='utf-8'))
            self.assertEqual(state['status'], 'completed')
            self.assertEqual(state['requested_model'], payload['model'])

    def test_complete_unicode_and_final_only(self):
        response = {'id': 'resp_test', 'status': 'completed', 'output': [
            {'type': 'message', 'phase': 'commentary', 'content': [{'type': 'output_text', 'text': 'working'}]},
            {'type': 'message', 'phase': 'final_answer', 'content': [{'type': 'output_text', 'text': '建议与计划'}]}]}
        wire = 'data: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n\n'
        with tempfile.TemporaryDirectory() as folder:
            result = receive(io.BytesIO(wire.encode()), Path(folder), {})
            self.assertEqual(answer_text(result), '建议与计划')
            self.assertTrue((Path(folder) / 'response.json').exists())

    def test_delta_and_done_are_not_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            wire = b'data: {"type":"response.output_text.delta","delta":"partial"}\n\ndata: [DONE]\n\n'
            with self.assertRaisesRegex(ValueError, 'without response.completed'):
                receive(io.BytesIO(wire), Path(folder), {})

    def test_failure_retains_terminal_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            wire = b'data: {"type":"response.failed","response":{"id":"resp_failed"}}\n\n'
            state = {}
            with self.assertRaisesRegex(ValueError, 'response.failed'):
                receive(io.BytesIO(wire), Path(folder), state)
            self.assertEqual(state['response_id'], 'resp_failed')
            self.assertTrue((Path(folder) / 'terminal.json').exists())

    def test_tools_are_never_accepted_as_advice(self):
        with self.assertRaisesRegex(ValueError, 'non-advisory'):
            answer_text({'status': 'completed', 'output': [{'type': 'function_call'}]})

    def test_empty_answer_not_success(self):
        with self.assertRaisesRegex(ValueError, 'no answer'):
            answer_text({'status': 'completed', 'output': []})


if __name__ == '__main__':
    unittest.main()
