from __future__ import annotations

import argparse
import os
import re
from datetime import date
from pathlib import Path

import pandas as pd

from .dart import (
    DartClient,
    derive_quarterly_metrics,
    extract_revenue_note_candidates,
    normalize_cumulative_financials,
    write_company_json,
    write_filings_csv,
)
from .materials import ingest_directory
from .models import Company
from .report import build_report


def _api_key(value: str | None) -> str:
    key = value or os.getenv("DART_API_KEY")
    if not key:
        raise SystemExit("DART API key is required. Pass --api-key or set DART_API_KEY.")
    return key


def slugify_company(company: Company | None = None, corp_name: str | None = None, stock_code: str | None = None) -> str:
    code = (company.stock_code if company else stock_code) or ""
    name = (company.corp_name if company else corp_name) or "company"
    cleaned_name = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", name).strip("_") or "company"
    return f"{code}_{cleaned_name}" if code else cleaned_name


def fetch_dart_outputs(args: argparse.Namespace) -> tuple[Company, Path]:
    client = DartClient(_api_key(args.api_key), cache_dir=args.cache_dir)
    company = client.find_company(args.corp_name, args.stock_code)
    output_dir = Path(args.output_dir) if args.output_dir else Path(args.output_base) / slugify_company(company)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_company_json(company, output_dir / "company.json")

    filings = client.filings(company.corp_code, args.start, args.end)
    write_filings_csv(filings, output_dir / "filings.csv")

    frames = []
    end_year = int(args.end[:4])
    for year in range(int(args.start[:4]), end_year + 1):
        for code in ("11013", "11012", "11014", "11011"):
            if year == end_year and code not in args.include_report_codes:
                continue
            try:
                frames.append((year, code, client.financial_statement_all(company.corp_code, year, code, args.fs_div)))
            except RuntimeError as exc:
                print(f"skip {year} {code}: {exc}")
    cumulative = normalize_cumulative_financials(frames)
    cumulative.to_csv(output_dir / "financials_cumulative.csv", index=False, encoding="utf-8-sig")
    quarterly = derive_quarterly_metrics(cumulative)
    quarterly.to_csv(output_dir / "financials_quarterly.csv", index=False, encoding="utf-8-sig")

    note_rows = []
    for filing in filings:
        try:
            for idx, note in enumerate(extract_revenue_note_candidates(client.document_text(filing.rcept_no)), start=1):
                note_rows.append({"rcept_no": filing.rcept_no, "report_nm": filing.report_nm, "rcept_dt": filing.rcept_dt, "rank": idx, "text": note})
        except Exception as exc:  # document availability differs by filing
            note_rows.append({"rcept_no": filing.rcept_no, "report_nm": filing.report_nm, "rcept_dt": filing.rcept_dt, "rank": 0, "text": f"document extract failed: {exc}"})
    pd.DataFrame(note_rows).to_csv(output_dir / "revenue_note_candidates.csv", index=False, encoding="utf-8-sig")
    print(f"Saved DART outputs for {company.corp_name}({company.stock_code}) to {output_dir}")
    return company, output_dir


def cmd_dart_fetch(args: argparse.Namespace) -> None:
    fetch_dart_outputs(args)


def cmd_ingest_materials(args: argparse.Namespace) -> None:
    materials = ingest_directory(Path(args.input_dir), Path(args.output))
    print(f"Ingested {len(materials)} supplemental materials into {args.output}")


def cmd_build_report(args: argparse.Namespace) -> None:
    revenue_breakdown = Path(args.revenue_breakdown_csv) if args.revenue_breakdown_csv else None
    build_report(Path(args.financial_csv), Path(args.materials_jsonl), Path(args.chart_html), Path(args.output), args.company_name, revenue_breakdown)
    print(f"Built report draft at {args.output}")


def cmd_run(args: argparse.Namespace) -> None:
    company, output_dir = fetch_dart_outputs(args)
    material_output = Path(args.materials_output) if args.materials_output else output_dir / "supplemental_materials.jsonl"
    ingest_directory(Path(args.materials_dir), material_output)
    report_dir = Path(args.report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify_company(company)
    chart_html = report_dir / f"{slug}_financial_chart.html"
    report_md = report_dir / f"{slug}_report.md"
    build_report(
        output_dir / "financials_quarterly.csv",
        material_output,
        chart_html,
        report_md,
        company.corp_name,
        output_dir / "revenue_breakdown.csv",
    )
    print(f"Completed {company.corp_name}({company.stock_code}) report: {report_md}")


def add_company_lookup_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--corp-name", default=None, help="DART 회사명. 예: 한글과컴퓨터")
    parser.add_argument("--stock-code", default=None, help="종목코드. 예: 030520, 005930")


def add_dart_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-key")
    add_company_lookup_args(parser)
    parser.add_argument("--start", default="20220101")
    parser.add_argument("--end", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--fs-div", default="CFS", choices=["CFS", "OFS"])
    parser.add_argument("--cache-dir", default=".cache/dart")
    parser.add_argument("--output-base", default="outputs")
    parser.add_argument("--output-dir", default=None, help="지정하지 않으면 outputs/{종목코드}_{회사명} 사용")
    parser.add_argument("--include-report-codes", nargs="+", default=["11013"], help="end year에 포함할 보고서 코드. Q1=11013, H1=11012, Q3=11014, FY=11011")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DART 기반 범용 종목 분석 리포트 빌더")
    sub = parser.add_subparsers(required=True)

    dart = sub.add_parser("dart-fetch", help="DART에서 특정 종목의 정기보고서와 분기 재무 데이터를 수집")
    add_dart_args(dart)
    dart.set_defaults(func=cmd_dart_fetch)

    ingest = sub.add_parser("ingest-materials", help="증권사 리포트/주주서한/뉴스 등 보조자료를 JSONL로 인입")
    ingest.add_argument("--input-dir", default="data/materials")
    ingest.add_argument("--output", default="outputs/supplemental_materials.jsonl")
    ingest.set_defaults(func=cmd_ingest_materials)

    report = sub.add_parser("build-report", help="분기 재무와 보조자료를 결합해 리포트 초안을 생성")
    report.add_argument("--company-name", required=True)
    report.add_argument("--financial-csv", required=True)
    report.add_argument("--materials-jsonl", required=True)
    report.add_argument("--chart-html", required=True)
    report.add_argument("--output", required=True)
    report.add_argument("--revenue-breakdown-csv", default=None, help="매출처/부문별 매출·이익 수기 검증 CSV")
    report.set_defaults(func=cmd_build_report)

    run = sub.add_parser("run", help="특정 종목의 DART 수집, 보조자료 인입, 리포트 생성을 한 번에 실행")
    add_dart_args(run)
    run.add_argument("--materials-dir", default="data/materials", help="보조자료 입력 폴더")
    run.add_argument("--materials-output", default=None, help="지정하지 않으면 종목 output 폴더 아래 JSONL 생성")
    run.add_argument("--report-dir", default="reports")
    run.set_defaults(func=cmd_run)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if hasattr(args, "corp_name") and not args.corp_name and not args.stock_code:
        parser.error("--corp-name 또는 --stock-code 중 하나는 필요합니다.")
    args.func(args)


if __name__ == "__main__":
    main()
