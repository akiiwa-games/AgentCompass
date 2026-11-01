"""Execute generated command hooks in their real shell, with only temp settings."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class HookCommandTransportTests(unittest.TestCase):
    def test_installed_command_with_spaces_and_quotes_preserves_gate_deny_and_allow(self):
        if os.name=='nt':
            shells=[('git-bash',Path('C:/Program Files/Git/bin/bash.exe'),['--noprofile','--norc','-c']),
                    ('git-sh',Path('C:/Program Files/Git/usr/bin/sh.exe'),['-c'])]
        else:
            shells=[('posix-sh',Path('/bin/sh'),['-c']),('bash',Path('/bin/bash'),['--noprofile','--norc','-c'])]
        shells=[item for item in shells if item[1].is_file()]
        if not shells:self.skipTest('No supported real shell available')
        setup=Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix='hook transport ') as tmp:
            root=Path(tmp)
            venv=root/'ve nv'
            subprocess.run([sys.executable,'-m','venv','--without-pip','--copies',str(venv)],check=True,
                           capture_output=True,timeout=45)
            python=venv/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
            source=root/"hook lib's & $scope"
            source.mkdir()
            for name in ('sync_host_globals.py','router_gate_hook.py','host_rules.py'):
                shutil.copy2(setup/name,source/name)
            home=root/'test home'
            (home/'.claude').mkdir(parents=True)
            env={key:value for key,value in os.environ.items() if key in ('SYSTEMROOT','WINDIR','TEMP','TMP','PATHEXT')}
            env.update(HOME=str(home),USERPROFILE=str(home),APPDATA=str(home/'appdata'),LOCALAPPDATA=str(home/'local'),
                PATH=str(python.parent),ROUTER_PROVIDER_HOME=str(home),ROUTER_STATE_DIR=str(home/'.agent-compass'),
                ROUTER_LEDGER=str(home/'ledger.jsonl'),ROUTER_SKILL_ROOT=str(setup.parent/'skills/agent-compass'),
                ROUTER_USAGE_CACHE=str(home/'usage.json'),ROUTER_ACCOUNTS_OVERRIDE=str(home/'accounts.json'),
                ROUTER_PASEO_AGENTS_DIR=str(home/'agents'),ROUTER_HOST_POLICY=str(home/'policy.json'),
                PASEO_HOME=str(home/'.paseo'),CODEX_HOME=str(home/'.codex'),CLAUDE_CONFIG_DIR=str(home/'.claude'),
                ROUTER_GATE_BYPASS='0')
            producer=("import pathlib,sys; sys.path.insert(0,sys.argv[1]); import sync_host_globals as s; "
                      "s.set_home(pathlib.Path(sys.argv[2])); s.MANIFEST={'hooks':{}}; s.configure_hooks()")
            made=subprocess.run([str(python),'-X','utf8','-c',producer,str(source),str(home)],env=env,
                                capture_output=True,text=True,encoding='utf-8',timeout=30)
            self.assertEqual(made.returncode,0,made.stdout+made.stderr)
            settings=json.loads((home/'.claude/settings.json').read_text(encoding='utf-8'))
            command=settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
            payload={'tool_name':'Agent','tool_input':{'subagent_type':'Explore','prompt':'Only fixture inspection'}}
            ledger=home/'ledger.jsonl'
            for name,shell,flags in shells:
                for recorded in (False,True):
                    with self.subTest(shell=name,recorded=recorded):
                        if recorded:
                            ledger.write_text(json.dumps({'at':datetime.now(timezone.utc).isoformat(),
                                'stage':'plan','choice':'sol_high','source':'deterministic_allocator'})+'\n',encoding='utf-8')
                        else:ledger.unlink(missing_ok=True)
                        result=subprocess.run([str(shell),*flags,command],input=json.dumps(payload),env=env,
                            capture_output=True,text=True,encoding='utf-8',timeout=30)
                        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                        answer=json.loads(result.stdout)
                        if recorded:self.assertEqual(answer,{})
                        else:self.assertEqual(answer['hookSpecificOutput']['permissionDecision'],'deny')


if __name__=='__main__':unittest.main()
