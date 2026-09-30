from datetime import datetime, timezone
from pathlib import Path

from discovery.http import HttpResponse
from discovery.institutional_press import (
    METHOD,
    InstitutionalPressDiscovery,
    _parse_page,
    validate_institutional_source,
)
from discovery.dispatch import load_discovery_sources


FIXTURE = Path(__file__).parent / "fixtures" / "institutional_press.html"
STAMP = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def source(**changes):
    value = {
        "source_id": "circle-pressroom",
        "name": "Circle",
        "authority_tier": 1,
        "category": "institutional",
        "discovery_method": METHOD,
        "source_url": "https://www.circle.com/pressroom",
        "enabled": True,
        "lookback_days": 180,
        "max_items": 50,
        "allowed_hosts": ["www.circle.com", "circle.com"],
        "article_path_regex": r"/pressroom/[^/?#]+",
    }
    value.update(changes)
    return value


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def response(payload):
    return HttpResponse(200, {"content-type": "text/html"}, payload, "https://www.circle.com/pressroom")


def test_registered_institutional_sources_are_bounded():
    sources = load_discovery_sources()
    ids = {item["source_id"] for item in sources}
    assert {"citi-press-releases", "circle-pressroom", "mastercard-press-releases", "coinbase-blog"} <= ids
    assert all(item["authority_tier"] == 1 for item in sources if item["discovery_method"] == METHOD)


def test_parser_keeps_only_dated_official_articles():
    rows, complete = _parse_page(source(), FIXTURE.read_bytes())
    assert complete is True
    assert len(rows) == 2
    assert rows[0]["native_id"] == "/pressroom/volante-technologies-and-circle-to-advance-stablecoin-payment-and-settlement-capabilities"
    assert rows[1]["date"] == datetime(2026, 9, 16, tzinfo=timezone.utc)


def test_collect_uses_lookback_and_deduplicates():
    http = FakeHttp(response(FIXTURE.read_bytes()))
    result = InstitutionalPressDiscovery(
        source(), http=http, now=lambda: STAMP,
    ).collect()
    assert result.status == "success"
    assert len(result.candidates) == 2
    assert len({item.candidate_id for item in result.candidates}) == 2


def test_external_host_is_ignored():
    html = b'<html><body><span>Sep 29, 2026</span><a href="https://evil.example/pressroom/fake">Fake</a></body></html>'
    result = InstitutionalPressDiscovery(
        source(), http=FakeHttp(response(html)), now=lambda: STAMP,
    ).collect()
    assert result.status == "failed"
    assert not result.candidates


def test_pipeline_scores_circle_stablecoin_payment_signal(tmp_path):
    import main
    from storage.database import JsonState
    from storage.discovery_state import JsonDiscoveryState

    http = FakeHttp(response(FIXTURE.read_bytes()))
    monkey = lambda source, state: InstitutionalPressDiscovery(
        source, http=http, now=lambda: STAMP
    ).collect(state)

    original = main.collect_source
    main.collect_source = monkey
    try:
        _, fresh, failures = main.run_pipeline(
            sources=[],
            state=JsonState(str(tmp_path / "seen.json")),
            discovery_sources=[source()],
            discovery_state=JsonDiscoveryState(tmp_path / "discovery.json"),
        )
    finally:
        main.collect_source = original

    assert failures == []
    assert len(fresh) == 2
    assert all(item.source_quality == "primary" for item in fresh)
    relevant = next(item for item in fresh if "stablecoin payment" in item.title.casefold())
    generic = next(item for item in fresh if "Arc Mainnet" in item.title)
    assert "institutional_digital_asset_payment" in relevant.relevance_categories
    assert relevant.relevance_score >= 35
    assert generic.relevance_score == 0
