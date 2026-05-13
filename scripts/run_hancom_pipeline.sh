#!/usr/bin/env bash
set -euo pipefail

# 하위 호환용 한글과컴퓨터 실행 래퍼입니다. 새 종목은 run_stock_pipeline.sh 또는 stock-analyst run을 사용하세요.
exec ./scripts/run_stock_pipeline.sh \
  --corp-name 한글과컴퓨터 \
  --stock-code 030520 \
  --start 20220101 \
  --end 20260513 \
  --include-report-codes 11013 \
  "$@"
