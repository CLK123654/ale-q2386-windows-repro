from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
WORK = ROOT / "work-reference"


def admin(sql: str) -> None:
    completed = subprocess.run(
        [os.environ["PSQL_PATH"], "--dbname", os.environ["SERVER_ADMIN_URL"], "-X", "--set", "ON_ERROR_STOP=1", "--command", sql],
        text=True, capture_output=True, timeout=60,
    )
    if completed.returncode:
        raise SystemExit(completed.stdout + completed.stderr)


if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir()
EVIDENCE.mkdir(exist_ok=True)
with zipfile.ZipFile(ROOT / "task/输入数据包.zip") as package:
    package.extractall(WORK)
database = "ledger_reference"
admin(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
admin(f"CREATE DATABASE {database}")
command = [
    sys.executable, str(ROOT / "implementation/build_delivery.py"),
    "--input", str(WORK / "input_data"), "--output", str(WORK / "output"),
    "--psql", os.environ["PSQL_PATH"],
    "--database-url", f"postgresql://postgres:root@127.0.0.1:5432/{database}",
]
completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=300)
if completed.returncode:
    raise SystemExit(completed.stdout + completed.stderr)
candidate = EVIDENCE / "reference-candidate.zip"
with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted((WORK / "output").rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(WORK).as_posix())
summary = {
    "result": "PASS", "mode": "reference", "commit_sha": os.getenv("GITHUB_SHA"),
    "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
    "reference_members": sorted(path.relative_to(WORK).as_posix() for path in (WORK / "output").rglob("*") if path.is_file()),
}
(EVIDENCE / "reference-generation.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
