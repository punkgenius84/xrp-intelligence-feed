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


def test_source_health_warning_after_24_hours(capsys):
    from datetime import datetime, timezone
    result = main.PipelineResult(
        discovery_results=[SimpleNamespace(source_id="swift")],
        health={
            "swift": {
                "last_status": "failed",
                "status_started_at": "2026-09-22T12:00:00+00:00",
                "last_attempt": "2026-09-23T11:45:00+00:00",
                "last_error": "HTTP 403",
            }
        },
    )
    main.print_source_health_warnings(
        result,
        now=datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
    )
    output = capsys.readouterr().out
    assert "swift is failing" in output
    assert "24.0h" in output
    assert "HTTP 403" in output


def test_source_health_warning_ignores_recent_or_healthy_sources(capsys):
    from datetime import datetime, timezone
    result = main.PipelineResult(
        discovery_results=[
            SimpleNamespace(source_id="recent"),
            SimpleNamespace(source_id="healthy"),
        ],
        health={
            "recent": {
                "last_status": "empty",
                "status_started_at": "2026-09-23T01:00:00+00:00",
                "last_attempt": "2026-09-23T11:45:00+00:00",
                "last_error": "",
            },
            "healthy": {
                "last_status": "success",
                "status_started_at": "2026-09-22T00:00:00+00:00",
                "last_attempt": "2026-09-23T11:45:00+00:00",
                "last_error": "",
            },
        },
    )
    main.print_source_health_warnings(
        result,
        now=datetime(2026, 9, 23, 12, tzinfo=timezone.utc),
    )
    assert capsys.readouterr().out == ""
