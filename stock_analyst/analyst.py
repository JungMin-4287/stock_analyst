"""
stock_analyst/analyst.py  ―  파생지표 계산 + 텍스트 기반 지표 추출

주요 기능:
  - derive_full_metrics()     : IS 분기 + BS 기말 합산 → 회전율 등 파생지표 계산
  - extract_order_backlog()   : 수주잔고 / 기납품 수주잔고 텍스트 추출
  - extract_utilization()     : 공장 가동율 텍스트 추출
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd


# ── 전체 지표 통합 ────────────────────────────────────────────────

def derive_full_metrics(
    quarterly_is: pd.DataFrame,
    bs_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    분기 IS DataFrame 과 BS 기말잔액 DataFrame 을 병합하고
    재고자산회전율, 매출채권회전율 등 파생지표를 추가해 반환.

    Parameters
    ----------
    quarterly_is : derive_quarterly_metrics() 결과 (period 컬럼 포함)
    bs_df        : normalize_bs_financials() 결과 (period 컬럼 포함)

    Returns
    -------
    모든 지표가 포함된 DataFrame (period 기준 인덱스)
    """
    if quarterly_is.empty:
        return quarterly_is

    df = quarterly_is.copy()

    if bs_df.empty:
        return df

    # BS 데이터를 wide format으로 변환
    bs_pivot = bs_df.pivot_table(
        index="period", columns="metric", values="amount", aggfunc="first"
    ).reset_index()

    # 차입금 합산: 직접 제공되는 total_borrowings_direct 우선,
    # 없으면 단기 + 장기 + 사채 + 유동성 합산
    borrow_sub_cols = [
        c for c in [
            "short_term_borrowings", "long_term_borrowings",
            "bonds_payable", "current_lt_debt",
        ]
        if c in bs_pivot.columns
    ]
    if "total_borrowings_direct" in bs_pivot.columns:
        bs_pivot["total_borrowings"] = bs_pivot["total_borrowings_direct"]
    elif borrow_sub_cols:
        bs_pivot["total_borrowings"] = bs_pivot[borrow_sub_cols].fillna(0).sum(axis=1)
        # 합산이 0이면 NaN 처리
        bs_pivot.loc[bs_pivot["total_borrowings"] == 0, "total_borrowings"] = float("nan")

    # IS + BS 병합
    df = df.merge(bs_pivot, on="period", how="left")

    # ── 회전율 계산 (분기 연환산: ×4) ──────────────────────────────
    # 평균은 (전분기말 + 당분기말) / 2

    if "inventory" in df.columns:
        df["avg_inventory"] = (df["inventory"] + df["inventory"].shift(1)) / 2
        cogs_col = "cogs" if "cogs" in df.columns else None
        if cogs_col:
            df["inventory_turnover"] = (df[cogs_col] * 4) / df["avg_inventory"]
        elif "revenue" in df.columns:
            # COGS 없을 때 매출액으로 근사
            df["inventory_turnover"] = (df["revenue"] * 4) / df["avg_inventory"]
        # DIO (재고일수)
        df["dio"] = 365 / df["inventory_turnover"]

    if "accounts_receivable" in df.columns and "revenue" in df.columns:
        df["avg_ar"] = (
            df["accounts_receivable"] + df["accounts_receivable"].shift(1)
        ) / 2
        df["ar_turnover"] = (df["revenue"] * 4) / df["avg_ar"]
        # DSO (매출채권회수일수)
        df["dso"] = 365 / df["ar_turnover"]

    # 불필요한 세부 차입금 컬럼 제거 (total_borrowings 로 통합)
    drop_cols = [
        c for c in [
            "avg_inventory", "avg_ar",
            "short_term_borrowings", "long_term_borrowings",
            "bonds_payable", "current_lt_debt", "total_borrowings_direct",
        ]
        if c in df.columns
    ]
    df = df.drop(columns=drop_cols)

    return df


# ── 텍스트 기반 지표 추출 ─────────────────────────────────────────

def extract_order_backlog(text: str) -> list[dict[str, Any]]:
    """
    DART 보고서 원문에서 수주잔고 / 기납품 수주잔고 관련 문단을 추출.
    반환값: [{"label": str, "snippet": str, "parsed_value": float|None, "unit": str|None}, ...]
    """
    results: list[dict[str, Any]] = []

    # ─ 패턴 1: 수주잔고 숫자 직접 파싱 ─
    # 예) 수주잔고 : 1,234,567백만원  /  수주잔액 2,345억원
    number_patterns = [
        r"수주잔고\s*[:\s]*([0-9,]+)\s*(백만원|억원|천억원|조원|원)",
        r"수주잔액\s*[:\s]*([0-9,]+)\s*(백만원|억원|천억원|조원|원)",
        r"잔여\s*수주\s*[:\s]*([0-9,]+)\s*(백만원|억원|천억원|조원|원)",
        r"Order\s*Backlog\s*[:\s]*([0-9,]+)\s*(백만원|억원|천억원|조원|원|KRW|billion|million)",
    ]
    for pattern in number_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            raw = m.group(1).replace(",", "")
            unit = m.group(2)
            try:
                val = float(raw)
                # 억원 기준으로 통일
                multiplier = {"백만원": 0.01, "억원": 1.0, "천억원": 1000.0, "조원": 10000.0, "원": 1e-8}
                val_billion = val * multiplier.get(unit, 1.0)
            except ValueError:
                val_billion = None
            start = max(0, m.start() - 200)
            end = min(len(text), m.end() + 200)
            results.append({
                "label": "수주잔고",
                "snippet": re.sub(r"\s+", " ", text[start:end]).strip(),
                "parsed_value": val_billion,
                "unit": "억원",
            })

    # ─ 패턴 2: 수주잔고 현황 테이블 전후 문맥 ─
    context_keywords = [
        "수주잔고 현황", "수주현황", "기납품 수주잔고", "수주잔고(기납품)",
        "수주잔량", "잔여수주", "수주 잔고", "납품예정",
    ]
    for keyword in context_keywords:
        for m in re.finditer(re.escape(keyword), text):
            start = max(0, m.start() - 100)
            end = min(len(text), m.end() + 800)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            label = "기납품 수주잔고" if "기납품" in keyword else "수주잔고"
            # 숫자 파싱 시도
            num_match = re.search(r"([0-9,]+)\s*(백만원|억원|조원|원)", snippet)
            parsed = None
            if num_match:
                raw = num_match.group(1).replace(",", "")
                unit = num_match.group(2)
                multiplier = {"백만원": 0.01, "억원": 1.0, "조원": 10000.0, "원": 1e-8}
                try:
                    parsed = float(raw) * multiplier.get(unit, 1.0)
                except ValueError:
                    pass
            results.append({
                "label": label,
                "snippet": snippet[:600],
                "parsed_value": parsed,
                "unit": "억원" if parsed is not None else None,
            })

    # 중복 제거 (snippet 앞 100자 기준)
    seen: set[str] = set()
    deduped = []
    for item in results:
        key = item["snippet"][:100]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped[:15]


def extract_utilization(text: str) -> list[dict[str, Any]]:
    """
    DART 보고서 원문에서 공장 가동율/가동률 관련 문단을 추출.
    반환값: [{"label": str, "snippet": str, "parsed_pct": float|None}, ...]
    """
    results: list[dict[str, Any]] = []

    # ─ 패턴 1: 가동율 숫자 직접 파싱 ─
    number_patterns = [
        r"가동율\s*[:\s]*([0-9]+\.?[0-9]*)\s*%",
        r"가동률\s*[:\s]*([0-9]+\.?[0-9]*)\s*%",
        r"공장\s*가동[율률]\s*[:\s]*([0-9]+\.?[0-9]*)\s*%",
        r"Utilization\s+[Rr]ate\s*[:\s]*([0-9]+\.?[0-9]*)\s*%",
        r"([0-9]+\.?[0-9]*)\s*%\s*(?:의\s*)?가동[율률]",
    ]
    for pattern in number_patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            try:
                pct = float(m.group(1))
            except (ValueError, IndexError):
                pct = None
            start = max(0, m.start() - 200)
            end = min(len(text), m.end() + 300)
            results.append({
                "label": "공장 가동율",
                "snippet": re.sub(r"\s+", " ", text[start:end]).strip(),
                "parsed_pct": pct,
            })

    # ─ 패턴 2: 가동율 관련 문맥 ─
    context_keywords = [
        "가동율 현황", "가동률 현황", "설비가동", "공장가동", "생산설비 가동",
        "생산능력", "가동 실적", "Capacity Utilization",
    ]
    for keyword in context_keywords:
        for m in re.finditer(re.escape(keyword), text, re.IGNORECASE):
            start = max(0, m.start() - 50)
            end = min(len(text), m.end() + 600)
            snippet = re.sub(r"\s+", " ", text[start:end]).strip()
            pct_match = re.search(r"([0-9]+\.?[0-9]*)\s*%", snippet)
            pct = None
            if pct_match:
                try:
                    pct = float(pct_match.group(1))
                except ValueError:
                    pass
            results.append({
                "label": "공장 가동율",
                "snippet": snippet[:600],
                "parsed_pct": pct,
            })

    # 중복 제거
    seen: set[str] = set()
    deduped = []
    for item in results:
        key = item["snippet"][:100]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped[:10]


# ── 컬럼 한글 레이블 매핑 ─────────────────────────────────────────

COLUMN_LABELS: dict[str, str] = {
    "period":              "분기",
    "revenue":             "매출액(십억원)",
    "cogs":                "매출원가(십억원)",
    "gross_profit":        "매출총이익(십억원)",
    "operating_profit":    "영업이익(십억원)",
    "net_income":          "순이익(십억원)",
    "opm":                 "OPM(%)",
    "gpm":                 "GPM(%)",
    "npm":                 "NPM(%)",
    "cogs_ratio":          "원가비중(%)",
    "inventory":           "재고자산(십억원)",
    "accounts_receivable": "매출채권(십억원)",
    "total_borrowings":    "차입금(십억원)",
    "inventory_turnover":  "재고자산회전율(회/년)",
    "ar_turnover":         "매출채권회전율(회/년)",
    "dio":                 "재고일수(DIO, 일)",
    "dso":                 "매출채권회수일(DSO, 일)",
}

# 표시할 때 십억원으로 변환이 필요한 컬럼
BILLION_WON_COLS = {
    "revenue", "cogs", "gross_profit", "operating_profit",
    "net_income", "inventory", "accounts_receivable", "total_borrowings",
}

# 퍼센트로 표시할 컬럼
PERCENT_COLS = {"opm", "gpm", "npm", "cogs_ratio"}


def format_display_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    숫자 포맷 적용된 표시용 DataFrame 반환.
    십억원 컬럼 → 소수점 1자리, 퍼센트 컬럼 → ×100 후 소수점 1자리
    """
    out = df.copy()
    for col in df.columns:
        if col in BILLION_WON_COLS and col in out.columns:
            out[col] = (out[col] / 1_000_000_000).round(1)
        elif col in PERCENT_COLS and col in out.columns:
            out[col] = (out[col] * 100).round(1)
        elif col in {"inventory_turnover", "ar_turnover"} and col in out.columns:
            out[col] = out[col].round(2)
        elif col in {"dio", "dso"} and col in out.columns:
            out[col] = out[col].round(1)
    # 컬럼명 한글로 치환
    rename_map = {c: COLUMN_LABELS.get(c, c) for c in out.columns}
    return out.rename(columns=rename_map)
