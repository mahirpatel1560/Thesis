"""yfinance adapter: prices, fundamentals, news.

Network calls live in the get_* functions; everything that transforms data is a
pure function so it can be tested offline.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------- formatting

def fmt_compact(value: float | int | None, prefix: str = "$") -> str:
    """391_035_000_000 -> '$391.04B'. None/NaN -> 'n/a'."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    sign = "-" if value < 0 else ""
    v = abs(float(value))
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= threshold:
            return f"{sign}{prefix}{v / threshold:.2f}{suffix}"
    return f"{sign}{prefix}{v:.2f}"


def fmt_pct(value: float | None) -> str:
    """0.246 -> '24.6%'. None/NaN -> 'n/a'."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value * 100:.1f}%"


# ------------------------------------------------------------------- prices

def get_prices(ticker: str, period: str = "2y") -> pd.DataFrame:
    """Daily OHLCV history (auto-adjusted) for the given period."""
    df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
    if df.empty:
        raise ValueError(f"No price history returned for {ticker!r} — bad ticker?")
    return df


def summarize_prices(df: pd.DataFrame) -> dict[str, Any]:
    """Reduce a price history frame to the numbers a brief needs. Pure."""
    close = df["Close"].dropna()
    last = float(close.iloc[-1])

    def ret_over(days: int) -> float | None:
        if len(close) <= days:
            return None
        return last / float(close.iloc[-(days + 1)]) - 1.0

    return {
        "last_close": last,
        "as_of": close.index[-1].strftime("%Y-%m-%d"),
        "high_52w": float(close.iloc[-252:].max()),
        "low_52w": float(close.iloc[-252:].min()),
        "ret_3m": ret_over(63),
        "ret_1y": ret_over(252),
        # return over the whole fetched window (a "2y" fetch is ~502 trading
        # days, so a fixed 504-day lookback would always come up empty)
        "ret_period": last / float(close.iloc[0]) - 1.0,
        "period_start": close.index[0].strftime("%Y-%m-%d"),
    }


# -------------------------------------------------- closes (journal + benchmark)

def _daily_closes(df: pd.DataFrame, ticker: str) -> pd.Series:
    """Close column as a date-indexed, tz-naive, sorted Series. Pure."""
    if df.empty or "Close" not in df:
        raise ValueError(f"No price history returned for {ticker!r} — bad ticker?")
    closes = df["Close"].dropna()
    if closes.empty:
        raise ValueError(f"No closes returned for {ticker!r}")
    index = pd.DatetimeIndex(closes.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    closes.index = index.normalize()
    return closes.sort_index()


def get_closes(ticker: str, start: str, end: str | None = None) -> pd.Series:
    """Daily adjusted closes from `start` (ISO date) through `end` (default: today)."""
    df = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=True)
    return _daily_closes(df, ticker)


def price_on(closes: pd.Series, on_date: Any, label: str = "price") -> float:
    """As-of close: the last close at or before `on_date`. Pure.

    Markets close on weekends and holidays, and a same-day lookup runs before
    the close, so "as of" is the only honest reading of a dated price.
    """
    prior = closes.loc[: pd.Timestamp(on_date)]
    if prior.empty:
        raise ValueError(f"no {label} close on or before {on_date} in the fetched history")
    return float(prior.iloc[-1])


def get_last_close(ticker: str) -> float:
    """Most recent close for a ticker (5-day window survives weekends/holidays)."""
    closes = _daily_closes(yf.Ticker(ticker).history(period="5d", auto_adjust=True), ticker)
    return float(closes.iloc[-1])


def get_last_closes(tickers: Iterable[str]) -> dict[str, float]:
    """Most recent close per ticker. Raises on the first ticker that fails."""
    return {t.upper(): get_last_close(t) for t in dict.fromkeys(t.upper() for t in tickers)}


# ------------------------------------------------------------- fundamentals

def _row(frame: pd.DataFrame | None, *labels: str) -> pd.Series | None:
    """First matching row from a yfinance statement frame, else None. Pure."""
    if frame is None or frame.empty:
        return None
    for label in labels:
        if label in frame.index:
            return frame.loc[label]
    return None


def extract_financials(
    income: pd.DataFrame | None,
    balance: pd.DataFrame | None,
    cashflow: pd.DataFrame | None,
) -> dict[str, Any]:
    """Pull revenue trend, margins, debt, and FCF out of yfinance statement frames.

    Columns of the input frames are fiscal-year end dates, newest first. Pure.
    """
    out: dict[str, Any] = {"revenue_by_year": [], "net_income_by_year": []}

    revenue = _row(income, "Total Revenue", "Operating Revenue")
    net_income = _row(income, "Net Income", "Net Income Common Stockholders")
    op_income = _row(income, "Operating Income", "EBIT")
    fcf = _row(cashflow, "Free Cash Flow")
    total_debt = _row(balance, "Total Debt")
    cash = _row(balance, "Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments")

    if revenue is not None:
        for date, value in revenue.dropna().items():
            year = date.year if hasattr(date, "year") else str(date)
            out["revenue_by_year"].append({"fy": year, "revenue": float(value)})
    if net_income is not None:
        for date, value in net_income.dropna().items():
            year = date.year if hasattr(date, "year") else str(date)
            out["net_income_by_year"].append({"fy": year, "net_income": float(value)})

    def latest(series: pd.Series | None) -> float | None:
        if series is None:
            return None
        clean = series.dropna()
        return float(clean.iloc[0]) if not clean.empty else None

    latest_revenue = latest(revenue)
    latest_op = latest(op_income)
    latest_ni = latest(net_income)
    out["operating_margin"] = (latest_op / latest_revenue) if latest_op and latest_revenue else None
    out["net_margin"] = (latest_ni / latest_revenue) if latest_ni and latest_revenue else None

    # Annual growth, computed here rather than taken from the provider: the
    # provider's `revenueGrowth` is a *quarterly* figure (see get_fundamentals).
    years = out["revenue_by_year"]
    out["annual_revenue_growth"] = (
        years[0]["revenue"] / years[1]["revenue"] - 1.0
        if len(years) > 1 and years[1]["revenue"]
        else None
    )
    out["free_cash_flow"] = latest(fcf)
    out["total_debt"] = latest(total_debt)
    out["cash"] = latest(cash)
    return out


def get_fundamentals(ticker: str) -> dict[str, Any]:
    """Valuation snapshot + annual financials for a ticker.

    `quarterly_revenue_growth_yoy` is the provider's `revenueGrowth` field, which
    measures the **most recent quarter against the same quarter a year earlier**,
    not the annual figure. Verified against the statements: AAPL 16.60%, KO
    12.07%, MU 345.72%, CAT 22.22% all reproduce the field to two decimals from
    quarterly revenue, while annual growth for the same names is 6.4%, 1.9%,
    48.9% and 4.3%. Mislabelling it as annual is how a brief ends up claiming
    Costco grew 21.5% when its annual figures imply 8.2%.

    It is also not always reconcilable with the provider's own statements — for
    COST the field reads 21.5% where its quarterly revenue implies 11.6% — so the
    packet carries it as a provider-reported metric next to an annual number we
    compute ourselves.
    """
    t = yf.Ticker(ticker)
    info: dict[str, Any] = t.info or {}

    snapshot = {
        "name": info.get("longName") or ticker.upper(),
        "sector": info.get("sector"),
        "industry": info.get("industry"),
        "market_cap": info.get("marketCap"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "price_to_sales": info.get("priceToSalesTrailing12Months"),
        "ev_to_ebitda": info.get("enterpriseToEbitda"),
        "dividend_yield": info.get("dividendYield"),
        "gross_margins": info.get("grossMargins"),
        "quarterly_revenue_growth_yoy": info.get("revenueGrowth"),
    }
    financials = extract_financials(t.income_stmt, t.balance_sheet, t.cashflow)
    return {"snapshot": snapshot, "financials": financials}


def get_valuation_snapshot(ticker: str) -> dict[str, Any]:
    """Lightweight valuation multiples for a single ticker (used for peers)."""
    info: dict[str, Any] = yf.Ticker(ticker).info or {}
    return {
        "ticker": ticker.upper(),
        "market_cap": info.get("marketCap"),
        "trailing_pe": info.get("trailingPE"),
        "forward_pe": info.get("forwardPE"),
        "price_to_sales": info.get("priceToSalesTrailing12Months"),
        "ev_to_ebitda": info.get("enterpriseToEbitda"),
        "quarterly_revenue_growth_yoy": info.get("revenueGrowth"),
        "gross_margins": info.get("grossMargins"),
    }


# --------------------------------------------------------------------- news

def normalize_news(raw: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Normalize yfinance news items across old (flat) and new (nested) shapes. Pure."""
    items: list[dict[str, str]] = []
    for entry in raw or []:
        content = entry.get("content") if isinstance(entry.get("content"), dict) else entry
        title = content.get("title") or ""
        if not title:
            continue
        provider = content.get("provider")
        if isinstance(provider, dict):
            publisher = provider.get("displayName") or ""
        else:
            publisher = content.get("publisher") or ""
        published = content.get("pubDate") or ""
        if not published and content.get("providerPublishTime"):
            published = datetime.fromtimestamp(
                int(content["providerPublishTime"]), tz=timezone.utc
            ).strftime("%Y-%m-%d")
        items.append({"title": title, "publisher": publisher, "published": str(published)[:10]})
    return items


def get_news(ticker: str, limit: int = 10) -> list[dict[str, str]]:
    """Recent news headlines for a ticker."""
    return normalize_news(yf.Ticker(ticker).news or [])[:limit]
