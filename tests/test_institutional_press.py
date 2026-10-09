from datetime import datetime, timezone
from pathlib import Path
import pytest

from discovery.http import HttpResponse
from discovery.institutional_press import (
    METHOD,
    InstitutionalPressDiscovery,
    _official_article,
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
    assert {
        "citi-press-releases", "circle-pressroom", "mastercard-press-releases",
        "coinbase-blog", "coinbase-investor-news", "jpmorgan-payments-newsroom", "bny-newsroom",
    } <= ids
    assert all(item["authority_tier"] == 1 for item in sources if item["discovery_method"] == METHOD)
    circle = next(item for item in sources if item["source_id"] == "circle-pressroom")
    mastercard = next(item for item in sources if item["source_id"] == "mastercard-press-releases")
    dbs = next(item for item in sources if item["source_id"] == "dbs-newsroom")
    assert circle["detail_fallback_limit"] == 1
    assert mastercard["detail_fallback_limit"] == 1
    assert dbs["detail_fallback_limit"] == 1
    coinbase_blog = next(item for item in sources if item["source_id"] == "coinbase-blog")
    assert coinbase_blog["source_url"] == "https://www.coinbase.com/blog"
    assert coinbase_blog["article_path_regex"] == r"(?:/[a-z]{2}-[a-z]{2})?/blog/[^/?#]+"
    coinbase_ir = next(item for item in sources if item["source_id"] == "coinbase-investor-news")
    jpmorgan = next(item for item in sources if item["source_id"] == "jpmorgan-payments-newsroom")
    assert coinbase_ir["detail_fallback_limit"] == 1
    assert jpmorgan["detail_fallback_limit"] == 1
    citi = next(item for item in sources if item["source_id"] == "citi-press-releases")
    assert citi["detail_fallback_limit"] == 1
    swift_registry = next(item for item in sources if item["source_id"] == "swift-press-releases")
    assert swift_registry["source_url"] == "https://www.swift.com/news-events/press-releases?page=0"
    assert swift_registry["article_path_regex"] == r"(?:/news-events/press-releases|/news-events/migrated-news/press-releases)/[^/?#]+"
    visa = next(item for item in sources if item["source_id"] == "visa-press-releases")
    assert visa["source_url"] == "https://usa.visa.com/about-visa/newsroom/press-releases-listing.html"
    assert visa["allowed_hosts"] == ["usa.visa.com"]
    assert visa["article_path_regex"] == r"/about-visa/newsroom/press-releases\.releaseId\.[^/?#]+"
    assert visa["numeric_date_order"] == "dmy"


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
        result = main.run_pipeline(
            sources=[],
            state=JsonState(str(tmp_path / "seen.json")),
            discovery_sources=[source()],
            discovery_state=JsonDiscoveryState(tmp_path / "discovery.json"),
        )
    finally:
        main.collect_source = original

    assert result.failures == []
    assert len(result.fresh) == 2
    assert all(item.source_quality == "primary" for item in result.fresh)
    relevant = next(item for item in result.fresh if "stablecoin payment" in item.title.casefold())
    generic = next(item for item in result.fresh if "Arc Mainnet" in item.title)
    assert "institutional_digital_asset_payment" in relevant.relevance_categories
    assert relevant.relevance_score >= 35
    assert generic.relevance_score == 0


def test_swift_and_visa_article_allowlists_and_dates():
    fixture = Path(__file__).parent / "fixtures" / "institutional_swift_visa.html"
    swift = source(
        source_id="swift-press-releases",
        name="Swift",
        source_url="https://www.swift.com/about-us/media-centre/press-releases",
        allowed_hosts=["www.swift.com", "swift.com"],
        article_path_regex=r"/news-events/migrated-news/press-releases/[^/?#]+",
    )
    visa = source(
        source_id="visa-press-releases",
        name="Visa",
        source_url="https://usa.visa.com/about-visa/newsroom/press-releases-listing.html",
        allowed_hosts=["usa.visa.com"],
        article_path_regex=r"/about-visa/newsroom/press-releases\.releaseId\.[^/?#]+",
        numeric_date_order="dmy",
    )
    swift_rows, swift_complete = _parse_page(swift, fixture.read_bytes())
    visa_rows, visa_complete = _parse_page(visa, fixture.read_bytes())
    assert swift_complete is True
    assert visa_complete is True
    assert len(swift_rows) == 1
    assert len(visa_rows) == 1
    assert swift_rows[0]["date"] == datetime(2026, 7, 9, tzinfo=timezone.utc)
    assert visa_rows[0]["date"] == datetime(2026, 6, 10, tzinfo=timezone.utc)


def test_dbs_supports_path_and_query_identifiers():
    fixture = Path(__file__).parent / "fixtures" / "dbs_newsroom.html"
    dbs = source(
        source_id="dbs-newsroom",
        name="DBS",
        source_url="https://www.dbs.com/media/default.page",
        allowed_hosts=["www.dbs.com", "dbs.com"],
        article_path_regex=r"(?:/newsroom/[^/?#]+|/NewsPrinter\.page)",
        native_id_query_param="newsId",
    )
    rows, complete = _parse_page(dbs, fixture.read_bytes())
    assert complete is True
    assert len(rows) == 2
    assert {row["date"] for row in rows} == {
        datetime(2026, 9, 10, tzinfo=timezone.utc),
        datetime(2026, 9, 7, tzinfo=timezone.utc),
    }
    assert rows[0]["native_id"] != rows[1]["native_id"]
    assert rows[1]["native_id"] == "/newsprinter.page?newsId=mt38s6jc"


def test_optional_native_id_query_parameter_is_valid():
    dbs = source(
        source_id="dbs-newsroom",
        name="DBS",
        source_url="https://www.dbs.com/media/default.page",
        allowed_hosts=["www.dbs.com", "dbs.com"],
        article_path_regex=r"(?:/newsroom/[^/?#]+|/NewsPrinter\.page)",
        native_id_query_param="newsId",
    )
    assert validate_institutional_source(dbs)["native_id_query_param"] == "newsId"


def test_json_ld_fallback_handles_js_heavy_institutional_index():
    html = b'''<html><head><script type="application/ld+json">{"@graph":[{"@type":"NewsArticle","headline":"Citi Token Services Expands Global Footprint","url":"https://www.citigroup.com/global/news/press-release/2026/citi-token-services-expands","datePublished":"2026-09-28T12:00:00Z"}]}</script></head><body></body></html>'''
    citi = source(
        source_id="citi-press-releases",
        name="Citi",
        source_url="https://www.citigroup.com/global/news/press-release",
        allowed_hosts=["www.citigroup.com", "citigroup.com"],
        article_path_regex=r"/global/news/press-release/(?:\d{4}/)?[^/?#]+",
    )
    rows, complete = _parse_page(citi, html)
    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Citi Token Services Expands Global Footprint"
    assert rows[0]["date"] == datetime(2026, 9, 28, 12, tzinfo=timezone.utc)



def test_mastercard_registry_matches_current_global_press_index():
    mastercard = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "mastercard-press-releases"
    )
    assert mastercard["source_url"] == "https://www.mastercard.com/global/en/news-and-trends/press.html"
    assert mastercard["allowed_hosts"] == ["www.mastercard.com", "mastercard.com"]
    assert mastercard["article_path_regex"] == r"/global/en/news-and-trends/press/\d{4}/[^/?#]+/[^/?#]+"


def test_mastercard_current_article_path_is_allowed():
    mastercard = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "mastercard-press-releases"
    )
    rows, complete = _parse_page(
        mastercard,
        b'''<html><body><a href="https://www.mastercard.com/global/en/news-and-trends/press/2026/october/mastercard-at-money-2020-2026.html">Mastercard at Money 20/20: Fueling momentum. Unlocking potential.</a><span>October 6, 2026</span></body></html>''',
    )
    assert complete is True
    assert len(rows) == 1
    assert rows[0]["date"] == datetime(2026, 10, 6, tzinfo=timezone.utc)


def test_visa_day_month_numeric_dates_are_parsed_without_partial_health():
    visa = source(
        source_id="visa-press-releases",
        name="Visa",
        source_url="https://usa.visa.com/about-visa/newsroom/press-releases-listing.html",
        allowed_hosts=["usa.visa.com"],
        article_path_regex=r"/about-visa/newsroom/press-releases\.releaseId\.[^/?#]+",
        numeric_date_order="dmy",
    )
    html = b"""<html><body>
    <span>01/10/2026</span><a href="https://usa.visa.com/about-visa/newsroom/press-releases.releaseId.22806.html">Visa Data Shows Stablecoins Gaining Traction in Business Payments</a>
    <span>30/09/2026</span><a href="https://usa.visa.com/about-visa/newsroom/press-releases.releaseId.22807.html">Visa Foundation Commits $2 Million to Boost Ecosystems Supporting Small Businesses</a>
    </body></html>"""
    rows, complete = _parse_page(visa, html)
    assert complete is True
    assert {row["title"]: row["date"] for row in rows} == {
        "Visa Data Shows Stablecoins Gaining Traction in Business Payments":
            datetime(2026, 10, 1, tzinfo=timezone.utc),
        "Visa Foundation Commits $2 Million to Boost Ecosystems Supporting Small Businesses":
            datetime(2026, 9, 30, tzinfo=timezone.utc),
    }

@pytest.mark.parametrize("value", ["ymd", [], None])
def test_institutional_numeric_date_order_rejects_unknown_values(value):
    with pytest.raises(ValueError, match="numeric_date_order must be"):
        validate_institutional_source(source(numeric_date_order=value))

class SequenceHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_citi_recovers_featured_release_but_keeps_source_partial():
    source_url = "https://www.citigroup.com/global/news/press-release"
    article_url = (
        "https://www.citigroup.com/global/news/press-release/2026/"
        "citi-commerce-media-deliver-more-personalized-customer-brand-experiences"
    )
    index = HttpResponse(
        200, {"content-type": "text/html"},
        b"""<html><body><h2>Citi Unveils Citi Commerce Media to Deliver More Personalized Experiences for Customers and Brands</h2>
        <a href="/global/news/press-release/2026/citi-commerce-media-deliver-more-personalized-customer-brand-experiences">Read More</a>
        <p>Loading</p></body></html>""",
        source_url,
    )
    detail = HttpResponse(
        200, {"content-type": "text/html"},
        b"""<html><body><h1>Citi Unveils Citi Commerce Media to Deliver More Personalized Experiences for Customers and Brands</h1>
        <p>September 23, 2026</p><p>For Immediate Release</p></body></html>""",
        article_url,
    )
    http = SequenceHttp([index, detail])
    citi = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "citi-press-releases"
    )
    result = InstitutionalPressDiscovery(citi, http=http, now=lambda: STAMP).collect()

    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.candidates[0].title == (
        "Citi Unveils Citi Commerce Media to Deliver More Personalized Experiences for Customers and Brands"
    )
    assert result.candidates[0].published_at == datetime(2026, 9, 23, tzinfo=timezone.utc)
    assert len(http.calls) == 2
    assert "bounded detail fallback" in " ".join(result.errors)


def test_citi_detail_fallback_rejects_missing_date_and_official_metadata():
    source_url = "https://www.citigroup.com/global/news/press-release"
    index = HttpResponse(
        200, {"content-type": "text/html"},
        b"""<html><body><a href="https://evil.example/global/news/press-release/2026/fake">Fake</a>
        <a href="/global/news/press-release/2026/citi-example">Read More</a></body></html>""",
        source_url,
    )
    detail = HttpResponse(
        200, {"content-type": "text/html"},
        b"<html><body><h1>Citi Example Press Release</h1><p>Date omitted</p></body></html>",
        "https://www.citigroup.com/global/news/press-release/2026/citi-example",
    )
    http = SequenceHttp([index, detail])
    citi = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "citi-press-releases"
    )
    result = InstitutionalPressDiscovery(citi, http=http, now=lambda: STAMP).collect()

    assert result.status == "failed"
    assert result.candidates == []
    assert len(http.calls) == 2
    assert http.calls[1][0] == "https://www.citigroup.com/global/news/press-release/2026/citi-example/"
    assert any("lacked grounded title/date metadata" in error for error in result.errors)


def test_institutional_detail_fallback_limit_is_bounded():
    base = source()
    for value in (0, 4, True, "1", None):
        try:
            validate_institutional_source({**base, "detail_fallback_limit": value})
        except ValueError as exc:
            assert "detail_fallback_limit" in str(exc)
        else:
            raise AssertionError(f"expected invalid detail fallback limit {value!r} to be rejected")



def test_official_article_canonical_url_is_idempotently_allowlisted():
    citi = source(
        source_id="citi-press-releases",
        name="Citi",
        source_url="https://www.citigroup.com/global/news/press-release",
        allowed_hosts=["www.citigroup.com", "citigroup.com"],
        article_path_regex=r"/global/news/press-release/(?:\d{4}/)?[^/?#]+",
    )
    first = _official_article(
        citi,
        "/global/news/press-release/2026/citi-commerce-media-deliver-more-personalized-customer-brand-experiences",
    )
    assert first is not None
    second = _official_article(citi, first[0])
    assert second is not None
    assert second[1] == first[1]
    assert second[0] == first[0]



def test_coinbase_investor_news_uses_official_dated_article_routes():
    investor = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "coinbase-investor-news"
    )
    assert investor["source_url"] == "https://investor.coinbase.com/news/"
    assert investor["allowed_hosts"] == ["investor.coinbase.com"]
    assert investor["article_path_regex"] == r"/news/news-details/\d{4}/[^/?#]+/default\.aspx"

    html = b"""<html><body>
    <span>09/03/2026</span><a href="https://investor.coinbase.com/news/news-details/2026/Coinbase-to-Participate-in-Citis-2026-Global-TMT-Conference/default.aspx">Coinbase to Participate in Citi's 2026 Global TMT Conference</a>
    <span>08/31/2026</span><a href="https://investor.coinbase.com/news/news-details/2026/Coinbase-Q2-Earnings/default.aspx">Coinbase Q2 Earnings</a>
    </body></html>"""
    rows, complete = _parse_page(investor, html)

    assert complete is True
    assert len(rows) == 2
    assert rows[0]["date"] == datetime(2026, 9, 3, tzinfo=timezone.utc)
    assert rows[1]["date"] == datetime(2026, 8, 31, tzinfo=timezone.utc)
    assert _official_article(
        investor,
        "https://evil.example/news/news-details/2026/fake/default.aspx",
    ) is None



def test_duplicate_official_article_cards_ignore_generic_links_and_merge_title_variants():
    mastercard = source(
        source_id="mastercard-press-releases",
        name="Mastercard",
        source_url="https://www.mastercard.com/global/en/news-and-trends/press.html",
        allowed_hosts=["www.mastercard.com", "mastercard.com"],
        article_path_regex=r"/global/en/news-and-trends/press/\d{4}/[^/?#]+/[^/?#]+",
    )
    article_url = (
        "https://www.mastercard.com/global/en/news-and-trends/press/2026/july/"
        "mastercard-expands-virtual-card-platform.html"
    )
    html = (
        '<html><body><article>'
        '<span>July 9, 2026</span>'
        f'<a href="{article_url}">Mastercard Expands Virtual Card Platform</a>'
        '<span>July 9, 2026</span>'
        f'<a href="{article_url}">Mastercard Expands Virtual Card Platform Across Asia Pacific</a>'
        '<span>July 9, 2026</span>'
        f'<a href="{article_url}">Read More</a>'
        '</article></body></html>'
    ).encode()

    rows, complete = _parse_page(mastercard, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Mastercard Expands Virtual Card Platform Across Asia Pacific"
    assert rows[0]["date"] == datetime(2026, 7, 9, tzinfo=timezone.utc)


def test_unique_official_article_without_date_still_marks_index_incomplete():
    mastercard = source(
        source_id="mastercard-press-releases",
        name="Mastercard",
        source_url="https://www.mastercard.com/global/en/news-and-trends/press.html",
        allowed_hosts=["www.mastercard.com", "mastercard.com"],
        article_path_regex=r"/global/en/news-and-trends/press/\d{4}/[^/?#]+/[^/?#]+",
    )
    html = b"""<html><body>
    <a href="https://www.mastercard.com/global/en/news-and-trends/press/2026/july/mastercard-example.html">Mastercard Example Release</a>
    <span>July 8, 2026</span><a href="https://www.mastercard.com/global/en/news-and-trends/press/2026/july/another-official-release.html">Another Official Release</a>
    </body></html>"""

    rows, complete = _parse_page(mastercard, html)

    assert complete is False
    assert len(rows) == 1
    assert rows[0]["title"] == "Another Official Release"



def test_official_article_preserves_file_extension_routes_and_is_idempotent():
    cases = [
        (
            source(
                source_id="coinbase-investor-news",
                name="Coinbase Investor Relations",
                source_url="https://investor.coinbase.com/news/",
                allowed_hosts=["investor.coinbase.com"],
                article_path_regex=r"/news/news-details/\d{4}/[^/?#]+/default\.aspx",
            ),
            "https://investor.coinbase.com/news/news-details/2026/Coinbase-Q2-Earnings/default.aspx",
        ),
        (
            source(
                source_id="mastercard-press-releases",
                name="Mastercard",
                source_url="https://www.mastercard.com/global/en/news-and-trends/press.html",
                allowed_hosts=["www.mastercard.com", "mastercard.com"],
                article_path_regex=r"/global/en/news-and-trends/press/\d{4}/[^/?#]+/[^/?#]+",
            ),
            "https://www.mastercard.com/global/en/news-and-trends/press/2026/july/mastercard-example.html",
        ),
    ]
    for source_config, article_url in cases:
        first = _official_article(source_config, article_url)
        assert first is not None
        assert first[0] == article_url
        assert _official_article(source_config, first[0]) == first



def test_coinbase_blog_accepts_current_localized_article_route():
    coinbase_blog = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "coinbase-blog"
    )
    html = b"""<html><body>
    <span>Oct 8, 2026</span>
    <a href="https://www.coinbase.com/en-sg/blog/coinbase-and-samsung-bring-usdc-to-samsung-wallet">Coinbase and Samsung Bring USDC to Samsung Wallet</a>
    </body></html>"""

    rows, complete = _parse_page(coinbase_blog, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Coinbase and Samsung Bring USDC to Samsung Wallet"
    assert rows[0]["date"] == datetime(2026, 10, 8, tzinfo=timezone.utc)
    assert rows[0]["url"] == "https://www.coinbase.com/en-sg/blog/coinbase-and-samsung-bring-usdc-to-samsung-wallet/"



def test_coinbase_investor_news_recovers_dated_article_with_bounded_fallback():
    article_url = (
        "https://investor.coinbase.com/news/news-details/2026/"
        "Coinbase-to-Participate-in-Citis-2026-Global-TMT-Conference/default.aspx"
    )
    index_url = "https://investor.coinbase.com/news/"
    index = HttpResponse(
        200, {"content-type": "text/html"},
        f'<html><body><a href="{article_url}">Read More</a><p>Loading</p></body></html>'.encode(),
        index_url,
    )
    detail = HttpResponse(
        200, {"content-type": "text/html"},
        b"<html><body><h1>Coinbase to Participate in Citi's 2026 Global TMT Conference</h1><p>September 3, 2026</p></body></html>",
        article_url,
    )
    http = SequenceHttp([index, detail])
    investor = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "coinbase-investor-news"
    )

    result = InstitutionalPressDiscovery(investor, http=http, now=lambda: STAMP).collect()

    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "Coinbase to Participate in Citi's 2026 Global TMT Conference"
    assert result.candidates[0].published_at == datetime(2026, 9, 3, tzinfo=timezone.utc)
    assert http.calls[1][0] == article_url
    assert "bounded detail fallback" in " ".join(result.errors)


def test_jpmorgan_newsroom_recovers_dated_article_with_bounded_fallback():
    article_url = "https://www.jpmorgan.com/payments/newsroom/cross-border-payments-thunes-expansion"
    index_url = "https://www.jpmorgan.com/payments/newsroom"
    index = HttpResponse(
        200, {"content-type": "text/html"},
        f'<html><body><a href="{article_url}">Read More</a><p>Loading</p></body></html>'.encode(),
        index_url,
    )
    detail = HttpResponse(
        200, {"content-type": "text/html"},
        b"<html><body><h1>J.P. Morgan Payments brings faster cross-border payments to more markets</h1><p>September 22, 2026</p></body></html>",
        article_url,
    )
    http = SequenceHttp([index, detail])
    jpmorgan = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "jpmorgan-payments-newsroom"
    )

    result = InstitutionalPressDiscovery(jpmorgan, http=http, now=lambda: STAMP).collect()

    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "J.P. Morgan Payments brings faster cross-border payments to more markets"
    assert result.candidates[0].published_at == datetime(2026, 9, 22, tzinfo=timezone.utc)
    assert http.calls[1][0] == article_url + "/"
    assert "bounded detail fallback" in " ".join(result.errors)



def test_swift_current_index_and_article_route_are_official_and_parseable():
    swift = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "swift-press-releases"
    )
    assert swift["source_url"] == "https://www.swift.com/news-events/press-releases?page=0"
    html = b"""<html><body>
    <span>28 September 2026</span>
    <a href="https://www.swift.com/news-events/press-releases/swift-and-its-community-innovate-bring-ease-of-domestic-consumer-payments-cross-border-transaction-experience">Swift and its community innovate to bring ease of domestic consumer payments to cross-border transaction experience</a>
    </body></html>"""

    rows, complete = _parse_page(swift, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Swift and its community innovate to bring ease of domestic consumer payments to cross-border transaction experience"
    assert rows[0]["date"] == datetime(2026, 9, 28, tzinfo=timezone.utc)
    assert rows[0]["url"] == (
        "https://www.swift.com/news-events/press-releases/"
        "swift-and-its-community-innovate-bring-ease-of-domestic-consumer-payments-cross-border-transaction-experience/"
    )



def test_json_ld_repairs_incomplete_card_when_other_html_cards_parse():
    mastercard = source(
        source_id="mastercard-press-releases",
        name="Mastercard",
        source_url="https://www.mastercard.com/global/en/news-and-trends/press.html",
        allowed_hosts=["www.mastercard.com", "mastercard.com"],
        article_path_regex=r"/global/en/news-and-trends/press/\d{4}/[^/?#]+/[^/?#]+",
    )
    complete_url = (
        "https://www.mastercard.com/global/en/news-and-trends/press/2026/july/"
        "mastercard-good-release.html"
    )
    structured_url = (
        "https://www.mastercard.com/global/en/news-and-trends/press/2026/july/"
        "mastercard-jsonld-backed-release.html"
    )
    html = f"""<html><head>
    <script type="application/ld+json">{{"@context":"https://schema.org","@type":"NewsArticle","headline":"Mastercard JSON-LD Backed Release","url":"{structured_url}","datePublished":"2026-07-08T12:00:00Z"}}</script>
    </head><body>
    <span>July 9, 2026</span><a href="{complete_url}">Mastercard Good Release</a>
    <a href="{structured_url}">Read More</a>
    </body></html>""".encode()

    rows, complete = _parse_page(mastercard, html)

    assert complete is True
    assert len(rows) == 2
    assert {row["title"] for row in rows} == {
        "Mastercard Good Release",
        "Mastercard JSON-LD Backed Release",
    }
    assert next(row for row in rows if row["url"] == structured_url)["date"] == datetime(
        2026, 7, 8, 12, tzinfo=timezone.utc
    )


def test_json_ld_resolves_duplicate_anchor_date_conflict_only_when_consistent():
    mastercard = source(
        source_id="mastercard-press-releases",
        name="Mastercard",
        source_url="https://www.mastercard.com/global/en/news-and-trends/press.html",
        allowed_hosts=["www.mastercard.com", "mastercard.com"],
        article_path_regex=r"/global/en/news-and-trends/press/\d{4}/[^/?#]+/[^/?#]+",
    )
    article_url = (
        "https://www.mastercard.com/global/en/news-and-trends/press/2026/july/"
        "mastercard-expands-virtual-card-platform.html"
    )
    html = f"""<html><head>
    <script type="application/ld+json">{{"@context":"https://schema.org","@type":"NewsArticle","headline":"Mastercard Expands Virtual Card Platform","url":"{article_url}","datePublished":"2026-07-09T12:00:00Z"}}</script>
    </head><body>
    <span>July 9, 2026</span><a href="{article_url}">Mastercard Expands Virtual Card Platform</a>
    <span>July 10, 2026</span><a href="{article_url}">Mastercard Expands Virtual Card Platform</a>
    </body></html>""".encode()

    rows, complete = _parse_page(mastercard, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["url"] == article_url
    assert rows[0]["title"] == "Mastercard Expands Virtual Card Platform"
    assert rows[0]["date"] == datetime(2026, 7, 9, 12, tzinfo=timezone.utc)



def test_partial_index_recovers_one_missing_official_card_without_claiming_full_health():
    index_url = "https://www.circle.com/pressroom"
    known_url = "https://www.circle.com/pressroom/known-good-release"
    missing_url = "https://www.circle.com/pressroom/missing-date-release"
    index = HttpResponse(
        200, {"content-type": "text/html"},
        f"""<html><body>
        <span>September 28, 2026</span><a href="{known_url}">Circle Known Good Release</a>
        <a href="{missing_url}">Read More</a>
        </body></html>""".encode(),
        index_url,
    )
    detail = HttpResponse(
        200, {"content-type": "text/html"},
        b"<html><body><h1>Circle Missing-Date Release</h1><p>September 27, 2026</p></body></html>",
        missing_url + "/",
    )
    http = SequenceHttp([index, detail])
    circle = source(detail_fallback_limit=1)

    result = InstitutionalPressDiscovery(circle, http=http, now=lambda: STAMP).collect()

    assert result.status == "partial"
    assert {item.title for item in result.candidates} == {
        "Circle Known Good Release",
        "Circle Missing-Date Release",
    }
    assert len(http.calls) == 2
    assert http.calls[1][0] == missing_url + "/"
    assert any("bounded detail fallback recovered 1 additional official article" in error for error in result.errors)
