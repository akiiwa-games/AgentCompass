import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import entry
import host_cindy
from test_native_hosts import parent


class CindyBridgeTests(unittest.TestCase):
    def test_real_fleet_bundle_without_mode_gets_explicit_launcher_default(self):
        import roll
        for family,expected in [('codex','full-access'),('claude','bypassPermissions'),('pi','full-access')]:
            bundle={'provider':family,'harness':family,'account':family,'model':'gpt-test',
                    'effort':'high','bundle_id':'sol_high'}
            self.assertNotIn('modeId',bundle)
            with patch.object(roll,'provider_definition',return_value=(family,{})):
                result,code=host_cindy.prepare({},bundle,Path('prompt.md'))
            self.assertEqual(code,0)
            self.assertEqual(result['route_bundle']['modeId'],expected)
            self.assertNotIn('modeId',bundle)

    def test_explicit_restricted_permission_is_not_replaced_by_default(self):
        import roll
        with patch.object(roll,'provider_definition',return_value=('codex',{})):
            result,code=host_cindy.prepare({}, {'provider':'codex','modeId':'read-only'},Path('prompt.md'))
        self.assertEqual(code,2)
        self.assertFalse(result['launched'])
        self.assertIn('read-only',result['error'])

    @unittest.skipUnless(__import__('shutil').which('node'), 'Node unavailable')
    def test_public_roll_default_fleet_permissions_reach_real_plugin_without_shim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'.codex').mkdir()
            (root/'bin').mkdir()
            # Discovery-only stand-in: Cindy owns execution, so no provider CLI
            # is executed by this public route. All credentials are synthetic.
            cli=root/'bin'/('codex.exe' if os.name=='nt' else 'codex')
            cli.write_bytes(b'fixture discovery placeholder')
            cli.chmod(0o755)
            payload={'https://api.openai.com/auth':{'chatgpt_plan_type':'plus','chatgpt_account_id':'fixture'}}
            token='e30.'+base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')+'.fixture'
            (root/'.codex'/'auth.json').write_text(json.dumps({'tokens':{'account_id':'fixture','access_token':'fixture-only','id_token':token}}))
            context=root/'context.json'
            context.write_text(json.dumps({'version':1,'source':'cindy-plugin-session-context','session_id':'actual-cindy-parent',
                'workdir':tmp,'workdir_is_local':True,'workdir_is_read_only':False}))
            env={key:value for key,value in os.environ.items() if key in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATHEXT')}
            env.update(PATH=str(root/'bin'),HOME=tmp,USERPROFILE=tmp,APPDATA=str(root/'appdata'),LOCALAPPDATA=str(root/'local'),
                CODEX_HOME=str(root/'.codex'),CLAUDE_CONFIG_DIR=str(root/'.claude'),PASEO_HOME=str(root/'.paseo'),
                ROUTER_PROVIDER_HOME=tmp,ROUTER_STATE_DIR=str(root/'state'),ROUTER_CINDY_CONTEXT=str(context),
                ROUTER_ACCOUNTS_FILE=str(Path(__file__).with_name('fixtures')/'reference-accounts.json'),
                ROUTER_ACCOUNTS_OVERRIDE=str(root/'override.json'),ROUTER_LEDGER=str(root/'ledger.jsonl'),
                ROUTER_DISPATCH_DB=str(root/'dispatch.sqlite3'),ROUTER_USAGE_CACHE=str(root/'usage.json'),ROUTER_SCORE_CACHE='off')
            proc=subprocess.run(entry.argv('roll','--task','A fixture task','--verbose',
                '--provider','codex','--account','codex','--model','gpt-6.1-sol','--effort','high',
                '--basis','The test user explicitly selected this fixture model'),
                cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=40)
            self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
            prepared=json.loads(proc.stdout)
            self.assertEqual(prepared['route_bundle']['modeId'],'full-access')
            prepared_file=root/'public-route.json'
            prepared_file.write_text(json.dumps(prepared),encoding='utf-8')
            plugin_env=dict(os.environ,CINDY_PUBLIC_ROUTE_FIXTURE=str(prepared_file))
            node=__import__('shutil').which('node')
            checked=subprocess.run([node,'--test',str(Path(__file__).with_name('test_cindy_plugin.mjs'))],
                env=plugin_env,capture_output=True,text=True,encoding='utf-8',timeout=30)
            self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)
            self.assertIn('actual public router route reaches explicit Cindy permission',checked.stdout)

    def test_trusted_context_and_nested_session_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/'context.json'
            file.write_text(json.dumps({'version':1,'source':'cindy-plugin-session-context',
                'session_id':'true-cindy-session','workdir':tmp,
                'workdir_is_local':True,'workdir_is_read_only':False}))
            env={'ROUTER_CINDY_CONTEXT':str(file),'CODEX_THREAD_ID':'provider-thread','ROUTER_PARENT_HOST':'paseo'}
            self.assertEqual(parent(env),('true-cindy-session','cindy','true-cindy-session'))
            self.assertEqual(parent(dict(env,PASEO_AGENT_ID='live-paseo')),('live-paseo','paseo','live-paseo'))
            with self.assertRaisesRegex(ValueError,'differs from Cindy'):
                parent(env,parent_id='wrong')

    def test_build_uses_public_entry_and_no_host_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'plugin'
            proc=subprocess.run(entry.argv('cindy','build-plugin','--output',output),capture_output=True,text=True,timeout=30)
            self.assertEqual(proc.returncode,0,proc.stderr)
            config=json.loads((output/'router.json').read_text())
            self.assertEqual(config['command'],entry.argv('roll'))
            self.assertNotIn('scripts',' '.join(config['command']))
            manifest=json.loads((output/'ghost.json').read_text())
            self.assertTrue(manifest['sessionContext'])
            self.assertTrue(manifest['agent']['tasks'])
            self.assertNotIn('skill',manifest)  # upstream deprecated new bundled skills
            self.assertFalse((Path(tmp)/'.cindy').exists())

    def test_unknown_mapping_schema_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError,'Account mappings'):
                host_cindy.build_plugin(Path(tmp)/'plugin',[{'model':'guessed'}])

    def test_plugin_assets_survive_wheel_skill_copy_and_standalone_skill(self):
        import importlib.util
        import shutil
        repo=Path(__file__).resolve().parents[3]
        spec=importlib.util.spec_from_file_location('stage_data_cindy',repo/'packaging/stage_data.py')
        stage=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'source'/'skills'/'agent-compass'
            (source/'scripts').mkdir(parents=True)
            for name in ('entry.py','host_cindy.py'):
                shutil.copy2(Path(__file__).with_name(name),source/'scripts'/name)
            shutil.copytree(Path(host_cindy.__file__).parents[1]/'assets',source/'assets')
            destination=root/'wheel'/'skills'
            stage.copy_skills(source.parent,destination)
            copied=destination/'agent-compass'
            for name in ('ghost.json','main.js','worker.cjs'):
                self.assertEqual((copied/'assets'/'cindy-plugin'/name).read_bytes(),
                                 (source/'assets'/'cindy-plugin'/name).read_bytes())
            output=root/'standalone-plugin'
            proc=subprocess.run([sys.executable,'-I','-X','utf8',str(copied/'scripts/host_cindy.py'),
                                 'build-plugin','--output',str(output)],capture_output=True,text=True,timeout=30)
            self.assertEqual(proc.returncode,0,proc.stderr)
            self.assertEqual(json.loads((output/'router.json').read_text())['command'],['agentcompass','roll'])

    @unittest.skipUnless(__import__('shutil').which('node'), 'Node unavailable')
    def test_actual_node_stdio_worker_external_process_context_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            output=root/'plugin'
            host_cindy.build_plugin(output)
            prompt=root/'prompt.md'
            prompt.write_bytes('unchanged 原文\r\nall lines'.encode())
            capture=root/'captured.json'
            fake=root/'public-router-fixture.py'
            fake.write_text(
                'import json,os,sys,pathlib\n'
                f'capture=pathlib.Path({str(capture)!r})\n'
                'text=sys.stdin.buffer.read().decode("utf-8")\n'
                'capture.write_text(json.dumps({"argv":sys.argv[1:],"stdin":text,"context":json.loads(pathlib.Path(os.environ["ROUTER_CINDY_CONTEXT"]).read_text()),"paseo":os.environ.get("PASEO_AGENT_ID"),"hc":os.environ.get("HC_TERMINAL_ID")}))\n'
                f'print(json.dumps({{"status":"native","route_bundle":{{"provider":"codex"}},"worker_prompt_file":{str(prompt)!r}}}))\n',encoding='utf-8')
            (output/'router.json').write_text(json.dumps({'command':[sys.executable,'-X','utf8',str(fake)],'stateRoot':str(root/'state')}))
            context={'session_id':'true-session','workdir':tmp,'workdir_is_local':True,'workdir_is_read_only':False}
            requests=[{'jsonrpc':'2.0','id':i,'method':'prepare','params':{'task':'full assignment 原文','context':context}} for i in (1,2)]
            env=dict(os.environ,PASEO_AGENT_ID='stale-paseo',HC_TERMINAL_ID='stale-terminal')
            proc=subprocess.run(['node',str(output/'worker.cjs')],input=''.join(json.dumps(r)+'\n' for r in requests),
                                env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
            self.assertEqual(proc.returncode,0,proc.stderr)
            replies=[json.loads(line) for line in proc.stdout.splitlines()]
            self.assertEqual(replies[0]['result']['operation'],replies[1]['result']['operation'])
            observed=json.loads(capture.read_text())
            self.assertEqual(observed['argv'],['--task','-','--verbose'])
            self.assertEqual(observed['stdin'],'full assignment 原文')
            self.assertEqual(observed['context']['session_id'],'true-session')
            self.assertIsNone(observed['paseo'])
            self.assertIsNone(observed['hc'])
            self.assertEqual(replies[0]['result']['prompt'],prompt.read_bytes().decode('utf-8'))


if __name__=='__main__':
    unittest.main()
