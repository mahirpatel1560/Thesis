"""The price cache and the retry/classification logic in `screen.fetch_prices`.

No network: bulk and single downloads are injected, and `sleep` is captured so
backoff is asserted rather than waited on.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from thesis import screen
from thesis.data import pricecache

TODAY = date(2026, 7, 28)
YESTERDAY = date(2026, 7, 27)


def frame(days: int, start: str = "2026-01-01", close: float = 100.0) -> pd.DataFrame:
    index = pd.date_range(start, periods=days, freq="B")
    return pd.DataFrame(
        {"Close": [close] * days, "Volume": [1_000.0] * days}, index=index
    )


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("THESIS_PRICE_CACHE", str(tmp_path / "prices.db"))
    conn = pricecache.connect()
    yield conn
    conn.close()


# ------------------------------------------------------------- the cache

def test_a_written_frame_reads_back_intact(cache) -> None:
    pricecache.write_frame(cache, "AAA", frame(5), TODAY)
    read = pricecache.read_frames(cache, ["AAA"])["AAA"]

    assert len(read) == 5
    assert list(read.columns) == ["Close", "Volume"]
    assert read["Close"].iloc[0] == pytest.approx(100.0)
    assert read.index[0] == pd.Timestamp("2026-01-01")


def test_fetch_state_records_when_and_how_far(cache) -> None:
    pricecache.write_frame(cache, "AAA", frame(5), TODAY)
    state = pricecache.fetch_state(cache, ["AAA"])["AAA"]

    assert state.fetched_on == TODAY
    assert state.last_bar == date(2026, 1, 7)  # 5 business days from Jan 1
    assert pricecache.is_fresh({"AAA": state}, "AAA", TODAY) is True
    assert pricecache.is_fresh({"AAA": state}, "AAA", date(2026, 7, 29)) is False


def test_rewriting_a_bar_updates_it_rather_than_duplicating(cache) -> None:
    pricecache.write_frame(cache, "AAA", frame(3, close=100.0), YESTERDAY)
    pricecache.write_frame(cache, "AAA", frame(3, close=111.0), TODAY)
    read = pricecache.read_frames(cache, ["AAA"])["AAA"]

    assert len(read) == 3
    assert read["Close"].iloc[0] == pytest.approx(111.0)


def test_merge_prefers_the_newer_copy_of_an_overlapping_bar() -> None:
    cached = frame(5, close=100.0)
    fresh = frame(3, start="2026-01-07", close=200.0)
    merged = pricecache.merge(cached, fresh)

    assert merged.index.is_monotonic_increasing
    assert not merged.index.has_duplicates
    assert merged.loc["2026-01-07", "Close"] == pytest.approx(200.0)  # overlap: fresh wins
    assert merged.loc["2026-01-01", "Close"] == pytest.approx(100.0)  # history kept


def test_merge_handles_either_side_missing() -> None:
    assert pricecache.merge(None, frame(2)).shape[0] == 2
    assert pricecache.merge(frame(2), None).shape[0] == 2
    assert pricecache.merge(None, None).empty


def test_a_failed_symbol_is_stamped_so_it_is_not_retried_all_day(cache) -> None:
    pricecache.mark_fetched(cache, ["GONE"], TODAY)
    state = pricecache.fetch_state(cache, ["GONE"])["GONE"]
    assert state.fetched_on == TODAY
    assert state.last_bar is None


# ------------------------------------------------- fetch_prices: caching

def make_bulk(available: dict[str, pd.DataFrame], calls: list) -> callable:
    def bulk(tickers, period=None, start=None):
        calls.append({"tickers": list(tickers), "period": period, "start": start})
        return {t: available[t] for t in tickers if t in available}

    return bulk


def test_a_cold_run_downloads_everything_and_fills_the_cache(cache) -> None:
    calls: list = []
    available = {"AAA": frame(10), "BBB": frame(10)}

    result = screen.fetch_prices(
        ["AAA", "BBB"], bulk=make_bulk(available, calls), conn=cache, today=TODAY
    )

    assert set(result.frames) == {"AAA", "BBB"}
    assert set(result.downloaded) == {"AAA", "BBB"}
    assert result.from_cache == ()
    assert calls[0]["period"] == "2y"
    assert set(pricecache.read_frames(cache, ["AAA", "BBB"])) == {"AAA", "BBB"}


def test_a_second_run_the_same_day_makes_no_network_call_at_all(cache) -> None:
    """This is what makes --explain free: the snapshot is already on disk."""
    available = {"AAA": frame(10), "BBB": frame(10)}
    screen.fetch_prices(["AAA", "BBB"], bulk=make_bulk(available, []), conn=cache, today=TODAY)

    calls: list = []
    result = screen.fetch_prices(
        ["AAA", "BBB"], bulk=make_bulk(available, calls), conn=cache, today=TODAY
    )

    assert calls == [], "a same-day rerun must not touch the network"
    assert set(result.from_cache) == {"AAA", "BBB"}
    assert result.network_calls == 0
    assert len(result.frames["AAA"]) == 10


def test_the_next_day_tops_up_incrementally_from_the_last_bar(cache) -> None:
    screen.fetch_prices(
        ["AAA"], bulk=make_bulk({"AAA": frame(10)}, []), conn=cache, today=YESTERDAY
    )

    calls: list = []
    extra = frame(3, start="2026-01-14", close=150.0)
    result = screen.fetch_prices(
        ["AAA"], bulk=make_bulk({"AAA": extra}, calls), conn=cache, today=TODAY
    )

    assert calls[0]["start"] == date(2026, 1, 14)  # from the last stored bar
    assert calls[0]["period"] is None, "an incremental top-up must not re-request 2y"
    assert result.topped_up == ("AAA",)

    merged = result.frames["AAA"]
    # 10 cached + 3 fetched, overlapping on the last stored bar: 12 distinct days.
    # Re-requesting that bar is deliberate — it is how a restated close gets fixed.
    assert len(merged) == 12
    assert not merged.index.has_duplicates
    assert merged.loc["2026-01-13", "Close"] == pytest.approx(100.0)  # untouched history
    assert merged.loc["2026-01-14", "Close"] == pytest.approx(150.0)  # restated bar
    assert merged["Close"].iloc[-1] == pytest.approx(150.0)


def test_refresh_forces_a_full_download_over_the_cache(cache) -> None:
    screen.fetch_prices(
        ["AAA"], bulk=make_bulk({"AAA": frame(10)}, []), conn=cache, today=TODAY
    )

    calls: list = []
    result = screen.fetch_prices(
        ["AAA"],
        bulk=make_bulk({"AAA": frame(10)}, calls),
        conn=cache,
        today=TODAY,
        refresh=True,
    )
    assert calls[0]["period"] == "2y"
    assert result.downloaded == ("AAA",)
    assert result.from_cache == ()


def test_the_cache_can_be_switched_off(cache) -> None:
    calls: list = []
    screen.fetch_prices(
        ["AAA"], bulk=make_bulk({"AAA": frame(10)}, calls), use_cache=False, today=TODAY
    )
    assert pricecache.read_frames(cache, ["AAA"]) == {}


# --------------------------------------- fetch_prices: retry and triage

def test_a_name_dropped_by_the_batch_is_retried_and_recovered(cache) -> None:
    """A transient batch failure must not read as a delisting."""
    attempts: list[str] = []

    def single(ticker, period=None, start=None):
        attempts.append(ticker)
        return frame(10) if len(attempts) >= 2 else None  # fails once, then works

    result = screen.fetch_prices(
        ["AAA"],
        bulk=lambda tickers, period=None, start=None: {},
        single=single,
        sleep=lambda _: None,
        conn=cache,
        today=TODAY,
    )

    assert result.recovered == ("AAA",)
    assert result.missing == ()
    assert "AAA" in result.frames
    assert len(attempts) == 2


def test_a_name_that_never_returns_data_is_reported_missing_not_recovered(cache) -> None:
    result = screen.fetch_prices(
        ["GONE"],
        bulk=lambda tickers, period=None, start=None: {},
        single=lambda ticker, period=None, start=None: None,
        sleep=lambda _: None,
        conn=cache,
        today=TODAY,
        retries=3,
    )

    assert result.missing == ("GONE",)
    assert result.recovered == ()
    assert "GONE" not in result.frames


def test_retries_back_off_exponentially(cache) -> None:
    waits: list[float] = []
    screen.fetch_prices(
        ["GONE"],
        bulk=lambda tickers, period=None, start=None: {},
        single=lambda ticker, period=None, start=None: None,
        sleep=waits.append,
        conn=cache,
        today=TODAY,
        retries=3,
        backoff=1.0,
    )
    assert waits == [1.0, 2.0, 4.0]


def test_transient_and_missing_are_reported_separately(cache) -> None:
    def single(ticker, period=None, start=None):
        return frame(10) if ticker == "FLAKY" else None

    result = screen.fetch_prices(
        ["FLAKY", "GONE"],
        bulk=lambda tickers, period=None, start=None: {},
        single=single,
        sleep=lambda _: None,
        conn=cache,
        today=TODAY,
    )

    assert result.recovered == ("FLAKY",)
    assert result.missing == ("GONE",)


def test_single_download_swallows_provider_exceptions(monkeypatch) -> None:
    class Boom:
        def history(self, **kwargs):
            raise RuntimeError("connection reset")

    monkeypatch.setattr(screen.yf, "Ticker", lambda t: Boom())
    assert screen.single_download("AAA", period="2y") is None


# -------------------------------------------- the screen's own reporting

def test_the_screen_separates_unknown_symbols_from_thin_data() -> None:
    good = frame(300)
    result = screen.run(
        ["AAA", "GONE"],
        price_fetcher=lambda tickers: screen.PriceFetch(
            frames={"AAA": good}, missing=("GONE",)
        ),
        quality_fetcher=lambda t: screen.Quality(t, 1.0, 1.0),
        as_of=TODAY,
    )
    assert result.excluded["GONE"] == screen.UNKNOWN_SYMBOL
    assert screen.NO_DATA not in result.excluded.values()


def test_the_table_reports_cache_hits_and_both_failure_kinds() -> None:
    result = screen.run(
        ["AAA", "FLAKY", "GONE"],
        price_fetcher=lambda tickers: screen.PriceFetch(
            frames={"AAA": frame(300), "FLAKY": frame(300)},
            from_cache=("AAA",),
            recovered=("FLAKY",),
            missing=("GONE",),
        ),
        quality_fetcher=lambda t: screen.Quality(t, 1.0, 1.0),
        as_of=TODAY,
    )
    table = screen.render_table(result)

    assert "1 from cache" in table
    assert "Transient provider failures recovered on retry (1): FLAKY" in table
    assert "No data after retries" in table and "GONE" in table


def test_explain_tells_you_a_symbol_is_dead_not_flaky() -> None:
    result = screen.run(
        ["GONE"],
        price_fetcher=lambda tickers: screen.PriceFetch(frames={}, missing=("GONE",)),
        quality_fetcher=lambda t: screen.Quality(t, 1.0, 1.0),
        as_of=TODAY,
    )
    text = screen.explain(result, "GONE")
    assert "renamed or delisted" in text
    assert "not a transient provider failure" in text
