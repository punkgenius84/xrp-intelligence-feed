import json
from pathlib import Path
from collectors.rss import RSSCollector
from intelligence.deduplication import deduplicate
from storage.database import JsonState

def load_sources() -> list[dict]:
    return json.loads(Path("config/sources.json").read_text(encoding="utf-8")).get("feeds", [])

def main() -> None:
    state = JsonState()
    seen = state.load()
    items = []
    for source in load_sources():
        items.extend(RSSCollector(source["name"], source["url"], source.get("source_type", "discovery")).collect())
    fresh, seen = deduplicate(items, seen)
    state.save(seen)
    print(f"Collected: {len(items)} | New: {len(fresh)}")

if __name__ == "__main__": main()
