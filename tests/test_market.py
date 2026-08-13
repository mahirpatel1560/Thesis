"""Offline tests for the yfinance adapter's pure transforms (no network)."""

import pandas as pd
import pytest

from thesis.data import market


# --------------------------------------------------------------- formatting

def test_fmt_compact() -> None:
    assert market.fmt_compact(391_035_000_000) == "$391.04B"
    assert market.fmt_compact(2_500_000) == "$2.50M"
    assert market.fmt_compact(-1_200_000_000) == "-$1.20B"
    assert market.fmt_compact(42.5) == "$42.50"
    assert market.fmt_compact(None) == "n/a"
    assert market.fmt_compact(float("nan")) == "n/a"


def test_fmt_pct() -> None:
    assert market.fmt_pct(0.246) == "24.6%"
    assert market.fmt_pct(-0.031) == "-3.1%"
    assert market.fmt_pct(None) == "n/a"


# --------------------------------------------------------- summarize_prices

def test_summarize_prices() -> None:
    idx = pd.date_range("2024-01-01", periods=300, freq="B")
    close = pd.Series(range(100, 400), index=idx, dtype=float)
    df = pd.DataFrame({"Close": close})

    summary = market.summarize_prices(df)

    assert summary["last_close"] == 399.0
    assert summary["as_of"] == idx[-1].strftime("%Y-%m-%d")
    assert summary["high_52w"] == 399.0
    assert summary["low_52w"] == float(close.iloc[-252:].min())
    assert summary["ret_3m"] == pytest.approx(399.0 / 336.0 - 1.0)
    assert summary["ret_1y"] == pytest.approx(399.0 / 147.0 - 1.0)
    assert summary["ret_period"] == pytest.approx(399.0 / 100.0 - 1.0)
    assert summary["period_start"] == idx[0].strftime("%Y-%m-%d")


# ------------------------------------------------------- extract_financials

def _statement(rows: dict[str, list[float]], years: list[str]) -> pd.DataFrame:
    cols = pd.to_datetime(years)
    return pd.DataFrame(rows, index=cols).T


def test_extract_financials() -> None:
    income = _statement(
        {"Total Revenue": [400.0, 380.0], "Net Income": [100.0, 90.0], "Operating Income": [120.0, 110.0]},
        ["2025-09-30", "2024-09-30"],
    )
    balance = _statement(
        {"Total Debt": [50.0, 60.0], "Cash And Cash Equivalents": [30.0, 25.0]},
        ["2025-09-30", "2024-09-30"],
    )
    cashflow = _statement({"Free Cash Flow": [95.0, 85.0]}, ["2025-09-30", "2024-09-30"])

    fin = market.extract_financials(income, balance, cashflow)

    assert fin["revenue_by_year"] == [
        {"fy": 2025, "revenue": 400.0},
        {"fy": 2024, "revenue": 380.0},
    ]
    assert fin["net_income_by_year"][0] == {"fy": 2025, "net_income": 100.0}
    assert fin["operating_margin"] == pytest.approx(120.0 / 400.0)
    assert fin["net_margin"] == pytest.approx(100.0 / 400.0)
    assert fin["free_cash_flow"] == 95.0
    assert fin["total_debt"] == 50.0
    assert fin["cash"] == 30.0
    # Annual growth is computed here, not taken from the provider's quarterly field.
    assert fin["annual_revenue_growth"] == pytest.approx(400.0 / 380.0 - 1.0)


def test_extract_financials_annual_growth_needs_two_years() -> None:
    income = _statement({"Total Revenue": [400.0]}, ["2025-09-30"])
    assert market.extract_financials(income, None, None)["annual_revenue_growth"] is None
    assert market.extract_financials(None, None, None)["annual_revenue_growth"] is None


def test_extract_financials_handles_missing_frames() -> None:
    fin = market.extract_financials(None, None, None)
    assert fin["revenue_by_year"] == []
    assert fin["operating_margin"] is None
    assert fin["free_cash_flow"] is None


# ------------------------------------------------------------ normalize_news

def test_normalize_news_new_nested_shape() -> None:
    raw = [
        {
            "id": "x",
            "content": {
                "title": "Acme beats estimates",
                "pubDate": "2026-07-10T12:00:00Z",
                "provider": {"displayName": "Reuters"},
            },
        }
    ]
    items = market.normalize_news(raw)
    assert items == [
        {"title": "Acme beats estimates", "publisher": "Reuters", "published": "2026-07-10"}
    ]


def test_normalize_news_old_flat_shape() -> None:
    raw = [
        {
            "title": "Acme launches product",
            "publisher": "Bloomberg",
            "providerPublishTime": 1752537600,  # 2025-07-15 UTC
        }
    ]
    items = market.normalize_news(raw)
    assert items[0]["title"] == "Acme launches product"
    assert items[0]["publisher"] == "Bloomberg"
    assert items[0]["published"] == "2025-07-15"


def test_normalize_news_skips_untitled_and_handles_empty() -> None:
    assert market.normalize_news([{"content": {"title": ""}}, {}]) == []
    assert market.normalize_news([]) == []
