#!/usr/bin/env bash
set -euo pipefail

# 범용 DART 수집 → 보조자료 인입 → 리포트 생성 스크립트.
# 예시:
#   DART_API_KEY='발급받은_키' ./scripts/run_stock_pipeline.sh --corp-name 한글과컴퓨터 --stock-code 030520
#   DART_API_KEY='발급받은_키' ./scripts/run_stock_pipeline.sh --stock-code 005930

python -m stock_analyst.cli run "$@"
