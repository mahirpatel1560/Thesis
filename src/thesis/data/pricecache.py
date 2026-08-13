"""Local price cache — SQLite, refreshed incrementally.

A screen over ~500 names pulls two years of daily bars every run, which is slow,
rude to the provider, and the main reason a run gets rate-limited into reporting
healthy companies as delisted. Bars that have already closed never change, so
they are stored once.

The cache is a plain table of `(ticker, date) -> close, volume` plus a record of
when each ticker was last fetched. A ticker fetched today is served without any
network call at all; a ticker fetched earlier is topped up from its last stored
bar rather than re-downloaded in full.

Freshness is "fetched today", not "the last bar is recent" — market holidays,
half-days and delistings all make bar-date arithmetic wrong in ways that silently
either re-download everything or serve stale data.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pandas as pd

from thesis import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    bar_date TEXT NOT NULL,
    close  REAL NOT NULL,
    volume REAL,
    PRIMARY KEY (ticker, bar_date)
);

CREATE TABLE IF NOT EXISTS fetches (
    ticker     TEXT PRIMARY KEY,
    fetched_on TEXT NOT NULL,
    last_bar   TEXT
);
"""


@dataclass(frozen=True)
class FetchState:
    ticker: str
    fetched_on: date
    last_bar: date | None


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the price cache."""
    db = Path(path) if path else config.price_cache_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def fetch_state(
    conn: sqlite3.Connection, tickers: Sequence[str]
) -> dict[str, FetchState]:
    """When each ticker was last fetched, and how recent its newest bar is."""
    if not tickers:
        return {}
    rows = conn.execute("SELECT ticker, fetched_on, last_bar FROM fetches").fetchall()
    wanted = set(tickers)
    state: dict[str, FetchState] = {}
    for row in rows:
        if row["ticker"] not in wanted:
            continue
        state[row["ticker"]] = FetchState(
            ticker=row["ticker"],
            fetched_on=date.fromisoformat(row["fetched_on"]),
            last_bar=date.fromisoformat(row["last_bar"]) if row["last_bar"] else None,
        )
    return state


def read_frames(
    conn: sqlite3.Connection, tickers: Sequence[str]
) -> dict[str, pd.DataFrame]:
    """Cached history per ticker, as Close/Volume frames on a DatetimeIndex."""
    if not tickers:
        return {}
    wanted = set(tickers)
    rows = conn.execute(
        "SELECT ticker, bar_date, close, volume FROM prices ORDER BY ticker, bar_date"
    ).fetchall()

    by_ticker: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        if row["ticker"] in wanted:
            by_ticker.setdefault(row["ticker"], []).append(row)

    frames: dict[str, pd.DataFrame] = {}
    for ticker, ticker_rows in by_ticker.items():
        index = pd.DatetimeIndex([pd.Timestamp(r["bar_date"]) for r in ticker_rows])
        frames[ticker] = pd.DataFrame(
            {
                "Close": [r["close"] for r in ticker_rows],
                "Volume": [r["volume"] for r in ticker_rows],
            },
            index=index,
        )
    return frames


def write_frame(
    conn: sqlite3.Connection,
    ticker: str,
    frame: pd.DataFrame,
    fetched_on: date,
    commit: bool = True,
) -> None:
    """Upsert a ticker's bars and stamp when it was fetched."""
    if frame is None or frame.empty or "Close" not in frame:
        return
    clean = frame.dropna(subset=["Close"])
    if clean.empty:
        return

    index = pd.DatetimeIndex(clean.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    index = index.normalize()
    volumes = clean["Volume"] if "Volume" in clean else pd.Series(index=clean.index, dtype=float)

    payload = [
        (
            ticker,
            stamp.date().isoformat(),
            float(close),
            None if pd.isna(volume) else float(volume),
        )
        for stamp, close, volume in zip(index, clean["Close"], volumes)
    ]
    conn.executemany(
        "INSERT INTO prices (ticker, bar_date, close, volume) VALUES (?,?,?,?) "
        "ON CONFLICT (ticker, bar_date) DO UPDATE SET "
        "close = excluded.close, volume = excluded.volume",
        payload,
    )
    conn.execute(
        "INSERT INTO fetches (ticker, fetched_on, last_bar) VALUES (?,?,?) "
        "ON CONFLICT (ticker) DO UPDATE SET "
        "fetched_on = excluded.fetched_on, last_bar = excluded.last_bar",
        (ticker, fetched_on.isoformat(), payload[-1][1]),
    )
    if commit:
        conn.commit()


def mark_fetched(
    conn: sqlite3.Connection, tickers: Iterable[str], fetched_on: date
) -> None:
    """Record an attempt that returned nothing, so a dead symbol is not retried all day."""
    conn.executemany(
        "INSERT INTO fetches (ticker, fetched_on, last_bar) VALUES (?,?,NULL) "
        "ON CONFLICT (ticker) DO UPDATE SET fetched_on = excluded.fetched_on",
        [(t, fetched_on.isoformat()) for t in tickers],
    )
    conn.commit()


def merge(cached: pd.DataFrame | None, fresh: pd.DataFrame | None) -> pd.DataFrame:
    """Combine cached and newly downloaded bars; the newer copy of a bar wins. Pure."""
    parts = [f for f in (cached, fresh) if f is not None and not f.empty]
    if not parts:
        return pd.DataFrame(columns=["Close", "Volume"])
    combined = pd.concat(parts)
    index = pd.DatetimeIndex(combined.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    combined.index = index.normalize()
    combined = combined[~combined.index.duplicated(keep="last")]
    return combined.sort_index()


def is_fresh(state: Mapping[str, FetchState], ticker: str, today: date) -> bool:
    """True when this ticker was already fetched today and needs no network call."""
    entry = state.get(ticker)
    return entry is not None and entry.fetched_on >= today


def stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Row and ticker counts, for the CLI to report cache size."""
    bars = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    tickers = conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0]
    return {"bars": bars, "tickers": tickers}
