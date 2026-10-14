"""Offline Python network guard for mutation workers and child interpreters.

Activate explicitly with PYTHONPATH pointing to this directory. This file is
never loaded by the application during normal operation.
"""

import ipaddress
import os
import socket

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
