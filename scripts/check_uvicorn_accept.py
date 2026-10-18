"""Bounded shared-listener/keep-alive probe, with no Brain app or external services.

This intentionally permits native Windows workers=2 to reproduce the upstream
transport limitation that the full load harness rejects. Success requires both
workers to serve requests; a healthy supervisor alone is insufficient.
"""
from __future__ import annotations

import argparse
import asyncio
import faulthandler
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        faulthandler.dump_traceback_later(10, repeat=True)
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                print(f"PROBE_READY:{os.getpid()}", flush=True)
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                faulthandler.cancel_dump_traceback_later()
                await send({"type": "lifespan.shutdown.complete"})
                return
    else:
        body = json.dumps({"pid": os.getpid(), "loop": type(asyncio.get_running_loop()).__name__}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


def main() -> int:
    sys.path.insert(0, str(ROOT))
    from scripts.team_memory_load_e2e import stop_process_tree

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--loop", default="auto", choices=("auto", "asyncio:ProactorEventLoop"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    argv = [sys.executable, "-m", "uvicorn", "scripts.check_uvicorn_accept:app",
            "--host", "127.0.0.1", "--port", str(port), "--workers", str(args.workers),
            "--loop", args.loop, "--log-level", "warning"]
    records: list[dict] = []
    clients: list[httpx.Client] = []
    log_path = args.out / "server.log"
    passed = False
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(argv, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=sys.platform != "win32")
        try:
            deadline = time.monotonic() + 30
            while True:
                ready = {line for line in log_path.read_text(encoding="utf-8").splitlines()
                         if line.startswith("PROBE_READY:")}
                if len(ready) == args.workers:
                    break
                if proc.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError("required workers did not start; see server.log")
                time.sleep(0.1)
            time.sleep(0.2)
            serving_pids = set()
            identities = []
            for index in range(24):
                client = httpx.Client(base_url=url, timeout=3, trust_env=False,
                                      limits=httpx.Limits(keepalive_expiry=60))
                clients.append(client)
                info = client.get("/identity").json()
                identities.append(info)
                serving_pids.add(info["pid"])
                records.append({"phase": "connect", "index": index, **info})
                if len(serving_pids) == args.workers:
                    break
            if len(serving_pids) != args.workers:
                raise RuntimeError(f"only {len(serving_pids)} of {args.workers} workers served; inconclusive")
            for index in range(40):
                with httpx.Client(timeout=3, trust_env=False) as fresh:
                    fresh.get(url + "/fresh").raise_for_status()
                round_pids = set()
                for client, identity in zip(clients, identities, strict=True):
                    started = time.monotonic()
                    info = client.get("/keep-alive").json()
                    if info != identity:
                        raise RuntimeError(f"persistent connection changed worker/loop: {identity} -> {info}")
                    round_pids.add(info["pid"])
                    records.append({"phase": "reuse", "index": index,
                                    "seconds": time.monotonic() - started, **info})
                if round_pids != serving_pids:
                    raise RuntimeError("not all original workers served this churn round")
            passed = True
        except Exception as exc:
            records.append({"error": type(exc).__name__, "detail": str(exc)})
            # Keep a failed worker alive for one bounded faulthandler sample.
            time.sleep(11)
        finally:
            for client in clients:
                client.close()
            if sys.platform == "win32":
                stop_process_tree(proc)
            else:
                # The supervisor can exit before its children. This is our own
                # session/process group, even when proc.poll() is not None.
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                finally:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    proc.wait(timeout=5)
    result = {"passed": passed, "platform": sys.platform, "workers": args.workers,
              "loop": args.loop, "records": records}
    (args.out / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "records"}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
