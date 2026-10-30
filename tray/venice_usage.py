"""Venice's read-only key balances. Currency balances are never invented percentages."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import urllib.request
import urllib.error


def key():
    value = os.environ.get('VENICE_API_KEY')
    if value:
        return value
    for path in (Path.home() / '.dsh/.env', Path(__file__).resolve().parents[1] / '.env'):
        if path.is_file():
            for line in path.read_text(encoding='utf-8-sig').splitlines():
                if line.startswith('VENICE_API_KEY='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    return None


def refresh(state):
    secret = key()
    if not secret:
        return None
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    request = urllib.request.Request('https://api.venice.ai/api/v1/api_keys/rate_limits',
        headers={'Authorization': 'Bearer ' + secret, 'Accept': 'application/json'})
    row = {'fetched_at': datetime.now(timezone.utc).isoformat()}
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
            data = json.load(response)['data']
        row.update(available=True, access_permitted=data.get('accessPermitted'),
            balances={k: v for k, v in (data.get('balances') or {}).items()
                      if isinstance(v, (int, float))}, next_epoch=data.get('nextEpochBegins'))
    except urllib.error.HTTPError as error:
        row.update(available=False, http_status=error.code)
    except Exception:
        row.update(available=False)
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True)
    target = state / 'venice-usage.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(row, indent=2), encoding='utf-8')
    temporary.replace(target)
    return row


def note(row):
    if not row.get('available'):
        return 'Venice 余额读取失败或未刷新' + (f" (HTTP {row['http_status']})" if row.get('http_status') else '')
    balances = row.get('balances') or {}
    text = ' · '.join(f'{k} {v:g}' for k, v in balances.items()) or 'Venice 余额未知'
    if row.get('access_permitted') is False:
        text += ' · 当前密钥不可用'
    return text + ' · ' + (row.get('fetched_at') or '')[:16].replace('T', ' ') + ' UTC'
