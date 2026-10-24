from scripts.probe_asian_central_bank_feeds import (
    SCRIPT_ASSET_PROBE_LABELS,
    TARGETS,
    _candidate_api_routes,
    _page_metadata,
    _probe_sgpc_listing_api,
    _route_context,
    _same_host_script_urls,
)


def test_probe_targets_known_official_bloom_and_thai_stablecoin_pages():
    targets = {label: (url, host) for label, url, host in TARGETS}
    assert targets["MAS BLOOM initiative"] == (
        "https://www.mas.gov.sg/schemes-and-initiatives/BLOOM",
        "www.mas.gov.sg",
    )
    assert targets["MAS media release"][1] == "www.mas.gov.sg"
    assert targets["BOT stablecoin hearing"] == (
        "https://www.bot.or.th/th/laws-and-rules/public-hearing/"
        "public-hearing-20261020-Stablecoin.html",
        "www.bot.or.th",
    )



def test_probe_inspects_official_bot_news_and_hearing_scripts_for_api_routes():
    targets = {label: (url, host) for label, url, host in TARGETS}
    assert targets["BOT English news"] == (
        "https://www.bot.or.th/en/news-and-media/news.html",
        "www.bot.or.th",
    )
    assert targets["BOT English hearings"] == (
        "https://www.bot.or.th/en/laws-and-rules/public-hearing.html",
        "www.bot.or.th",
    )
    assert targets["BOT Thai hearings"] == (
        "https://www.bot.or.th/th/laws-and-rules/public-hearing.html",
        "www.bot.or.th",
    )
    assert {
        "BOT English news",
        "BOT English hearings",
        "BOT Thai hearings",
    }.issubset(SCRIPT_ASSET_PROBE_LABELS)


def test_page_metadata_reports_article_owned_dates_and_same_host_canonical():
    html = """
    <html><head>
      <title>BLOOM announcement</title>
      <link rel="canonical" href="https://www.mas.gov.sg/news/media-releases/2026/bloom">
      <meta property="og:title" content="BLOOM announcement">
      <meta property="article:published_time" content="2026-10-23T00:00:00+08:00">
      <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"NewsArticle",
         "headline":"BLOOM announcement","datePublished":"2026-10-23T00:00:00+08:00"}
      </script>
    </head><body><time datetime="2026-10-23T00:00:00+08:00">23 October 2026</time></body></html>
    """
    metadata = _page_metadata(
        html, "https://www.mas.gov.sg/news/media-releases/2026/bloom", "www.mas.gov.sg"
    )
    assert metadata["canonical"] == [
        "https://www.mas.gov.sg/news/media-releases/2026/bloom"
    ]
    assert metadata["article:published_time"] == ["2026-10-23T00:00:00+08:00"]
    assert metadata["jsonld.datePublished"] == ["2026-10-23T00:00:00+08:00"]
    assert metadata["jsonld.headline"] == ["BLOOM announcement"]
    assert metadata["time.datetime"] == ["2026-10-23T00:00:00+08:00"]


def test_page_metadata_rejects_external_canonical_and_deduplicates_values():
    html = """
    <html><head>
      <link rel="canonical" href="https://example.invalid/bloom">
      <meta property="og:title" content="BLOOM announcement">
      <meta name="twitter:title" content="BLOOM announcement">
    </head></html>
    """
    metadata = _page_metadata(html, "https://www.mas.gov.sg/news", "www.mas.gov.sg")
    assert "canonical" not in metadata
    assert metadata["og:title"] == ["BLOOM announcement"]
    assert metadata["twitter:title"] == ["BLOOM announcement"]



def test_probe_includes_singapore_government_press_centre_routes():
    targets = {label: (url, host) for label, url, host in TARGETS}
    assert targets["SG Press Centre root"] == (
        "https://www.sgpc.gov.sg/",
        "www.sgpc.gov.sg",
    )
    assert targets["SG Press Centre releases page"] == (
        "https://www.sgpc.gov.sg/home?Page=1",
        "www.sgpc.gov.sg",
    )


def test_same_host_script_discovery_rejects_external_assets_and_caps_results():
    html = """
    <script src="/ResourcePackages/site/assets/js/jquery.min.js"></script>
    <script src="/ResourcePackages/site/assets/js/app.js"></script>
    <script src="https://www.mas.gov.sg/static/site.mjs"></script>
    <script src="https://cdn.example.invalid/app.js"></script>
    <script src="/assets/ignored.js"></script>
    """
    found = _same_host_script_urls(
        html, "https://www.mas.gov.sg/news?page=1", "www.mas.gov.sg", limit=2
    )
    assert found == [
        "https://www.mas.gov.sg/ResourcePackages/site/assets/js/app.js",
        "https://www.mas.gov.sg/static/site.mjs",
    ]


def test_candidate_api_routes_extracts_routes_without_requesting_them():
    script = r'''
      const searchPath = "/api/v1/search?section=news";
      const contentPath = "\/api/v2/releases";
      const graphqlPath = "/graphql";
      const asset = "/assets/app.js";
      const absolute = "https://third-party.invalid/api/data";
    '''
    routes = _candidate_api_routes(script)
    assert "/api/v1/search?section=news" in routes
    assert "/api/v2/releases" in routes
    assert "/graphql" in routes
    assert all(not route.startswith("https://") for route in routes)
    assert "/assets/app.js" not in routes



def test_route_context_reports_bounded_call_site_without_fetching_route():
    script = 'fetch("/api/ApiMainListing/SubmitData", { method: "POST", body: payload })'
    context = _route_context(script, "/api/ApiMainListing/SubmitData", radius=25)
    assert "/api/ApiMainListing/SubmitData" in context
    assert 'method: "POST"' in context
    assert len(context) <= 1500
    assert _route_context(script, "/api/missing") == ""



def test_sgpc_listing_api_probe_validates_json_shape_and_reports_dated_items(capsys):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://www.sgpc.gov.sg/api/ApiMainListing/SubmitData"
        content = (
            b'{"result":{"items":[{"Title":"BLOOM announcement",'
            b'"PublishDate":"23-Oct-2026","Url":"/news/media-releases/2026/bloom",'
            b'"Agency":"MAS"}]}}'
        )

    class Client:
        def get(self, url, *, expected_content_types):
            assert url.startswith("https://www.sgpc.gov.sg/api/ApiMainListing/SubmitData?")
            assert "StartDate=" in url and "EndDate=" in url and "page=1" in url
            assert expected_content_types == ("application/json", "text/json")
            return Response()

    _probe_sgpc_listing_api(Client())
    output = capsys.readouterr().out
    assert "sgpc_listing_api=valid http=200 items=1" in output
    assert "BLOOM announcement" in output
    assert "23-Oct-2026" in output


def test_sgpc_listing_api_probe_rejects_redirect_to_unrelated_host(capsys):
    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        url = "https://example.invalid/api/ApiMainListing/SubmitData"
        content = b'{"result":{"items":[]}}'

    class Client:
        def get(self, url, *, expected_content_types):
            return Response()

    _probe_sgpc_listing_api(Client())
    assert "sgpc_listing_api=rejected unexpected_final_host=example.invalid" in capsys.readouterr().out
