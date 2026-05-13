import pandas as pd

from stock_analyst.dart import derive_quarterly_metrics, normalize_cumulative_financials


def test_derive_quarterly_metrics_from_cumulative_values():
    cumulative = pd.DataFrame(
        [
            {"year": 2025, "report_period": "Q1", "metric": "revenue", "amount": 100.0},
            {"year": 2025, "report_period": "H1", "metric": "revenue", "amount": 230.0},
            {"year": 2025, "report_period": "Q3", "metric": "revenue", "amount": 390.0},
            {"year": 2025, "report_period": "FY", "metric": "revenue", "amount": 600.0},
            {"year": 2025, "report_period": "Q1", "metric": "operating_profit", "amount": 10.0},
            {"year": 2025, "report_period": "H1", "metric": "operating_profit", "amount": 31.0},
            {"year": 2025, "report_period": "Q3", "metric": "operating_profit", "amount": 55.0},
            {"year": 2025, "report_period": "FY", "metric": "operating_profit", "amount": 85.0},
        ]
    )
    quarterly = derive_quarterly_metrics(cumulative)
    assert quarterly["revenue"].tolist() == [100.0, 130.0, 160.0, 210.0]
    assert quarterly["operating_profit"].tolist() == [10.0, 21.0, 24.0, 30.0]
    assert quarterly["opm"].round(4).tolist() == [0.1, 0.1615, 0.15, 0.1429]


def test_normalize_cumulative_financials_matches_korean_accounts():
    frame = pd.DataFrame(
        [
            {"sj_div": "IS", "account_nm": "매출액", "account_id": "ifrs-full_Revenue", "thstrm_amount": "1,000", "currency": "KRW", "ord": "1"},
            {"sj_div": "IS", "account_nm": "영업이익", "account_id": "dart_OperatingIncomeLoss", "thstrm_amount": "100", "currency": "KRW", "ord": "2"},
        ]
    )
    normalized = normalize_cumulative_financials([(2026, "11013", frame)])
    assert set(normalized["metric"]) == {"revenue", "operating_profit"}
    assert normalized.loc[normalized["metric"] == "revenue", "amount"].iloc[0] == 1000.0
