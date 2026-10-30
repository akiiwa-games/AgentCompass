import json
from unittest.mock import MagicMock, patch
import venice_usage


def test_balance_probe_uses_only_read_only_endpoint_and_stores_no_key(tmp_path):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({'data': {
        'accessPermitted': True, 'balances': {'USD': 12.5, 'DIEM': 4}, 'nextEpochBegins': 'later'}}).encode()
    with patch.object(venice_usage, 'key', return_value='fixture-secret'), \
         patch.object(venice_usage.urllib.request, 'build_opener') as opener:
        opener.return_value.open.return_value = response
        result = venice_usage.refresh(tmp_path)
    req = opener.return_value.open.call_args.args[0]
    assert req.get_method() == 'GET'
    assert req.full_url.endswith('/api_keys/rate_limits')
    assert result['balances']['USD'] == 12.5
    assert 'fixture-secret' not in (tmp_path / 'venice-usage.json').read_text()
    assert 'USD 12.5' in venice_usage.note(result)


def test_failure_is_unknown_not_zero_or_unlimited(tmp_path):
    with patch.object(venice_usage, 'key', return_value='fixture-secret'), \
         patch.object(venice_usage.urllib.request, 'build_opener', side_effect=OSError):
        result = venice_usage.refresh(tmp_path)
    assert result['available'] is False
    assert 'balances' not in result
    assert '失败' in venice_usage.note(result)
