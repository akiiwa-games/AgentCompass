import io
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

import pytest
import consult_bridge
from consult_bridge import receive, answer_text


class CaptureTests(unittest.TestCase):
    @pytest.mark.slow
    def test_cli_sends_one_pinned_pro_request_and_captures_answer(self):
        response = {'id': 'resp_pinned', 'status': 'completed', 'model': 'chatgpt-web/gpt-6-pro',
                    'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '测试完成'}]}]}
        wire = ('data: ' + json.dumps({'type': 'response.completed', 'response': response}) + '\n\n').encode()
        requests = []
        class Bridge(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                assert self.path == '/healthz'
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'service': 'codex-chatgpt-web', 'mode': 'browser-only',
                                             'accepting_turns': True}).encode())

            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(wire)

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Bridge)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory(prefix='pro consultation ') as folder:
            root = Path(folder)
            prompt = root / 'question.txt'
            prompt.write_text('无敏感信息的测试', encoding='utf-8')
            out = root / 'run'
            (root / 'config.json').write_text(json.dumps({'mode': 'browser-only', 'browserInteractionMode': 'automatic',
                'host': '127.0.0.1', 'port': server.server_port, 'proAvailable': True}))
            env = {k: v for k, v in os.environ.items() if k.upper() in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')}
            env.update(HOME=str(root), USERPROFILE=str(root), PATH='', PRO_CONSULT_HOME=str(root),
                       PRO_CONSULT_LAUNCHER=str(root / 'not-installed.exe'))
            entry = Path(__file__).resolve().parents[3] / 'agentcompass'
            done = subprocess.run([sys.executable, '-X', 'utf8', str(entry), 'chatgpt-web',
                                   '--prompt', str(prompt), '--out', str(out)], cwd=root, env=env,
                                  capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0][0], '/v1/responses')
            payload = requests[0][1]
            self.assertEqual(payload['model'], 'chatgpt-web/gpt-6-pro')
            self.assertEqual(payload['reasoning'], {'effort': 'max'})
            self.assertEqual(payload['tools'], [])
            self.assertEqual(payload['tool_choice'], 'none')
            self.assertEqual(payload['input'][0]['content'][0]['text'], prompt.read_text(encoding='utf-8'))
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
