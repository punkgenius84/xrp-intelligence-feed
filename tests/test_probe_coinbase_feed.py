from scripts.probe_coinbase_feed import _is_coinbase_https_url, inspect_feed


def test_recognizes_rss_feed_with_publication_dates():
    result = inspect_feed(
        b'<rss version="2.0"><channel><item><title>Story</title><pubDate>Sun, 18 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>',
        "application/rss+xml; charset=utf-8",
    )
    assert result == {"valid": True, "reason": "rss", "items": 1, "dated_items": 1}


def test_recognizes_atom_feed():
    result = inspect_feed(
        b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Story</title><updated>2026-10-18T10:00:00Z</updated></entry></feed>',
        "application/atom+xml",
    )
    assert result == {"valid": True, "reason": "feed", "items": 1, "dated_items": 1}


def test_rejects_html_even_when_it_is_well_formed_xml():
    result = inspect_feed(b'<html><body>blocked</body></html>', "text/html")
    assert result["valid"] is False
    assert result["reason"] == "unexpected_xml_root"


def test_rejects_generic_xml_and_malformed_responses():
    assert inspect_feed(b'<error>denied</error>', "application/xml")["valid"] is False
    assert inspect_feed(b'<rss><channel>', "application/rss+xml")["reason"] == "invalid_xml"


def test_rejects_oversized_payload():
    assert inspect_feed(b" " * 1_000_001, "application/rss+xml")["reason"] == "body_too_large"


def test_probe_urls_are_restricted_to_coinbase_https_hosts():
    assert _is_coinbase_https_url("https://www.coinbase.com/blog/feed")
    assert _is_coinbase_https_url("https://coinbase.com/blog/feed")
    assert not _is_coinbase_https_url("http://www.coinbase.com/blog/feed")
    assert not _is_coinbase_https_url("https://coinbase.com.attacker.example/blog/feed")
    assert not _is_coinbase_https_url("https://evil.example/blog/feed")
    assert not _is_coinbase_https_url("https://user:pass@coinbase.com/blog/feed")
