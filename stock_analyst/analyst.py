"""
stock_analyst/analyst.py  ―  파생지표 계산 + 텍스트 기반 지표 추출

주요 기능:
  - derive_full_metrics()       : IS 분기 + BS 기말 합산 → 회전율 등 파생지표 계산
  - extract_order_backlog()     : 수주잔고 / 기납품 수주잔고 텍스트 추출
  - extract_utilization()       : 공장 가동율 텍스트 추출
  - extract_product_revenue()   : 제품별/부문별 매출 현황 텍스트 추출
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

def _parse_amount_in_snippet(snippet: str, hint_unit: str | None = None) -> tuple[float | None, str | None]:
    """
    스니펫에서 금액 숫자를 찾아 (값_억원, 단위문자열) 반환.
    hint_unit: 표 단위 헤더에서 미리 파악된 단위 (예: "억원", "백만원")
    """
    multiplier_map = {
        "백만원": 0.01, "억원": 1.0, "천억원": 1000.0,
        "조원": 10000.0, "원": 1e-8,
    }
    # 단위가 표 헤더에 적혀 있는 경우 (예: "(단위 : 억원)")
    if hint_unit is None:
        unit_header = re.search(r"단위\s*[：:]\s*(백만원|억원|천억원|조원|원)", snippet)
        if unit_header:
            hint_unit = unit_header.group(1)

    # 명시적 단위 붙은 숫자 우선
    m = re.search(r"([0-9,]{3,})\s*(백만원|억원|천억원|조원|원)", snippet)
    if m:
        raw = m.group(1).replace(",", "")
        unit = m.group(2)
        try:
            return float(raw) * multiplier_map.get(unit, 1.0), unit
        except ValueError:
            pass

    # 단위 헤더 있고, 순수 숫자만 있는 경우 (표 셀 분리된 경우)
    if hint_unit:
        nums = re.findall(r"\b([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{4,})\b", snippet)
        if nums:
            raw = nums[-1].replace(",", "")  # 마지막 큰 숫자 사용
            try:
                return float(raw) * multiplier_map.get(hint_unit, 1.0), hint_unit
            except ValueError:
                pass

    return None, None


def extract_order_backlog(text: str) -> list[dict[str, Any]]:
    """
    DART 보고서 원문에서 수주잔고 / 기납품 수주잔고 관련 문단을 추출.

    DART HTML 표는 텍스트 추출 시 셀이 줄바꿈으로 분리되므로
    "수주상황" 섹션 전체를 넓게 캡처하는 방식으로 탐지합니다.

    반환값: [{"label": str, "snippet": str, "parsed_value": float|None, "unit": str|None}, ...]
    """
    results: list[dict[str, Any]] = []

    # ── 표 단위 헤더 파싱 (전체 텍스트에서 먼저 확인) ──
    global_unit: str | None = None
    unit_m = re.search(r"단위\s*[：:]\s*(?:천[㎡㎥]?\s*,\s*)?(백만원|억원|천억원|조원|원)", text)
    if unit_m:
        global_unit = unit_m.group(1)

    # ── 섹션 헤더 탐지 (넓은 창으로 전체 섹션 캡처) ──
    section_patterns = [
        # "라. 수주상황", "마. 수주현황" 등 목차 형식
        r"[가나다라마바사아자차카타파하]\.\s*수주[상황현황잔고]",
        r"\d+\.\s*수주[상황현황잔고]",
        r"수주\s*상황",
        r"수주\s*현황",
        r"수주잔고\s*현황",
        r"Order\s*Backlog",
        r"수주\s*실적",
    ]
    for pat in section_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            start = max(0, m.start() - 50)
            end = min(len(text), m.end() + 2000)   # 섹션 전체 캡처 (2000자)
            block = text[start:end]
            snippet = re.sub(r"\s+", " ", block).strip()

            # 단위 파악 (로컬 우선, 없으면 전역)
            local_unit_m = re.search(r"단위\s*[：:]\s*(?:천[㎡㎥]?\s*,\s*)?(백만원|억원|천억원|조원|원)", block)
            hint = local_unit_m.group(1) if local_unit_m else global_unit

            parsed, found_unit = _parse_amount_in_snippet(snippet, hint)
            label = "기납품 수주잔고" if "기납품" in snippet[:300] else "수주잔고"
            results.append({
                "label": label,
                "snippet": snippet[:1000],
                "parsed_value": parsed,
                "unit": found_unit or hint or "억원",
            })

    # ── 수주잔고 숫자 직접 패턴 (단위 명시된 경우) ──
    inline_patterns = [
        r"수주잔고\s*[:\s]*([0-9,]+)\s*(백만원|억원|천억원|조원|원)",
        r"수주잔액\s*[:\s]*([0-9,]+)\s*(백만원|억원|천억원|조원|원)",
        r"잔여\s*수주\s*[:\s]*([0-9,]+)\s*(백만원|억원|천억원|조원|원)",
        r"Order\s*Backlog\s*[:\s]*([0-9,]+)\s*(백만원|억원|천억원|조원|원)",
        r"기납품\s*수주잔고\s*[:\s]*([0-9,]+)\s*(백만원|억원|천억원|조원|원)",
    ]
    multiplier_map = {"백만원": 0.01, "억원": 1.0, "천억원": 1000.0, "조원": 10000.0, "원": 1e-8}
    for pat in inline_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            raw = m.group(1).replace(",", "")
            unit = m.group(2)
            try:
                val = float(raw) * multiplier_map.get(unit, 1.0)
            except ValueError:
                val = None
            start = max(0, m.start() - 200)
            end = min(len(text), m.end() + 300)
            label = "기납품 수주잔고" if "기납품" in m.group(0) else "수주잔고"
            results.append({
                "label": label,
                "snippet": re.sub(r"\s+", " ", text[start:end]).strip(),
                "parsed_value": val,
                "unit": unit,
            })

    # ── 기납품 키워드 별도 탐지 ──
    for m in re.finditer(r"기납품", text):
        start = max(0, m.start() - 100)
        end = min(len(text), m.end() + 600)
        block = text[start:end]
        snippet = re.sub(r"\s+", " ", block).strip()
        local_unit_m = re.search(r"단위\s*[：:]\s*(?:천[㎡㎥]?\s*,\s*)?(백만원|억원|천억원|조원|원)", block)
        hint = local_unit_m.group(1) if local_unit_m else global_unit
        parsed, found_unit = _parse_amount_in_snippet(snippet, hint)
        results.append({
            "label": "기납품 수주잔고",
            "snippet": snippet[:600],
            "parsed_value": parsed,
            "unit": found_unit or hint,
        })

    # 중복 제거
    seen: set[str] = set()
    deduped = []
    for item in results:
        key = item["snippet"][:120]
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
            pct_match = re.search(r"([0-9]+\.?[0-9]*)\s*%", snippe