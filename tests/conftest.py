import pytest


@pytest.fixture(autouse=True)
def _disable_discord_max_age(monkeypatch):
    """Most tests use fixed historical dates; the real age window would make them time-dependent.

    Tests of the max-age behavior set DISCORD_MAX_AGE_DAYS or pass max_age_days explicitly.
    """
    monkeypatch.setenv("DISCORD_MAX_AGE_DAYS", "0")
