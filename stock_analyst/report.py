from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .materials import load_materials


def build_financial_chart(df: pd.DataFrame, output_html: Path) -> None:
    output_html.parent.mkdir(parents=True, exist_ok=True)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    if "revenue" in df:
        fig.add_trace(go.Bar(x=df["period"], y=df["revenue"] / 1_000_000_000, name="매출액(십억원)"), secondary_y=False)
    if "operating_profit" in df:
        fig.add_trace(go.Bar(x=df["period"], y=df["operating_profit"] / 1_000_000_000, name="영업이익(십억원)"), secondary_y=False)
    if "opm" in df:
        fig.add_trace(go.Scatter(x=df["period"], y=df["opm"] * 100, name="OPM(%)", mode="lines+markers"), secondary_y=True)
    if "gpm" in df:
        fig.add_trace(go.Scatter(x=df["period"], y=df["gpm"] * 100, name="GPM(%)", mode="lines+markers"), secondary_y=True)
    fig.update_layout(title="분기 실적 및 마진 추이", barmode="group", template="plotly_white")
    fig.update_yaxes(title_text="금액(십억원)", secondary_y=False)
    fig.update_yaxes(title_text="마진(%)", secondary_y=True)
    fig.write_html(output_html, include_plotlyjs="cdn")


def _format_table(df: pd.DataFrame) -> str:
    table = df.copy()
    for col in ["revenue", "gross_profit", "operating_profit", "net_income"]:
        if col in table:
            table[col] = (table[col] / 1_000_000_000).round(1)
    for col in ["opm", "gpm", "npm"]:
        if col in table:
            table[col] = (table[col] * 100).round(1)
    columns = [c for c in ["period", "revenue", "gross_profit", "operating_profit", "net_income", "opm", "gpm", "npm"] if c in table]
    return table[columns].to_markdown(index=False)



def _format_breakdown_table(df: pd.DataFrame) -> str:
    table = df.copy()
    for col in ["revenue", "gross_profit", "operating_profit"]:
        if col in table:
            table[col] = (pd.to_numeric(table[col], errors="coerce") / 1_000_000_000).round(1)
    for col in ["opm", "gpm"]:
        if col in table:
            table[col] = (pd.to_numeric(table[col], errors="coerce") * 100).round(1)
    columns = [c for c in ["period", "category_type", "category_name", "revenue", "gross_profit", "operating_profit", "opm", "gpm", "source", "notes"] if c in table]
    return table[columns].to_markdown(index=False)

def build_report(
    financial_csv: Path,
    materials_jsonl: Path,
    chart_html: Path,
    output_md: Path,
    company_name: str,
    revenue_breakdown_csv: Path | None = None,
) -> None:
    df = pd.read_csv(financial_csv)
    materials = load_materials(materials_jsonl)
    revenue_breakdown = pd.DataFrame()
    if revenue_breakdown_csv and revenue_breakdown_csv.exists():
        revenue_breakdown = pd.read_csv(revenue_breakdown_csv)
        if {"revenue", "gross_profit", "operating_profit"}.issubset(revenue_breakdown.columns):
            revenue_breakdown["gpm"] = revenue_breakdown["gross_profit"] / revenue_breakdown["revenue"]
            revenue_breakdown["opm"] = revenue_breakdown["operating_profit"] / revenue_breakdown["revenue"]
    build_financial_chart(df, chart_html)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    latest_period = df["period"].iloc[-1] if not df.empty else "N/A"
    sections = [
        f"# {company_name} 투자 분석 리포트 작업본",
        "",
        f"- 최신 반영 분기: **{latest_period}**",
        f"- 차트: [{chart_html.name}]({chart_html.as_posix()})",
        "",
        "## 1. 분기 재무 테이블",
        "",
        _format_table(df) if not df.empty else "재무 데이터가 없습니다.",
        "",
        "## 2. 매출처/부문별 수익성 테이블",
        "",
        _format_breakdown_table(revenue_breakdown) if not revenue_breakdown.empty else "매출처/부문별 구조화 데이터가 없습니다. `templates/revenue_breakdown_template.csv`를 복사해 DART 주석 후보와 보조자료에서 확인한 값을 입력하세요.",
        "",
        "## 3. 투자전략 업데이트 체크리스트",
        "",
        "- 매출 성장률: 제품/클라우드/AI 오피스 전환 기여도를 보조자료로 검증",
        "- 수익성: GPM·OPM 개선이 일회성 비용 감소인지 구조적 믹스 개선인지 검증",
        "- 현금흐름: 영업현금흐름과 운전자본 변동이 순이익을 뒷받침하는지 검증",
        "- 주주환원/자본배치: 배당, 자사주, M&A, R&D 투자 우선순위 점검",
        "- 리스크: 공공/기업 IT 예산, 경쟁사 가격정책, 클라우드 전환 비용, 연결 자회사 변동 점검",
        "",
        "## 4. 보조자료 인입 현황",
        "",
    ]
    if materials:
        for material in materials:
            preview = " ".join(material.text.split())[:300]
            sections.append(f"### {material.title}")
            sections.append(f"- 파일: `{material.source_path}`")
            sections.append(f"- 종류: {material.kind}")
            sections.append(f"- 요약 후보: {preview}")
            sections.append("")
    else:
        sections.append("아직 인입된 보조자료가 없습니다. `data/materials/`에 증권사 리포트, 주주서한, 뉴스 텍스트/PDF를 넣고 ingest 명령을 실행하세요.")
    sections.extend(
        [
            "",
            "## 5. 다음 작성 지시 프롬프트",
            "",
            "> 새 자료를 `data/materials/`에 추가한 뒤 `stock-analyst ingest-materials`와 `stock-analyst build-report`를 재실행하세요. 자료별 핵심 수치, 투자포인트, 리스크, 기존 가설의 변경 여부를 위 테이블과 함께 갱신합니다.",
        ]
    )
    output_md.write_text("\n".join(sections) + "\n", encoding="utf-8")
