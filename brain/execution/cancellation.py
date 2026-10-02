"""Execution Cancellation Manager ensuring zero orphan child processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


class ExecutionCancellationManager:
    """Manages process supervision and cancellation cleanup."""

    @staticmethod
    def terminate_process_group(process: subprocess.Popen, timeout_seconds: float = 5.0):
        if process.poll() is not None:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, signal.SIGTERM)
                try:
                    process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass
        finally:
            if process.poll() is None:
                try:
                    process.kill()
                except Exception:
                    pass
