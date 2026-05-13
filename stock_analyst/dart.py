from __future__ import annotations

import io
import json
import re
import time
import zipfile
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET

import pandas as pd
import requests
from bs4 import BeautifulSoup

from .models import Company, Filing

DART_BASE = "https://opendart.fss.or.kr/api"
REPORT_CODES = {
    "11013": "Q1",
    "11012": "H1",
    "11014": "Q3",
    "11011": "FY",
}
PERIOD_ORDER = {"Q1": 1, "Q2": 2, "Q3": 3, "Q4": 4}
ACCOUNT_ALIASES = {
    "revenue": ("매출액", "수익(매출액)", "영업수익", "영업수익(매출액)", "Revenue"),
    "gross_profit": ("매출총이익", "Gross profit"),
    "operating_profit": ("영업이익", "영업이익(손실)", "영업손익", "Profit (loss) from operating activities"),
    "net_income": ("당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익", "Profit (loss)"),
}


class DartClient:
    def __init__(self, api_key: str, cache_dir: str | Path = ".cache/dart", sleep_seconds: float = 0.15):
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sleep_seconds = sleep_seconds
        self.session = requests.Session()

    def _get_json(self, endpoint: str, **params: str) -> dict:
        time.sleep(self.sleep_seconds)
        payload = {"crtfc_key": self.api_key, **params}
        response = self.session.get(f"{DART_BASE}/{endpoint}", params=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("status") not in {"000", "013"}:
            raise RuntimeError(f"DART API error {data.get('status')}: {data.get('message')}")
        return data

    def load_companies(self, force: bool = False) -> list[Company]:
        cache_file = self.cache_dir / "corpCode.xml"
        if force or not cache_file.exists():
            response = self.session.get(f"{DART_BASE}/corpCode.xml", params={"crtfc_key": self.api_key}, timeout=60)
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
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

    def find_company(self, corp_name: str | None = None, stock_code: str | None = None) -> Company:
        if not corp_name and not stock_code:
            raise ValueError("corp_name or stock_code is required")
        matches = self.load_companies()
        if corp_name:
            matches = [c for c in matches if c.corp_name == corp_name or corp_name in c.corp_name]
        if stock_code:
            matches = [c for c in matches if c.stock_code == stock_code]
        listed = [c for c in matches if c.stock_code]
        if not listed:
            raise ValueError(f"No listed DART company matched corp_name={corp_name!r}, stock_code={stock_code!r}")
        listed.sort(key=lambda c: (c.corp_name != corp_name if corp_name else False, c.stock_code))
        return listed[0]

    def filings(self, corp_code: str, start: str, end: str, final_only: bool = True) -> list[Filing]:
        page_no = 1
        rows: list[Filing] = []
        while True:
            data = self._get_json(
                "list.json",
                corp_code=corp_code,
                bgn_de=start,
                end_de=end,
                pblntf_ty="A",
                last_reprt_at="Y" if final_only else "N",
                page_no=str(page_no),
                page_count="100",
            )
            for item in data.get("list", []):
                report_nm = item.get("report_nm", "")
                if any(keyword in report_nm for keyword in ("분기보고서", "반기보고서", "사업보고서")):
                    rows.append(
                        Filing(
                            corp_code=item.get("corp_code", ""),
                            corp_name=item.get("corp_name", ""),
                            stock_code=item.get("stock_code", ""),
                            corp_cls=item.get("corp_cls", ""),
                            report_nm=report_nm,
                            rcept_no=item.get("rcept_no", ""),
                            flr_nm=item.get("flr_nm", ""),
                            rcept_dt=item.get("rcept_dt", ""),
                            rm=item.get("rm"),
                        )
                    )
            if page_no >= int(data.get("total_page", 1)):
                break
            page_no += 1
        return rows

    def financial_statement_all(self, corp_code: str, year: int, reprt_code: str, fs_div: str = "CFS") -> pd.DataFrame:
        data = self._get_json(
            "fnlttSinglAcntAll.json",
            corp_code=corp_code,
            bsns_year=str(year),
            reprt_code=reprt_code,
            fs_div=fs_div,
        )
        return pd.DataFrame(data.get("list", []))

    def document_text(self, rcept_no: str) -> str:
        cache_file = self.cache_dir / f"{rcept_no}.xml"
        if not cache_file.exists():
            response = self.session.get(
                f"{DART_BASE}/document.xml",
                params={"crtfc_key": self.api_key, "rcept_no": rcept_no},
                timeout=60,
            )
            response.raise_for_status()
            cache_file.write_bytes(response.content)
        raw = cache_file.read_bytes()
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                raw = zf.read(zf.namelist()[0])
        except zipfile.BadZipFile:
            pass
        soup = BeautifulSoup(raw, "xml")
        return soup.get_text("\n", strip=True)


def period_from_report_code(year: int, reprt_code: str) -> str:
    return f"{year}-{REPORT_CODES[reprt_code]}"


def _to_number(value: object) -> float | None:
    if value is None or pd.isna(value):
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


def _match_account(row: pd.Series) -> str | None:
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


def normalize_cumulative_financials(frames: Iterable[tuple[int, str, pd.DataFrame]]) -> pd.DataFrame:
    records: list[dict] = []
    for year, reprt_code, frame in frames:
        if frame.empty:
            continue
        income = frame[frame.get("sj_div", pd.Series(dtype=str)).isin(["IS", "CIS"])]
        for _, row in income.iterrows():
            metric = _match_account(row)
            if not metric:
                continue
            amount = _to_number(row.get("thstrm_amount"))
            if amount is None:
                continue
            records.append(
                {
                    "year": year,
                    "reprt_code": reprt_code,
                    "period_label": period_from_report_code(year, reprt_code),
                    "report_period": REPORT_CODES[reprt_code],
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
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df["ordinal_num"] = pd.to_numeric(df["ordinal"], errors="coerce")
    df = df.sort_values(["year", "reprt_code", "metric", "ordinal_num"], na_position="last")
    return df.drop_duplicates(["period_label", "metric"], keep="first").drop(columns=["ordinal_num"])


def derive_quarterly_metrics(cumulative: pd.DataFrame) -> pd.DataFrame:
    if cumulative.empty:
        return cumulative
    pivot = cumulative.pivot_table(index=["year", "report_period"], columns="metric", values="amount", aggfunc="first").reset_index()
    report_to_quarter = {"Q1": 1, "H1": 2, "Q3": 3, "FY": 4}
    pivot["quarter"] = pivot["report_period"].map(report_to_quarter)
    pivot = pivot.sort_values(["year", "quarter"])
    metric_cols = [c for c in ["revenue", "gross_profit", "operating_profit", "net_income"] if c in pivot.columns]
    out = pivot[["year", "quarter"]].copy()
    out["period"] = out["year"].astype(str) + "Q" + out["quarter"].astype(str)
    for metric in metric_cols:
        out[metric] = pivot.groupby("year")[metric].diff().fillna(pivot[metric])
    if {"operating_profit", "revenue"}.issubset(out.columns):
        out["opm"] = out["operating_profit"] / out["revenue"]
    if {"gross_profit", "revenue"}.issubset(out.columns):
        out["gpm"] = out["gross_profit"] / out["revenue"]
    if {"net_income", "revenue"}.issubset(out.columns):
        out["npm"] = out["net_income"] / out["revenue"]
    return out


def extract_revenue_note_candidates(text: str) -> list[str]:
    keywords = ("매출처", "주요 고객", "주요 매출", "매출실적", "영업부문", "제품별", "지역별")
    blocks = re.split(r"\n{2,}|(?=\d+\.\s)", text)
    candidates = []
    for block in blocks:
        if any(keyword in block for keyword in keywords) and ("매출" in block or "수익" in block):
            compact = re.sub(r"\s+", " ", block).strip()
            if 60 <= len(compact) <= 3000:
                candidates.append(compact)
    return candidates[:20]


def write_filings_csv(filings: list[Filing], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(f) for f in filings]).to_csv(output, index=False, encoding="utf-8-sig")


def write_company_json(company: Company, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(asdict(company), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
