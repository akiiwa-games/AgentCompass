"""Configure DSH and Paseo using Venice's read-only model catalog; no inference.

Run from this checkout: python examples/configure_venice_dsh.py
DSH reads VENICE_API_KEY from its private home .env, never from this script.
"""
import json
import os
from pathlib import Path
import shutil
import time
import urllib.request


def main():
    root = Path(__file__).resolve().parents[1]
    key = os.environ.get('VENICE_API_KEY')
    if not key:
        for line in (root / '.env').read_text(encoding='utf-8-sig').splitlines():
            if line.startswith('VENICE_API_KEY='):
                key = line.split('=', 1)[1].strip().strip('"').strip("'")
    if not key:
        raise SystemExit('VENICE_API_KEY missing')
    request = urllib.request.Request('https://api.venice.ai/api/v1/models?type=text',
        headers={'Authorization': 'Bearer ' + key})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=20) as response:
        models = json.load(response)['data']
    ids = sorted({row['id'] for row in models})
    home = Path.home() / '.dsh'
    home.mkdir(exist_ok=True)
    env_path = home / '.env'
    old = env_path.read_text(encoding='utf-8') if env_path.exists() else ''
    lines = [line for line in old.splitlines() if not line.startswith('VENICE_API_KEY=')]
    env_path.write_text('\n'.join(lines + ['VENICE_API_KEY=' + key]) + '\n', encoding='utf-8')
    patch_path = home / 'cordis.patch.yml'
    if patch_path.exists():
        content = patch_path.read_text(encoding='utf-8')
        try:
            patch = json.loads(content)
        except json.JSONDecodeError:
            import yaml
            patch = yaml.safe_load(content) or []
        shutil.copy2(patch_path, patch_path.with_suffix('.yml.bak-' + str(int(time.time()))))
    else:
        patch = []
    providers = {'venice-api': {'displayName': 'Venice AI', 'apiKeyEnv': 'VENICE_API_KEY',
        'api': 'openai-completions', 'baseURL': 'https://api.venice.ai/api/v1',
        'models': [{'id': mid} for mid in ids]}}
    for target, config in [('llm-pi-ai', {'providers': providers}),
            ('agent-default-model', {'provider': 'venice-api', 'model': 'zai-org-glm-5-2'})]:
        entry = next((e for e in patch if e.get('id') == target), None)
        if entry is None:
            entry = {'id': target, 'config': {}}
            patch.append(entry)
        if target == 'llm-pi-ai':
            entry.setdefault('config', {}).setdefault('providers', {}).update(providers)
        else:
            entry['config'] = {**entry.get('config', {}), **config}
    patch_path.write_text(json.dumps(patch, indent=2) + '\n', encoding='utf-8')
    path = Path.home() / '.paseo/config.json'
    document = json.loads(path.read_text(encoding='utf-8-sig'))
    shutil.copy2(path, path.with_suffix('.json.bak-dsh-' + str(int(time.time()))))
    settings = document.setdefault('agents', {}).setdefault('providers', {})
    settings['dsh'] = {**settings.get('dsh', {}), 'extends': 'acp', 'label': 'DeepSeek Harness',
        'command': [shutil.which('node'), str(Path(os.environ['APPDATA']) /
            'npm/node_modules/@deepseek-ai/dsh/lib/bin.js'), 'acp'],
        'enabled': True, 'models': [{'id': json.dumps(['venice-api', mid], separators=(',', ':')), 'label': mid,
            'isDefault': mid == 'zai-org-glm-5-2'} for mid in ids]}
    path.write_text(json.dumps(document, indent=2) + '\n', encoding='utf-8')
    catalog = Path.home() / '.agent-compass/venice-models.json'
    catalog.write_text(json.dumps({'models': models}, indent=2), encoding='utf-8')
    print(f'Configured DSH, Paseo and {len(ids)} Venice text models; no inference requests.')


if __name__ == '__main__':
    main()
