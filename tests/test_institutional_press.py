from datetime import datetime, timezone
from pathlib import Path
import pytest

from discovery.http import HttpResponse
from discovery.institutional_press import (
    METHOD,
    InstitutionalPressDiscovery,
    _official_article,
    _parse_article_detail,
    _index_request_id,
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


def coinbase_html_source():
    return source(
        source_id="coinbase-blog",
        name="Coinbase",
        source_url="https://www.coinbase.com/blog",
        allowed_hosts=["www.coinbase.com", "coinbase.com"],
        article_path_regex=r"(?:/[a-z]{2}-[a-z]{2})?/blog/(?!landing/?$)[^/?#]+",
    )


class FakeHttp:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def response(payload):
    return HttpResponse(200, {"content-type": "text/html"}, payload, "https://www.circle.com/pressroom")


def test_unchanged_index_is_not_reported_as_empty():
    unchanged = HttpResponse(
        304,
        {},
        b"",
        "https://www.circle.com/pressroom",
    )
    http = FakeHttp(unchanged)
    state = {
        "sources": {
            "circle-pressroom": {
                "requests": {
                    _index_request_id(source()["source_url"]): {
                        "etag": '"circle-v1"',
                        "last_modified": "Tue, 29 Sep 2026 12:00:00 GMT",
                    }
                }
            }
        }
    }

    result = InstitutionalPressDiscovery(
        source(), http=http, now=lambda: STAMP
    ).collect(state)

    assert result.status == "not_modified"
    assert result.candidates == []
    assert result.errors == []
    assert result.state_updates == {
        _index_request_id(source()["source_url"]): {
            "etag": '"circle-v1"',
            "last_modified": "Tue, 29 Sep 2026 12:00:00 GMT",
        }
    }
    assert len(http.calls) == 1
    assert http.calls[0][1]["etag"] == '"circle-v1"'
    assert http.calls[0][1]["last_modified"] == "Tue, 29 Sep 2026 12:00:00 GMT"


def test_index_validators_are_not_reused_after_source_url_changes():
    legacy_state = {
        "sources": {
            "circle-pressroom": {
                "requests": {
                    "index": {
                        "etag": '"old-url-etag"',
                        "last_modified": "Tue, 29 Sep 2026 12:00:00 GMT",
                    }
                }
            }
        }
    }
    http = FakeHttp(response(FIXTURE.read_bytes()))
    result = InstitutionalPressDiscovery(source(), http=http, now=lambda: STAMP).collect(legacy_state)
    assert result.status in {"success", "partial"}
    assert http.calls[0][1]["etag"] == ""
    assert http.calls[0][1]["last_modified"] == ""


def test_registered_institutional_sources_are_bounded():
    sources = load_discovery_sources()
    ids = {item["source_id"] for item in sources}
    assert {
        "citi-press-releases", "circle-pressroom", "mastercard-press-releases",
        "coinbase-blog", "coinbase-investor-news", "jpmorgan-payments-newsroom", "bny-newsroom",
        "imf-blogs", "imf-global-financial-stability-report",
    } <= ids
    assert all(item["authority_tier"] == 1 for item in sources if item["discovery_method"] == METHOD)
    imf_sources = {item["source_id"]: item for item in sources if item["source_id"].startswith("imf-")}
    assert set(imf_sources) == {"imf-blogs", "imf-global-financial-stability-report", "imf-media-center-news"}
    assert imf_sources["imf-blogs"]["enabled"] is False
    assert imf_sources["imf-global-financial-stability-report"]["enabled"] is False
    assert imf_sources["imf-media-center-news"]["enabled"] is False  # Runner receives HTTP 202 from the official listing.
    circle = next(item for item in sources if item["source_id"] == "circle-pressroom")
    mastercard = next(item for item in sources if item["source_id"] == "mastercard-press-releases")
    dbs = next(item for item in sources if item["source_id"] == "dbs-newsroom")
    assert circle["detail_fallback_limit"] == 1
    assert mastercard["detail_fallback_limit"] == 1
    assert dbs["detail_fallback_limit"] == 5
    coinbase_blog = next(item for item in sources if item["source_id"] == "coinbase-blog")
    assert coinbase_blog["source_url"] == "https://www.coinbase.com/blog"
    assert coinbase_blog["discovery_method"] == METHOD
    # The article detail route returns HTTP 403 in Actions; do not retry it on every poll.
    assert "detail_fallback_limit" not in coinbase_blog
    citi = next(item for item in sources if item["source_id"] == "citi-press-releases")
    assert citi["source_url"] == "https://www.citigroup.com/global/news/press-release"
    assert citi["article_path_regex"] == r"/global/news/press-release/\d{4}/[^/?#]+"
    assert citi["enabled"] is False  # Parked until the runner can read an official article index.
    assert citi["detail_fallback_limit"] == 3
    coinbase_ir = next(item for item in sources if item["source_id"] == "coinbase-investor-news")
    assert coinbase_ir["enabled"] is False  # Static IR index has no runner-readable dated article cards.
    jpmorgan = next(item for item in sources if item["source_id"] == "jpmorgan-payments-newsroom")
    assert coinbase_ir["detail_fallback_limit"] == 1
    assert jpmorgan["detail_fallback_limit"] == 1
    citi = next(item for item in sources if item["source_id"] == "citi-press-releases")
    assert citi["enabled"] is False
    assert citi["detail_fallback_limit"] == 3
    swift_registry = next(item for item in sources if item["source_id"] == "swift-press-releases")
    assert swift_registry["source_url"] == "https://www.swift.com/about-us/media-centre/press-releases"
    assert swift_registry["article_path_regex"] == r"(?:/about-us/media-centre/press-releases|/news-events/press-releases|/news-events/migrated-news/press-releases)/[^/?#]+"
    visa = next(item for item in sources if item["source_id"] == "visa-press-releases")
    assert visa["source_url"] == "https://usa.visa.com/about-visa/newsroom/press-releases-listing.html"
    assert visa["allowed_hosts"] == ["usa.visa.com"]
    assert visa["article_path_regex"] == r"/about-visa/newsroom/press-releases\.releaseId\.[^/?#]+"
    assert visa["numeric_date_order"] == "dmy"



def test_parser_accepts_unquoted_href_and_decodes_html_entities():
    html = b'''<html><body>
      <article><time datetime="2026-10-13T09:30:00Z"></time>
      <a class="release-link" href=/pressroom/example-unquoted-release>Circle &amp; Partners Announce Tokenization Pilot</a>
      </article></body></html>'''
    rows, complete = _parse_page(source(), html)
    assert complete is True
    assert len(rows) == 1
    assert rows[0]["url"] == "https://www.circle.com/pressroom/example-unquoted-release/"
    assert rows[0]["title"] == "Circle & Partners Announce Tokenization Pilot"
    assert rows[0]["date"] == datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)


def test_parser_reads_machine_readable_time_datetime_for_official_article():
    html = b'''<html><body><article>
      <time datetime="2026-10-13T09:30:00-04:00"></time>
      <a href="/pressroom/example-tokenization-announcement">Example tokenization announcement</a>
    </article></body></html>'''
    rows, complete = _parse_page(source(), html)
    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Example tokenization announcement"
    assert rows[0]["date"] == datetime(2026, 10, 13, 13, 30, tzinfo=timezone.utc)


def test_parser_accepts_official_blogposting_json_ld():
    html = b'''<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"BlogPosting",
     "headline":"Official blog announcement",
     "url":"https://www.circle.com/pressroom/example-blog-announcement",
     "datePublished":"2026-10-12T12:00:00Z"}
    </script></head><body></body></html>'''
    rows, complete = _parse_page(source(), html)
    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Official blog announcement"
    assert rows[0]["date"] == datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def test_parser_accepts_json_ld_type_after_other_script_attributes():
    html = b'''<html><head><script async type=application/ld+json>
    {"@context":"https://schema.org","@type":"NewsArticle",
     "headline":"JSON-LD with reordered unquoted type attribute",
     "url":"https://www.circle.com/pressroom/reordered-jsonld",
     "datePublished":"2026-10-12T12:00:00Z"}
    </script></head><body></body></html>'''
    rows, complete = _parse_page(source(), html)
    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "JSON-LD with reordered unquoted type attribute"
    assert rows[0]["native_id"] == "/pressroom/reordered-jsonld"


def test_parser_accepts_schema_org_url_json_ld_type():
    html = b'''<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"https://schema.org/NewsArticle",
     "headline":"Official URL-typed news article",
     "url":"https://www.circle.com/pressroom/url-typed-news-article",
     "datePublished":"2026-10-12T12:00:00Z"}
    </script></head><body></body></html>'''
    rows, complete = _parse_page(source(), html)
    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Official URL-typed news article"
    assert rows[0]["native_id"] == "/pressroom/url-typed-news-article"


def test_parser_ignores_unrelated_schema_org_url_json_ld_type():
    html = b'''<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"https://schema.org/Organization",
     "name":"Circle",
     "url":"https://www.circle.com/pressroom/not-an-article",
     "datePublished":"2026-10-12T12:00:00Z"}
    </script></head><body></body></html>'''
    with pytest.raises(ValueError, match="missing links and structured article metadata"):
        _parse_page(source(), html)


def test_detail_fallback_reads_machine_readable_time_datetime():
    html = b'''<html><body><h1>Official detail announcement</h1>
      <time datetime="2026-10-11T08:00:00Z"></time>
    </body></html>'''
    row = _parse_article_detail(
        source(),
        "https://www.circle.com/pressroom/example-detail-announcement",
        html,
    )
    assert row is not None
    assert row["title"] == "Official detail announcement"
    assert row["date"] == datetime(2026, 10, 11, 8, tzinfo=timezone.utc)


def test_parser_maps_dates_after_generic_div_cards_without_borrowing_neighbors():
    coinbase = source(
        source_id="coinbase-blog",
        name="Coinbase",
        source_url="https://www.coinbase.com/blog",
        allowed_hosts=["www.coinbase.com", "coinbase.com"],
        article_path_regex=r"(?:/[a-z]{2}-[a-z]{2})?/blog/[^/?#]+",
    )
    html = b'''<html><body>
      <div class="story-card"><a href="/blog/first-announcement">First official announcement</a>
        <span>Oct 14, 2026</span></div>
      <div class="story-card"><a href="/blog/second-announcement">Second official announcement</a>
        <span>Oct 13, 2026</span></div>
    </body></html>'''
    rows, complete = _parse_page(coinbase, html)
    assert complete is True
    assert {row["title"]: row["date"] for row in rows} == {
        "First official announcement": datetime(2026, 10, 14, tzinfo=timezone.utc),
        "Second official announcement": datetime(2026, 10, 13, tzinfo=timezone.utc),
    }


def test_parser_associates_dates_after_titles_with_their_own_cards():
    coinbase = source(
        source_id="coinbase-blog",
        name="Coinbase",
        source_url="https://www.coinbase.com/blog",
        allowed_hosts=["www.coinbase.com", "coinbase.com"],
        article_path_regex=r"(?:/[a-z]{2}-[a-z]{2})?/blog/[^/?#]+",
    )
    html = b'''<html><body>
      <article><a href="/blog/first-announcement">First official announcement</a>
        <span>Oct 14, 2026</span></article>
      <article><a href="/blog/second-announcement">Second official announcement</a>
        <span>Oct 13, 2026</span></article>
    </body></html>'''
    rows, complete = _parse_page(coinbase, html)
    assert complete is True
    assert {row["title"]: row["date"] for row in rows} == {
        "First official announcement": datetime(2026, 10, 14, tzinfo=timezone.utc),
        "Second official announcement": datetime(2026, 10, 13, tzinfo=timezone.utc),
    }


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
        source_url="https://www.swift.com/about-us/media-centre",
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


def test_citi_source_is_parked_without_network_requests():
    citi = next(item for item in load_discovery_sources() if item["source_id"] == "citi-press-releases")
    http = SequenceHttp([])

    result = InstitutionalPressDiscovery(citi, http=http, now=lambda: STAMP).collect()

    assert citi["enabled"] is False
    assert result.status == "not_configured"
    assert result.candidates == []
    assert http.calls == []


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
    citi = {**citi, "enabled": True}  # Exercise parser behavior while production source is parked.
    result = InstitutionalPressDiscovery(citi, http=http, now=lambda: STAMP).collect()

    assert result.status == "partial"
    assert len(result.candidates) == 1
    assert result.candidates[0].title == (
        "Citi Unveils Citi Commerce Media to Deliver More Personalized Experiences for Customers and Brands"
    )
    assert result.candidates[0].published_at == datetime(2026, 9, 23, tzinfo=timezone.utc)
    assert len(http.calls) == 2
    assert "bounded detail fallback" in " ".join(result.errors)


def test_citi_detail_fallback_can_use_validated_route_in_embedded_page_data():
    source_url = "https://www.citigroup.com/global/news/press-release"
    article_url = "https://www.citigroup.com/global/news/press-release/2026/citi-example"
    index = HttpResponse(
        200, {"content-type": "text/html"},
        b'''<html><body><script type="application/json">{"article":"/global/news/press-release/2026/citi-example"}</script></body></html>''',
        source_url,
    )
    detail = HttpResponse(
        200, {"content-type": "text/html"},
        b"<html><body><h1>Citi Example Press Release</h1><p>September 23, 2026</p></body></html>",
        article_url,
    )
    http = SequenceHttp([index, detail])
    citi = next(item for item in load_discovery_sources() if item["source_id"] == "citi-press-releases")
    citi = {**citi, "enabled": True}  # Exercise parser behavior while production source is parked.
    result = InstitutionalPressDiscovery(citi, http=http, now=lambda: STAMP).collect()

    assert result.status == "partial", result.errors
    assert len(result.candidates) == 1, result.errors
    assert result.candidates[0].url == article_url + "/"
    assert result.candidates[0].title == "Citi Example Press Release"
    assert len(http.calls) == 2


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
    citi = {**citi, "enabled": True}  # Exercise parser behavior while production source is parked.
    result = InstitutionalPressDiscovery(citi, http=http, now=lambda: STAMP).collect()

    assert result.status == "failed"
    assert result.candidates == []
    assert len(http.calls) == 2
    assert http.calls[1][0] == "https://www.citigroup.com/global/news/press-release/2026/citi-example/"
    assert any("lacked grounded title/date metadata" in error for error in result.errors)


def test_institutional_detail_fallback_limit_is_bounded():
    base = source()
    for value in (0, 6, True, "1", None):
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


def test_parser_diagnostics_ignore_embedded_page_data_routes():
    html = b'''<html><body>
      <span>Sep 29, 2026</span>
      <a href="https://www.circle.com/pressroom/visible-release">Visible release</a>
      <script>const oldRoute = "https://www.circle.com/pressroom/embedded-old-release";</script>
    </body></html>'''
    diagnostics = {}
    rows, complete = _parse_page(source(), html, diagnostics)
    assert complete is True
    assert len(rows) == 1
    assert diagnostics["incomplete_native_ids"] == []
    assert diagnostics["incomplete_reasons"] == {}
    assert diagnostics["incomplete_date_proximity"] == {}
    assert diagnostics["conflicting_native_ids"] == []


def test_collect_reports_bounded_sample_of_unparsed_official_routes():
    html = (
        '<html><body><span>Sep 29, 2026</span>'
        '<a href="https://www.circle.com/pressroom/official-article">Official Article</a>'
        + (" " * 1500)
        + '<a href="https://www.circle.com/pressroom/undated-article">Undated Article</a>'
        + '<script>const hidden = "https://www.circle.com/pressroom/old-embedded-route";</script>'
        + '</body></html>'
    ).encode()
    result = InstitutionalPressDiscovery(
        source(), http=FakeHttp(response(html)), now=lambda: STAMP
    ).collect()
    assert result.status == "partial"
    diagnostic = next(error for error in result.errors if "malformed article on official index" in error)
    assert "index_unparsed_routes=1" in diagnostic
    assert "recovered_unparsed_routes=0" in diagnostic
    assert "unresolved_after_fallback=1" in diagnostic
    assert "undated-article" in diagnostic
    assert "incomplete_reasons=" in diagnostic
    assert "'date'" in diagnostic
    assert "date_proximity=" in diagnostic
    assert "nearest_gap" in diagnostic
    assert "conflicting_duplicate_routes=0" in diagnostic


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



def test_historical_mastercard_route_outside_lookback_does_not_mark_index_incomplete():
    mastercard = source(
        source_id="mastercard-press-releases",
        name="Mastercard",
        source_url="https://www.mastercard.com/global/en/news-and-trends/press.html",
        allowed_hosts=["www.mastercard.com", "mastercard.com"],
        article_path_regex=r"/global/en/news-and-trends/press/\d{4}/[^/?#]+/[^/?#]+",
    )
    current_url = "https://www.mastercard.com/global/en/news-and-trends/press/2026/september/current-release.html"
    old_url = "https://www.mastercard.com/global/en/news-and-trends/press/2024/april/old-release.html"
    html = (
        f'<span>September 29, 2026</span><a href="{current_url}">Current Release</a>'
        + (" " * 1600)
        + f'<a href="{old_url}">Old Release</a>'
    ).encode()
    diagnostics = {}
    cutoff = datetime(2026, 4, 18, tzinfo=timezone.utc)

    rows, complete = _parse_page(mastercard, html, diagnostics, lookback_cutoff=cutoff)

    assert complete is True
    assert [row["url"] for row in rows] == [current_url]
    assert diagnostics["incomplete_native_ids"] == []


def test_current_lookback_month_undated_mastercard_route_remains_incomplete():
    mastercard = source(
        source_id="mastercard-press-releases",
        name="Mastercard",
        source_url="https://www.mastercard.com/global/en/news-and-trends/press.html",
        allowed_hosts=["www.mastercard.com", "mastercard.com"],
        article_path_regex=r"/global/en/news-and-trends/press/\d{4}/[^/?#]+/[^/?#]+",
    )
    current_url = "https://www.mastercard.com/global/en/news-and-trends/press/2026/april/undated-release.html"
    html = (
        '<span>September 29, 2026</span>'
        '<a href="https://www.mastercard.com/global/en/news-and-trends/press/2026/september/current-release.html">Current Release</a>'
        + (" " * 1600)
        + f'<a href="{current_url}">Undated Release</a>'
    ).encode()
    diagnostics = {}
    cutoff = datetime(2026, 4, 18, tzinfo=timezone.utc)

    rows, complete = _parse_page(mastercard, html, diagnostics, lookback_cutoff=cutoff)

    assert complete is False
    assert diagnostics["incomplete_native_ids"] == ["/global/en/news-and-trends/press/2026/april/undated-release.html"]


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



def test_coinbase_uses_route_matching_aria_label_for_empty_anchor():
    coinbase_blog = coinbase_html_source()
    article_url = "https://www.coinbase.com/en-us/blog/coinbase-is-building-the-most-complete-trading-platform"
    title = "Coinbase Is Building the Most Complete Trading Platform"
    html = (
        f'<span>September 29, 2026</span><a href="{article_url}" aria-label="{title}"></a>'
    ).encode()

    rows, complete = _parse_page(coinbase_blog, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == title


def test_coinbase_rejects_unrelated_aria_label_as_article_title():
    coinbase_blog = coinbase_html_source()
    article_url = "https://www.coinbase.com/en-us/blog/coinbase-is-building-the-most-complete-trading-platform"
    html = (
        f'<span>September 29, 2026</span><a href="{article_url}" aria-label="Read our blog"></a>'
        + (" " * 3000)
        + '<span>September 28, 2026</span><a href="https://www.coinbase.com/en-us/blog/another-valid-post">Another Valid Post</a>'
    ).encode()
    diagnostics = {}

    rows, complete = _parse_page(coinbase_blog, html, diagnostics)

    assert complete is False
    assert all(row["title"] != "Read our blog" for row in rows)
    assert any(
        native_id.endswith("/blog/coinbase-is-building-the-most-complete-trading-platform")
        for native_id in diagnostics["incomplete_native_ids"]
    )


def test_coinbase_uses_image_alt_when_it_matches_the_official_route_slug():
    coinbase_blog = coinbase_html_source()
    article_url = (
        "https://www.coinbase.com/en-us/blog/"
        "coinbase-is-building-the-most-complete-trading-platform"
    )
    title = "Coinbase Is Building the Most Complete Trading Platform"
    html = (
        f'<span>September 29, 2026</span><a href="{article_url}">'
        f'<img src="/image.jpg" alt="{title}"></a>'
    ).encode()

    rows, complete = _parse_page(coinbase_blog, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == title
    assert rows[0]["url"] == article_url + "/"


def test_coinbase_rejects_unrelated_image_alt_as_article_title():
    coinbase_blog = coinbase_html_source()
    article_url = "https://www.coinbase.com/en-us/blog/coinbase-is-building-the-most-complete-trading-platform"
    html = (
        f'<span>September 29, 2026</span><a href="{article_url}">'
        '<img src="/image.jpg" alt="Read our blog"></a>'
        + (" " * 1600)
        + '<span>September 28, 2026</span><a href="https://www.coinbase.com/en-us/blog/another-valid-post">Another Valid Post</a>'
    ).encode()
    diagnostics = {}

    rows, complete = _parse_page(coinbase_blog, html, diagnostics)

    assert complete is False
    assert all(row["title"] != "Read our blog" for row in rows)
    assert any(
        native_id.endswith("/blog/coinbase-is-building-the-most-complete-trading-platform")
        for native_id in diagnostics["incomplete_native_ids"]
    )


def test_coinbase_heading_selection_is_consistent_across_duplicate_image_links():
    coinbase_blog = coinbase_html_source()
    article_url = "https://www.coinbase.com/en-us/blog/system-update-take-control-of-your-money-with-coinbase"
    html = (
        '<html><body><span>Oct 8, 2026</span>'
        + '<h2>How to contact support about account security</h2>'
        + f'<a href="{article_url}"><img src="/first.png" alt=""></a>'
        + '<h2>System Update: Take Control of Your Money With Coinbase</h2>'
        + f'<a href="{article_url}"><img src="/second.png" alt=""></a>'
        + '</body></html>'
    ).encode()

    rows, complete = _parse_page(coinbase_blog, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "System Update: Take Control of Your Money With Coinbase"
    assert rows[0]["date"] == datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_coinbase_recovers_title_from_nearby_heading_when_link_is_empty():
    coinbase_blog = coinbase_html_source()
    article_url = "https://www.coinbase.com/en-us/blog/example-official-post"
    html = (
        '<html><body><span>Oct 8, 2026</span>'
        + (" " * 250)
        + '<h2>Official Example Post About Institutional Payments</h2>'
        + (" " * 100)
        + f'<a href="{article_url}"><img src="/image.png" alt=""></a>'
        + '</body></html>'
    ).encode()

    rows, complete = _parse_page(coinbase_blog, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Official Example Post About Institutional Payments"
    assert rows[0]["date"] == datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_article_date_association_allows_bounded_generic_card_spacing():
    coinbase_blog = source(
        source_id="coinbase-blog",
        name="Coinbase Blog",
        source_url="https://www.coinbase.com/blog",
        allowed_hosts=["www.coinbase.com"],
        article_path_regex=r"/(?:[a-z]{2}-[a-z]{2}/)?blog/[^/?#]+",
    )
    article_url = "https://www.coinbase.com/en-us/blog/example-official-post"
    html = (
        '<html><body><span>Oct 8, 2026</span>'
        + (" " * 1800)
        + f'<a href="{article_url}">Official Example Post</a>'
        + '</body></html>'
    ).encode()

    rows, complete = _parse_page(coinbase_blog, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Official Example Post"
    assert rows[0]["date"] == datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_date_nearest_to_generic_duplicate_link_is_shared_by_same_article_title_link():
    coinbase_blog = coinbase_html_source()
    article_url = "https://www.coinbase.com/en-us/blog/example-official-post"
    html = (
        '<html><body><span>Oct 8, 2026</span>'
        + f'<a href="{article_url}">Read More</a>'
        + (" " * 1600)
        + f'<a href="{article_url}">Official Example Post</a>'
        + '</body></html>'
    ).encode()

    rows, complete = _parse_page(coinbase_blog, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Official Example Post"
    assert rows[0]["date"] == datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_coinbase_article_shell_can_fall_back_to_nearby_outside_date():
    coinbase_blog = coinbase_html_source()
    article_url = "https://www.coinbase.com/en-us/blog/example-official-post"
    html = (
        '<html><body><span>Oct 8, 2026</span>'
        + (" " * 1800)
        + f'<article><a href="{article_url}">Official Example Post</a></article>'
        + '</body></html>'
    ).encode()

    rows, complete = _parse_page(coinbase_blog, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Official Example Post"
    assert rows[0]["date"] == datetime(2026, 10, 8, tzinfo=timezone.utc)


def test_coinbase_date_uses_slug_matched_heading_only_when_link_association_is_unresolved():
    coinbase_blog = coinbase_html_source()
    article_url = "https://www.coinbase.com/blog/alpha-release-announcement"
    filler_before_heading = " summary detail " * 80
    filler_before_date = " additional product context " * 50
    html = (
        f'<a href="{article_url}"><img alt="Alpha release"></a>'
        + filler_before_heading
        + "<h2>Alpha Release Announcement</h2>"
        + filler_before_date
        + "<span>September 30, 2026</span>"
    ).encode()
    diagnostics = {}
    rows, complete = _parse_page(coinbase_blog, html, diagnostics)

    assert complete is True, {"rows": rows, "diagnostics": diagnostics}
    assert len(rows) == 1
    assert rows[0]["native_id"] == "/blog/alpha-release-announcement"
    assert rows[0]["date"] == datetime(2026, 9, 30, tzinfo=timezone.utc)



def test_coinbase_card_container_assigns_date_to_its_single_official_route():
    coinbase_blog = coinbase_html_source()
    alpha_url = "https://www.coinbase.com/blog/alpha-release-announcement"
    beta_url = "https://www.coinbase.com/blog/beta-release-announcement"
    filler_before_heading = " summary detail " * 80
    filler_before_date = " additional product context " * 50
    alpha_card = (
        f'<div class="card"><a href="{alpha_url}"><img alt="Alpha release"></a>'
        + filler_before_heading
        + "<h2>Alpha Release Announcement</h2>"
        + filler_before_date
        + "<span>September 30, 2026</span></div>"
    )
    beta_card = (
        f'<div class="card"><a href="{beta_url}"><img alt="Beta release"></a>'
        + filler_before_heading
        + "<h2>Beta Release Announcement</h2>"
        + filler_before_date
        + "<span>September 29, 2026</span></div>"
    )
    diagnostics = {}
    rows, complete = _parse_page(
        coinbase_blog, ("<html><body>" + alpha_card + beta_card + "</body></html>").encode(), diagnostics
    )

    assert diagnostics["coinbase_container_diagnostics"]["owned_date_spans"] == 2
    assert complete is True
    by_id = {row["native_id"]: row for row in rows}
    assert set(by_id) == {"/blog/alpha-release-announcement", "/blog/beta-release-announcement"}
    assert by_id["/blog/alpha-release-announcement"]["date"] == datetime(2026, 9, 30, tzinfo=timezone.utc)
    assert by_id["/blog/beta-release-announcement"]["date"] == datetime(2026, 9, 29, tzinfo=timezone.utc)


def test_coinbase_blog_rejects_landing_index_as_article_route():
    coinbase_blog = coinbase_html_source()
    assert _official_article(coinbase_blog, "https://www.coinbase.com/blog/landing") is None
    assert _official_article(coinbase_blog, "https://www.coinbase.com/blog/landing/") is None
    assert _official_article(
        coinbase_blog,
        "https://www.coinbase.com/en-us/blog/coinbase-is-building-the-most-complete-trading-platform",
    ) is not None


def test_coinbase_blog_accepts_current_localized_article_route():
    coinbase_blog = coinbase_html_source()
    assert coinbase_blog["source_url"] == "https://www.coinbase.com/blog"

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



def test_coinbase_investor_news_is_parked_until_static_article_data_is_accessible():
    investor = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "coinbase-investor-news"
    )
    assert investor["enabled"] is False

    http = SequenceHttp([])
    result = InstitutionalPressDiscovery(investor, http=http, now=lambda: STAMP).collect()

    assert result.status == "not_configured"
    assert result.candidates == []
    assert http.calls == []


def test_jpmorgan_newsroom_recovers_dated_article_with_bounded_fallback():
    article_url = "https://www.jpmorgan.com/payments/newsroom/cross-border-payments-thunes-expansion"
    index_url = "https://www.jpmorgan.com/payments/newsroom"
    index = HttpResponse(
        200, {"content-type": "text/html"},
        f'<html><body><a href={article_url}>Read More</a><p>Loading</p></body></html>'.encode(),
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
    assert swift["source_url"] == "https://www.swift.com/about-us/media-centre/press-releases"
    assert swift["enabled"] is False
    html = b"""<html><body>
    <span>28 September 2026</span>
    <a href="https://www.swift.com/about-us/media-centre/press-releases/swift-and-its-community-innovate-bring-ease-of-domestic-consumer-payments-cross-border-transaction-experience">Swift and its community innovate to bring ease of domestic consumer payments to cross-border transaction experience</a>
    </body></html>"""

    rows, complete = _parse_page(swift, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["title"] == "Swift and its community innovate to bring ease of domestic consumer payments to cross-border transaction experience"
    assert rows[0]["date"] == datetime(2026, 9, 28, tzinfo=timezone.utc)
    assert rows[0]["url"] == (
        "https://www.swift.com/about-us/media-centre/press-releases/"
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



def test_detail_fallback_resolves_conflicting_duplicate_article_rows():
    article_url = (
        "https://www.mastercard.com/global/en/news-and-trends/press/2026/july/"
        "mastercard-expands-virtual-card-platform.html"
    )
    index = HttpResponse(
        200, {"content-type": "text/html", "etag": '"index-v1"'},
        (
            f'<span>July 9, 2026</span><a href="{article_url}">Mastercard Expands Virtual Card Platform</a>'
            f'<span>July 10, 2026</span><a href="{article_url}">Unrelated Anchor Label</a>'
        ).encode(),
        "https://www.mastercard.com/global/en/news-and-trends/press.html",
    )
    detail = HttpResponse(
        200, {"content-type": "text/html"},
        b'<html><body><h1>Mastercard Expands Virtual Card Platform</h1>'
        b'<time datetime="2026-07-09T12:00:00Z">July 9, 2026</time></body></html>',
        article_url,
    )
    mastercard = source(
        source_id="mastercard-press-releases",
        name="Mastercard",
        source_url="https://www.mastercard.com/global/en/news-and-trends/press.html",
        allowed_hosts=["www.mastercard.com", "mastercard.com"],
        article_path_regex=r"/global/en/news-and-trends/press/\d{4}/[^/?#]+/[^/?#]+",
        detail_fallback_limit=1,
    )
    http = SequenceHttp([index, detail])

    result = InstitutionalPressDiscovery(mastercard, http=http, now=lambda: STAMP).collect()

    assert result.status == "success"
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "Mastercard Expands Virtual Card Platform"
    assert result.candidates[0].published_at == datetime(2026, 7, 9, 12, tzinfo=timezone.utc)
    assert not any("conflicting duplicate article" in error for error in result.errors)
    assert len(http.calls) == 2
    assert http.calls[1][0] == article_url


def test_partial_index_recovers_one_missing_official_card_without_claiming_full_health(capsys):
    index_url = "https://www.circle.com/pressroom"
    known_url = "https://www.circle.com/pressroom/known-good-release"
    missing_url = "https://www.circle.com/pressroom/missing-date-release"
    unresolved_url = "https://www.circle.com/pressroom/second-missing-date-release"
    index = HttpResponse(
        200, {"content-type": "text/html"},
        (
            f"""<html><body>
            <span>September 28, 2026</span><a href="{known_url}">Circle Known Good Release</a>
            <a href="{missing_url}">Read More</a>"""
            + (" " * 3000)
            + f'<a href="{unresolved_url}">Second Missing Date Release</a></body></html>'
        ).encode(),
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
    diagnostic = next(error for error in result.errors if "malformed article on official index" in error)
    assert "index_unparsed_routes=2" in diagnostic
    assert "recovered_unparsed_routes=1" in diagnostic
    assert "unresolved_after_fallback=1" in diagnostic
    assert "second-missing-date-release" in diagnostic
    captured = capsys.readouterr().out
    assert "Source index diagnostics: circle-pressroom" in captured
    assert "official_links_in_article_blocks=" in captured
    assert "script_date_tokens=" in captured
    assert "script_iso_date_strings=" in captured
    assert "json_data_key_counts=" in captured



def test_detail_fallback_marks_source_healthy_when_all_incomplete_routes_are_recovered():
    index_url = "https://www.circle.com/pressroom"
    known_url = "https://www.circle.com/pressroom/known-good-release"
    missing_url = "https://www.circle.com/pressroom/missing-date-release"
    index = HttpResponse(
        200, {"content-type": "text/html", "etag": '"index-v1"'},
        (
            f'<span>September 28, 2026</span><a href="{known_url}">Circle Known Good Release</a>'
            f'<a href="{missing_url}">Read More</a>'
        ).encode(), index_url,
    )
    detail = HttpResponse(
        200, {"content-type": "text/html"},
        b"<html><body><h1>Circle Missing-Date Release</h1><p>September 27, 2026</p></body></html>",
        missing_url + "/",
    )
    http = SequenceHttp([index, detail])

    result = InstitutionalPressDiscovery(
        source(detail_fallback_limit=1), http=http, now=lambda: STAMP
    ).collect()

    assert result.status == "success"
    assert len(result.candidates) == 2
    assert {item.title for item in result.candidates} == {
        "Circle Known Good Release", "Circle Missing-Date Release"
    }
    assert result.errors == []
    assert len(http.calls) == 2
    assert http.calls[1][0] == missing_url + "/"


def test_detail_fallback_reports_when_index_has_no_allowlisted_article_links():
    index_url = "https://www.circle.com/pressroom"
    index = HttpResponse(
        200, {"content-type": "text/html"},
        b'<html><body><a href="/about">About</a><a href="/company">Company</a></body></html>',
        index_url,
    )
    http = SequenceHttp([index])

    result = InstitutionalPressDiscovery(
        source(detail_fallback_limit=1), http=http, now=lambda: STAMP
    ).collect()

    assert result.status == "failed"
    assert result.candidates == []
    assert len(http.calls) == 1
    assert any("bounded detail fallback found no allowlisted official article links" in error
               for error in result.errors)
    diagnostic = next(error for error in result.errors if "index diagnostics:" in error)
    assert "content_type='text/html'" in diagnostic
    assert "bytes=" in diagnostic
    assert "allowlisted_article_links=0" in diagnostic
    assert "json_ld_scripts=0" in diagnostic
    assert "date_tokens_near_article_links=" in diagnostic
    assert "articles_with_nearby_date_tokens=" in diagnostic
    assert "script_tags=" in diagnostic
    assert "json_data_scripts=" in diagnostic
    assert "script_date_tokens=" in diagnostic
    assert "script_bytes=" in diagnostic
    assert "date_tokens_within_2500=" in diagnostic
    assert "articles_within_5000=" in diagnostic
    assert "date_tokens_within_10000=" in diagnostic
    assert "median_nearest_date_distance=" in diagnostic
    assert "parseable_date_tokens=" in diagnostic
    assert "parseable_date_tokens_within_2500=" in diagnostic
    assert "official_links_with_text=" in diagnostic
    assert "official_links_with_generic_text=" in diagnostic
    assert "official_links_in_article_blocks=" in diagnostic
    assert "official_links_without_text=" in diagnostic
    assert "heading_tags=" in diagnostic
    assert "heading_titles_with_text=" in diagnostic
    assert "https://" not in diagnostic


@pytest.mark.parametrize(
    ("source_id", "article_url", "title", "date"),
    [
        (
            "imf-blogs",
            "https://www.imf.org/en/blogs/articles/2026/07/02/tokenization-can-change-the-worlds-financial-architecture/",
            "Tokenization Can Change The World's Financial Architecture",
            datetime(2026, 7, 2, tzinfo=timezone.utc),
        ),
        (
            "imf-global-financial-stability-report",
            "https://www.imf.org/en/publications/gfsr/issues/2026/10/13/global-financial-stability-report-october-2026/",
            "Global Financial Stability Report — Resilience, Risk, and Transformation",
            datetime(2026, 10, 13, tzinfo=timezone.utc),
        ),
    ],
)
def test_enabled_imf_sources_parse_official_dated_article_routes(source_id, article_url, title, date):
    imf_source = next(
        item for item in load_discovery_sources()
        if item["source_id"] == source_id
    )
    # The source is parked in production; force-enable only for deterministic parser-fixture coverage.
    imf_source = {**imf_source, "enabled": True}
    html = (
        f'<html><body><span>{date.strftime("%B")} {date.day}, {date.year}</span>'
        f'<a href="{article_url}">{title}</a></body></html>'
    ).encode()

    rows, complete = _parse_page(imf_source, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["url"] == article_url
    assert rows[0]["title"] == title
    assert rows[0]["date"] == date


def test_imf_sources_are_parked_without_network_requests():
    source_ids = {
        "imf-blogs", "imf-global-financial-stability-report", "imf-media-center-news"
    }
    for imf_source in load_discovery_sources():
        if imf_source["source_id"] not in source_ids:
            continue
        http = SequenceHttp([])
        result = InstitutionalPressDiscovery(imf_source, http=http, now=lambda: STAMP).collect()
        assert result.status == "not_configured"
        assert result.candidates == []
        assert http.calls == []


def test_imf_media_center_gfsr_source_uses_official_dated_news_routes():
    imf_source = next(
        item for item in load_discovery_sources()
        if item["source_id"] == "imf-media-center-news"
    )
    assert imf_source["enabled"] is False
    assert imf_source["source_url"] == "https://mediacenter.imf.org/newslisting"
    article_url = (
        "https://mediacenter.imf.org/news/"
        "imf---global-financial-stability-report-annual-meetings--2026/"
        "s/f55368c1-983b-45b7-ba7c-1978f67c168c"
    )
    html = (
        '<html><body><span>October 13, 2026</span>'
        f'<a href="{article_url}">IMF / Global Financial Stability Report Annual Meetings, 2026</a>'
        '</body></html>'
    ).encode()

    rows, complete = _parse_page(imf_source, html)

    assert complete is True
    assert len(rows) == 1
    assert rows[0]["url"] == article_url + "/"
    assert rows[0]["title"] == "IMF / Global Financial Stability Report Annual Meetings, 2026"
    assert rows[0]["date"] == datetime(2026, 10, 13, tzinfo=timezone.utc)





def test_coinbase_unresolved_date_diagnostic_reports_date_owner_without_mutating_parse():
    coinbase_blog = coinbase_html_source()
    known_url = "https://www.coinbase.com/blog/known-official-article"
    unresolved_url = "https://www.coinbase.com/blog/coinbase-opens-stock-trading-to-everyone-in-the-us"
    html = (
        f'<div class="card"><span>October 15, 2026</span><a href="{known_url}">Known Official Article</a></div>'
        + (" " * 300)
        + '<h2>Coinbase Opens Stock Trading to Everyone in the US</h2>'
        + f'<div class="card"><a href="{unresolved_url}">Read More</a></div>'
    ).encode()
    diagnostics = {}
    rows, complete = _parse_page(coinbase_blog, html, diagnostics)
    assert complete is False
    assert any(row["native_id"] == "/blog/known-official-article" for row in rows)
    unresolved_id = "/blog/coinbase-opens-stock-trading-to-everyone-in-the-us"
    assert unresolved_id in diagnostics["incomplete_date_assignment_context"]
    context = diagnostics["incomplete_date_assignment_context"][unresolved_id]
    assert context["global_date_owners"] == [unresolved_id] or context["global_date_owners"] == ["/blog/known-official-article"]
    assert "card_date_owners" in context
    assert "nearest_heading_gap" in context
