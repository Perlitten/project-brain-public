import pytest

from brain.config.settings import Settings


def test_postgres_pool_settings_have_bounded_operational_defaults():
    configured = Settings(_env_file=None)

    assert configured.POSTGRES_POOL_SIZE == 10
    assert configured.POSTGRES_MAX_OVERFLOW == 10
    assert configured.POSTGRES_POOL_TIMEOUT_SECONDS == 15.0


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("POSTGRES_POOL_SIZE", 0, "POSTGRES_POOL_SIZE"),
        ("POSTGRES_POOL_SIZE", 51, "POSTGRES_POOL_SIZE"),
        ("POSTGRES_MAX_OVERFLOW", -1, "POSTGRES_MAX_OVERFLOW"),
        ("POSTGRES_MAX_OVERFLOW", 51, "POSTGRES_MAX_OVERFLOW"),
        ("POSTGRES_POOL_TIMEOUT_SECONDS", 0, "POSTGRES_POOL_TIMEOUT_SECONDS"),
        ("POSTGRES_POOL_TIMEOUT_SECONDS", 301, "POSTGRES_POOL_TIMEOUT_SECONDS"),
    ],
)
def test_postgres_pool_settings_reject_unsafe_ranges(name, value, message):
    with pytest.raises(ValueError, match=message):
        Settings(_env_file=None, **{name: value})
