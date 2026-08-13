"""Shared fixtures and builders for journal/track tests (no network, no API)."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Iterable, Sequence

import pandas as pd
import pytest

from thesis import journal


def d(value: str) -> date:
    return date.fromisoformat(value)


# A plan that satisfies rule 1, so tests can vary one field at a time.
GOOD_THESIS = (
    "Dominant installed base, pricing power in services, and buybacks that shrink "
    "the share count every year."
)
GOOD_INVALIDATION = "Services revenue growth falls below 5% for two straight quarters."
GOOD_HORIZON = "Three to five years."
GOOD_EXIT = "Trim if it exceeds 25% of the account; sell outright if the thesis breaks."


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch) -> None:
    """No test may touch the project's real journal or price cache.

    Autouse because the review banner now opens the journal on *every* command,
    including `research` and `screen` — without this, running the CLI tests would
    create a journal.db in the repo root.
    """
    monkeypatch.setenv("THESIS_PRICE_CACHE", str(tmp_path / "prices.db"))
    monkeypatch.setenv("THESIS_DB", str(tmp_path / "journal.db"))


@pytest.fixture
def conn(tmp_path, monkeypatch) -> Iterable[sqlite3.Connection]:
    """A journal database in a tmp dir, also wired up for CLI runs via THESIS_DB."""
    db = tmp_path / "journal.db"
    monkeypatch.setenv("THESIS_DB", str(db))
    connection = journal.connect(db)
    yield connection
    connection.close()


def buy_request(**over) -> journal.BuyRequest:
    """A valid Core buy: 2 shares at $100 = $200. Override any field."""
    kwargs = dict(
        book=journal.REAL,
        ticker="ACME",
        bucket=journal.CORE,
        shares=2.0,
        price=100.0,
        thesis=GOOD_THESIS,
        invalidation=GOOD_INVALIDATION,
        horizon=GOOD_HORIZON,
        exit_plan=GOOD_EXIT,
        stop_price=None,
        trade_date=d("2026-07-20"),
    )
    kwargs.update(over)
    return journal.BuyRequest(**kwargs)


def deposit(amount: float, on: str = "2026-07-01", book: str = journal.REAL, id: int = 1):
    return journal.Deposit(id, book, d(on), amount, "")


def position(**over) -> journal.Position:
    kwargs = dict(
        id=1,
        book=journal.REAL,
        ticker="ACME",
        bucket=journal.CORE,
        entry_date=d("2026-07-10"),
        opened_at="2026-07-10T14:00:00+00:00",
        thesis=GOOD_THESIS,
        invalidation=GOOD_INVALIDATION,
        horizon=GOOD_HORIZON,
        exit_plan=GOOD_EXIT,
        stop_price=None,
        status="open",
        closed_at=None,
        outcome=None,
    )
    kwargs.update(over)
    return journal.Position(**kwargs)


def trade(
    side: str,
    shares: float,
    price: float,
    on: str,
    ticker: str = "ACME",
    id: int = 1,
    book: str = journal.REAL,
    position_id: int = 1,
) -> journal.Trade:
    when = d(on)
    settles = journal.settlement_date(when) if side == "sell" else when
    return journal.Trade(id, book, position_id, ticker, side, shares, price, when, settles)


def holding(trades: Sequence[journal.Trade], **position_over) -> journal.Holding:
    ticker = trades[0].ticker if trades else "ACME"
    position_over.setdefault("ticker", ticker)
    return journal.Holding(position=position(**position_over), trades=tuple(trades))


def state(
    deposits: Sequence[journal.Deposit] = (),
    holdings: Sequence[journal.Holding] = (),
    last_review: date | None = None,
    book: str = journal.REAL,
) -> journal.BookState:
    return journal.BookState(book, tuple(deposits), tuple(holdings), last_review)


def spy_series(prices: dict[str, float]) -> pd.Series:
    """A synthetic SPY close series: {"2026-07-01": 500.0, ...}."""
    index = pd.DatetimeIndex([pd.Timestamp(k) for k in prices])
    return pd.Series(list(prices.values()), index=index, dtype=float).sort_index()


#: A flat SPY tape — makes SPY P&L exactly zero so position math is isolated.
FLAT_SPY = spy_series({f"2026-07-{day:02d}": 500.0 for day in range(1, 32)})
