"""
stock_analyst/backlog_probe.py  ―  수주잔고 자동 탐지 + 패턴 학습

핵심 원리:
  수주잔고(C) = 수주총액(A) - 기납품액(B)  → triple 검증으로 정확도 확보

결과 정책:
  - 보고서 1건당 최대 2개 반환 (수주잔고 1 + 기납품 수주잔고 1)
  - triple 검증 통과 결과만 신뢰; fallback은 "수주잔고" 키워드 컨텍스트가 있는 경우만
  - 중복은 텍스트 위치(offset) 기준으로 제거
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MULTIPLIER: dict[str, float] = {
    "백만원": 0.01, "억원": 1.0, "천억원": 1_000.0,
    "조원": 10_000.0, "원": 1e-8, "천원": 1e-5,
}
_CACHE_FILE = Path(__file__).parent / "data" / "learned_patterns.json"
_TRIPLE_TOL = 0.15
_NUM_PAT = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{5,}")
_YEAR_PAT = re.compile(r"^(?:19|20)\d{2}$")

# 신뢰도 등급 (낮을수록 우선)
_RANK = {"triple_passed": 0, "context_keyword": 1, "last_col_smallest": 2, "single_num": 9}


# ── 패턴 캐시 ─────────────────────────────────────────────────────

class PatternCache:
    def __init__(self, path: Path = _CACHE_FILE):
        self._path = path
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, corp_code: str) -> dict | None:
        return self._data.get(corp_code)

    def save(self, corp_code: str, corp_name: str, record: dict) -> None:
        self._data[corp_code] = {"corp_name": corp_name, **record}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    def all_entries(self) -> dict[str, Any]:
        return dict(self._data)


_cache = PatternCache()


# ── 유틸 ──────────────────────────────────────────────────────────

def _detect_unit(text: str) -> str | None:
    m = re.search(
        r"단위\s*[：:\s]\s*(?:천[㎡㎥]?\s*[,，]\s*)?(백만원|억원|천억원|조원|원|천원)",
        text,
    )
    return m.group(1) if m else None


def _extract_nums(text: str) -> list[float]:
    result = []
    for r in _NUM_PAT.findall(text):
        clean = r.replace(",", "")
        if _YEAR_PAT.match(clean):
            continue
        try:
            result.append(float(clean))
        except ValueError:
            pass
    return result


def _triple_check(a: float, b: float, c: float) -> bool:
    if a <= 0 or b < 0 or c < 0:
        return False
    expected = a - b
    if expected <= 0:
        return False
    denom = max(expected, c, 1.0)
    return abs(expected - c) / denom < _TRIPLE_TOL


# ── 블록 단위 추출 ────────────────────────────────────────────────

def _extract_from_block(
    block: str, offset: int, unit: str | None
) -> dict | None:
    """
    하나의 텍스트 블록에서 수주잔고 값을 추출.
    triple 검증 → context_keyword → last_col_smallest 순으로 시도.
    반환: {value, unit, validation, offset_start} 또는 None
    """
    local_unit = _detect_unit(block) or unit
    mult = MULTIPLIER.get(local_unit or "", 1.0)

    # ── ① triple 검증: 합계행 이후 3개 숫자 ──
    agg_m = re.search(r"(?:합\s*계|소\s*계)", block)
    if agg_m:
        window = block[agg_m.end(): agg_m.end() + 400]
        fn_m = re.search(r"(?:주\s*\d+\s*\)|※|참\s*고\s*:|\n{3,})", window)
        if fn_m:
            window = window[:fn_m.start()]
        nums = _extract_nums(window)
        if len(nums) >= 3:
            a, b, c = nums[-3], nums[-2], nums[-1]
            if _triple_check(a, b, c):
                return {"value": c * mult, "unit": local_unit, "validation": "triple_passed",
                        "offset_start": offset, "triple": (a, b, c)}
            # 마지막 값이 가장 작고 triple과 유사한 경우
            if c < a and c < b and c == nums[-1]:
                return {"value": c * mult, "unit": local_unit, "validation": "last_col_smallest",
                        "offset_start": offset}

    # ── ② "수주잔고" 키워드 바로 뒤 숫자+단위 ──
    ctx_m = re.search(
        r"수주\s*잔고.{0,60}?([0-9,]{3,})\s*(백만원|억원|천억원|조원|원|천원)",
        block, re.DOTALL,
    )
    if ctx_m:
        raw = ctx_m.group(1).replace(",", "")
        u2 = ctx_m.group(2)
        try:
            val = float(raw) * MULTIPLIER.get(u2, 1.0)
            return {"value": val, "unit": u2, "validation": "context_keyword",
                    "offset_start": offset}
        except ValueError:
            pass

    # ── ③ 단위 있고 합계행 이후 숫자 있으면 마지막 값 ──
    if local_unit and agg_m:
        window = block[agg_m.end(): agg_m.end() + 300]
        nums = _extract_nums(window)
        if nums:
            return {"value": nums[-1] * mult, "unit": local_unit, "validation": "single_num",
                    "offset_start": offset}

    return None


# ── 블록 수집 ─────────────────────────────────────────────────────

_HEADING_PATS = [
    r"[가나다라마바사아자차카타파하]\.\s*수주[상황현황잔고]",
    r"\d+\.\s*수주[상황현황잔고실적]",
    r"\d+\.\s*매출\s*(?:및\s*)?수주[상황현황]",
    r"매출\s*및\s*수주[상황현황]",
    r"수주\s*상황", r"수주\s*현황", r"수주잔고\s*현황",
    r"Order\s*Backlog", r"수주\s*실적", r"수주\s*잔고",
    r"[가나다라마바사아자차카타파하]\.\s*수주",
    r"수주\s*관련", r"신규\s*수주",
]


def _collect_blocks(text: str, global_unit: str | None) -> list[dict]:
    """
    수주 관련 텍스트 블록을 수집. 위치(offset) 기반으로 중복 제거.
    반환: [{"block": str, "offset": int, "is_kibnapum": bool}, ...]
    """
    blocks: list[dict] = []
    used_ranges: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        for s, e in used_ranges:
            overlap = min(end, e) - max(start, s)
            span = min(end - start, e - s)
            if span > 0 and overlap / span > 0.5:
                return True
        return False

    # 헤딩 패턴
    for pat in _HEADING_PATS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            start = max(0, m.start() - 50)
            end = min(len(text), m.end() + 3_000)
            if overlaps(start, end):
                continue
            used_ranges.append((start, end))
            block = text[start:end]
            blocks.append({
                "block": block, "offset": start,
                "is_kibnapum": "기납품" in block[:500],
            })

    # exhaustive: 수주총액 키워드 (헤딩 못 잡은 경우)
    for m in re.finditer(r"수주\s*총\s*액", text):
        start = max(0, m.start() - 300)
        end = min(len(text), m.end() + 2_000)
        if overlaps(start, end):
            continue
        used_ranges.append((start, end))
        block = text[start:end]
        blocks.append({
            "block": block, "offset": start,
            "is_kibnapum": "기납품" in block[:500],
        })

    return blocks


# ── 공개 API ──────────────────────────────────────────────────────

def probe_backlog(
    text: str,
    corp_code: str = "",
    corp_name: str = "",
    period: str = "",
) -> list[dict]:
    """
    수주잔고 추출 메인 함수.

    정책:
      - 블록당 하나의 결과만 생성
      - triple 검증 통과 결과 우선
      - 최종적으로 수주잔고 1개 + 기납품 수주잔고 1개만 반환
    """
    global_unit = _detect_unit(text)
    blocks = _collect_blocks(text, global_unit)

    candidates: list[dict] = []
    for b in blocks:
        extracted = _extract_from_block(b["block"], b["offset"], global_unit)
        if extracted is None:
            continue
        # 신뢰도 낮은(single_num) 결과는 단위 없으면 제외
        if extracted["validation"] == "single_num" and not extracted.get("unit"):
            continue
        label = "기납품 수주잔고" if b["is_kibnapum"] else "수주잔고"
        snippet = re.sub(r"\s+", " ", b["block"][:1200]).strip()
        candidates.append({
            "label": label,
            "snippet": snippet,
            "parsed_value": extracted["value"],
            "unit": extracted["unit"] or global_unit or "억원",
            "_rank": _RANK.get(extracted["validation"], 9),
            "_validation": extracted["validation"],
            "_offset": extracted["offset_start"],
        })

    # 신뢰도 순 정렬
    candidates.sort(key=lambda x: x["_rank"])

    # 수주잔고 / 기납품 수주잔고 각 1개씩만 선택 (가장 신뢰도 높은 것)
    result: list[dict] = []
    picked_labels: set[str] = set()
    for c in candidates:
        lbl = c["label"]
        if lbl not in picked_labels:
            picked_labels.add(lbl)
            # 내부 필드 제거
            clean = {k: v for k, v in c.items() if not k.startswith("_")}
            result.append(clean)
        if len(picked_labels) >= 2:
            break

    # 패턴 캐시 저장 (triple 검증 통과한 경우만)
    if corp_code:
        for c in candidates:
            if c["_validation"] == "triple_passed":
                _cache.save(corp_code, corp_name, {
                    "unit": c.get("unit"),
                    "last_verified": period,
                    "last_value": c.get("parsed_value"),
                })
                break

    return result


def get_cached_patterns() -> dict[str, Any]:
    return _cache.all_entries()
