from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
import xml.etree.ElementTree as ET

DART_BASE = "https://opendart.fss.or.kr/api"
REPORT_CODES = {"11013": "Q1", "11012": "H1", "11014": "Q3", "11011": "FY"}
ACCOUNT_ALIASES = {
    "revenue": ("매출액", "수익(매출액)", "영업수익", "영업수익(매출액)", "Revenue"),
    "gross_profit": ("매출총이익", "Gross profit"),
    "operating_profit": ("영업이익", "영업이익(손실)", "영업손익", "Profit (loss) from operating activities"),
    "net_income": ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익", "Profit (loss)"),
}


@dataclass(frozen=True)
class Company:
    corp_code: str
    corp_name: str
    stock_code: str
    modify_date: str | None = None


def request_bytes(url: str, params: dict[str, str], timeout: int = 60) -> bytes:
    query = urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(f"{url}?{query}", timeout=timeout) as response:
            return response.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "DART 서버에 접속하지 못했습니다. 현재 실행 환경의 네트워크/프록시가 "
            f"외부 HTTPS 요청을 차단했을 수 있습니다: {exc}"
        ) from exc


def request_json(endpoint: str, api_key: str, **params: str) -> dict:
    time.sleep(0.15)
    raw = request_bytes(f"{DART_BASE}/{endpoint}", {"crtfc_key": api_key, **params}, timeout=60)
    data = json.loads(raw.decode("utf-8"))
    if data.get("status") not in {"000", "013"}:
        raise RuntimeError(f"DART API error {data.get('status')}: {data.get('message')}")
    return data


def load_companies(api_key: str, cache_dir: Path) -> list[Company]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "corpCode.xml"
    if not cache_file.exists():
        raw = request_bytes(f"{DART_BASE}/corpCode.xml", {"crtfc_key": api_key}, timeout=90)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            cache_file.write_bytes(zf.read("CORPCODE.xml"))
    root = ET.parse(cache_file).getroot()
    companies: list[Company] = []
    for item in root.findall("list"):
        companies.append(
            Company(
                corp_code=(item.findtext("corp_code") or "").strip(),
                corp_name=(item.findtext("corp_name") or "").strip(),
                stock_code=(item.findtext("stock_code") or "").strip(),
                modify_date=(item.findtext("modify_date") or "").strip() or None,
            )
        )
    return companies


def find_company(api_key: str, cache_dir: Path, corp_name: str | None, stock_code: str | None) -> Company:
    if not corp_name and not stock_code:
        raise SystemExit("--corp-name 또는 --stock-code 중 하나는 필요합니다.")
    matches = load_companies(api_key, cache_dir)
    if corp_name:
        matches = [c for c in matches if c.corp_name == corp_name or corp_name in c.corp_name]
    if stock_code:
        matches = [c for c in matches if c.stock_code == stock_code]
    listed = [c for c in matches if c.stock_code]
    if not listed:
        raise SystemExit(f"DART 상장회사 검색 실패: corp_name={corp_name!r}, stock_code={stock_code!r}")
    listed.sort(key=lambda c: (c.corp_name != corp_name if corp_name else False, c.stock_code))
    return listed[0]


def slugify(company: Company) -> str:
    name = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", company.corp_name).strip("_") or "company"
    return f"{company.stock_code}_{name}" if company.stock_code else name


def write_csv(path: Path, rows: list[dict], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fieldnames is None:
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def fetch_filings(api_key: str, company: Company, start: str, end: str) -> list[dict]:
    page_no = 1
    rows: list[dict] = []
    while True:
        data = request_json(
            "list.json",
            api_key,
            corp_code=company.corp_code,
            bgn_de=start,
            end_de=end,
            pblntf_ty="A",
            last_reprt_at="Y",
            page_no=str(page_no),
            page_count="100",
        )
        for item in data.get("list", []):
            report_nm = item.get("report_nm", "")
            if any(keyword in report_nm for keyword in ("분기보고서", "반기보고서", "사업보고서")):
                rows.append(item)
        if page_no >= int(data.get("total_page", 1)):
            break
        page_no += 1
    return rows


def to_number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").replace(" ", "").strip()
    if text in {"", "-"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return float(text)
    except ValueError:
        return None


def match_account(row: dict) -> str | None:
    account_nm = str(row.get("account_nm", ""))
    account_id = str(row.get("account_id", ""))
    for metric, aliases in ACCOUNT_ALIASES.items():
        if account_nm in aliases or any(alias.lower() == account_nm.lower() for alias in aliases):
            return metric
    if "Revenue" in account_id and "Cost" not in account_id:
        return "revenue"
    if "GrossProfit" in account_id:
        return "gross_profit"
    if "Operating" in account_id and "Profit" in account_id:
        return "operating_profit"
    return None


def fetch_financials(api_key: str, company: Company, start: str, end: str, fs_div: str, include_report_codes: list[str]) -> tuple[list[dict], list[dict]]:
    cumulative: list[dict] = []
    end_year = int(end[:4])
    for year in range(int(start[:4]), end_year + 1):
        for reprt_code, report_period in REPORT_CODES.items():
            if year == end_year and reprt_code not in include_report_codes:
                continue
            try:
                data = request_json(
                    "fnlttSinglAcntAll.json",
                    api_key,
                    corp_code=company.corp_code,
                    bsns_year=str(year),
                    reprt_code=reprt_code,
                    fs_div=fs_div,
                )
            except RuntimeError as exc:
                print(f"skip {year} {reprt_code}: {exc}")
                continue
            for row in data.get("list", []):
                if row.get("sj_div") not in {"IS", "CIS"}:
                    continue
                metric = match_account(row)
                amount = to_number(row.get("thstrm_amount"))
                if not metric or amount is None:
                    continue
                cumulative.append(
                    {
                        "year": year,
                        "reprt_code": reprt_code,
                        "period_label": f"{year}-{report_period}",
                        "report_period": report_period,
                        "metric": metric,
                        "amount": amount,
                        "currency": row.get("currency") or "KRW",
                        "fs_div": row.get("fs_div"),
                        "fs_nm": row.get("fs_nm"),
                        "account_nm": row.get("account_nm"),
                        "account_id": row.get("account_id"),
                        "ordinal": row.get("ord"),
                    }
                )
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for row in sorted(cumulative, key=lambda r: (r["year"], r["reprt_code"], r["metric"], int(r["ordinal"] or 9999))):
        key = (row["period_label"], row["metric"])
        if key not in seen:
            seen.add(key)
            deduped.append(row)
    return deduped, derive_quarterly(deduped)


def derive_quarterly(cumulative: list[dict]) -> list[dict]:
    order = {"Q1": 1, "H1": 2, "Q3": 3, "FY": 4}
    by_period: dict[tuple[int, int], dict] = {}
    for row in cumulative:
        quarter = order[row["report_period"]]
        target = by_period.setdefault((row["year"], quarter), {"year": row["year"], "quarter": quarter, "period": f"{row['year']}Q{quarter}"})
        target[row["metric"]] = row["amount"]
    rows: list[dict] = []
    previous_by_year: dict[int, dict] = {}
    for key in sorted(by_period):
        current = by_period[key]
        previous = previous_by_year.get(current["year"], {})
        out = {"year": current["year"], "quarter": current["quarter"], "period": current["period"]}
        for metric in ("revenue", "gross_profit", "operating_profit", "net_income"):
            if metric in current:
                out[metric] = current[metric] - previous.get(metric, 0)
        revenue = out.get("revenue")
        if revenue:
            if "operating_profit" in out:
                out["opm"] = out["operating_profit"] / revenue
            if "gross_profit" in out:
                out["gpm"] = out["gross_profit"] / revenue
            if "net_income" in out:
                out["npm"] = out["net_income"] / revenue
        rows.append(out)
        previous_by_year[current["year"]] = current
    return rows


def document_text(api_key: str, rcept_no: str, cache_dir: Path) -> str:
    cache_file = cache_dir / f"{rcept_no}.xml"
    if not cache_file.exists():
        cache_file.write_bytes(request_bytes(f"{DART_BASE}/document.xml", {"crtfc_key": api_key, "rcept_no": rcept_no}, timeout=90))
    raw = cache_file.read_bytes()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            raw = zf.read(zf.namelist()[0])
    except zipfile.BadZipFile:
        pass
    text = re.sub(rb"<[^>]+>", b"\n", raw).decode("utf-8", errors="ignore")
    return re.sub(r"\s+", " ", text)


def extract_revenue_note_candidates(text: str) -> list[str]:
    keywords = ("매출처", "주요 고객", "주요 매출", "매출실적", "영업부문", "제품별", "지역별")
    candidates = []
    for keyword in keywords:
        for match in re.finditer(keyword, text):
            start = max(0, match.start() - 500)
            end = min(len(text), match.end() + 1800)
            snippet = text[start:end].strip()
            if "매출" in snippet or "수익" in snippet:
                candidates.append(snippet)
    unique: list[str] = []
    for item in candidates:
        compact = re.sub(r"\s+", " ", item)
        if compact not in unique:
            unique.append(compact)
    return unique[:20]


def ingest_materials(input_dir: Path, output_jsonl: Path) -> list[dict]:
    input_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        rows.append({"source_path": str(path), "kind": path.suffix.lower().lstrip("."), "title": path.stem, "text": text})
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with output_jsonl.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return rows


def build_report(company: Company, quarterly: list[dict], materials: list[dict], output_md: Path, chart_html: Path, breakdown_csv: Path) -> None:
    output_md.parent.mkdir(parents=True, exist_ok=True)
    chart_html.parent.mkdir(parents=True, exist_ok=True)
    chart_rows = json.dumps(quarterly, ensure_ascii=False)
    chart_html.write_text(
        "<html><head><meta charset='utf-8'><title>Financial Chart</title></head><body>"
        f"<h1>{html.escape(company.corp_name)} 분기 실적</h1><pre id='data'>{html.escape(chart_rows)}</pre>"
        "<p>Plotly 의존성을 설치하지 못하는 환경용 기본 HTML입니다. CSV를 내려받아 차트 도구에 연결할 수 있습니다.</p></body></html>",
        encoding="utf-8",
    )
    lines = [f"# {company.corp_name}({company.stock_code}) 투자 분석 리포트 작업본", ""]
    if quarterly:
        lines += [f"- 최신 반영 분기: **{quarterly[-1]['period']}**", f"- 차트: `{chart_html}`", "", "## 1. 분기 재무 테이블", ""]
        cols = ["period", "revenue", "gross_profit", "operating_profit", "net_income", "opm", "gpm", "npm"]
        lines.append("|" + "|".join(cols) + "|")
        lines.append("|" + "|".join(["---"] * len(cols)) + "|")
        for row in quarterly:
            vals = []
            for col in cols:
                val = row.get(col, "")
                if isinstance(val, float) and col in {"opm", "gpm", "npm"}:
                    val = f"{val * 100:.1f}%"
                elif isinstance(val, float):
                    val = f"{val / 1_000_000_000:.1f}"
                vals.append(str(val))
            lines.append("|" + "|".join(vals) + "|")
    else:
        lines += ["- 재무 데이터가 비어 있습니다."]
    lines += ["", "## 2. 매출처/부문별 수익성 테이블", ""]
    if breakdown_csv.exists():
        lines.append(f"`{breakdown_csv}` 파일을 확인해 매출처/부문별 수치를 반영하세요.")
    else:
        lines.append("아직 구조화된 매출처/부문별 데이터가 없습니다. revenue_note_candidates.csv와 보조자료를 보고 revenue_breakdown.csv를 작성하세요.")
    lines += ["", "## 3. 투자전략 업데이트 체크리스트", "", "- 성장률: 매출 성장의 제품/지역/고객별 원천 확인", "- 수익성: GPM/OPM 개선의 구조적 요인과 일회성 요인 구분", "- 현금흐름: 이익의 현금 전환율과 운전자본 점검", "- 리스크: 경쟁, 규제, 고객 집중도, 비용 증가 여부 점검", "", "## 4. 보조자료 인입 현황", ""]
    if materials:
        for item in materials:
            preview = " ".join(item.get("text", "").split())[:300]
            lines += [f"### {item.get('title')}", f"- 파일: `{item.get('source_path')}`", f"- 요약 후보: {preview}", ""]
    else:
        lines.append("인입된 보조자료가 없습니다.")
    output_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> None:
    api_key = args.api_key or os.getenv("DART_API_KEY")
    if not api_key:
        raise SystemExit("DART_API_KEY 환경변수 또는 --api-key가 필요합니다.")
    cache_dir = Path(args.cache_dir)
    company = find_company(api_key, cache_dir, args.corp_name, args.stock_code)
    output_dir = Path(args.output_dir) if args.output_dir else Path(args.output_base) / slugify(company)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "company.json").write_text(json.dumps(asdict(company), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    filings = fetch_filings(api_key, company, args.start, args.end)
    write_csv(output_dir / "filings.csv", filings)
    cumulative, quarterly = fetch_financials(api_key, company, args.start, args.end, args.fs_div, args.include_report_codes)
    write_csv(output_dir / "financials_cumulative.csv", cumulative)
    write_csv(output_dir / "financials_quarterly.csv", quarterly)
    note_rows = []
    for filing in filings:
        try:
            for idx, note in enumerate(extract_revenue_note_candidates(document_text(api_key, filing["rcept_no"], cache_dir)), start=1):
                note_rows.append({"rcept_no": filing["rcept_no"], "report_nm": filing.get("report_nm"), "rcept_dt": filing.get("rcept_dt"), "rank": idx, "text": note})
        except Exception as exc:
            note_rows.append({"rcept_no": filing.get("rcept_no"), "report_nm": filing.get("report_nm"), "rcept_dt": filing.get("rcept_dt"), "rank": 0, "text": f"document extract failed: {exc}"})
    write_csv(output_dir / "revenue_note_candidates.csv", note_rows)
    materials = ingest_materials(Path(args.materials_dir), output_dir / "supplemental_materials.jsonl")
    report_dir = Path(args.report_dir)
    report_md = report_dir / f"{slugify(company)}_report.md"
    chart_html = report_dir / f"{slugify(company)}_financial_chart.html"
    build_report(company, quarterly, materials, report_md, chart_html, output_dir / "revenue_breakdown.csv")
    print(f"완료: {company.corp_name}({company.stock_code})")
    print(f"- 데이터 폴더: {output_dir}")
    print(f"- 리포트: {report_md}")
    print(f"- 차트: {chart_html}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="외부 패키지 없이 실행 가능한 DART 종목 분석 파이프라인")
    parser.add_argument("--api-key")
    parser.add_argument("--corp-name")
    parser.add_argument("--stock-code")
    parser.add_argument("--start", default="20220101")
    parser.add_argument("--end", default=date.today().strftime("%Y%m%d"))
    parser.add_argument("--include-report-codes", nargs="+", default=["11013"])
    parser.add_argument("--fs-div", default="CFS", choices=["CFS", "OFS"])
    parser.add_argument("--cache-dir", default=".cache/dart")
    parser.add_argument("--output-base", default="outputs")
    parser.add_argument("--output-dir")
    parser.add_argument("--materials-dir", default="data/materials")
    parser.add_argument("--report-dir", default="reports")
    return parser


def main() -> None:
    try:
        run(build_parser().parse_args())
    except RuntimeError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    main()
