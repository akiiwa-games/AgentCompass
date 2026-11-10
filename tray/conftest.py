"""The tray finds the router under CODEX_HOME. Under test that must be this checkout, not whatever this machine installed."""
import importlib
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
os.environ['CODEX_HOME'] = str(ROOT)
# Tray tests never read this machine's provider-retry holds; a test that needs them points this at its own file.
os.environ.setdefault('ROUTER_PROVIDER_RETRY', 'off')
# Nor its live-model cache, and never the Paseo bridge; a test that needs live models points this at its own file.
os.environ.setdefault('ROUTER_LIVE_MODELS', 'off')
sys.path.insert(0, str(ROOT / 'skills' / 'agent-compass' / 'scripts'))
importlib.import_module('allocator')
importlib.import_module('route_judge')
