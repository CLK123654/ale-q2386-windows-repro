from __future__ import annotations

import argparse
import atexit
import csv
import json
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path


def run(command: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, input=stdin, text=True, capture_output=True, timeout=300)
    if completed.returncode:
        raise RuntimeError(
            f"command failed with return code {completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def psql(psql_bin: str, database_url: str) -> list[str]:
    return [psql_bin, "--dbname", database_url, "-X", "--set", "ON_ERROR_STOP=1"]


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def copy_delivery_material(output: Path) -> None:
    script = Path(__file__).resolve()
    template = script.parent / "template_output"
    sql_source = template / "sql" if template.is_dir() else script.parent.parent / "sql"
    shutil.copytree(sql_source, output / "sql")
    (output / "tools").mkdir()
    shutil.copy2(script, output / "tools" / "build_delivery.py")


def export_csv(psql_bin: str, database_url: str, query: str, target: Path) -> None:
    completed = run(
        psql(psql_bin, database_url)
        + ["--quiet", "--command", f"COPY ({query}) TO STDOUT WITH(FORMAT CSV,HEADER TRUE)"]
    )
    target.write_text(completed.stdout, encoding="utf-8", newline="")


def scalar(psql_bin: str, database_url: str, query: str) -> str:
    return run(
        psql(psql_bin, database_url)
        + ["--tuples-only", "--no-align", "--command", query]
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--psql", default="psql")
    parser.add_argument("--database-url", required=True)
    args = parser.parse_args()

    input_root = Path(args.input).resolve()
    output = Path(args.output).resolve()
    required = [
        input_root / "data/accounts.csv",
        input_root / "data/ledger_periods.csv",
        input_root / "requests/request_headers.csv",
        input_root / "requests/request_lines.csv",
        input_root / "requests/reversal_requests.csv",
        input_root / "contracts/ledger_rules.json",
        input_root / "contracts/review_cases.csv",
        input_root / "starter/ledger_starter.sql",
        input_root / "README.md",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("missing input files: " + ", ".join(missing))

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    completed = {"value": False}

    def clean_failure() -> None:
        if not completed["value"] and output.exists():
            shutil.rmtree(output)

    atexit.register(clean_failure)

    accounts = read_csv(required[0])
    periods = read_csv(required[1])
    headers = read_csv(required[2])
    lines = read_csv(required[3])
    reversals = read_csv(required[4])
    rules = json.loads(required[5].read_text(encoding="utf-8"))
    cases = read_csv(required[6])
    if len({row["account_id"] for row in accounts}) != len(accounts):
        raise SystemExit("duplicate account_id")
    if len({row["period_id"] for row in periods}) != len(periods):
        raise SystemExit("duplicate period_id")
    if len({row["external_ref"] for row in headers}) != len(headers):
        raise SystemExit("duplicate external_ref")
    if len({row["case_id"] for row in cases}) != len(cases):
        raise SystemExit("duplicate case_id")
    header_refs = {row["external_ref"] for row in headers}
    if {row["external_ref"] for row in lines} != header_refs:
        raise SystemExit("request headers and lines differ")
    if rules.get("idempotency_line_order") != "line_no":
        raise SystemExit("unsupported idempotency line order")

    copy_delivery_material(output)

    scripts = []
    for name in ("01_schema.sql", "02_functions.sql", "03_load.sql"):
        text = (output / "sql" / name).read_text(encoding="utf-8")
        text = text.replace("__ACCOUNTS_CSV__", sql_path(required[0]))
        text = text.replace("__PERIODS_CSV__", sql_path(required[1]))
        scripts.append(text)
    run(psql(args.psql, args.database_url), stdin="BEGIN;\n" + "\n".join(scripts) + "\nCOMMIT;\n")

    lines_by_ref: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in lines:
        lines_by_ref[row["external_ref"]].append({
            "line_no": int(row["line_no"]),
            "account_id": row["account_id"],
            "side": row["side"],
            "amount": str(Decimal(row["amount"]).quantize(Decimal("0.01"))),
            "memo": row["memo"],
        })
    for row in headers:
        line_json = json.dumps(lines_by_ref[row["external_ref"]], separators=(",", ":"))
        query = (
            "SELECT * FROM ledger.post_entry("
            f"{sql_literal(row['external_ref'])},{sql_literal(row['booked_on'])}::date,"
            f"{sql_literal(row['currency'])},{sql_literal(line_json)}::jsonb)"
        )
        run(psql(args.psql, args.database_url) + ["--command", query])
    for row in reversals:
        query = (
            "SELECT * FROM ledger.reverse_entry("
            f"{sql_literal(row['original_ref'])},{sql_literal(row['reversal_ref'])},"
            f"{sql_literal(row['booked_on'])}::date)"
        )
        run(psql(args.psql, args.database_url) + ["--command", query])

    for row in cases:
        statement = (
            "INSERT INTO ledger.review_result(case_id,case_type,expected_outcome,actual_outcome,actual_sqlstate,evidence,result) "
            f"SELECT {sql_literal(row['case_id'])},{sql_literal(row['case_type'])},"
            f"{sql_literal(row['expected_outcome'])},actual_outcome,actual_sqlstate,evidence,"
            f"CASE WHEN actual_outcome={sql_literal(row['expected_outcome'])} THEN 'PASS' ELSE 'FAIL' END "
            f"FROM ledger.run_review_case({sql_literal(row['case_type'])})"
        )
        run(psql(args.psql, args.database_url) + ["--command", statement])

    results = output / "results"
    results.mkdir()
    export_csv(args.psql, args.database_url,
        "SELECT entry_id,external_ref,booked_on,currency,reversal_of FROM ledger.journal_entry ORDER BY external_ref",
        results / "journal_entries.csv")
    export_csv(args.psql, args.database_url,
        "SELECT entry_id,line_no,account_id,side,amount,memo FROM ledger.posting ORDER BY entry_id,line_no",
        results / "postings.csv")
    export_csv(args.psql, args.database_url,
        "SELECT currency,sum(amount) FILTER(WHERE side='D')::numeric(18,2) AS debit_total,sum(amount) FILTER(WHERE side='C')::numeric(18,2) AS credit_total,(sum(amount) FILTER(WHERE side='D')-sum(amount) FILTER(WHERE side='C'))::numeric(18,2) AS difference FROM ledger.journal_entry JOIN ledger.posting USING(entry_id) GROUP BY currency ORDER BY currency",
        results / "trial_balance.csv")
    export_csv(args.psql, args.database_url,
        "SELECT original.external_ref AS original_ref,reversal.external_ref AS reversal_ref,o.line_no,o.account_id,o.side AS original_side,r.side AS reversal_side,o.amount AS original_amount,r.amount AS reversal_amount,(o.account_id=r.account_id AND o.amount=r.amount AND o.side<>r.side) AS matches FROM ledger.journal_entry original JOIN ledger.journal_entry reversal ON reversal.reversal_of=original.entry_id JOIN ledger.posting o ON o.entry_id=original.entry_id JOIN ledger.posting r ON r.entry_id=reversal.entry_id AND r.line_no=o.line_no ORDER BY original.external_ref,o.line_no",
        results / "reversal_review.csv")
    export_csv(args.psql, args.database_url,
        "SELECT case_id,case_type,expected_outcome,actual_outcome,actual_sqlstate,evidence,result FROM ledger.review_result ORDER BY case_id",
        results / "review_results.csv")
    export_csv(args.psql, args.database_url,
        "SELECT case_id,case_type,actual_outcome,actual_sqlstate,evidence,result FROM ledger.review_result WHERE case_type IN ('baseline_requests','identical_retry','changed_retry') ORDER BY case_id",
        results / "request_results.csv")

    source_counts = {
        "accounts": len(accounts), "periods": len(periods), "request_headers": len(headers),
        "request_lines": len(lines), "reversal_requests": len(reversals), "review_cases": len(cases),
    }
    (results / "source_counts.json").write_text(
        json.dumps(source_counts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state = {
        "journal_entries": int(scalar(args.psql, args.database_url, "SELECT count(*) FROM ledger.journal_entry")),
        "postings": int(scalar(args.psql, args.database_url, "SELECT count(*) FROM ledger.posting")),
        "request_payloads": int(scalar(args.psql, args.database_url, "SELECT count(*) FROM ledger.request_payload")),
        "review_passes": int(scalar(args.psql, args.database_url, "SELECT count(*) FROM ledger.review_result WHERE result='PASS'")),
        "review_failures": int(scalar(args.psql, args.database_url, "SELECT count(*) FROM ledger.review_result WHERE result<>'PASS'")),
        "unbalanced_currencies": int(scalar(args.psql, args.database_url, "SELECT count(*) FROM (SELECT currency FROM ledger.journal_entry JOIN ledger.posting USING(entry_id) GROUP BY currency HAVING sum(CASE side WHEN 'D' THEN amount ELSE -amount END)<>0) q")),
    }
    handover = {
        "status": "READY" if state["review_failures"] == 0 and state["unbalanced_currencies"] == 0 else "HOLD",
        "source_counts": source_counts,
        "result_counts": state,
    }
    (results / "handover.json").write_text(
        json.dumps(handover, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if handover["status"] != "READY":
        raise RuntimeError("ledger handover is not ready")
    (output / "HANDOVER.md").write_text(
        "# 结算账本交接\n\n"
        "input_data目录提供科目、期间、入账请求、冲正请求和复核口径。\n\n"
        "使用tools/build_delivery.py连接空数据库，脚本会部署sql目录内容并生成results目录。"
        "handover.json状态为READY时，账本可进入下一步发布检查。\n",
        encoding="utf-8",
    )
    completed["value"] = True


if __name__ == "__main__":
    main()
