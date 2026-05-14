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

def _parse_backlog_value(snippet: str, hint_unit: str | None = None) -> tuple[float | None, str | None]:
    """
    수주잔고 섹션 스니펫에서 잔고 금액을 추출.

    DART 수주잔고 표 구조 (테이블 → 텍스트 추출 시):
        품목  수주일  납기  수주총액  기납품액  수주잔고
        ...
        합계  -  -  [수주총액합]  [기납품합]  [수주잔고합]  ← 마지막 열이 수주잔고

    핵심: 수주잔고 = 마지막 열 = 수주총액보다 작은 값.
          max() 로 가져오면 수주총액이 걸린다 → WRONG.
    """
    multiplier_map = {
        "백만원": 0.01, "억원": 1.0, "천억원": 1000.0,
        "조원": 10000.0, "원": 1e-8, "천원": 1e-5,
    }

    # 단위 재확인
    if hint_unit is None:
        unit_m = re.search(
            r"단위\s*[：:\s]\s*(?:천[㎡㎥]?\s*[,，]\s*)?(백만원|억원|천억원|조원|원|천원)",
            snippet
        )
        if unit_m:
            hint_unit = unit_m.group(1)

    mult = multiplier_map.get(hint_unit or "", 1.0)

    # ① "수주잔고" 컨텍스트에서 명시적 단위 붙은 숫자만 (수주총액 오인 방지)
    #    수주잔고 키워드 뒤 50자 이내에 number+unit 패턴이 있을 때만 사용
    #    (H1/Q3 보고서에서 "수주총액은 13,162억원" 같은 문장이 앞에 나와
    #     수주총액을 잘못 반환하는 버그 방지)
    backlog_ctx = re.search(
        r"수주\s*잔고.{0,50}?([0-9,]{3,})\s*(백만원|억원|천억원|조원|원|천원)",
        snippet, re.DOTALL
    )
    if backlog_ctx:
        raw = backlog_ctx.group(1).replace(",", "")
        unit = backlog_ctx.group(2)
        try:
            return float(raw) * multiplier_map.get(unit, 1.0), unit
        except ValueError:
            pass

    if not hint_unit:
        return None, None

    # ② "합계" / "계" 행 — 같은 행(개행 전)의 숫자만 추출 → 마지막 = 수주잔고 열
    #    300자 윈도우는 각주 숫자를 오염시키므로 첫 개행까지만 사용
    agg_m = re.search(r"(?:합\s*계|소\s*계)", snippet)
    if agg_m:
        rest = snippet[agg_m.end():]
        # 같은 행 끝(개행) 또는 최대 200자 — 각주가 섞이지 않도록
        nl_pos = rest.find("\n")
        row_text = rest[:nl_pos] if 0 < nl_pos <= 200 else rest[:200]
        nums = re.findall(r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{5,}", row_text)
        # 연도 제외 (4자리 19xx/20xx)
        nums = [n for n in nums if not re.fullmatch(r"(?:19|20)\d{2}", n.replace(",", ""))]
        if nums:
            try:
                # 마지막 값 = 수주잔고 열 (수주총액 → 기납품액 → 수주잔고 순서)
                return float(nums[-1].replace(",", "")) * mult, hint_unit
            except ValueError:
                pass

    # ③ "수주잔고" 헤더 이후 첫 번째 숫자 (단일 행 형태)
    col_m = re.search(r"수주\s*잔고[^0-9]{0,30}([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{5,})", snippet)
    if col_m:
        try:
            return float(col_m.group(1).replace(",", "")) * mult, hint_unit
        except ValueError:
            pass

    # ④ 전체 스니펫에서 숫자 목록 수집 후 마지막-에서-두번째 값
    #    (마지막 열 = 수주잔고, 그 직전 = 기납품액으로 가정)
    nums_all = re.findall(r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{5,}", snippet)
    nums_all = [n for n in nums_all if not re.fullmatch(r"(?:19|20)\d{2}", n.replace(",", ""))]
    if len(nums_all) >= 3:
        # 마지막 숫자: 수주잔고 (가장 적은 값) — max 쓰지 말고 last 사용
        try:
            return float(nums_all[-1].replace(",", "")) * mult, hint_unit
        except ValueError:
            pass
    elif nums_all:
        try:
            return float(nums_all[-1].replace(",", "")) * mult, hint_unit
        except ValueError:
            pass

    return None, None


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
        # "라. 수주상황", "가. 수주잔고" 등 목차 하위 항목
        r"[가나다라마바사아자차카타파하]\.\s*수주[상황현황잔고]",
        # "4. 수주상황", "4. 수주현황"
        r"\d+\.\s*수주[상황현황잔고실적]",
        # ★ "4. 매출 및 수주상황" — DART 빈출 헤딩 (대명에너지 등)
        r"\d+\.\s*매출\s*(?:및\s*)?수주[상황현황]",
        r"매출\s*및\s*수주[상황현황]",
        # 단독 키워드
        r"수주\s*상황",
        r"수주\s*현황",
        r"수주잔고\s*현황",
        r"Order\s*Backlog",
        r"수주\s*실적",
        r"수주\s*잔고",
    ]
    for pat in section_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            start = max(0, m.start() - 50)
            end = min(len(text), m.end() + 3000)   # 섹션 전체 캡처 (3000자)
            block = text[start:end]
            snippet = re.sub(r"\s+", " ", block).strip()

            # 단위 파악 (로컬 우선, 없으면 전역)
            local_unit_m = re.search(
                r"단위\s*[：:\s]\s*(?:천[㎡㎥]?\s*[,，]\s*)?(백만원|억원|천억원|조원|원|천원)",
                block
            )
            hint = local_unit_m.group(1) if local_unit_m else global_unit

            # 합계/잔고 행에서 마지막 큰 수치 우선 파싱
            parsed, found_unit = _parse_backlog_value(snippet, hint)
            label = "기납품 수주잔고" if "기납품" in snippet[:400] else "수주잔고"
            results.append({
                "label": label,
                "snippet": snippet[:1200],
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

    seen: set[str] = set()
    deduped = []
    for item in results:
        key = item["snippet"][:100]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped[:10]


def extract_product_revenue(text: str) -> list[dict]:
    results: list[dict] = []
    section_patterns = [
        r"[가나다라마바사아자차카타파하]\.\s*(?:주요\s*)?제품[별]?\s*(?:및\s*서비스)?\s*(?:매출|현황|실적)",
        r"\d+\.\s*(?:주요\s*)?제품[별]?\s*(?:매출|현황|실적)",
        r"제품별\s*매출\s*현황",
        r"품목별\s*매출",
        r"매출\s*현황",
        r"사업부문별\s*(?:매출|실적|현황)",
        r"부문별\s*(?:매출|실적|현황)",
        r"제품\s*및\s*서비스\s*현황",
        r"주요\s*제품\s*및\s*서비스",
        r"매출에\s*관한\s*사항",
        r"영업부문\s*(?:매출|현황)",
        r"지역별\s*(?:매출|현황)",
    ]
    for pat in section_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 2500)
            block = text[start:end]
            snippet = re.sub(r"\s+", " ", block).strip()
            unit_m = re.search(r"단위\s*[:：]\s*(백만원|억원|천억원|조원|원|천원)", block)
            unit = unit_m.group(1) if unit_m else None
            numbers = re.findall(r"\b([0-9]{1,3}(?:,[0-9]{3})+|[0-9]{5,})\b", snippet)
            label_text = re.sub(r"\s+", " ", text[m.start():m.end()]).strip()
            results.append({
                "label": label_text[:40],
                "snippet": snippet[:1200],
                "unit": unit,
                "numbers_found": len(numbers),
            })

    seen: set[str] = set()
    deduped = []
    for item in sorted(results, key=lambda x: -x["numbers_found"]):
        key = item["snippet"][:150]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return deduped[:12]


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
    "data_source":         "데이터출처",
}

BILLION_WON_COLS = {
    "revenue", "cogs", "gross_profit", "operating_profit",
    "net_income", "inventory", "accounts_receivable", "total_borrowings",
}

PERCENT_COLS = {"opm", "gpm", "npm", "cogs_ratio"}


def format_display_df(df: pd.DataFrame) -> pd.DataFrame:
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
    rename_map = {c: COLUMN_LABELS.get(c, c) for c in out.columns}
    return out.rename(columns=rename_map)
