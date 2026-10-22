from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.probe_coinbase_landing_index import probe_landing_index
from scripts.probe_coinbase_product_index import probe_product_index

# Exact unresolved route tracked by issue #264. Keep the Thunes article as a
# separate current canary; do not let its date or index presence stand in for this route.
YAHOO_TARGET_PATH = (
    "/blog/coinbase-opens-stock-trading-to-everyone-in-the-us-"
    "partners-with-yahoo-finance-to-power-discovery"
)


def main() -> None:
    print(f"Coinbase Yahoo Finance route diagnostic: target_path={YAHOO_TARGET_PATH}")
    print(f"dated_index target={YAHOO_TARGET_PATH} {probe_landing_index(target_path=YAHOO_TARGET_PATH)}")
    print(f"product_index target={YAHOO_TARGET_PATH} {probe_product_index(target_path=YAHOO_TARGET_PATH)}")


if __name__ == "__main__":
    main()
