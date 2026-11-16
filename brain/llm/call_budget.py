"""Task-local fail-closed zero-provider budget for unchanged-index maintenance.

This is not a paid-usage estimator. A zero budget denies provider access,
including cached clients, rather than hoping the unchanged-file path skips it.
"""
from contextlib import contextmanager
from contextvars import ContextVar

_forbidden = ContextVar("brain_provider_calls_forbidden", default=False)


class ProviderBudgetExceeded(RuntimeError):
    pass


def provider_calls_forbidden() -> bool:
    return _forbidden.get()


def require_provider_budget() -> None:
    if provider_calls_forbidden():
        raise ProviderBudgetExceeded("Provider access denied: this refresh has a zero-call budget")


@contextmanager
def no_provider_calls():
    token = _forbidden.set(True)
    try:
        yield
    finally:
        _forbidden.reset(token)
