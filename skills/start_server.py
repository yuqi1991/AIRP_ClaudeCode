#!/usr/bin/env python3
"""
start_server.py — 桥接服务器启动器。

检查服务器是否已在运行，若未运行则清理残留并启动。
替代 CLAUDE.md「自动启动流程」中的手动 curl 检查 + 条件启动 + sleep 等待。

用法:
  python start_server.py <ROOT>
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


SERVER_PORT = 8765
MVU_PORT = 8766
POLL_INTERVAL = 0.5
MAX_WAIT = 15.0


def _server_responding():
    """Quick check: is the bridge server already serving JSON?"""
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "2", f"http://localhost:{SERVER_PORT}/api/pending"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and result.stdout.strip():
            json.loads(result.stdout)  # validate it's JSON
            return True
    except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
        pass
    return False


def _pgrep(pattern: str) -> set[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()

    pids = set()
    for line in result.stdout.splitlines():
        try:
            pids.add(int(line.strip()))
        except ValueError:
            pass
    return pids


def _terminate_pids(pids: set[int]) -> None:
    targets = [pid for pid in sorted(pids) if pid not in {os.getpid(), os.getppid()}]
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass

    if targets:
        time.sleep(0.3)

    for pid in targets:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def _kill_stale(root_dir: str):
    """Kill leftover bridge server and MVU helper processes."""
    skills_dir = Path(root_dir).resolve() / "skills"
    pids = set()
    pids.update(_pgrep(str(skills_dir / "server.py")))
    pids.update(_pgrep(str(skills_dir / "mvu_server.js")))
    _terminate_pids(pids)


def _start_server(root_dir: str):
    """Launch server.py as a detached background process."""
    server_py = str(Path(root_dir) / "skills" / "server.py")
    skills_dir = str(Path(root_dir) / "skills")

    subprocess.Popen(
        [sys.executable, server_py],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=skills_dir,
        start_new_session=True,
    )


def main():
    if len(sys.argv) < 2:
        print(json.dumps({
            "ok": False, "action": "error",
            "error": "Usage: python start_server.py <ROOT>"
        }, ensure_ascii=False))
        sys.exit(1)

    root_dir = sys.argv[1]

    # 1. Already running?
    if _server_responding():
        print(json.dumps({
            "ok": True,
            "action": "already_running",
            "port": SERVER_PORT,
            "message": f"Bridge server already running on port {SERVER_PORT}"
        }, ensure_ascii=False))
        return

    # 2. Kill stale processes and start fresh
    _kill_stale(root_dir)

    # Brief wait for ports to release
    time.sleep(0.5)

    _start_server(root_dir)

    # 3. Poll until ready
    waited = 0.0
    while waited < MAX_WAIT:
        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL
        if _server_responding():
            print(json.dumps({
                "ok": True,
                "action": "started",
                "port": SERVER_PORT,
                "mvu_port": MVU_PORT,
                "wait_seconds": round(waited, 1),
                "message": f"Server started on port {SERVER_PORT} after {waited:.1f}s"
            }, ensure_ascii=False))
            return

    # 4. Timeout — server didn't come up
    print(json.dumps({
        "ok": False,
        "action": "timeout",
        "error": f"Server did not respond on port {SERVER_PORT} within {MAX_WAIT}s"
    }, ensure_ascii=False))
    sys.exit(1)


if __name__ == "__main__":
    main()
