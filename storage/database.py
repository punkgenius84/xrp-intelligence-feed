import json
from pathlib import Path

class JsonState:
    def __init__(self, path: str = "state/seen.json"):
        self.path = Path(path)

    def load(self) -> set[str]:
        if not self.path.exists(): return set()
        try: return set(json.loads(self.path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError): return set()

    def save(self, fingerprints: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(sorted(fingerprints)[-5000:], indent=2), encoding="utf-8")
