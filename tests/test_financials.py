import unittest

from stock_analyst.standalone import derive_quarterly, match_account, to_number


class StandaloneFinancialTests(unittest.TestCase):
    def test_derive_quarterly_metrics_from_cumulative_values_without_optional_dependencies(self):
        cumulative = [
            {"year": 2025, "report_period": "Q1", "metric": "revenue", "amount": 100.0},
            {"year": 2025, "report_period": "H1", "metric": "revenue", "amount": 230.0},
            {"year": 2025, "report_period": "Q3", "metric": "revenue", "amount": 390.0},
            {"year": 2025, "report_period": "FY", "metric": "revenue", "amount": 600.0},
            {"year": 2025, "report_period": "Q1", "metric": "operating_profit", "amount": 10.0},
            {"year": 2025, "report_period": "H1", "metric": "operating_profit", "amount": 31.0},
            {"year": 2025, "report_period": "Q3", "metric": "operating_profit", "amount": 55.0},
            {"year": 2025, "report_period": "FY", "metric": "operating_profit", "amount": 85.0},
        ]
        quarterly = derive_quarterly(cumulative)
        self.assertEqual([row["revenue"] for row in quarterly], [100.0, 130.0, 160.0, 210.0])
        self.assertEqual([row["operating_profit"] for row in quarterly], [10.0, 21.0, 24.0, 30.0])
        self.assertEqual([round(row["opm"], 4) for row in quarterly], [0.1, 0.1615, 0.15, 0.1429])

    def test_match_account_and_to_number_without_optional_dependencies(self):
        revenue_row = {"account_nm": "매출액", "account_id": "ifrs-full_Revenue"}
        operating_row = {"account_nm": "영업이익", "account_id": "dart_OperatingIncomeLoss"}
        self.assertEqual(match_account(revenue_row), "revenue")
        self.assertEqual(match_account(operating_row), "operating_profit")
        self.assertEqual(to_number("1,000"), 1000.0)
        self.assertEqual(to_number("(250)"), -250.0)


class PandasFinancialTests(unittest.TestCase):
    def setUp(self):
        try:
            import pandas as pd
        except ModuleNotFoundError:
            self.skipTest("pandas is not installed; standalone financial tests cover dependency-free behavior")
        self.pd = pd

    def test_package_normalize_cumulative_financials_when_pandas_is_available(self):
        from stock_analyst.dart import normalize_cumulative_financials

        frame = self.pd.DataFrame(
            [
                {"sj_div": "IS", "account_nm": "매출액", "account_id": "ifrs-full_Revenue", "thstrm_amount": "1,000", "currency": "KRW", "ord": "1"},
                {"sj_div": "IS", "account_nm": "영업이익", "account_id": "dart_OperatingIncomeLoss", "thstrm_amount": "100", "currency": "KRW", "ord": "2"},
            ]
        )
        normalized = normalize_cumulative_financials([(2026, "11013", frame)])
        self.assertEqual(set(normalized["metric"]), {"revenue", "operating_profit"})
        self.assertEqual(normalized.loc[normalized["metric"] == "revenue", "amount"].iloc[0], 1000.0)


if __name__ == "__main__":
    unittest.main()
