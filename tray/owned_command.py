"""Job-owned command helper; stdin release happens only after assignment to the Job."""
import subprocess
import sys


def main() -> int:
    if sys.stdin.buffer.read(1) != b"1":
        return 125
    return subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL)


if __name__ == "__main__":
    raise SystemExit(main())
