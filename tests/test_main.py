from types import SimpleNamespace

import pytest

import main


def test_main_catches_pipeline_value_error_like_registry_errors(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings_from_env",
        lambda: SimpleNamespace(dry_run=False),
    )

    def fail():
        raise ValueError("bad discovery configuration")

    monkeypatch.setattr(main, "run_pipeline", fail)

    with pytest.raises(SystemExit, match="Pipeline configuration error: bad discovery configuration"):
        main.main()


def test_main_keeps_discovery_registry_errors_labeled_as_discovery_configuration(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings_from_env",
        lambda: SimpleNamespace(dry_run=False),
    )

    def fail():
        raise main.DiscoveryRegistryError("bad discovery registry")

    monkeypatch.setattr(main, "run_pipeline", fail)

    with pytest.raises(SystemExit, match="Discovery configuration error: bad discovery registry"):
        main.main()
