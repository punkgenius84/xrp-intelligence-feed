from urllib.parse import urlsplit

from scripts.probe_coinbase_article_metadata import TARGET_PATH as ARTICLE_PATH, URL as ARTICLE_URL
from scripts.probe_coinbase_blog_rss import TARGET_PATH as RSS_PATH
from scripts.probe_coinbase_landing_index import TARGET_PATH as LANDING_PATH
from scripts.probe_coinbase_product_index import TARGET_PATH as PRODUCT_PATH


# Keep the diagnostic canary recent enough to test current article discovery.
# Verified on Coinbase's official blog: "Coinbase and Thunes connect stablecoin infrastructure with global fiat payouts" (Oct 19, 2026).
CURRENT_CANARY_PATH = "/blog/coinbase-and-thunes-connect-stablecoin-infrastructure"


def test_coinbase_diagnostics_share_current_official_article_canary():
    assert {ARTICLE_PATH, RSS_PATH, LANDING_PATH, PRODUCT_PATH} == {CURRENT_CANARY_PATH}
    assert urlsplit(ARTICLE_URL).path == CURRENT_CANARY_PATH
