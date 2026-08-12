from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
expected = json.loads((ROOT / "qa/expected_hashes.json").read_text(encoding="utf-8"))
actual = {name: hashlib.sha256((ROOT / "task" / name).read_bytes()).hexdigest() for name in expected}
if actual != expected:
    raise SystemExit(f"attachment hashes differ\nexpected={expected}\nactual={actual}")
