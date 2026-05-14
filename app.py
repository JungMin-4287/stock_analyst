"""
app.py  ―  주식 재무 분석 Streamlit 대시보드

실행 방법:
    streamlit run app.py

사전 준비:
    pip install -r requirements.txt
    DART_API_KEY 환경변수 설정 (또는 사이드바에 직접 입력)
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

# ──────────────────────────────────────────────────────────────────
# 페이지 설정 (반드시 첫 번째 st 호출)
# ──────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="주식 재무 분석기",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ──────────────────────────────────────────────────────────────────
# 패키지 임포트
# ──────────────────────────────────────────────────────────────────
try:
    from stock_analyst.dart import (
        DartClient,
        normalize_cumulative_financials,
        normalize_bs_financials,
        derive_quarterly_metrics,
        extract_revenue_note_candidates,
        REPORT_CODES,
    )
    from stock_analyst.analyst import (
        derive_full_metrics,
        extract_order_backlog,
        extract_utilization,
        extract_product_revenue,
        format_display_df,
        BILLION_WON_COLS,
        PERCENT_COLS,
    )
    PACKAGE_OK = True
except ImportError as _e:
    PACKAGE_OK = False
    _import_error = str(_e)


# ──────────────────────────────────────────────────────────────────
# CSS 스타일
# ──────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .metric-card {
        background: #f0f2f6;
        border-radius: 8px;
        padding: 12px 16px;
        margin: 4px 0;
    }
    .metric-label { font-size: 0.8rem; color: #666; margin-bottom: 2px; }
    .metric-value { font-size: 1.4rem; font-weight: bold; color: #1f2937; }
    .metric-sub   { font-size: 0.75rem; color: #888; }
    [data-testid="stSidebar"] { min-width: 280px; max-width: 320px; }
</style>
""", unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────────
# 패키지 오류 처리
# ──────────────────────────────────────────────────────────────────
if not PACKAGE_OK:
    st.error(f"""
**stock_analyst 패키지를 불러오지 못했습니다.**

오류: `{_import_error}`

해결 방법:
```bash
pip install -r requirements.txt
pip install -e .
```
""")
    st.stop()


# ──────────────────────────────────────────────────────────────────
# 헬퍼 함수
# ──────────────────────────────────────────────────────────────────

def make_bar_line_chart(
    df: pd.DataFrame,
    bar_cols: list[tuple[str, str]],   # [(col, label), ...]
    line_cols: list[tuple[str, str]],  # [(col, label), ...]
    title: str,
    bar_unit: str = "십억원",
    line_unit: str = "%",
) -> go.Figure:
    """막대(금액) + 선(마진) 이중축 차트."""
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    colors = ["#4C9BE8", "#F5A623", "#7ED321", "#D0021B", "#9B59B6"]

    for i, (col, label) in enumerate(bar_cols):
        if col in df.columns:
            fig.add_trace(
                go.Bar(
                    x=df["period"],
                    y=df[col] / 1_000_000_000,
                    name=f"{label}({bar_unit})",
                    marker_color=colors[i % len(colors)],
                    opacity=0.85,
                ),
                secondary_y=False,
            )

    for i, (col, label) in enumerate(line_cols):
        if col in df.columns:
            fig.add_trace(
                go.Scatter(
                    x=df["period"],
                    y=df[col] * 100,
                    name=f"{label}({line_unit})",
                    mode="lines+markers",
                    line=dict(width=2, dash="solid"),
                    marker=dict(size=6),
                ),
                secondary_y=True,
            )

    fig.update_layout(
        title=title,
        barmode="group",
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=420,
        margin=dict(l=40, r=40, t=60, b=40),
    )
    fig.update_yaxes(title_text=f"금액({bar_unit})", secondary_y=False)
    fig.update_yaxes(title_text=f"비율({line_unit})", secondary_y=True)
    return fig


def make_line_chart(
    df: pd.DataFrame,
    cols: list[tuple[str, str]],  # [(col, label), ...]
    title: str,
    unit: str = "십억원",
    divisor: float = 1_000_000_000,
    multiply: float = 1.0,
) -> go.Figure:
    """단일축 선 차트."""
    fig = go.Figure()
    colors = ["#4C9BE8", "#F5A623", "#7ED321", "#D0021B"]

    for i, (col, label) in enumerate(cols):
        if col in df.columns:
            y = df[col] / divisor * multiply
            fig.add_trace(
                go.Scatter(
                    x=df["period"],
                    y=y,
                    name=f"{label}({unit})",
                    mode="lines+markers",
                    line=dict(color=colors[i % len(colors)], width=2),
                    marker=dict(size=7),
                )
            )

    fig.update_layout(
        title=title,
        template="plotly_white",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=380,
        margin=dict(l=40, r=40, t=60, b=40),
        yaxis_title=f"({unit})",
    )
    return fig


def make_backlog_chart(order_backlogs: list) -> go.Figure | None:
    """수주잔고 시계열 막대 차트."""
    parsed = [i for i in order_backlogs if i.get("parsed_value") is not None]
    if not parsed:
        return None

    # 보고서당 첫 번째 값만 사용 (중복 제거)
    seen: dict[tuple, dict] = {}
    for item in parsed:
        key = (item.get("rcept_dt", ""), item.get("label", ""))
        if key not in seen:
            seen[key] = item

    items = sorted(seen.values(), key=lambda x: x.get("rcept_dt", ""))
    backlog_items = [i for i in items if "기납품" not in i.get("label", "")]
    delivered_items = [i for i in items if "기납품" in i.get("label", "")]

    def fmt_dt(d: str) -> str:
        return f"{d[:4]}.{d[4:6]}" if len(d) == 8 else d

    fig = go.Figure()
    if backlog_items:
        vals = [i["parsed_value"] for i in backlog_items]
        fig.add_trace(go.Bar(
            x=[fmt_dt(i.get("rcept_dt", "")) for i in backlog_items],
            y=vals,
            name="수주잔고",
            marker_color="#4C9BE8",
            text=[f"{v:,.0f}" for v in vals],
            textposition="outside",
        ))
    if delivered_items:
        vals = [i["parsed_value"] for i in delivered_items]
        fig.add_trace(go.Bar(
            x=[fmt_dt(i.get("rcept_dt", "")) for i in delivered_items],
            y=vals,
            name="기납품 수주잔고",
            marker_color="#F5A623",
            text=[f"{v:,.0f}" for v in vals],
            textposition="outside",
        ))

    fig.update_layout(
        title="수주잔고 추이 (단위: 억원)",
        barmode="group",
        template="plotly_white",
        height=380,
        margin=dict(l=40, r=40, t=60, b=40),
        yaxis_title="금액(억원)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def make_stacked_area_chart(
    df: pd.DataFrame,
    title: str,
) -> go.Figure:
    """매출원가 + 매출총이익 stacked bar."""
    fig = go.Figure()
    if "cogs" in df.columns:
        fig.add_trace(go.Bar(
            x=df["period"],
            y=df["cogs"] / 1_000_000_000,
            name="매출원가(십억원)",
            marker_color="#E74C3C",
        ))
    if "gross_profit" in df.columns:
        fig.add_trace(go.Bar(
            x=df["period"],
            y=df["gross_profit"] / 1_000_000_000,
            name="매출총이익(십억원)",
            marker_color="#2ECC71",
        ))
    fig.update_layout(
        title=title,
        barmode="stack",
        template="plotly_white",
        height=380,
        margin=dict(l=40, r=40, t=60, b=40),
        yaxis_title="금액(십억원)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )
    return fig


def latest_metric_card(df: pd.DataFrame, col: str, label: str, fmt: str = "{:.1f}") -> None:
    """최신 분기 지표 카드 표시."""
    if col not in df.columns:
        return
    series = df[col].dropna()
    if series.empty:
        return
    val = series.iloc[-1]
    prev = series.iloc[-2] if len(series) >= 2 else None

    if col in BILLION_WON_COLS:
        display_val = f"{val / 1_000_000_000:,.1f}"
        unit = "십억원"
    elif col in PERCENT_COLS:
        display_val = f"{val * 100:.1f}"
        unit = "%"
    elif col in {"inventory_turnover", "ar_turnover"}:
        display_val = f"{val:.2f}"
        unit = "회"
    elif col in {"dio", "dso"}:
        display_val = f"{val:.0f}"
        unit = "일"
    else:
        display_val = fmt.format(val)
        unit = ""

    if prev is not None and prev != 0:
        if col in BILLION_WON_COLS:
            delta = (val - prev) / abs(prev) * 100
        elif col in PERCENT_COLS:
            delta = (val - prev) * 100
        else:
            delta = val - prev
        delta_str = f"전분기比 {delta:+.1f}"
    else:
        delta_str = ""

    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-label">{label}</div>
        <div class="metric-value">{display_val}<span style="font-size:0.9rem;color:#666"> {unit}</span></div>
        <div class="metric-sub">{delta_str}</div>
    </div>
    """, unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────────
# 데이터 로딩 (session_state 캐시)
# ──────────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False, ttl=3600)
def load_data(
    api_key: str,
    corp_name: str | None,
    stock_code: str | None,
    start_year: int,
    end_year: int,
    fs_div: str,
    include_all_quarters: bool,
) -> dict:
    """DART 에서 IS + BS 데이터를 수집해 dict 로 반환.

    일부 기업(중소형 코스닥 등)은 연결(CFS) 분기/반기보고서를 제출하지 않고
    사업보고서(FY)만 CFS로 제출합니다. 이 경우 Q4 = FY 전체가 되는 오류를 방지하기 위해
    CFS 분기 데이터가 없으면 별도(OFS)로 자동 전환합니다.
    """
    client = DartClient(api_key, cache_dir=".cache/dart")
    company = client.find_company(corp_name or None, stock_code or None)

    # 조회할 보고서 코드 결정
    all_report_codes = ["11013", "11012", "11014", "11011"]  # Q1, H1, Q3, FY
    if include_all_quarters:
        end_year_codes = all_report_codes
    else:
        end_year_codes = ["11013"]  # Q1만

    year_code_pairs: list[tuple[int, str]] = []
    for year in range(start_year, end_year + 1):
        codes = all_report_codes if year < end_year else end_year_codes
        for code in codes:
            year_code_pairs.append((year, code))

    total_calls = len(year_code_pairs)
    progress = st.progress(0, text="DART 데이터 수집 중...")

    def _fetch_all(target_fs_div: str, progress_offset: int = 0) -> list:
        frames_out: list[tuple[int, str, pd.DataFrame]] = []
        for i, (year, code) in enumerate(year_code_pairs):
            frac = (progress_offset + i) / max(total_calls * 2, 1)
            progress.progress(min(frac, 0.99),
                              text=f"{year}년 {REPORT_CODES[code]} ({target_fs_div}) 수집 중...")
            try:
                df = client.financial_statement_all(
                    company.corp_code, year, code, target_fs_div
                )
                frames_out.append((year, code, df))
            except RuntimeError:
                pass
        return frames_out

    # 1차: 선택 fs_div 로 수집
    frames = _fetch_all(fs_div, 0)

    # CFS 분기/반기 데이터 없으면 OFS 자동 전환
    # (일부 기업은 CFS 사업보고서만 제출하고 분기는 OFS만 존재)
    quarterly_codes = {"11013", "11012", "11014"}
    has_quarterly = any(
        code in quarterly_codes and not df.empty
        for _, code, df in frames
    )
    fs_div_actual = fs_div
    if not has_quarterly and fs_div == "CFS":
        fs_div_actual = "OFS"
        frames = _fetch_all("OFS", total_calls)

    progress.progress(1.0, text="데이터 처리 중...")

    is_cumulative = normalize_cumulative_financials(frames)
    bs_data = normalize_bs_financials(frames)
    quarterly_is = derive_quarterly_metrics(is_cumulative)
    full_df = derive_full_metrics(quarterly_is, bs_data)

    progress.empty()

    return {
        "company": company,
        "full_df": full_df,
        "bs_data": bs_data,
        "fs_div_actual": fs_div_actual,
    }


@st.cache_data(show_spinner=False, ttl=3600)
def load_text_data(
    api_key: str,
    corp_code: str,
    corp_name: str,
    start_date: str,
    end_date: str,
) -> dict:
    """DART 보고서 원문을 수집해 수주잔고/가동율/제품별매출을 추출."""
    client = DartClient(api_key, cache_dir=".cache/dart")
    filings = client.filings(corp_code, start_date, end_date)

    order_backlogs: list[dict] = []
    utilizations: list[dict] = []
    product_revenues: list[dict] = []

    text_progress = st.progress(0, text="보고서 원문 수집 중 (시간이 걸릴 수 있습니다)...")
    for i, filing in enumerate(filings):
        text_progress.progress(
            (i + 1) / max(len(filings), 1),
            text=f"원문 분석 중: {filing.report_nm} ({filing.rcept_dt})"
        )
        try:
            text = client.document_text(filing.rcept_no)
            # 보고서 기간 레이블 (패턴 학습용)
            period_label = filing.rcept_dt[:4] + "-" + filing.report_nm[:4]
            bl = extract_order_backlog(
                text,
                corp_code=corp_code,
                corp_name=corp_name,
                period=period_label,
            )
            ut = extract_utilization(text)
            pr = extract_product_revenue(text)
            for item in bl:
                item["report_nm"] = filing.report_nm
                item["rcept_dt"] = filing.rcept_dt
                order_backlogs.append(item)
            for item in ut:
                item["report_nm"] = filing.report_nm
                item["rcept_dt"] = filing.rcept_dt
                utilizations.append(item)
            for item in pr:
                item["report_nm"] = filing.report_nm
                item["rcept_dt"] = filing.rcept_dt
                product_revenues.append(item)
        except Exception:
            pass

    text_progress.empty()
    return {
        "order_backlogs": order_backlogs,
        "utilizations": utilizations,
        "product_revenues": product_revenues,
    }


# ──────────────────────────────────────────────────────────────────
# 사이드바
# ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("📊 주식 재무 분석기")
    st.caption("DART 공시 기반 5개년 분기 재무 대시보드")
    st.markdown("---")

    api_key_input = st.text_input(
        "🔑 DART API 키",
        type="password",
        value=os.getenv("DART_API_KEY", ""),
        help="https://opendart.fss.or.kr 에서 무료 발급 가능합니다.",
        placeholder="발급받은 API 키를 입력하세요",
    )

    st.markdown("**🔍 종목 검색**")
    corp_name_input = st.text_input(
        "회사명",
        placeholder="예: 삼성전자, 현대차, POSCO",
    )
    stock_code_input = st.text_input(
        "종목코드 (선택)",
        placeholder="예: 005930",
        help="회사명으로 중복될 경우 종목코드도 입력하세요.",
    )

    st.markdown("**📅 조회 기간**")
    current_year = date.today().year
    year_options = list(range(2018, current_year + 1))

    col_s, col_e = st.columns(2)
    with col_s:
        start_year = st.selectbox(
            "시작",
            options=year_options,
            index=year_options.index(max(2021, year_options[0])),
        )
    with col_e:
        end_year = st.selectbox(
            "종료",
            options=year_options,
            index=len(year_options) - 1,
        )

    fs_div = st.radio(
        "재무제표 기준",
        options=["CFS", "OFS"],
        format_func=lambda x: "연결" if x == "CFS" else "별도",
        horizontal=True,
    )

    include_all_q = st.checkbox(
        "종료연도 전분기 포함",
        value=True,
        help="체크 해제 시 종료연도 1분기(Q1)만 포함됩니다.",
    )

    fetch_text = st.checkbox(
        "📄 수주잔고 / 가동율 분석",
        value=False,
        help="보고서 원문을 추가 다운로드합니다. 분석이 더 오래 걸립니다.",
    )

    st.markdown("---")
    fetch_btn = st.button(
        "🚀 분석 시작",
        use_container_width=True,
        type="primary",
        disabled=not api_key_input or not (corp_name_input or stock_code_input),
    )
    if not api_key_input:
        st.caption("⚠️ API 키를 입력해주세요")
    elif not (corp_name_input or stock_code_input):
        st.caption("⚠️ 종목명 또는 종목코드를 입력해주세요")


# ──────────────────────────────────────────────────────────────────
# 분석 실행 및 session_state 저장
# ──────────────────────────────────────────────────────────────────

if fetch_btn:
    # 새 검색이면 캐시 초기화
    st.session_state.pop("result", None)
    st.session_state.pop("text_result", None)
    try:
        result = load_data(
            api_key=api_key_input,
            corp_name=corp_name_input.strip() or None,
            stock_code=stock_code_input.strip() or None,
            start_year=start_year,
            end_year=end_year,
            fs_div=fs_div,
            include_all_quarters=include_all_q,
        )
        st.session_state["result"] = result
        st.session_state["fetch_text"] = fetch_text
        st.session_state["api_key"] = api_key_input
        st.session_state["start_year"] = start_year
        st.session_state["end_year"] = end_year
    except ValueError as e:
        st.error(f"회사를 찾을 수 없습니다: {e}")
    except RuntimeError as e:
        st.error(f"DART API 오류: {e}")
    except Exception as e:
        st.error(f"예상치 못한 오류: {e}")


# ──────────────────────────────────────────────────────────────────
# 결과 표시
# ──────────────────────────────────────────────────────────────────

if "result" not in st.session_state:
    # 초기 화면
    st.markdown("## 📊 주식 재무 분석기")
    st.markdown("""
왼쪽 사이드바에서 종목을 입력하고 **분석 시작** 버튼을 누르세요.

**분석 항목:**
- 매출액 / 영업이익 / 순이익 / GPM / OPM / NPM
- 매출원가 / 원가비중
- 재고자산 / 재고자산회전율 / 재고일수(DIO)
- 매출채권 / 매출채권회전율 / 매출채권회수일(DSO)
- 차입금 추이
- 수주잔고 / 기납품 수주잔고 / 공장 가동율 (원문 분석 옵션)
    """)
    st.info("💡 DART API 키는 [opendart.fss.or.kr](https://opendart.fss.or.kr) 에서 무료로 발급받을 수 있습니다.")
    st.stop()


result = st.session_state["result"]
company = result["company"]
df: pd.DataFrame = result["full_df"]
fs_div_actual = result.get("fs_div_actual", fs_div)

# ── 헤더 ──
st.markdown(f"## {company.corp_name}  `{company.stock_code}`")
latest_period = df["period"].iloc[-1] if not df.empty else "N/A"
fs_label = "연결" if fs_div_actual == "CFS" else "별도"
st.caption(
    f"{fs_label} 재무제표 기준  ·  "
    f"최근 분기: **{latest_period}**  ·  "
    f"조회기간: {start_year}~{end_year}"
)
if fs_div_actual != fs_div:
    st.warning(
        f"⚠️ 연결(CFS) 분기/반기보고서 데이터가 없어 **별도(OFS) 기준**으로 자동 전환했습니다. "
        f"사업보고서는 연결 기준이지만 분기 실적은 별도 기준입니다."
    )

# ── 최신 분기 요약 지표 (상단 카드) ──
if not df.empty:
    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        latest_metric_card(df, "revenue", "최근 분기 매출액")
    with col2:
        latest_metric_card(df, "operating_profit", "최근 분기 영업이익")
    with col3:
        latest_metric_card(df, "opm", "최근 분기 OPM")
    with col4:
        latest_metric_card(df, "inventory_turnover", "재고자산회전율")
    with col5:
        latest_metric_card(df, "ar_turnover", "매출채권회전율")

st.markdown("---")

# ── 탭 구성 ──
tab_labels = [
    "📈 손익계산서",
    "⚙️ 원가 구조",
    "🏦 재무상태표",
    "🔄 효율성 지표",
    "📋 수주잔고 / 가동율",
    "📦 제품별 매출",
    "📥 전체 데이터",
]
tabs = st.tabs(tab_labels)
tab_pl, tab_cost, tab_bs, tab_eff, tab_order, tab_prod, tab_data = tabs


# ═════════════════════════════════════════════════
# TAB 1: 손익계산서
# ═════════════════════════════════════════════════
with tab_pl:
    st.subheader("매출액 / 영업이익 / 순이익 추이")

    if df.empty:
        st.warning("수집된 재무 데이터가 없습니다.")
    else:
        # 매출+영업이익 차트
        fig1 = make_bar_line_chart(
            df,
            bar_cols=[("revenue", "매출액"), ("operating_profit", "영업이익")],
            line_cols=[("opm", "OPM"), ("gpm", "GPM")],
            title="매출액 / 영업이익 및 마진율 추이",
        )
        st.plotly_chart(fig1, use_container_width=True)

        # 순이익 차트
        fig2 = make_bar_line_chart(
            df,
            bar_cols=[("net_income", "순이익")],
            line_cols=[("npm", "NPM")],
            title="순이익 및 순이익률(NPM) 추이",
        )
        st.plotly_chart(fig2, use_container_width=True)

        # 테이블 (data_source 컬럼으로 Q4 파생 방식 확인 가능)
        st.subheader("데이터 테이블")
        is_cols = ["period", "data_source", "revenue", "gross_profit",
                   "operating_profit", "net_income", "gpm", "opm", "npm"]
        display_cols = [c for c in is_cols if c in df.columns]
        st.dataframe(
            format_display_df(df[display_cols]),
            use_container_width=True,
            height=320,
        )
        if "data_source" in df.columns:
            st.caption("※ 데이터출처: Q4는 사업보고서(FY) 누적값에서 직전 분기 누적을 차감해 산출합니다."
                       " FY−Q3 = Q4, FY−H1 = H2 (Q3 보고서 없는 경우) 등으로 표시됩니다.")


# ═════════════════════════════════════════════════
# TAB 2: 원가 구조
# ═════════════════════════════════════════════════
with tab_cost:
    st.subheader("원가 구조 분석")

    cogs_available = "cogs" in df.columns or "gross_profit" in df.columns

    if df.empty or not cogs_available:
        st.info(
            "이 종목은 DART 공시에 매출원가 계정이 별도로 표시되지 않습니다. "
            "(서비스업 등 일부 업종에서 발생)\n\n"
            "매출총이익으로 GPM 분석은 가능합니다."
        )
        if "gpm" in df.columns:
            fig_gpm = make_line_chart(
                df,
                cols=[("gpm", "GPM")],
                title="GPM(매출총이익률) 추이",
                unit="%",
                divisor=1.0,
                multiply=100.0,
            )
            st.plotly_chart(fig_gpm, use_container_width=True)
    else:
        # Stacked bar 차트
        fig_stack = make_stacked_area_chart(df, "매출원가 / 매출총이익 구성 추이")
        st.plotly_chart(fig_stack, use_container_width=True)

        # 원가비중 / GPM 선 차트
        fig_ratio = make_line_chart(
            df,
            cols=[("cogs_ratio", "원가비중"), ("gpm", "GPM")],
            title="원가비중(%) / GPM(%) 추이",
            unit="%",
            divisor=1.0,
            multiply=100.0,
        )
        st.plotly_chart(fig_ratio, use_container_width=True)

        # 테이블
        st.subheader("데이터 테이블")
        cost_cols = ["period", "revenue", "cogs", "gross_profit", "cogs_ratio", "gpm"]
        display_cols = [c for c in cost_cols if c in df.columns]
        st.dataframe(
            format_display_df(df[display_cols]),
            use_container_width=True,
            height=320,
        )


# ═════════════════════════════════════════════════
# TAB 3: 재무상태표
# ═════════════════════════════════════════════════
with tab_bs:
    st.subheader("주요 재무상태표 항목 추이")

    bs_cols_present = [
        c for c in ["inventory", "accounts_receivable", "total_borrowings"]
        if c in df.columns
    ]

    if not bs_cols_present:
        st.info(
            "DART 공시에서 재고자산 / 매출채권 / 차입금 계정을 찾지 못했습니다.\n\n"
            "가능한 원인:\n"
            "- 금융업 등 BS 구조가 다른 업종\n"
            "- DART 계정명이 표준 명칭과 다른 경우\n"
            "- 연결재무제표(CFS) ↔ 별도(OFS) 전환 후 재시도"
        )
    else:
        col_a, col_b = st.columns(2)

        with col_a:
            if "inventory" in df.columns:
                fig_inv = make_line_chart(
                    df,
                    cols=[("inventory", "재고자산")],
                    title="재고자산 추이",
                )
                st.plotly_chart(fig_inv, use_container_width=True)

        with col_b:
            if "accounts_receivable" in df.columns:
                fig_ar = make_line_chart(
                    df,
                    cols=[("accounts_receivable", "매출채권")],
                    title="매출채권 추이",
                )
                st.plotly_chart(fig_ar, use_container_width=True)

        if "total_borrowings" in df.columns:
            fig_debt = make_line_chart(
                df,
                cols=[("total_borrowings", "차입금")],
                title="차입금 추이",
            )
            st.plotly_chart(fig_debt, use_container_width=True)

        # 테이블
        st.subheader("데이터 테이블")
        bs_display_cols = ["period"] + bs_cols_present
        st.dataframe(
            format_display_df(df[bs_display_cols]),
            use_container_width=True,
            height=320,
        )


# ═════════════════════════════════════════════════
# TAB 4: 효율성 지표
# ═════════════════════════════════════════════════
with tab_eff:
    st.subheader("운전자본 효율성 지표")

    turnover_present = any(
        c in df.columns
        for c in ["inventory_turnover", "ar_turnover", "dio", "dso"]
    )

    if not turnover_present:
        st.info(
            "재고자산 또는 매출채권 데이터가 없어 회전율을 계산할 수 없습니다.\n"
            "[재무상태표] 탭을 먼저 확인해주세요."
        )
    else:
        col_a, col_b = st.columns(2)

        with col_a:
            if "inventory_turnover" in df.columns:
                fig_it = make_line_chart(
                    df,
                    cols=[("inventory_turnover", "재고자산회전율")],
                    title="재고자산회전율 (연환산, 회/년)",
                    unit="회/년",
                    divisor=1.0,
                )
                st.plotly_chart(fig_it, use_container_width=True)

        with col_b:
            if "ar_turnover" in df.columns:
                fig_art = make_line_chart(
                    df,
                    cols=[("ar_turnover", "매출채권회전율")],
                    title="매출채권회전율 (연환산, 회/년)",
                    unit="회/년",
                    divisor=1.0,
                )
                st.plotly_chart(fig_art, use_container_width=True)

        col_c, col_d = st.columns(2)
        with col_c:
            if "dio" in df.columns:
                fig_dio = make_line_chart(
                    df,
                    cols=[("dio", "재고일수(DIO)")],
                    title="재고일수 DIO (일)",
                    unit="일",
                    divisor=1.0,
                )
                st.plotly_chart(fig_dio, use_container_width=True)

        with col_d:
            if "dso" in df.columns:
                fig_dso = make_line_chart(
                    df,
                    cols=[("dso", "매출채권회수일(DSO)")],
                    title="매출채권 회수일 DSO (일)",
                    unit="일",
                    divisor=1.0,
                )
                st.plotly_chart(fig_dso, use_container_width=True)

        # 테이블
        st.subheader("데이터 테이블")
        eff_cols = ["period", "inventory_turnover", "ar_turnover", "dio", "dso"]
        display_cols = [c for c in eff_cols if c in df.columns]
        if display_cols:
            st.dataframe(
                format_display_df(df[display_cols]),
                use_container_width=True,
                height=320,
            )

        st.caption(
            "※ 회전율 = 분기 매출원가(or 매출액) × 4 / 기초·기말 평균 잔액 (연환산)\n"
            "※ 첫 분기는 이전 분기 잔액이 없어 계산에서 제외됩니다."
        )


# ═════════════════════════════════════════════════
# TAB 5: 수주잔고 / 가동율
# ═════════════════════════════════════════════════
with tab_order:
    st.subheader("수주잔고 / 공장 가동율")

    if not st.session_state.get("fetch_text"):
        st.info(
            "이 분석은 DART 보고서 원문을 추가로 다운로드해야 합니다.\n\n"
            "사이드바에서 **'수주잔고 / 가동율 분석'** 체크박스를 켜고 **분석 시작**을 다시 클릭하세요.\n\n"
            "⚠️ 원문 다운로드는 보고서 수에 따라 수 분이 소요될 수 있습니다."
        )
    else:
        # 텍스트 데이터 로딩
        if "text_result" not in st.session_state:
            try:
                text_result = load_text_data(
                    api_key=st.session_state["api_key"],
                    corp_code=company.corp_code,
                    corp_name=company.corp_name,
                    start_date=f"{st.session_state['start_year']}0101",
                    end_date=f"{st.session_state['end_year']}1231",
                )
                st.session_state["text_result"] = text_result
            except Exception as e:
                st.error(f"원문 수집 오류: {e}")
                text_result = {"order_backlogs": [], "utilizations": []}
        else:
            text_result = st.session_state["text_result"]

        order_backlogs = text_result.get("order_backlogs", [])
        utilizations = text_result.get("utilizations", [])

        # ─ 수주잔고 ─
        st.markdown("#### 📦 수주잔고 / 기납품 수주잔고")
        if not order_backlogs:
            st.warning(
                "수주잔고 관련 내용을 찾지 못했습니다.\n"
                "수주 기반 사업이 아니거나 DART 보고서에 구조화된 수주 현황이 없을 수 있습니다."
            )
        else:
            # 파싱된 수치가 있는 항목 우선
            parsed_items = [i for i in order_backlogs if i.get("parsed_value") is not None]
            if parsed_items:
                # 수주잔고 추이 차트
                fig_bl = make_backlog_chart(order_backlogs)
                if fig_bl is not None:
                    st.plotly_chart(fig_bl, use_container_width=True)

                parsed_df = pd.DataFrame([
                    {
                        "보고서": i["report_nm"],
                        "공시일": i["rcept_dt"],
                        "구분": i["label"],
                        "수주잔고(억원)": f"{i['parsed_value']:,.1f}",
                    }
                    for i in parsed_items
                ])
                st.dataframe(parsed_df, use_container_width=True)
            else:
                st.info("수주잔고 섹션은 찾았으나 수치 파싱에 실패했습니다. 아래 원문을 직접 확인하세요.")

            st.markdown("**추출된 원문 스니펫**")
            for item in order_backlogs[:6]:
                with st.expander(
                    f"[{item.get('rcept_dt', '')}] {item.get('report_nm', '')} — {item['label']}"
                ):
                    st.text(item["snippet"])

        st.markdown("---")

        # ─ 공장 가동율 ─
        st.markdown("#### 🏭 공장 가동율")
        if not utilizations:
            st.warning(
                "공장 가동율 관련 내용을 찾지 못했습니다.\n"
                "제조업이 아니거나 DART 보고서에 가동율 기재가 없을 수 있습니다."
            )
        else:
            parsed_ut = [i for i in utilizations if i.get("parsed_pct") is not None]
            if parsed_ut:
                ut_df = pd.DataFrame([
                    {
                        "보고서": i["report_nm"],
                        "공시일": i["rcept_dt"],
                        "가동율(%)": f"{i['parsed_pct']:.1f}",
                    }
                    for i in parsed_ut
                ])
                st.dataframe(ut_df, use_container_width=True)

            st.markdown("**추출된 원문 스니펫**")
            for item in utilizations[:6]:
                with st.expander(
                    f"[{item.get('rcept_dt', '')}] {item.get('report_nm', '')} — 가동율"
                ):
                    st.text(item["snippet"])

        st.caption(
            "※ 텍스트 파싱 결과는 정규식 기반으로 완전하지 않을 수 있습니다.\n"
            "원문 스니펫을 직접 확인해 수치를 검증하세요."
        )


# ═════════════════════════════════════════════════
# TAB 6: 제품별 / 부문별 매출
# ═════════════════════════════════════════════════
with tab_prod:
    st.subheader("제품별 / 부문별 / 지역별 매출 현황")

    if not st.session_state.get("fetch_text"):
        st.info(
            "이 분석은 DART 보고서 원문을 추가로 다운로드해야 합니다.\n\n"
            "사이드바에서 **'수주잔고 / 가동율 분석'** 체크박스를 켜고 **분석 시작**을 다시 클릭하세요."
        )
    else:
        if "text_result" not in st.session_state:
            st.warning("먼저 수주잔고/가동율 탭을 열어 데이터를 로딩해주세요.")
        else:
            text_result = st.session_state["text_result"]
            product_revenues = text_result.get("product_revenues", [])

            if not product_revenues:
                st.warning(
                    "제품별/부문별 매출 현황 섹션을 찾지 못했습니다.\n\n"
                    "가능한 원인:\n"
                    "- DART 보고서에 별도 표기 없음\n"
                    "- 섹션 명칭이 표준과 다름 (원문 스니펫을 직접 확인하세요)"
                )
            else:
                # 최신 보고서 우선 표시 (rcept_dt 내림차순)
                product_revenues_sorted = sorted(
                    product_revenues,
                    key=lambda x: (x.get("rcept_dt", ""), -x.get("numbers_found", 0)),
                    reverse=True,
                )
                # 보고서별로 그룹핑해서 표시
                seen_reports: dict[str, list] = {}
                for item in product_revenues_sorted:
                    key = f"{item.get('rcept_dt', '')}_{item.get('report_nm', '')}"
                    seen_reports.setdefault(key, []).append(item)

                for report_key, items in list(seen_reports.items())[:8]:
                    first = items[0]
                    with st.expander(
                        f"[{first.get('rcept_dt', '')}] {first.get('report_nm', '')}",
                        expanded=(list(seen_reports.keys()).index(report_key) == 0),
                    ):
                        for item in items[:4]:
                            unit_str = f"  *(unit: {item['unit']})*" if item.get("unit") else ""
                            st.markdown(f"**{item['label']}**{unit_str}")
                            st.text(item["snippet"])
                            st.markdown("---")

            st.caption(
                "DART 보고서 원문 텍스트 추출 결과입니다. "
                "표 구조가 무너진 경우 원본 공시를 함께 확인하세요."
            )


# TAB 7: 전체 데이터 다운로드
with tab_data:
    st.subheader("전체 데이터 테이블 및 다운로드")

    if df.empty:
        st.warning("데이터가 없습니다.")
    else:
        display_full = format_display_df(df)
        st.dataframe(display_full, use_container_width=True, height=500)

        csv_bytes = display_full.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
        st.download_button(
            label="CSV 다운로드",
            data=csv_bytes,
            file_name=f"{company.stock_code}_{company.corp_name}_financial.csv",
            mime="text/csv",
        )

        col_desc = {
            "분기": "YYYYQN (예: 2024Q1)",
            "데이터출처": "Q4 등 파생 분기의 차감 출처 (예: FY - Q3)",
            "매출액(십억원)": "분기 매출액",
            "매출원가(십억원)": "분기 매출원가",
            "매출총이익(십억원)": "매출액 - 매출원가",
            "영업이익(십억원)": "분기 영업이익",
            "순이익(십억원)": "분기 당기순이익",
            "GPM(%)": "매출총이익률",
            "OPM(%)": "영업이익률",
            "NPM(%)": "순이익률",
            "원가비중(%)": "매출원가 / 매출액",
            "재고자산(십억원)": "분기말 재고자산",
            "매출채권(십억원)": "분기말 매출채권",
            "차입금(십억원)": "단기+장기차입금+사채 합계",
            "재고자산회전율(회/년)": "연환산 재고자산회전율",
            "매출채권회전율(회/년)": "연환산 매출채권회전율",
            "재고일수(DIO, 일)": "365 / 재고자산회전율",
            "매출채권회수일(DSO, 일)": "365 / 매출채권회전율",
        }
        desc_df = pd.DataFrame(
            [{"컬럼명": k, "설명": v} for k, v in col_desc.items()
             if k in display_full.columns]
        )
        st.dataframe(desc_df, use_container_width=True, height=300, hide_index=True)
