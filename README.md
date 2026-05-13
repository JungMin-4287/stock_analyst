# stock_analyst

DART 정기보고서 기반으로 **사용자가 입력한 종목명 또는 종목코드**의 최근 분기/사업보고서 재무 데이터를 수집하고, 보조자료(증권사 리포트, 주주서한, 뉴스, 업황 자료)를 계속 인입해 투자 분석 리포트 초안을 갱신하는 범용 도구입니다.

## 지금 만든 범위

- 종목명(`--corp-name`) 또는 종목코드(`--stock-code`)로 DART 상장회사 코드를 찾습니다.
- 정기보고서 목록, 전체 재무제표, 보고서 원문을 DART에서 가져옵니다.
- 누적 손익계정에서 분기 매출, 매출총이익, 영업이익, 순이익, OPM, GPM, NPM을 산출합니다.
- 보고서 원문에서 매출처/주요 고객/제품별/지역별/영업부문 관련 주석 후보를 추출합니다.
- 증권사 리포트, 회사 주주서한, 외부 뉴스, 글로벌 업황 자료를 `data/materials/`로 받아 리포트 작업본에 반영합니다.
- Markdown 리포트와 HTML 차트를 생성합니다.

## 바로 실행하기

아직 특정 회사 데이터가 저장된 것이 아니라, **어떤 종목이든 입력하면 수집·분석·리포트 생성까지 수행하는 파이프라인을 만든 상태**입니다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 키를 명령어 기록에 남기기 싫으면 환경변수 권장
export DART_API_KEY='여기에_DART_API_KEY'

# 종목명 + 종목코드로 실행
./scripts/run_stock_pipeline.sh --corp-name 한글과컴퓨터 --stock-code 030520 --start 20220101 --end 20260513 --include-report-codes 11013

# 종목코드만으로도 실행 가능
./scripts/run_stock_pipeline.sh --stock-code 005930 --start 20220101 --end 20260513 --include-report-codes 11013
```

`stock-analyst` 콘솔 스크립트를 설치하지 않아도 `python -m stock_analyst.cli run ...` 방식으로 동일하게 실행할 수 있습니다.

> 실행 환경에서 `pip install`이 막히는 경우에도 `./scripts/run_stock_pipeline.sh`는 표준 라이브러리 기반 실행기를 사용하므로 바로 실행할 수 있습니다. 단, 이 경우 HTML 차트는 Plotly 인터랙티브 차트가 아니라 CSV 내용을 확인하기 위한 기본 HTML로 생성됩니다.

## 실행 후 확인할 파일

종목별 결과는 기본적으로 `outputs/{종목코드}_{회사명}/` 아래에 저장됩니다.

- `company.json`: DART에서 찾은 회사 메타데이터
- `filings.csv`: 기간 내 정기보고서 목록
- `financials_cumulative.csv`: DART 누적 손익계정 원천 데이터
- `financials_quarterly.csv`: 누적 실적을 차감해 산출한 분기 매출, 매출총이익, 영업이익, 순이익, OPM, GPM, NPM
- `revenue_note_candidates.csv`: 보고서 원문에서 추출한 매출처/제품/지역/영업부문 관련 주석 후보
- `supplemental_materials.jsonl`: 보조자료 인입 결과

리포트는 기본적으로 `reports/{종목코드}_{회사명}_report.md`, 차트는 `reports/{종목코드}_{회사명}_financial_chart.html`로 생성됩니다.

## 보조자료 인입 방식

아래 폴더에 증권사 리포트, 회사 주주서한, 외부 뉴스, 글로벌 업황 자료를 `txt`, `md`, `json`, `csv`, `yaml`, `pdf` 형식으로 넣고 `run`을 다시 실행하세요.

```bash
mkdir -p data/materials
cp ~/Downloads/새_리포트.pdf data/materials/
./scripts/run_stock_pipeline.sh --stock-code 030520 --start 20220101 --end 20260513 --include-report-codes 11013
```

보조자료가 회사별로 섞이는 것이 싫으면 `--materials-dir data/materials/030520`처럼 종목별 폴더를 지정하면 됩니다.

## 명령어를 단계별로 실행하기

### 1) DART 데이터 수집

```bash
python -m stock_analyst.cli dart-fetch \
  --stock-code 030520 \
  --start 20220101 \
  --end 20260513 \
  --include-report-codes 11013
```

### 2) 보조자료 인입

```bash
python -m stock_analyst.cli ingest-materials \
  --input-dir data/materials/030520 \
  --output outputs/030520_한글과컴퓨터/supplemental_materials.jsonl
```

### 3) 리포트 초안 및 차트 생성

```bash
python -m stock_analyst.cli build-report \
  --company-name 한글과컴퓨터 \
  --financial-csv outputs/030520_한글과컴퓨터/financials_quarterly.csv \
  --materials-jsonl outputs/030520_한글과컴퓨터/supplemental_materials.jsonl \
  --revenue-breakdown-csv outputs/030520_한글과컴퓨터/revenue_breakdown.csv \
  --chart-html reports/030520_한글과컴퓨터_financial_chart.html \
  --output reports/030520_한글과컴퓨터_report.md
```

## 매출처별 매출/이익 처리

DART 표준 재무제표 API만으로는 매출처별 매출과 이익이 항상 구조화되어 제공되지 않습니다. 그래서 이 도구는 먼저 `revenue_note_candidates.csv`에 관련 주석 후보를 뽑아두고, 확인된 값을 `templates/revenue_breakdown_template.csv` 형식으로 `outputs/{종목코드}_{회사명}/revenue_breakdown.csv`에 입력하도록 설계했습니다.

입력 컬럼은 다음과 같습니다.

```csv
period,category_type,category_name,revenue,gross_profit,operating_profit,source,notes
2026Q1,customer_or_segment,예시_공공,0,0,0,DART_주석_또는_보조자료,매출처/부문별 수치를 확인 후 입력
```

이 파일이 있으면 리포트에 매출처/부문별 매출, 영업이익, OPM, GPM 테이블이 자동으로 포함됩니다.

## 데이터 해석 메모

- DART 단일회사 전체 재무제표 API는 분기 보고서의 누적 손익계정을 제공합니다. 이 도구는 같은 회계연도 안에서 누적값을 차감해 Q2, Q3, Q4 분기값을 산출합니다.
- `--include-report-codes 11013`은 종료연도에 1분기 보고서까지만 포함한다는 뜻입니다. 반기·3분기·사업보고서까지 확장하려면 `11012`, `11014`, `11011`을 추가하세요.
- 기본값은 연결재무제표(`CFS`)입니다. 별도 기준이 필요하면 `--fs-div OFS`를 사용하세요.
