"""Compatibility entrypoint forwarding to route_judge.main.

Use `route_judge.py scorer --action show` for the effective scorer and available
options. This alias defines no classifier, judge or worker routing policy.
"""

from route_judge import main


if __name__ == '__main__':
    raise SystemExit(main())
