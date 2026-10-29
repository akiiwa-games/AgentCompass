"""Frozen equivalent of launch_chatgpt_web.pyw, also usable from source."""
import sys
from services import start_chatgpt_web

if __name__ == "__main__":
    start_chatgpt_web(show="--show" in sys.argv[1:])
