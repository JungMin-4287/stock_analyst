"""
stock_analyst/backlog_probe.py  ―  수주잔고 자동 탐지 + LLM fallback + 패턴 학습

처리 순서:
  1. regex triple 검증 (A-B=C) → 확실하면 즉시 반환
  2. regex 실패/불확실 → Claude Haiku 로 섹션 텍스트 직접 해석
  3. 성공 결과는 learned_patterns.json 에 캐시

수주상황 표 구조 (BeautifulSoup 추출 후 수량/금액 혼재):
  합계: 수주총액_수량 | 수주총액_금액 | 기납품_수량 | 기납품_금액 | 잔고_수량 | 잔고_금액
       691           10,577          528          7,467         163        3,110
  → nums[-3:] = [7467, 163, 3110] → triple 실패 → exhaustive 탐색 or LLM
"""

from __future__ import annotations
import json, re
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
_RANK = {"triple_passed": 0, "llm": 1, "context_keyword": 2, "last_col_smallest": 3, "single_num": 9}


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


# ── LLM fallback ──────────────────────────────────────────────────

_LLM_PROMPT = """다음은 DART 공시 보고서에서 추출한 수주상황 관련 텍스트입니다.
이 텍스트에서 수주잔고 합계 금액을 찾아주세요.

규칙:
- 수주잔고 = 수주총액 - 기납품액 (마지막 열의 합계 값)
- 수주총액이나 기납품액이 아닌, 수주잔고(잔액)를 반환
- 단위(억원/백만원 등)도 함께 반환

텍스트:
{section}

JSON으로만 응답 (설명 없이):
{{"value": <숫자>, "unit": "<단위>"}}

수주잔고를 찾을 수 없으면:
{{"value": null, "unit": null}}"""


def _llm_extract(section_text: str, api_key: str) -> dict | None:
    """
    Claude Haiku로 수주잔고 값 추출.
    triple 검증이 실패한 섹션에 대해 fallback으로 호출.
    """
    try:
        import anthropic
    except ImportError:
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        # 섹션 텍스트를 2000자로 제한 (비용 절감)
        truncated = section_text[:2000]
        prompt = _LLM_PROMPT.format(section=truncated)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        # JSON 파싱
        # 혹시 마크다운 코드블록이 들어오면 제거
        raw = re.sub(r"```[a-z]*\n?", "", raw).strip().rstrip("`")
        parsed = json.loads(raw)
        val = parsed.get("value")
        unit = parsed.get("unit")
        if val is None:
            return None
        # 단위 적용
        mult = MULTIPLIER.get(unit or "", 1.0)
        return {
            "value": float(val) * mult,
            "unit": unit or "억원",
            "validation": "llm",
        }
    except Exception:
        return None


# ── 유틸 ──────────────────────────────────────────────────────────

def _detect_unit(text: str) -> str | None:
    m = re.search(
        r"단위\s*[:：\s]\s*(?:천[㎡㎥]?\s*[,，]\s*)?(백만원|억원|천억원|조원|원|천원)",
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
    if a <= 0 or b < 0 or c <= 0:
        return False
    expected = a - b
    if expected <= 0:
        return False
    denom = max(expected, c, 1.0)
    return abs(expected - c) / denom < _TRIPLE_TOL


def _find_backlog_triple(nums: list[float]) -> tuple[float, float, float] | None:
    """
    숫자 목록에서 A - B = C 탐색. 수량/금액 혼재 표 대응.
    최대값 2% 미만 = 수량으로 간주 제거 후 전체 조합 탐색.
    """
    if len(nums) < 3:
        return None
    max_val = max(nums)
    amounts = [n for n in nums if n >= max_val * 0.02]
    if len(amounts) < 3:
        amounts = nums

    # 마지막 3개 먼저 시도
    a, b, c = amounts[-3], amounts[-2], amounts[-1]
    if _triple_check(a, b, c):
        return (a, b, c)

    # 전체 조합 탐색 (n 보통 3~8)
    n = len(amounts)
    for i in range(n - 2):
        for j in range(i + 1, n - 1):
            for k in range(j + 1, n):
                a2, b2, c2 = amounts[i], amounts[j], amounts[k]
                if _triple_check(a2, b2, c2):
                    return (a2, b2, c2)
    return None


def _is_kibnapum_section(heading_text: str) -> bool:
    """헤딩 자체가 '기납품 수주잔고' 섹션인지 판단. 표 내 컬럼명은 해당 없음."""
    return bool(re.search(r"기납품\s*수주\s*잔고", heading_text))


# ── 블록 단위 추출 ────────────────────────────────────────────────

def _extract_from_block(
    block: str, offset: int, unit: str | None, heading_text: str = ""
) -> dict | None:
    local_unit = _detect_unit(block) or unit
    mult = MULTIPLIER.get(local_unit or "", 1.0)

    # ① triple 검증 (exhaustive)
    agg_m = re.search(r"(?:합\s*계|소\s*계)", block)
    if agg_m:
        window = block[agg_m.end(): agg_m.end() + 500]
        fn_m = re.search(r"(?:주\s*\d+\s*\)|※|참\s*고\s*:|\n{3,})", window)
        if fn_m:
            window = window[:fn_m.start()]
        nums = _extract_nums(window)
        triple = _find_backlog_triple(nums)
        if triple:
            a, b, c = triple
            return {"value": c * mult, "unit": local_unit,
                    "validation": "triple_passed", "offset_start": offset,
                    "triple": (a, b, c)}

    # ② "수주잔고" 키워드 + 숫자+단위
    # "수주잔고" 또는 "수주잔액" 키워드 바로 뒤 숫자+단위
    ctx_m = re.search(
        r"수주\s*잔[고액].{0,60}?([0-9,]{3,})\s*(백만원|억원|천억원|조원|원|천원)",
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

    # ③ 단위 있고 합계행 이후 마지막 금액
    if local_unit and agg_m:
        window = block[agg_m.end(): agg_m.end() + 300]
        nums = _extract_nums(window)
        if nums:
            max_v = max(nums)
            amounts = [n for n in nums if n >= max_v * 0.02] or nums
            return {"value": amounts[-1] * mult, "unit": local_unit,
                    "validation": "single_num", "offset_start": offset}

    # 합계행 없는 단독 표 (RFHIC 등): "수주잔액" 컬럼 바로 다음 숫자
    if local_unit:
        janaek_m = re.search(
            r"수주\s*잔[고액][^\n]{0,30}\n([0-9,]{5,})",
            block,
        )
        if janaek_m:
            raw = janaek_m.group(1).replace(",", "")
            if not re.fullmatch(r"(?:19|20)\d{2}", raw):
                try:
                    return {"value": float(raw) * mult, "unit": local_unit,
                            "validation": "single_num", "offset_start": offset}
                except ValueError:
                    pass
        # 블록 내 가장 큰 숫자 1개 (단독 숫자 표)
        all_nums = _extract_nums(block)
        if all_nums:
            max_v = max(all_nums)
            # 단위 있고, 블록 내 수주 관련 키워드 있을 때만
            if re.search(r"수주\s*잔[고액]", block) and max_v > 0:
                return {"value": max_v * mult, "unit": local_unit,
                        "validation": "single_num", "offset_start": offset}

    return None


_HEADING_PATS = [
    r"[가나다라마바사아자차카타파하]\.\s*수주[상황현황잔고]",
    r"\d+\.\s*수주[상황현황잔고실적]",
    r"\d+\.\s*매출\s*(?:및\s*)?수주[상황현황]",
    r"매출\s*및\s*수주[상황현황]",
    r"수주\s*상황", r"수주\s*현황", r"수주잔고\s*현황",
    r"Order\s*Backlog", r"수주\s*실적", r"수주\s*잔고",
    r"[가나다라마바사아자차카타파하]\.\s*수주",
]


def _collect_blocks(text: str) -> list[dict]:
    blocks: list[dict] = []
    used_ranges: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        for s, e in used_ranges:
            overlap = min(end, e) - max(start, s)
            span = min(end - start, e - s)
            if span > 0 and overlap / span > 0.5:
                return True
        return False

    for pat in _HEADING_PATS:
        for m in re.finditer(pat, text, re.IGNORECASE):
            start = max(0, m.start() - 50)
            end = min(len(text), m.end() + 3_000)
            if overlaps(start, end):
                continue
            used_ranges.append((start, end))
            blocks.append({
                "block": text[start:end], "offset": start,
                "heading": text[m.start():m.end()],
                "is_kibnapum": _is_kibnapum_section(text[m.start():m.end()]),
            })

    for m in re.finditer(r"수주\s*총\s*액", text):
        start = max(0, m.start() - 300)
        end = min(len(text), m.end() + 2_000)
        if overlaps(start, end):
            continue
        used_ranges.append((start, end))
        blocks.append({
            "block": text[start:end], "offset": start,
            "heading": "", "is_kibnapum": False,
        })

    return blocks


# ── 공개 API ──────────────────────────────────────────────────────

def probe_backlog(
    text: str,
    corp_code: str = "",
    corp_name: str = "",
    period: str = "",
    llm_api_key: str | None = None,
) -> list[dict]:
    """
    수주잔고 추출 메인 함수.

    1) regex triple 검증 → 통과 시 즉시 사용
    2) regex 실패/저신뢰 → LLM(Claude Haiku) fallback (llm_api_key 제공 시)
    3) 결과 캐시 저장

    반환: 최대 2개 [수주잔고, 기납품 수주잔고]
    """
    global_unit = _detect_unit(text)
    blocks = _collect_blocks(text)

    candidates: list[dict] = []
    for b in blocks:
        extracted = _extract_from_block(
            b["block"], b["offset"], global_unit, b["heading"]
        )

        # regex 실패 or 저신뢰 → LLM 시도
        if (extracted is None or extracted["validation"] not in ("triple_passed",)) \
                and llm_api_key:
            llm_result = _llm_extract(b["block"], llm_api_key)
            if llm_result:
                llm_result["offset_start"] = b["offset"]
                # LLM이 regex보다 신뢰도 높으면 교체
                if extracted is None or \
                        _RANK.get(llm_result["validation"], 9) < \
                        _RANK.get(extracted.get("validation", ""), 9):
                    extracted = llm_result

        if extracted is None:
            continue
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

    candidates.sort(key=lambda x: x["_rank"])

    # 수주잔고 / 기납품 수주잔고 각 1개씩만 선택
    result: list[dict] = []
    picked_labels: set[str] = set()
    for c in candidates:
        lbl = c["label"]
        if lbl not in picked_labels:
            picked_labels.add(lbl)
            result.append({k: v for k, v in c.items() if not k.startswith("_")})
        if len(picked_labels) >= 2:
            break

    # 패턴 캐시 저장 (triple 또는 LLM 통과 결과)
    if corp_code:
        for c in candidates:
            if c["_validation"] in ("triple_passed", "llm"):
                _cache.save(corp_code, corp_name, {
                    "unit": c.get("unit"),
                    "last_verified": period,
                    "last_value": c.get("parsed_value"),
                    "method": c["_validation"],
                })
                break

    return result


def get_cached_patterns() -> dict[str, Any]:
    return _cache.all_entries()
