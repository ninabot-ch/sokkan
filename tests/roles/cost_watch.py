"""Hard cost cap of a Claude calibration run: reads the meter's wire.jsonl, prices each request
with Anthropic's list prices (real usage of the API), and stops the bench (SIGTERM to its PID,
whose command line is checked first) as soon as the cap is reached.

    python tests/roles/cost_watch.py <wire.jsonl> <bench pid> <model> <cap usd>
"""
import json
import os
import signal
import sys
import time

PRICES = {"claude-sonnet-5-5": (2.00, 10.00, 0.10), "claude-opus-5-5": (4.00, 20.00, 0.20)}


def spent(path: str, model: str) -> float:
    pi, po, pr = PRICES[model]
    total = 0.0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    u = json.loads(line).get("usage_raw") or {}
                except ValueError:
                    continue
                total += (int(u.get("input_tokens") or 0) * pi
                          + int(u.get("cache_creation_input_tokens") or 0) * pi * 1.25
                          + int(u.get("cache_read_input_tokens") or 0) * pr
                          + int(u.get("output_tokens") or 0) * po) / 1e6
    except FileNotFoundError:
        pass
    return total


def main() -> int:
    path, pid, model, cap = sys.argv[1], int(sys.argv[2]), sys.argv[3], float(sys.argv[4])
    while True:
        try:
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode()
        except FileNotFoundError:
            print(f"bench ended, spent {spent(path, model):.2f} $")
            return 0
        s = spent(path, model)
        if s >= cap:
            if "real_runs.py" in cmd:
                os.kill(pid, signal.SIGTERM)
                print(f"CAP {cap} $ reached ({s:.2f} $): bench {pid} stopped")
            return 1
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
