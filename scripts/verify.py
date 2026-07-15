#!/usr/bin/env python3
"""One-command offline verification gate for MiloAgent.

Runs, in order:
  1. Python bytecode compile of the production source trees.
  2. The offline pytest suite (tests/).
  3. A JS syntax check of the dashboard client.

Prints ``PASS <name>`` per gate that succeeds and exits non-zero on the
first failure. Designed to be run from the repo root:

    python scripts/verify.py

No network, no real account, no browser. If ``node`` is not on PATH the
script fails loudly with an actionable message rather than skipping the
gate silently.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Production paths whose Python must compile cleanly.
COMPILE_TARGETS = ["core", "dashboard", "platforms", "safety", "miloagent.py"]

# Dashboard client JS that must parse.
JS_CHECK_TARGET = "dashboard/static/app.js"


def _run(cmd, cwd=None) -> subprocess.CompletedProcess:
    """Run a command, streaming output inline, returning the completed process."""
    return subprocess.run(cmd, cwd=cwd or REPO_ROOT)


def gate_compileall() -> bool:
    """Gate 1: byte-compile production Python."""
    cmd = [sys.executable, "-m", "compileall", "-q", *COMPILE_TARGETS]
    proc = _run(cmd)
    if proc.returncode != 0:
        print(f"FAIL compileall (exit {proc.returncode})")
        return False
    print("PASS compileall")
    return True


def gate_pytest() -> bool:
    """Gate 2: offline pytest suite."""
    cmd = [sys.executable, "-m", "pytest", "-q"]
    proc = _run(cmd)
    if proc.returncode != 0:
        print(f"FAIL pytest (exit {proc.returncode})")
        return False
    print("PASS pytest")
    return True


def gate_node_check() -> bool:
    """Gate 3: dashboard JS syntax check via ``node --check``."""
    node = shutil.which("node")
    if node is None:
        print(
            "FAIL node-check: 'node' executable not found on PATH. "
            "Install Node.js (https://nodejs.org/) before re-running; "
            "this gate is mandatory and is not skipped."
        )
        return False
    proc = _run([node, "--check", JS_CHECK_TARGET])
    if proc.returncode != 0:
        print(f"FAIL node-check {JS_CHECK_TARGET} (exit {proc.returncode})")
        return False
    print(f"PASS node-check {JS_CHECK_TARGET}")
    return True


GATES = [
    ("compileall", gate_compileall),
    ("pytest", gate_pytest),
    ("node-check", gate_node_check),
]


def main() -> int:
    for _name, fn in GATES:
        if not fn():
            # First failure short-circuits; a FAIL line was already printed.
            return 1
    print("ALL GATES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
