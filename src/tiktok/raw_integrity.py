from __future__ import annotations

import hashlib
from pathlib import Path

from src.config import PROJECT_ROOT


def verify_and_backfill_raw_hashes(root: Path | None = None) -> dict[str, object]:
    raw_root = root or PROJECT_ROOT / "data" / "raw" / "tiktok"
    checked = 0
    created = 0
    errors: list[str] = []
    for json_path in sorted(raw_root.rglob("*.json")):
        checked += 1
        digest = hashlib.sha256(json_path.read_bytes()).hexdigest()
        sidecar = json_path.with_suffix(json_path.suffix + ".sha256")
        expected = f"{digest}  {json_path.name}\n"
        if sidecar.exists():
            if sidecar.read_text(encoding="ascii") != expected:
                errors.append(str(sidecar))
            continue
        sidecar.write_text(expected, encoding="ascii")
        created += 1
    return {
        "ok": not errors,
        "raw_root": str(raw_root),
        "checked": checked,
        "created": created,
        "errors": errors,
    }
