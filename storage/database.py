import json
import os
from pathlib import Path

MAX_STATE_ENTRIES = 25000


class StateFileError(RuntimeError):
    """Raised when an existing deduplication state file cannot be trusted."""


class JsonState:
    def __init__(self, path: str = "state/seen.json"):
        self.path = Path(path)

    def _read_existing(self) -> list[str] | None:
        try:
            content = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StateFileError(f"Cannot read deduplication state {self.path}: {exc}") from exc
        except UnicodeError as exc:
            raise StateFileError(f"Deduplication state {self.path} is not valid UTF-8: {exc}") from exc
        try:
            value = json.loads(content)
        except json.JSONDecodeError as exc:
            raise StateFileError(f"Deduplication state {self.path} contains malformed JSON: {exc}") from exc
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise StateFileError(
                f"Deduplication state {self.path} has an unexpected shape; expected a JSON list of strings"
            )
        return value

    def load(self) -> set[str]:
        value = self._read_existing()
        return set(value or ())

    def save(self, fingerprints: set[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous = self._read_existing() or []
        # Preserve prior order; append new keys in sorted order for deterministic recency.
        ordered = list(dict.fromkeys(item for item in previous if item in fingerprints))
        ordered.extend(sorted(fingerprints.difference(ordered)))
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(ordered[-MAX_STATE_ENTRIES:], indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
