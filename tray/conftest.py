"""The tray finds the router under CODEX_HOME. Under test that must be this checkout, not whatever this machine installed."""
import importlib
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ['CODEX_HOME'] = str(ROOT)
# Tray tests never read this machine's provider-retry holds; a test that needs them points this at its own file.
os.environ.setdefault('ROUTER_PROVIDER_RETRY', 'off')
# Nor its live-model cache, and never the Paseo bridge; a test that needs live models points this at its own file.
os.environ.setdefault('AGENTCOMPASS_LIVE_MODELS', 'off')
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / 'skills' / 'agent-compass' / 'scripts'))
importlib.import_module('allocator')
importlib.import_module('route_judge')
importlib.import_module('update_status')


@pytest.fixture(autouse=True)
def cleanup_shared_tk_root():
    yield
    tk_mod = sys.modules.get('tkinter')
    root = getattr(tk_mod, '_tray_shared_root', None) if tk_mod is not None else None
    if root is not None:
        import tk_test_support
        try:
            if int(root.tk.call('winfo', 'exists', root._w)):
                tk_test_support.reset_tk_root(root)
        except Exception:
            pass
        tk_test_support.restore_window_visibility()
