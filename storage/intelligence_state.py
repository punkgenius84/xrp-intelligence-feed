from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_EVENTS = 500


class IntelligenceStateError(RuntimeError):
    pass


def _validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION or not isinstance(value.get("events"), dict):
        raise IntelligenceStateError("invalid intelligence state schema")
    for key, event in value["events"].items():
        if not isinstance(key, str) or not isinstance(event, dict):
            raise IntelligenceStateError("invalid intelligence event record")
    return value


class JsonIntelligenceState:
    def __init__(self, path: str | Path = "state/intelligence.json"):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "events": {}}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                return _validate(json.load(handle))
        except (OSError, json.JSONDecodeError, IntelligenceStateError) as exc:
            raise IntelligenceStateError(f"cannot load intelligence state: {exc}") from exc

    def save(self, state: dict[str, Any]) -> None:
        _validate(state)
        events = state["events"]
        if len(events) > MAX_EVENTS:
            ordered = sorted(events.items(), key=lambda pair: str(pair[1].get("updated_at", "")), reverse=True)[:MAX_EVENTS]
            state["events"] = dict(sorted(ordered, key=lambda pair: pair[0]))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\\n"
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except OSError as exc:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise IntelligenceStateError(f"cannot save intelligence state: {exc}") from exc
