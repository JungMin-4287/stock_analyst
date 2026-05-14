"""
stock_analyst/backlog_probe.py  ―  수주잔고 자동 탐지 + 패턴 학습

핵심 원리:
  수주잔고(C) = 수주총액(A) - 기납품액(B)
  → 이 삼각 관계로 추출값 검증 + 컬럼 위치 자동 인식

흐름:
  1. 회사별 cached 패턴 우선 적용
  2. 없으면 exhaustive scan (섹션 헤딩 무관, 표 구조 자체를 분석)
  3. triple validation 통과한 결과를 패턴 캐시에 저장
  4. 캐시는 JSON 파일(data/learned_patterns.json)로 영속화 → git commit 대상
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# ── 상수 ──────────────────────────────────────────────────────────

MULTIPLIER: dict[str, float] = {
    "백만원": 0.01,
    "억원": 1.0,
    "천억원": 1_000.0,
    "조원": 10_000.0,
    "원": 1e-8,
    "천원": 1e-5,
}

# 패턴 캐시 파일 경로 (stock_analyst 패키지 디렉터리 기준)
_CACHE_FILE = Path(__file__).parent / "data" / "learned_patterns.json"

# triple check 허용 오차 (15%)
_TRIPLE_TOL = 0.15

# 숫자 찾기용 패턴 (연도·날짜 제외)
_NUM_PAT = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+|[0-9]{5,}")
_YEAR_PAT = re.compile(r"^(?:19|20)\d{2}$")


# ── 패턴 캐시 ─────────────────────────────────────────────────────

class PatternCache:
    """회사별 수주잔고 파싱 패턴을 JSON 파일에 저장/로드."""

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
        """성공적인 파싱 결과를 캐시에 저장."""
        self._data[corp_code] = {"corp_name": corp_name, **record}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # 저장 실패는 무시 (읽기 전용 환경 등)

    def all_entries(self) -> dict[str, Any]:
        return dict(self._data)


# 전역 싱글톤
_cache = PatternCache()


# ── 유틸 ──────────────────────────────────────────────────────────

def _detect_unit(text: str) -> str | None:
    m = re.search(
        r"단위\s*[：:\s]\s*(?:천[㎡㎥]?\s*[,，]\s*)?(백만원|억원|천억원|조원|원|천원)",
        text,
    )
    return m.group(1) if m else None


def _extract_nums(text: str) -> list[float]:
    """텍스트에서 숫자 목록 추출 (연도 제외)."""
    raw = _NUM_PAT.findall(text)
    result = []
    for r in raw:
        clean = r.replace(",", "")
        if _YEAR_PAT.match(clean):
            continue
        try:
            result.append(float(clean))
        except ValueError:
            pass
    return result


def _triple_check(a: float, b: float, c: float) -> bool:
    """a - b ≈ c 검증 (허용 오차 _TRIPLE_TOL)."""
    if a <= 0 or b < 0 or c < 0:
        return False
    expected = a - b
    if expected <= 0:
        return False
    denom = max(expected, c, 1.0)
    return abs(expected - c) / denom < _TRIPLE_TOL


# ── 핵심 추출 로직 ────────────────────────────────────────────────

def _parse_agg_row(
    block: str, unit: str | None
) -> tuple[float | None, str | None, dict]:
    """
    합계/소계 행 이후 숫자들을 추출해 triple check.
    반환: (수주잔고_억원, 단위, debug_dict)
    """
    agg_m = re.search(r"(?:합\s*계|소\s*계)", block)
    if not agg_m:
        return None, None, {"reason": "no_agg_row"}

    window = block[agg_m.end() : agg_m.end() + 400]
    # 각주 마커에서 자르기
    fn_m = re.search(r"(?:주\s*\d+\s*\)|※|참\s*고\s*:|\n{3,})", window)
    if fn_m:
        window = window[: fn_m.start()]

    nums = _extract_nums(window)
    debug = {"window_nums": nums, "unit": unit}

    mult = MULTIPLIER.get(unit or "", 1.0)

    # 3개 이상: 마지막 3개가 [수주총액, 기납품액, 수주잔고]
    if len(nums) >= 3:
        a, b, c = nums[-3], nums[-2], nums[-1]
        debug["triple_attempt"] = (a, b, c)
        if _triple_check(a, b, c):
            debug["validation"] = "triple_passed"
            return c * mult, unit, debug
        # 마지막 값이 나머지보다 작으면 수주잔고로 판단
        if c < a and c < a * 0.99:
            debug["validation"] = "last_col_smallest"
            return c * mult, unit, debug

    # 1~2개: 마지막 값 반환 (단독 행 등)
    if nums and unit:
        debug["validation"] = "single_num"
        return nums[-1] * mult, unit, debug

    return None, None, {**debug, "reason": "insufficient_nums"}


def _scan_exhaustive(text: str, unit: str | None) -> list[dict]:
    """
    섹션 헤딩 무관하게 '수주총액' 키워드 기준으로 표 블록을 모두 탐색.
    """
    results = []
    for m in re.finditer(r"수주\s*총\s*액", text):
        start = max(0, m.start() - 300)
        end = min(len(text), m.end() + 2_000)
        block = text[start:end]
        local_unit = _detect_unit(block) or unit
        val, found_unit, dbg = _parse_agg_row(block, local_unit)
        if val is not None:
            snippet = re.sub(r"\s+", " ", block[:1200]).strip()
            results.append({
                "label": "수주잔고",
                "snippet": snippet,
                "parsed_value": val,
                "unit": found_unit or local_unit or "억원",
                "_debug": dbg,
                "_source": "exhaustive",
            })
    return results


def _scan_heading_patterns(text: str, unit: str | None) -> list[dict]:
    """
    기존 섹션 헤딩 패턴 목록으로 탐색.
    각 블록에 대해 triple check 포함 파싱.
    """
    HEADING_PATS = [
        r"[가나다라마바사아자차카타파하]\.\s*수주[상황현황잔고]",
        r"\d+\.\s*수주[상황현황잔고실적]",
        r"\d+\.\s*매출\s*(?:및\s*)?수주[상황현황]",
        r"매출\s*및\s*수주[상황현황]",
        r"수주\s*상황",
        r"수주\s*현황",
        r"수주잔고\s*현황",
        r"Order\s*Backlog",
        r"수주\s*실적",
        r"수주\s*잔고",
        # 분기보고서 빈출 패턴 추가
        r"[가나다라마바사아자차카타파하]\.\s*수주",
        r"수주\s*관련",
        r"신규\s*수주",
        r"수주\s*및\s*공급",
    ]
    results = []
    seen_starts: set[int] = set()
    for pat in HEADING_PATS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            # 중복 블록 방지 (100자 이내 같은 위치)
            if any(abs(m.start() - s) < 100 for s in seen_starts):
                continue
            seen_starts.add(m.start())
            start = max(0, m.start() - 50)
            end = min(len(text), m.end() + 3_000)
            block = text[start:end]
            local_unit = _detect_unit(block) or unit
            snippet = re.sub(r"\s+", " ", block[:1200]).strip()

            # triple check 우선 시도
            val, found_unit, dbg = _parse_agg_row(block, local_unit)
            if val is None:
                # "수주잔고" 컨텍스트 직접 탐색
                ctx_m = re.search(
                    r"수주\s*잔고.{0,60}?([0-9,]{3,})\s*(백만원|억원|천억원|조원|원|천원)",
                    snippet, re.DOTALL,
                )
                if ctx_m:
                    raw = ctx_m.group(1).replace(",", "")
                    u2 = ctx_m.group(2)
                    try:
                        val = float(raw) * MULTIPLIER.get(u2, 1.0)
                        found_unit = u2
                        dbg = {"validation": "context_keyword"}
                    except ValueError:
                        pass

            label = "기납품 수주잔고" if "기납품" in snippet[:400] else "수주잔고"
            results.append({
                "label": label,
                "snippet": snippet,
                "parsed_value": val,
                "unit": found_unit or local_unit or "억원",
                "_debug": dbg,
                "_source": "heading",
            })
    return results


# ── 공개 API ──────────────────────────────────────────────────────

def probe_backlog(
    text: str,
    corp_code: str = "",
    corp_name: str = "",
    period: str = "",
) -> list[dict]:
    """
    수주잔고 추출 메인 함수.

    1) 캐시된 패턴 우선 적용
    2) 섹션 헤딩 패턴 스캔
    3) 수주총액 키워드 exhaustive 스캔
    4) triple 검증 통과 시 패턴 캐시 업데이트

    반환: extract_order_backlog()와 동일한 형태의 list[dict]
    """
    global_unit = _detect_unit(text)
    results: list[dict] = []

    # ── 1. 헤딩 패턴 스캔 ──
    heading_results = _scan_heading_patterns(text, global_unit)
    results.extend(heading_results)

    # ── 2. exhaustive 스캔 (중복 방지: 이미 triple 통과한 snippet 제외) ──
    existing_snippets = {r["snippet"][:80] for r in results if r.get("parsed_value")}
    for item in _scan_exhaustive(text, global_unit):
        if item["snippet"][:80] not in existing_snippets:
            results.append(item)
            existing_snippets.add(item["snippet"][:80])

    # ── 3. 중복 제거 ──
    seen: set[str] = set()
    deduped: list[dict] = []
    for item in results:
        key = item["snippet"][:120]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    # ── 4. triple 검증 통과 결과를 패턴 캐시에 저장 ──
    if corp_code:
        for item in deduped:
            dbg = item.get("_debug") or {}
            if dbg.get("validation") in ("triple_passed", "triple_check_passed"):
                _cache.save(
                    corp_code,
                    corp_name,
                    {
                        "unit": item.get("unit"),
                        "last_verified": period,
                        "last_value": item.get("parsed_value"),
                        "source": item.get("_source"),
                    },
                )
                break

    return deduped[:15]


def get_cached_patterns() -> dict[str, Any]:
    """현재 캐시된 모든 회사 패턴 반환 (디버그·UI 표시용)."""
    return _cache.all_entries()
