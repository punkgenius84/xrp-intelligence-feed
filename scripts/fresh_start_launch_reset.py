from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from storage.correlation_state import JsonCorrelationState, empty_correlation_state
from storage.database import JsonState
from storage.delivery_history import JsonDeliveryHistory
from storage.discovery_state import JsonDiscoveryState, empty_discovery_state
from storage.intelligence_delivery_history import JsonIntelligenceDeliveryHistory
from storage.intelligence_outbox import JsonIntelligenceOutboxState
from storage.intelligence_quarantine import JsonIntelligenceQuarantineState
from storage.intelligence_state import JsonIntelligenceState
from storage.outbox import JsonOutboxState

CONFIRMATION = "FRESH-START-XRP-INTELLIGENCE"
SCHEMA_VERSION = 1
OPERATIONAL_FILES = (
    "seen.json",
    "discovery.json",
    "correlation.json",
    "intelligence.json",
    "intelligence_outbox.json",
    "outbox.json",
    "delivery_history.json",
    "intelligence_delivery_history.json",
)
ARCHIVE_FILES = OPERATIONAL_FILES + ("intelligence_quarantine.json",)


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError as exc:
        raise RuntimeError(f"could not atomically write {path.name}: {exc}") from exc


def inspect_state(state_dir: str | Path = "state") -> dict[str, int]:
    root = Path(state_dir)
    seen = JsonState(root / "seen.json").load()
    discovery = JsonDiscoveryState(root / "discovery.json").load()
    correlation = JsonCorrelationState(root / "correlation.json").load()
    intelligence = JsonIntelligenceState(root / "intelligence.json").load()
    normal_outbox = JsonOutboxState(root / "outbox.json").load()
    normal_history = JsonDeliveryHistory(root / "delivery_history.json").load()
    intelligence_outbox = JsonIntelligenceOutboxState(root / "intelligence_outbox.json").load()
    intelligence_history = JsonIntelligenceDeliveryHistory(
        root / "intelligence_delivery_history.json"
    ).load()
    quarantine = JsonIntelligenceQuarantineState(root / "intelligence_quarantine.json").load()
    return {
        "seen_fingerprints": len(seen),
        "discovery_sources": len(discovery["sources"]),
        "discovery_candidates": len(discovery["candidates"]),
        "correlation_cards": len(correlation["cards"]),
        "intelligence_events": len(intelligence["events"]),
        "normal_outbox": len(normal_outbox),
        "normal_delivery_history": len(normal_history),
        "intelligence_outbox": len(intelligence_outbox),
        "intelligence_delivery_history": len(intelligence_history),
        "quarantine_audit_records": len(quarantine),
    }


def _archive_state(state_dir: Path, archive_path: Path, *, now: datetime) -> dict:
    if archive_path.exists():
        raise FileExistsError(
            f"archive already exists at {archive_path}; refusing to overwrite an audit copy"
        )
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "created_at": now.astimezone(timezone.utc).isoformat(),
        "repository_state_reset": True,
        "files": [],
        "note": "Pre-reset state archive. Secrets/webhook settings are not included.",
    }
    with ZipFile(archive_path, "x", compression=ZIP_DEFLATED) as archive:
        for name in ARCHIVE_FILES:
            path = state_dir / name
            if not path.exists():
                continue
            data = path.read_bytes()
            archive.write(path, arcname=f"state/{name}")
            manifest["files"].append({
                "path": f"state/{name}",
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
        archive.writestr(
            "manifest.json",
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )
    return manifest


def _verify_archive(archive_path: Path) -> None:
    """Verify ZIP integrity and every archived state file before any reset writes."""
    try:
        with ZipFile(archive_path, "r") as archive:
            corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise RuntimeError(f"archive CRC check failed for {corrupt_member!r}")
            manifest = json.loads(archive.read("manifest.json"))
            entries = manifest.get("files") if isinstance(manifest, dict) else None
            if manifest.get("repository_state_reset") is not True or not isinstance(entries, list):
                raise RuntimeError("archive manifest is missing required reset metadata")
            listed_paths: set[str] = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    raise RuntimeError("archive manifest contains an invalid file entry")
                name = entry.get("path")
                expected_bytes = entry.get("bytes")
                expected_sha256 = entry.get("sha256")
                if (
                    not isinstance(name, str)
                    or not name.startswith("state/")
                    or name in listed_paths
                    or type(expected_bytes) is not int
                    or not isinstance(expected_sha256, str)
                ):
                    raise RuntimeError("archive manifest contains invalid file metadata")
                data = archive.read(name)
                if len(data) != expected_bytes:
                    raise RuntimeError(f"archive size mismatch for {name!r}")
                if hashlib.sha256(data).hexdigest() != expected_sha256:
                    raise RuntimeError(f"archive SHA-256 mismatch for {name!r}")
                listed_paths.add(name)
            actual_paths = {name for name in archive.namelist() if name.startswith("state/")}
            if listed_paths != actual_paths:
                raise RuntimeError("archive manifest does not match the archived state files")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"pre-reset archive verification failed: {exc}") from exc


def fresh_start_reset(
    *,
    state_dir: str | Path = "state",
    archive_path: str | Path = "artifacts/fresh-start-state-archive.zip",
    apply: bool = False,
    confirmation: str | None = None,
    now: datetime | None = None,
) -> dict[str, int | bool | str]:
    """Preview or archive and reset active feed state; never publishes to Discord."""
    root = Path(state_dir)
    before = inspect_state(root)
    result: dict[str, int | bool | str] = {
        **before,
        "applied": False,
        "archive_path": str(archive_path),
    }
    if not apply:
        return result
    if confirmation != CONFIRMATION:
        raise ValueError(f"apply requires the exact confirmation {CONFIRMATION}")
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    archive = Path(archive_path)
    _archive_state(root, archive, now=stamp)
    # Fail closed: never clear state unless the complete audit archive can be
    # reopened, passes ZIP CRC checks, and matches its manifest hashes.
    _verify_archive(archive)

    # Clear operational memory and both normal/intelligence delivery ledgers.
    # The quarantine store remains intact as an audit-only record.
    JsonState(root / "seen.json").save(set())
    JsonDiscoveryState(root / "discovery.json").save(empty_discovery_state())
    JsonCorrelationState(root / "correlation.json").save(empty_correlation_state())
    JsonIntelligenceState(root / "intelligence.json").save(
        {"schema_version": 2, "events": {}}
    )
    JsonIntelligenceOutboxState(root / "intelligence_outbox.json").save([])
    JsonOutboxState(root / "outbox.json").save([])
    JsonDeliveryHistory(root / "delivery_history.json").save([], now=stamp)
    _write_json_atomic(
        root / "intelligence_delivery_history.json",
        {"schema_version": SCHEMA_VERSION, "items": []},
    )

    after = inspect_state(root)
    operational_remaining = sum(after[key] for key in (
        "seen_fingerprints",
        "discovery_sources",
        "discovery_candidates",
        "correlation_cards",
        "intelligence_events",
        "normal_outbox",
        "normal_delivery_history",
        "intelligence_outbox",
        "intelligence_delivery_history",
    ))
    if operational_remaining:
        raise RuntimeError(
            f"fresh-start reset verification failed; {operational_remaining} operational records remain"
        )
    if after["quarantine_audit_records"] != before["quarantine_audit_records"]:
        raise RuntimeError("quarantine audit records changed during reset")
    result.update(after)
    result["applied"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or perform a full fresh-start reset of active feed state. "
            "Apply mode archives state first, clears operational dedup/delivery memory, "
            "preserves quarantine audit records, and never publishes to Discord."
        )
    )
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--archive", default="artifacts/fresh-start-state-archive.zip")
    parser.add_argument("--apply", action="store_true", help="Apply the reset; preview is the default.")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--verify-empty", action="store_true")
    args = parser.parse_args()

    try:
        if args.verify_empty:
            counts = inspect_state(args.state_dir)
            remaining = sum(counts[key] for key in (
                "seen_fingerprints", "discovery_sources", "discovery_candidates",
                "correlation_cards", "intelligence_events", "normal_outbox",
                "normal_delivery_history", "intelligence_outbox",
                "intelligence_delivery_history",
            ))
            if remaining:
                raise RuntimeError(f"{remaining} operational records remain")
            print(
                "Fresh-start verification passed: operational state is empty; "
                f"quarantine_audit_records={counts['quarantine_audit_records']}"
            )
            return 0

        result = fresh_start_reset(
            state_dir=args.state_dir,
            archive_path=args.archive,
            apply=args.apply,
            confirmation=args.confirm,
        )
    except Exception as exc:
        print(f"Fresh-start reset refused/failed: {exc}", file=sys.stderr)
        return 2

    print(
        "Fresh-start state: "
        + " ".join(f"{key}={value}" for key, value in result.items())
    )
    if args.apply:
        print(
            "Reset applied. State archive created before mutation; operational state is empty. "
            "Quarantine was preserved for audit. No Discord post attempted."
        )
    else:
        print("Preview only: no state changed and no Discord post attempted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
