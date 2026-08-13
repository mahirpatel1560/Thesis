"""The momentum + quality screen: every factor formula, offline."""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from thesis import screen
from thesis.data import universe

AS_OF = date(2026, 7, 29)


def price_frame(
    *, n: int = 300, base: float = 100.0, points: dict[int, float] | None = None,
    volume: float = 1_000.0,
) -> pd.DataFrame:
    """A synthetic history. `points` pins individual bars so returns are exact."""
    closes = [base] * n
    for index, value in (points or {}).items():
        closes[index] = value
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({"Close": closes, "Volume": [volume] * n}, index=dates)


# With n=300: the last bar is index 299, 6m back is 299-126=173, 12m back is 299-252=47.
LAST, SIX, TWELVE = 299, 173, 47


def winner_frame(volume: float = 1_000.0) -> pd.DataFrame:
    """+50% over 6 months, +140% over 12."""
    return price_frame(points={TWELVE: 50.0, SIX: 80.0, LAST: 120.0}, volume=volume)


# ------------------------------------------------------------- factor formulas

def test_total_return_uses_the_close_lookback_days_ago() -> None:
    closes = winner_frame()["Close"]
    assert screen.total_return(closes, screen.LOOKBACK_6M) == pytest.approx(120 / 80 - 1)
    assert screen.total_return(closes, screen.LOOKBACK_12M) == pytest.approx(120 / 50 - 1)


def test_total_return_needs_more_bars_than_the_lookback() -> None:
    closes = price_frame(n=100)["Close"]
    assert screen.total_return(closes, screen.LOOKBACK_6M) is None
    assert screen.total_return(closes, 98) is not None


def test_median_dollar_volume_ignores_a_single_spike() -> None:
    """Median, not mean: one frantic session should not qualify a quiet name."""
    frame = price_frame(n=300, volume=1_000.0)
    frame.iloc[-1, frame.columns.get_loc("Volume")] = 10_000_000.0
    value = screen.median_dollar_volume(frame["Close"], frame["Volume"])
    assert value == pytest.approx(100_000.0)


def test_median_dollar_volume_uses_only_the_liquidity_window() -> None:
    frame = price_frame(n=300, volume=1_000.0)
    frame.iloc[: -screen.LIQUIDITY_WINDOW, frame.columns.get_loc("Volume")] = 0.0
    assert screen.median_dollar_volume(frame["Close"], frame["Volume"]) == pytest.approx(
        100_000.0
    )


def test_compute_factors_reports_a_reason_instead_of_guessing() -> None:
    short = price_frame(n=100)
    assert screen.compute_factors("ACME", short["Close"], short["Volume"]) == (
        screen.SHORT_HISTORY
    )
    empty = pd.Series(dtype=float)
    assert screen.compute_factors("ACME", empty, empty) == screen.NO_DATA


def test_compute_factors_fills_in_every_number() -> None:
    frame = winner_frame()
    factors = screen.compute_factors("ACME", frame["Close"], frame["Volume"])
    assert isinstance(factors, screen.Factors)
    assert factors.last_price == pytest.approx(120.0)
    assert factors.ret_6m == pytest.approx(0.5)
    assert factors.ret_12m == pytest.approx(1.4)
    assert factors.bars == 300


# ------------------------------------------------------------ liquidity gate

def test_liquidity_gate_rejects_a_penny_price() -> None:
    frame = price_frame(points={LAST: 3.0}, volume=1_000_000.0)
    factors = screen.compute_factors("PENNY", frame["Close"], frame["Volume"])
    assert screen.liquidity_reason(factors, screen.ScreenParams()) == screen.ILLIQUID_PRICE


def test_liquidity_gate_rejects_a_thin_tape() -> None:
    factors = screen.compute_factors("THIN", *_cols(winner_frame(volume=10.0)))
    assert screen.liquidity_reason(factors, screen.ScreenParams()) == screen.ILLIQUID_VOLUME


def test_liquidity_gate_passes_a_tradeable_name() -> None:
    factors = screen.compute_factors("LIQD", *_cols(winner_frame(volume=1_000_000.0)))
    assert screen.liquidity_reason(factors, screen.ScreenParams()) is None


def _cols(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    return frame["Close"], frame["Volume"]


# --------------------------------------------------------------- the scoring

def test_percentile_ranks_put_the_best_at_100() -> None:
    ranks = screen.percentile_ranks({"A": 0.1, "B": 0.2, "C": 0.3, "D": 0.4})
    assert ranks["D"] == pytest.approx(100.0)
    assert ranks["A"] == pytest.approx(25.0)
    assert ranks["C"] == pytest.approx(75.0)


def test_percentile_ranks_average_ties() -> None:
    ranks = screen.percentile_ranks({"A": 0.5, "B": 0.5, "C": 0.1})
    assert ranks["A"] == pytest.approx(ranks["B"])
    assert ranks["A"] == pytest.approx(83.333, abs=0.01)  # (2+3)/2 / 3 * 100


def test_composite_score_is_the_documented_weighted_sum() -> None:
    params = screen.ScreenParams()
    assert screen.composite_score(80.0, 60.0, params) == pytest.approx(70.0)
    tilted = screen.ScreenParams(weight_6m=0.25, weight_12m=0.75)
    assert screen.composite_score(80.0, 60.0, tilted) == pytest.approx(65.0)


def test_rank_momentum_orders_by_score_and_breaks_ties_reproducibly() -> None:
    def factors(ticker: str, r6: float, r12: float) -> screen.Factors:
        return screen.Factors(ticker, 100.0, r6, r12, 5e7, 300)

    rows = screen.rank_momentum(
        [
            factors("LOW", 0.01, 0.02),
            factors("ZZZ", 0.50, 0.50),
            factors("AAA", 0.50, 0.50),
            factors("MID", 0.20, 0.30),
        ],
        screen.ScreenParams(),
    )
    assert [r.ticker for r in rows] == ["AAA", "ZZZ", "MID", "LOW"]
    assert [r.momentum_rank for r in rows] == [1, 2, 3, 4]
    assert rows[0].score == pytest.approx(rows[1].score)  # tie broken on ticker


# ------------------------------------------------------------- the full run

UNIVERSE = ["AAA", "BBB", "CCC", "DDD", "EEE", "THIN", "NEW", "GONE"]

FRAMES = {
    "AAA": price_frame(points={TWELVE: 50.0, SIX: 80.0, LAST: 130.0}, volume=1_000_000.0),
    "BBB": price_frame(points={TWELVE: 60.0, SIX: 90.0, LAST: 125.0}, volume=1_000_000.0),
    "CCC": price_frame(points={TWELVE: 70.0, SIX: 95.0, LAST: 120.0}, volume=1_000_000.0),
    "DDD": price_frame(points={TWELVE: 80.0, SIX: 100.0, LAST: 110.0}, volume=1_000_000.0),
    "EEE": price_frame(points={TWELVE: 120.0, SIX: 110.0, LAST: 100.0}, volume=1_000_000.0),
    "THIN": price_frame(points={TWELVE: 10.0, SIX: 20.0, LAST: 200.0}, volume=1.0),
    "NEW": price_frame(n=50, volume=1_000_000.0),
    # GONE has no frame at all — a delisted or renamed symbol.
}

QUALITY = {
    "AAA": screen.Quality("AAA", 1_000.0, 900.0),   # passes
    "BBB": screen.Quality("BBB", -50.0, 900.0),     # loses money
    "CCC": screen.Quality("CCC", 1_000.0, -20.0),   # burns cash
    "DDD": screen.Quality("DDD", 500.0, 400.0),     # passes
    "EEE": screen.Quality("EEE", 500.0, 400.0),     # passes, but weak momentum
}


def fake_fetch(tickers, missing: tuple[str, ...] = ()) -> screen.PriceFetch:
    return screen.PriceFetch(
        frames={t: FRAMES[t] for t in tickers if t in FRAMES},
        downloaded=tuple(t for t in tickers if t in FRAMES),
        missing=missing,
    )


def run_screen(top: int = 20, **kwargs) -> screen.ScreenResult:
    calls: list[str] = kwargs.pop("calls", [])

    def quality_fetcher(ticker: str) -> screen.Quality:
        calls.append(ticker)
        return QUALITY.get(ticker, screen.Quality(ticker, None, None))

    kwargs.setdefault("price_fetcher", fake_fetch)
    return screen.run(
        UNIVERSE,
        top=top,
        quality_fetcher=quality_fetcher,
        as_of=AS_OF,
        **kwargs,
    )


def test_the_screen_excludes_before_it_ranks() -> None:
    result = run_screen()
    assert result.excluded["GONE"] == screen.NO_DATA
    assert result.excluded["NEW"] == screen.SHORT_HISTORY
    assert result.excluded["THIN"] == screen.ILLIQUID_VOLUME
    assert result.liquid_count == 5
    assert result.universe_size == 8


def test_the_quality_gate_removes_names_it_does_not_rank_them() -> None:
    result = run_screen()
    # Momentum order is AAA > BBB > CCC > DDD > EEE; BBB and CCC fail quality.
    assert [r.ticker for r in result.ranked] == ["AAA", "BBB", "CCC", "DDD", "EEE"]
    assert [r.ticker for r in result.selected] == ["AAA", "DDD", "EEE"]
    assert result.row("BBB").momentum_rank == 2  # rank is unchanged by the gate
    assert result.row("BBB").passes_quality is False


def test_quality_is_checked_lazily_down_the_ranking() -> None:
    """Only as many fundamentals as the top-N needs — it is a network call each."""
    calls: list[str] = []
    result = run_screen(top=1, calls=calls)
    assert [r.ticker for r in result.selected] == ["AAA"]
    assert calls == ["AAA"]
    assert result.quality_checks == 1
    assert result.row("EEE").quality_checked is False


def test_the_check_budget_stops_a_runaway_walk() -> None:
    calls: list[str] = []
    result = run_screen(top=20, params=screen.ScreenParams(max_quality_checks=2), calls=calls)
    assert calls == ["AAA", "BBB"]
    assert [r.ticker for r in result.selected] == ["AAA"]


def test_require_forces_a_quality_check_for_explain() -> None:
    calls: list[str] = []
    result = run_screen(top=1, require=["eee"], calls=calls)
    assert "EEE" in calls
    assert result.row("EEE").quality_checked is True
    assert result.selected == [result.row("AAA")]  # forcing does not change selection


def test_a_name_with_no_fundamentals_fails_the_gate_loudly() -> None:
    quality = screen.Quality("XXX", None, None)
    assert quality.passes is False
    assert quality.reason == screen.NO_FUNDAMENTALS


# ---------------------------------------------------------------- rendering

def test_the_table_shows_the_numbers_behind_each_rank() -> None:
    table = screen.render_table(run_screen(), top=20)

    assert "| 1 | AAA |" in table
    assert "Score = 0.5 x pctl(6m return) + 0.5 x pctl(12m return)" in table
    assert "Liquidity floor" in table
    assert "trailing net income > 0 and trailing FCF > 0" in table
    assert "Universe 8 · liquid 5" in table
    # Exclusions are summarised, not hidden.
    assert "no price history" in table
    assert "insufficient history" in table
    assert "thesis research TICKER" in table


def test_the_table_says_so_when_nothing_qualifies() -> None:
    result = screen.run(
        ["AAA"],
        price_fetcher=lambda tickers: screen.PriceFetch(frames={"AAA": FRAMES["AAA"]}),
        quality_fetcher=lambda t: screen.Quality(t, -1.0, -1.0),
        as_of=AS_OF,
    )
    assert "No name cleared every gate" in screen.render_table(result)


# ----------------------------------------------------------------- --explain

def test_explain_shows_the_arithmetic_for_a_selected_name() -> None:
    result = run_screen()
    text = screen.explain(result, "AAA")

    assert "6-month return" in text
    assert "12-month return" in text
    assert "momentum rank **1 of 5**" in text
    assert "Score = 0.5 x 100.0 + 0.5 x 100.0 = **100.0**" in text
    assert "quality gate is cleared" in text
    assert "Shown at position 1" in text


def test_explain_names_the_gate_that_removed_a_name() -> None:
    result = run_screen()
    text = screen.explain(result, "BBB")
    assert "Fails the quality gate" in text
    assert screen.NEGATIVE_EARNINGS in text
    assert "Not shown" in text
    assert "momentum rank 2 of 5" in text.lower().replace("**", "")


def test_explain_covers_a_name_that_never_reached_the_ranking() -> None:
    result = run_screen()
    assert screen.ILLIQUID_VOLUME in screen.explain(result, "THIN")
    assert "excluded before ranking" in screen.explain(result, "GONE")
    assert "insufficient history" in screen.explain(result, "NEW")


def test_explain_is_case_insensitive_and_handles_strangers() -> None:
    result = run_screen()
    assert "AAA" in screen.explain(result, "aaa")
    assert "not in the screened universe" in screen.explain(result, "NVDA")


def test_explain_says_when_quality_was_never_checked() -> None:
    result = run_screen(top=1)
    assert "Not checked" in screen.explain(result, "EEE")


# ----------------------------------------------------------------- universe

def test_the_default_universe_is_the_sp500_snapshot() -> None:
    tickers = universe.load()
    assert 400 < len(tickers) < 520
    assert "AAPL" in tickers
    assert len(set(tickers)) == len(tickers), "the snapshot contains duplicates"
    assert all(t == t.upper().strip() for t in tickers)
    assert "BRK-B" in tickers, "symbols use the Yahoo convention"


def test_a_custom_universe_file_overrides_the_snapshot(tmp_path) -> None:
    path = tmp_path / "watchlist.txt"
    path.write_text("# my list\naapl\nMSFT\n\nAAPL  # duplicate\n", encoding="utf-8")
    assert universe.load(path) == ("AAPL", "MSFT")


def test_a_universe_file_with_a_bom_still_parses(tmp_path) -> None:
    """Notepad and PowerShell write a BOM; a BOM must not corrupt the first ticker."""
    path = tmp_path / "bom.txt"
    path.write_text("AAPL\nMSFT\n", encoding="utf-8-sig")
    assert universe.load(path) == ("AAPL", "MSFT")


def test_an_empty_universe_file_is_an_error(tmp_path) -> None:
    path = tmp_path / "empty.txt"
    path.write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no tickers"):
        universe.load(path)


# ------------------------------------------- renamed and retired symbols

def test_renamed_tickers_are_resolved_to_their_current_symbol() -> None:
    """Regression: BK/MMC/FI/PARA were reported delisted; they had been renamed."""
    assert universe.resolve(["BK", "MMC", "FI", "PARA"]) == ("BNY", "MRSH", "FISV", "PSKY")


def test_retired_tickers_are_dropped_rather_than_screened_every_run() -> None:
    assert universe.resolve(["AAPL", "HES", "WBA", "MSFT"]) == ("AAPL", "MSFT")


def test_the_snapshot_contains_no_known_dead_or_renamed_symbols() -> None:
    tickers = set(universe.load())
    assert not tickers & set(universe.RETIRED), "a retired symbol is still screened"
    assert not tickers & set(universe.RENAMED), "an old symbol is still screened"
    for replacement in universe.RENAMED.values():
        assert replacement in tickers, f"{replacement} should have replaced its old symbol"


def test_an_old_symbol_in_a_watchlist_file_still_works(tmp_path) -> None:
    path = tmp_path / "watchlist.txt"
    path.write_text("BK\nAAPL\n", encoding="utf-8")
    assert universe.load(path) == ("BNY", "AAPL")


def test_resolve_does_not_duplicate_when_both_symbols_are_listed() -> None:
    assert universe.resolve(["BK", "BNY"]) == ("BNY",)


# ------------------------------------------------- snapshot age and staleness

def test_snapshot_describes_its_age() -> None:
    snap = universe.describe(today=date.fromisoformat(universe.AS_OF) + timedelta(days=30))
    assert snap.as_of == universe.AS_OF
    assert snap.age_days == 30
    assert snap.stale is False
    assert "snapshot" in snap.describe() and universe.AS_OF in snap.describe()
    assert "30 days old" in snap.describe()
    assert snap.warning() is None


def test_a_snapshot_past_ninety_days_warns() -> None:
    old = date.fromisoformat(universe.AS_OF) + timedelta(days=universe.STALE_AFTER_DAYS + 1)
    snap = universe.describe(today=old)

    assert snap.stale is True
    warning = snap.warning()
    assert warning is not None
    assert "91 days old" in warning
    assert "thesis universe --check" in warning


def test_the_staleness_boundary_is_exactly_ninety_days() -> None:
    base = date.fromisoformat(universe.AS_OF)
    assert universe.describe(today=base + timedelta(days=90)).stale is False
    assert universe.describe(today=base + timedelta(days=91)).stale is True


def test_a_custom_list_has_no_snapshot_date_to_judge(tmp_path) -> None:
    path = tmp_path / "watchlist.txt"
    path.write_text("AAPL\nMSFT\n", encoding="utf-8")
    snap = universe.describe(path)

    assert snap.as_of is None
    assert snap.stale is False
    assert snap.warning() is None
    assert "watchlist.txt" in snap.describe()
    assert "no snapshot date" in snap.describe()


def test_every_screen_is_stamped_with_the_snapshot_it_used() -> None:
    result = run_screen(snapshot=universe.describe(today=date.fromisoformat(universe.AS_OF)))
    table = screen.render_table(result)
    assert f"snapshot {universe.AS_OF}" in table
    assert "0 days old" in table


def test_a_stale_snapshot_warns_inside_the_screen_output() -> None:
    old = date.fromisoformat(universe.AS_OF) + timedelta(days=120)
    result = run_screen(snapshot=universe.describe(today=old))
    table = screen.render_table(result)

    assert "120 days old" in table
    assert "renames since then are invisible" in table


def test_a_screen_without_provenance_still_renders() -> None:
    table = screen.render_table(run_screen())
    assert "Universe 8 ·" in table
    assert "snapshot" not in table
