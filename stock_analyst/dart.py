"""
stock_analyst/dart.py  ―  DART OpenAPI 연동 모듈 (업데이트 버전)

변경 내역:
  - ACCOUNT_ALIASES 에 cogs(매출원가) 추가
  - BS_ACCOUNT_ALIASES 추가 (재고자산, 매출채권, 차입금 계열)
  - _match_bs_account() 추가
  - normalize_bs_financials() 추가
  - 기존 함수/클래스는 모두 동일하게 유지 (하위호환)
"""

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

# 보고서 코드 → 분기 번호 매핑
PERIOD_ORDER = {"Q1": 1, "H1": 2, "Q3": 3, "FY": 4}

# ── 손익계산서(IS/CIS) 계정 ──────────────────────────────────────
ACCOUNT_ALIASES = {
    "revenue": (
        "매출액", "수익(매출액)", "영업수익", "영업수익(매출액)", "Revenue",
        "매출", "순매출액",
    ),
    "cogs": (
        "매출원가", "Cost of sales", "영업비용(매출원가)",
        "매출원가(영업비용)", "영업비용",
    ),
    "gross_profit": (
        "매출총이익", "Gross profit", "매출총손익",
    ),
    "operating_profit": (
        "영업이익", "영업이익(손실)", "영업손익",
        "Profit (loss) from operating activities",
    ),
    "net_income": (
        "당기순이익", "당기순이익(손실)", "분기순이익", "반기순이익",
        "Profit (loss)", "당기순손익",
    ),
}

# ── 재무상태표(BS) 계정 ──────────────────────────────────────────
BS_ACCOUNT_ALIASES = {
    "inventory": (
        "재고자산", "Inventories", "재고자산합계",
    ),
    "accounts_receivable": (
        "매출채권", "매출채권 및 기타유동채권", "매출채권 및 기타채권",
        "Trade and other receivables", "매출채권 및 기타수취채권",
        "Trade receivables", "매출채권(순액)",
    ),
    "short_term_borrowings": (
        "단기차입금", "Short-term borrowings", "단기차입금 및 유동성장기부채",
    ),
    "long_term_borrowings": (
        "장기차입금", "Long-term borrowings",
    ),
    "bonds_payable": (
        "사채", "Bonds payable", "장기사채",
    ),
    "current_lt_debt": (
        "유동성장기부채", "유동성장기차입금",
        "Current portion of long-term borrowings",
    ),
    # 일부 기업은 차입금 합계를 직접 제공하기도 함
    "total_borrowings_direct": (
        "차입금", "총차입금",
    ),
}


class DartClient:
    def __init__(
        self,
        api_key: str,
        cache_dir: str | Path = ".cache/dart",
        sleep_seconds: float = 0.15,
    ):
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.sleep_seconds = sleep_seconds
        self.session = requests.Session()

    def _get_json(self, endpoint: str, **params: str) -> dict:
        time.sleep(self.sleep_seconds)
        payload = {"crtfc_key": self.api_key, **params}
        response = self.session.get(
            f"{DART_BASE}/{endpoint}", params=payload, timeout=30
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") not in {"000", "013"}:
            raise RuntimeError(
                f"DART API error {data.get('status')}: {data.get('message')}"
            )
        return data

    def load_companies(self, force: bool = False) -> list[Company]:
        cache_file = self.cache_dir / "corpCode.xml"
        if force or not cache_file.exists():
            response = self.session.get(
                f"{DART_BASE}/corpCode.xml",
                params={"crtfc_key": self.api_key},
                timeout=60,
            )
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

    def find_company(
        self,
        corp_name: str | None = None,
        stock_code: str | None = None,
    ) -> Company:
        if not corp_name and not stock_code:
            raise ValueError("corp_name or stock_code is required")
        matches = self.load_companies()
        if corp_name:
            matches = [
                c for c in matches
                if c.corp_name == corp_name or corp_name in c.corp_name
            ]
        if stock_code:
            matches = [c for c in matches if c.stock_code == stock_code]
        listed = [c for c in matches if c.stock_code]
        if not listed:
            raise ValueError(
                f"No listed DART company matched "
                f"corp_name={corp_name!r}, stock_code={stock_code!r}"
            )
        listed.sort(
            key=lambda c: (c.corp_name != corp_name if corp_name else False, c.stock_code)
        )
        return listed[0]

    def filings(
        self,
        corp_code: str,
        start: str,
        end: str,
        final_only: bool = True,
    ) -> list[Filing]:
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
                if any(
                    kw in report_nm
                    for kw in ("분기보고서", "반기보고서", "사업보고서")
                ):
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

    def financial_statement_all(
        self,
        corp_code: str,
        year: int,
        reprt_code: str,
        fs_div: str = "CFS",
    ) -> pd.DataFrame:
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


# ── 내부 유틸 ────────────────────────────────────────────────────

def period_from_report_code(year: int, reprt_code: str) -> str:
    return f"{year}-{REPORT_CODES[reprt_code]}"


def _to_number(value: object) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
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
    """손익계산서(IS) 계정명 → 내부 metric 이름 매핑."""
    account_nm = str(row.get("account_nm", ""))
    account_id = str(row.get("account_id", ""))
    for metric, aliases in ACCOUNT_ALIASES.items():
        if account_nm in aliases or any(
            alias.lower() == account_nm.lower() for alias in aliases
        ):
            return metric
    # account_id 기반 fallback
    if "Revenue" in account_id and "Cost" not in account_id:
        return "revenue"
    if "GrossProfit" in account_id:
        return "gross_profit"
    if "Operating" in account_id and "Profit" in account_id:
        return "operating_profit"
    if "CostOfSales" in account_id or "CostOfGoodsSold" in account_id:
        return "cogs"
    return None


def _match_bs_account(row: pd.Series) -> str | None:
    """재무상태표(BS) 계정명 → 내부 metric 이름 매핑."""
    account_nm = str(row.get("account_nm", ""))
    account_id = str(row.get("account_id", ""))
    for metric, aliases in BS_ACCOUNT_ALIASES.items():
        if account_nm in aliases or any(
            alias.lower() == account_nm.lower() for alias in aliases
        ):
            return metric
    # account_id 기반 fallback
    if "Inventories" in account_id:
        return "inventory"
    if "TradeReceivables" in account_id or "TradeAndOtherReceivables" in account_id:
        return "accounts_receivable"
    if "ShortTermBorrowings" in account_id:
        return "short_term_borrowings"
    if "LongTermBorrowings" in account_id:
        return "long_term_borrowings"
    if "BondsPayable" in account_id or "Bonds" in account_id:
        return "bonds_payable"
    return None


# ── IS 누적 데이터 정규화 ─────────────────────────────────────────

def normalize_cumulative_financials(
    frames: Iterable[tuple[int, str, pd.DataFrame]],
) -> pd.DataFrame:
    """
    (year, reprt_code, DataFrame) 리스트에서 IS 누적 데이터를 추출해
    period_label × metric 형태로 반환.

    DART 분기보고서 금액 필드 구분
    ────────────────────────────────────────────────────────────────
    분기보고서(11013/11014)의 손익계산서:
      thstrm_amount     : 당해 분기 3개월 단독 금액   ← Q1·Q3 단독값
      thstrm_add_amount : 당해 사업연도 누적 금액     ← Q1 누적 = Q1,
                                                        Q3 누적 = Q1+Q2+Q3
    반기보고서(11012):
      thstrm_amount     : 반기(1~6월) 누적 금액
    사업보고서(11011):
      thstrm_amount     : 연간 전체 금액

    Q4 차감 계산이 올바르려면 Q3에는 반드시 누적값(thstrm_add_amount)을 써야 한다.
    """
    records: list[dict] = []
    for year, reprt_code, frame in frames:
        if frame.empty:
            continue
        income = frame[
            frame.get("sj_div", pd.Series(dtype=str)).isin(["IS", "CIS"])
        ]
        report_period = REPORT_CODES.get(reprt_code, "")
        for _, row in income.iterrows():
            metric = _match_account(row)
            if not metric:
                continue

            # DART 분기보고서 금액 필드 선택 전략
            # ─────────────────────────────────────────────────────
            # Q3(11014): thstrm_amount = 3개월 단독 / thstrm_add_amount = 9개월 누적
            #            → add_amount 가 있고 abs(add_amount) >= abs(thstrm_amount) 이면 누적으로 판단
            #            → 없거나 작으면 thstrm_amount 사용 (일부 회사는 thstrm_amount 가 이미 누적)
            # Q1(11013): 둘 다 1분기 값이므로 동일. thstrm_amount 우선
            # H1(11012) / FY(11011): thstrm_amount 가 이미 누적값
            if report_period == "Q3":
                add_amt  = _to_number(row.get("thstrm_add_amount"))
                term_amt = _to_number(row.get("thstrm_amount"))
                if add_amt is not None and term_amt is not None:
                    # 절댓값 기준: add_amount >= thstrm_amount → 누적으로 판단
                    amount = add_amt if abs(add_amt) >= abs(term_amt) else term_amt
                elif add_amt is not None:
                    amount = add_amt
                else:
                    amount = term_amt
            else:
                # Q1·H1·FY 는 thstrm_amount 사용 (이미 누적 또는 전체)
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
    df = df.sort_values(
        ["year", "reprt_code", "metric", "ordinal_num"], na_position="last"
    )
    return df.drop_duplicates(["period_label", "metric"], keep="first").drop(
        columns=["ordinal_num"]
    )


# ── BS 시점 데이터 정규화 (신규) ──────────────────────────────────

def normalize_bs_financials(
    frames: Iterable[tuple[int, str, pd.DataFrame]],
) -> pd.DataFrame:
    """
    (year, reprt_code, DataFrame) 리스트에서 BS 기말 잔액을 추출해
    period × metric 형태로 반환.
    BS 항목은 누적값이 아닌 시점 잔액이므로 차감 계산 불필요.
    """
    records: list[dict] = []
    for year, reprt_code, frame in frames:
        if frame.empty:
            continue
        bs = frame[frame.get("sj_div", pd.Series(dtype=str)) == "BS"]
        quarter = PERIOD_ORDER[REPORT_CODES[reprt_code]]
        period = f"{year}Q{quarter}"
        for _, row in bs.iterrows():
            metric = _match_bs_account(row)
            if not metric:
                continue
            amount = _to_number(row.get("thstrm_amount"))
            if amount is None:
                continue
            records.append(
                {
                    "year": year,
                    "reprt_code": reprt_code,
                    "period": period,
                    "metric": metric,
                    "amount": amount,
                    "account_nm": row.get("account_nm"),
                    "account_id": row.get("account_id"),
                    "ordinal": row.get("ord"),
                }
            )
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    df["ordinal_num"] = pd.to_numeric(df["ordinal"], errors="coerce")
    df = df.sort_values(
        ["year", "reprt_code", "metric", "ordinal_num"], na_position="last"
    )
    return df.drop_duplicates(["period", "metric"], keep="first").drop(
        columns=["ordinal_num"]
    )


# ── IS 분기 파생 ──────────────────────────────────────────────────

# DART 보고서 코드별 처리 순서 (연도 내 시간 순)
_REPORT_PROCESS_ORDER = {"Q1": 1, "H1": 2, "Q3": 3, "FY": 4}
# 보고서 코드 → 분기 번호 매핑 (H1=반기 → Q2, FY=사업보고서 → Q4)
_REPORT_TO_QUARTER = {"Q1": 1, "H1": 2, "Q3": 3, "FY": 4}


def derive_quarterly_metrics(cumulative: pd.DataFrame) -> pd.DataFrame:
    """
    누적 IS 데이터 → 실제 분기 실적 DataFrame

    DART 보고서별 누적 구조
    ───────────────────────────────────────────────────────────
    11013 (분기보고서 Q1) : 1~3월 누적
    11012 (반기보고서 H1) : 1~6월 누적
    11014 (분기보고서 Q3) : 1~9월 누적
    11011 (사업보고서  FY) : 1~12월 누적  ← Q4 별도 보고서 없음

    차감 규칙 (연도 내 순서대로 누적 추적)
    ───────────────────────────────────────────────────────────
    Q1 실적 = Q1 누적                     (기준값, 차감 없음)
    Q2 실적 = H1 누적  − Q1 누적
    Q3 실적 = Q3 누적  − H1 누적
    Q4 실적 = FY 누적  − Q3 누적          ← 사업보고서에서 직전 9개월 차감

    중간 보고서 누락 시 처리
    ───────────────────────────────────────────────────────────
    예) Q3 보고서 없이 FY만 있으면:
        Q4로 표기하되 data_note = "FY−H1 (Q3 보고서 없음)"
    예) Q1·H1 없이 FY만 있으면:
        Q4로 표기하되 data_note = "FY only (중간 보고서 없음)"
    이 경우 해당 분기 수치는 복수 분기 합산임을 data_note 로 표시.
    """
    if cumulative.empty:
        return pd.DataFrame()

    # ── 피벗: (year, report_period) × metric ──
    metric_cols = [
        c for c in ["revenue", "cogs", "gross_profit", "operating_profit", "net_income"]
        if c in cumulative["metric"].values
    ]
    if not metric_cols:
        return pd.DataFrame()

    pivot = (
        cumulative.pivot_table(
            index=["year", "report_period"],
            columns="metric",
            values="amount",
            aggfunc="first",
        )
        .reset_index()
    )
    pivot["_order"] = pivot["report_period"].map(_REPORT_PROCESS_ORDER)
    pivot = pivot.sort_values(["year", "_order"]).reset_index(drop=True)

    # ── 연도별 명시적 누적 차감 루프 ──
    records: list[dict] = []

    for year, year_df in pivot.groupby("year", sort=True):
        year_df = year_df.sort_values("_order").reset_index(drop=True)

        # 직전 누적값 (연초 = 0으로 초기화)
        prev_cum: dict[str, float] = {m: 0.0 for m in metric_cols}
        prev_label: str = "연초"   # 어떤 보고서를 baseline으로 쓰는지 추적

        for _, row in year_df.iterrows():
            rp: str = row["report_period"]   # "Q1" | "H1" | "Q3" | "FY"
            quarter: int = _REPORT_TO_QUARTER[rp]


            rec: dict = {
                "year": int(year),
                "quarter": quarter,
                "period": f"{int(year)}Q{quarter}",
                "data_source": f"{rp} - {prev_label}" if prev_label != "연초" else rp,
            }

            for m in metric_cols:
                cum_val = row.get(m)
                if pd.isna(cum_val):
                    rec[m] = float("nan")
                    continue
                cum_val = float(cum_val)
                rec[m] = cum_val - prev_cum[m]
                prev_cum[m] = cum_val

            prev_label = rp
            records.append(rec)

    if not records:
        return pd.DataFrame()

    out = pd.DataFrame(records)

    # 마진율 계산
    if "revenue" in out.columns:
        rev = out["revenue"].replace(0, float("nan"))
        if "operating_profit" in out.columns:
            out["opm"] = out["operating_profit"] / rev
        if "gross_profit" in out.columns:
            out["gpm"] = out["gross_profit"] / rev
        if "net_income" in out.columns:
            out["npm"] = out["net_income"] / rev
        if "cogs" in out.columns:
            out["cogs_ratio"] = out["cogs"] / rev

    # cogs 없을 경우 gross_profit 역산
    if "cogs" not in out.columns and "gross_profit" in out.columns and "revenue" in out.columns:
        out["cogs"] = out["revenue"] - out["gross_profit"]
        out["cogs_ratio"] = out["cogs"] / out["revenue"].replace(0, float("nan"))

    return out


# -- 텍스트 추출 --

def extract_revenue_note_candidates(text: str) -> list[str]:
    import re
    keywords = ("매출처", "주요 고객", "주요 매출", "매출실적", "영업부문", "제품별", "지역별")
    blocks = re.split(r"\n{2,}|(?=\d+\.\s)", text)
    candidates = []
    for block in blocks:
        if any(keyword in block for keyword in keywords) and (
            "매출" in block or "수익" in block
        ):
            compact = re.sub(r"\s+", " ", block).strip()
            if 60 <= len(compact) <= 3000:
                candidates.append(compact)
    return candidates[:20]


def write_filings_csv(filings, output: Path) -> None:
    from dataclasses import asdict
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([asdict(f) for f in filings]).to_csv(
        output, index=False, encoding="utf-8-sig"
    )


def write_company_json(company, output: Path) -> None:
    from dataclasses import asdict
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(asdict(company), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
