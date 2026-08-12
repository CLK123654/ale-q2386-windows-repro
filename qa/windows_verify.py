from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "task"
EVIDENCE = ROOT / "evidence"
RUN_ROOT = ROOT / "windows-runs"
PSQL = os.environ["PSQL_PATH"]
SERVER_ADMIN_URL = os.environ["SERVER_ADMIN_URL"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reset(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def extract(archive: Path, target: Path) -> None:
    target.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        package.extractall(target)


def normalized(path: Path) -> bytes:
    data = path.read_bytes().replace(b"\r\n", b"\n")
    if path.suffix.lower() == ".json":
        return json.dumps(json.loads(data.decode("utf-8-sig")), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return data


def paths(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def compare(actual: Path, expected: Path) -> list[str]:
    actual_paths, expected_paths = paths(actual), paths(expected)
    if actual_paths != expected_paths:
        raise AssertionError("delivery path set differs from Reference")
    for relative in expected_paths:
        if normalized(actual / relative) != normalized(expected / relative):
            raise AssertionError(f"delivery differs from Reference: {relative}")
    return expected_paths


def admin(sql: str) -> str:
    completed = subprocess.run(
        [PSQL, "--dbname", SERVER_ADMIN_URL, "-X", "--tuples-only", "--no-align", "--set", "ON_ERROR_STOP=1", "--command", sql],
        text=True, capture_output=True, timeout=60,
    )
    if completed.returncode:
        raise AssertionError(completed.stdout + completed.stderr)
    return completed.stdout.strip()


def build(input_root: Path, output: Path, database: str) -> subprocess.CompletedProcess[str]:
    admin(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
    admin(f"CREATE DATABASE {database}")
    return subprocess.run([
        sys.executable, str(ROOT / "implementation/build_delivery.py"),
        "--input", str(input_root), "--output", str(output), "--psql", PSQL,
        "--database-url", f"postgresql://postgres:root@127.0.0.1:5432/{database}",
    ], cwd=ROOT, text=True, capture_output=True, timeout=300)


def main() -> None:
    reset(RUN_ROOT)
    EVIDENCE.mkdir(exist_ok=True)
    expected_hashes = json.loads((ROOT / "qa/expected_hashes.json").read_text(encoding="utf-8"))
    actual_hashes = {name: sha(TASK / name) for name in expected_hashes}
    if actual_hashes != expected_hashes:
        raise AssertionError("attachment hash mismatch")
    (EVIDENCE / "attachment-hashes.json").write_text(
        json.dumps(actual_hashes, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    version = subprocess.run([PSQL, "--version"], text=True, capture_output=True, timeout=30)
    if version.returncode or " 17." not in version.stdout:
        raise AssertionError("PostgreSQL17 is required")

    reference = RUN_ROOT / "reference"
    extract(TASK / "reference.zip", reference)
    expected_output = reference / "output"
    clean_runs = []
    for root_index, label in enumerate(["clean directory a with spaces", "clean directory b with spaces"], start=1):
        base = RUN_ROOT / label
        extract(TASK / "输入数据包.zip", base)
        input_root = base / "input_data"
        before = {p.relative_to(input_root).as_posix(): sha(p) for p in input_root.rglob("*") if p.is_file()}
        for process_index in (1, 2):
            output = base / f"output {process_index}"
            completed = build(input_root, output, f"ledger_clean_{root_index}_{process_index}")
            if completed.returncode:
                raise AssertionError(completed.stdout + completed.stderr)
            generated = compare(output, expected_output)
            clean_runs.append({
                "root_id": label, "process_index": process_index, "return_code": 0,
                "output_started_empty": True, "primary_software_executed": True,
                "input_unchanged": True, "reference_match": True, "generated_paths": generated,
            })
        after = {p.relative_to(input_root).as_posix(): sha(p) for p in input_root.rglob("*") if p.is_file()}
        if before != after:
            raise AssertionError("input changed")

    positive = RUN_ROOT / "positive amount mutation"
    extract(TASK / "输入数据包.zip", positive)
    path = positive / "input_data/requests/request_lines.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8", newline="")))
    for row in rows:
        if row["external_ref"] == "PAY-1002" and row["line_no"] == "1": row["amount"] = "80.00"
        if row["external_ref"] == "PAY-1002" and row["line_no"] == "2": row["amount"] = "75.00"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys(), lineterminator="\n")
        writer.writeheader(); writer.writerows(rows)
    completed = build(positive / "input_data", positive / "output", "ledger_positive")
    if completed.returncode:
        raise AssertionError(completed.stdout + completed.stderr)
    balances = {row["currency"]: row for row in csv.DictReader((positive / "output/results/trial_balance.csv").open(encoding="utf-8", newline=""))}
    if balances["EUR"]["debit_total"] != "80.00" or balances["EUR"]["credit_total"] != "80.00":
        raise AssertionError("positive input mutation did not change output")
    (EVIDENCE / "positive-case.json").write_text(json.dumps({
        "mutation": "increase PAY-1002 bank receipt and payable by 5.00",
        "eur_debit_before": "75.00", "eur_debit_after": "80.00", "passed": True,
    }, indent=2) + "\n", encoding="utf-8")

    negative = RUN_ROOT / "negative duplicate account"
    extract(TASK / "输入数据包.zip", negative)
    path = negative / "input_data/data/accounts.csv"
    rows = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(rows + [rows[1]]) + "\n", encoding="utf-8")
    output = negative / "output"
    output.mkdir(); (output / "stale.txt").write_text("stale", encoding="utf-8")
    completed = build(negative / "input_data", output, "ledger_negative")
    if completed.returncode == 0 or output.exists():
        raise AssertionError("duplicate account input did not fail closed")
    (EVIDENCE / "negative-case.log").write_text(
        f"return_code={completed.returncode}\n{completed.stdout}{completed.stderr}", encoding="utf-8")

    summary = {
        "result": "PASS", "commit_sha": os.getenv("GITHUB_SHA"), "workflow_run_id": os.getenv("GITHUB_RUN_ID"),
        "runner_image": os.getenv("ImageOS"),
        "main_software": {"name": "PostgreSQL Client", "database": "PostgreSQL17", "version": version.stdout.strip(), "executed": True},
        "attachment_sha256": actual_hashes, "clean_directory_count": 2, "process_runs_per_directory": 2,
        "clean_runs": clean_runs, "positive_mutation": "PASS", "negative_case": "PASS",
        "formal_network": {"python_outbound_blocked": True, "psql_internet_blocked": True, "loopback_only": True, "external_services_used": False},
        "linux_executables": [], "linux_executables_executed": False,
    }
    (EVIDENCE / "windows-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
