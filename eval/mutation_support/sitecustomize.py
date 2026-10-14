"""Offline Python network guard for mutation workers and child interpreters.

Activate explicitly with PYTHONPATH pointing to this directory. This file is
never loaded by the application during normal operation.
"""

import atexit
import ipaddress
import json
import os
import socket
import sys
import time
from pathlib import Path

os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")
for _key in ("NVIDIA_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY",
             "OPENROUTER_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY", "DEEPSEEK_API_KEY",
             "MISTRAL_API_KEY", "LLM_API_KEY", "EMBEDDING_API_KEY"):
    os.environ.pop(_key, None)

_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo
_sendto = socket.socket.sendto
_blocked_ports = {int(value) for value in os.getenv(
    "BRAIN_MUTATION_BLOCK_PORTS", "5433,6379,7687,8000,3100",
).split(",") if value}


def _check(address):
    if not isinstance(address, tuple):
        return  # Local Unix-domain fixtures.
    host, port = address[:2]
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = False
    if not local or port in _blocked_ports:
        raise RuntimeError("Offline mutation guard blocked a connection outside owned fixtures")


def connect(self, address):
    _check(address)
    return _connect(self, address)


def connect_ex(self, address):
    _check(address)
    return _connect_ex(self, address)


def getaddrinfo(host, port, *args, **kwargs):
    if host is not None:
        _check((host, port or 0))
    return _getaddrinfo(host, port, *args, **kwargs)


def sendto(self, data, *args):
    _check(args[-1])
    return _sendto(self, data, *args)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = getaddrinfo
socket.socket.sendto = sendto

# CLI integration tests intentionally execute Python from another repository.
# Configure the instrumentation against the campaign root while preserving the
# CLI's working directory. Otherwise stats mode guesses source paths in the
# target repository and fails before any mutation is executed.
_campaign_root = os.getenv("BRAIN_MUTATION_CONFIG_ROOT")
_active_mutant = os.getenv("BRAIN_MUTATION_ORIGINAL_MUTANT") or os.getenv("MUTANT_UNDER_TEST")
_parent_test = os.getenv("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0]
_main_module = None
if _campaign_root:
    if "-m" in sys.orig_argv:
        _main_module = sys.orig_argv[sys.orig_argv.index("-m") + 1]
    elif sys.argv and sys.argv[0].endswith(".py"):
        _script = Path(sys.argv[0]).resolve()
        _generated = Path(_campaign_root) / "mutants"
        if _script.is_relative_to(_generated):
            _main_module = ".".join(_script.relative_to(_generated).with_suffix("").parts)
if _campaign_root and _active_mutant:
    _cwd = os.getcwd()
    try:
        os.chdir(_campaign_root)
        from mutmut.configuration import config
        from mutmut.mutation.trampoline import set_mutant_under_test

        config()
        _dispatch_mutant = _active_mutant
        if _main_module and _active_mutant.rpartition(".")[0] == _main_module:
            _dispatch_mutant = "__main__." + _active_mutant.rpartition(".")[2]
        os.environ["BRAIN_MUTATION_ORIGINAL_MUTANT"] = _active_mutant
        set_mutant_under_test(_dispatch_mutant)
    finally:
        os.chdir(_cwd)


def _save_child_stats():
    directory = os.getenv("BRAIN_MUTATION_CHILD_STATS")
    module = sys.modules.get("mutmut.state")
    if _active_mutant != "stats" or not _parent_test or not directory or module is None:
        return
    functions = sorted(
        (_main_module + function[len("__main__"):])
        if _main_module and function.startswith("__main__.") else function
        for function in module.state()._stats
    )
    target = Path(directory)
    target.mkdir(exist_ok=True, parents=True)
    path = target / f"{os.getpid()}-{time.monotonic_ns()}.json"
    path.write_text(json.dumps({"test": _parent_test, "functions": functions}) + "\n", encoding="utf-8")


atexit.register(_save_child_stats)
