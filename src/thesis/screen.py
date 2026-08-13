"""Momentum + quality screen over a liquid US universe — Week 3.

No black boxes. Every factor is a plain formula, written out here and in the
README, and `--explain TICKER` will show the arithmetic for any name in the
universe — including the ones that were thrown out, and which gate threw them.

The pipeline, in order:

1. **Data gate** — a name needs `LOOKBACK_12M + 1` daily closes. Anything with a
   shorter history (recent IPO, delisted, bad symbol) is excluded and counted.
2. **Liquidity gate** — last close >= `min_price` and 63-day median dollar volume
   >= `min_dollar_volume`. A screen you cannot trade is a daydream.
3. **Momentum score** — cross-sectional percentile ranks of 6- and 12-month total
   return, equally weighted. Ranks, not raw returns, so one melt-up cannot drag
   the whole score.
4. **Quality gate** — trailing net income > 0 and trailing free cash flow > 0.
   A binary filter, not a score: it removes names, it does not rank them.

Quality is checked lazily, walking down the momentum ranking until `top` names
pass, because it costs one network round trip per name. That makes the momentum
rank universe-wide and honest, while keeping the run to seconds rather than
minutes.

Pure functions do the arithmetic; the `fetch_*` functions do the I/O.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd
import yfinance as yf

from thesis.data import pricecache, universe
from thesis.data.market import fmt_compact, fmt_pct

# ------------------------------------------------------------------ parameters

#: Trading days in the lookback windows. ~21 trading days per month.
LOOKBACK_6M = 126
LOOKBACK_12M = 252
#: Window for the liquidity measure, in trading days (~one quarter).
LIQUIDITY_WINDOW = 63

#: Equal weights: neither horizon is more trustworthy than the other.
WEIGHT_6M = 0.5
WEIGHT_12M = 0.5

DEFAULT_MIN_DOLLAR_VOLUME = 20_000_000.0
DEFAULT_MIN_PRICE = 5.0

# Exclusion reasons, as constants so tests and `--explain` agree on the wording.
NO_DATA = "no price history"
SHORT_HISTORY = "insufficient history"
ILLIQUID_PRICE = "price below the floor"
ILLIQUID_VOLUME = "dollar volume below the floor"
NEGATIVE_EARNINGS = "trailing earnings not positive"
NEGATIVE_FCF = "trailing free cash flow not positive"
NO_FUNDAMENTALS = "no fundamentals available"
#: Distinct from NO_DATA: the provider returned nothing even on individual
#: retries, which means the symbol is wrong or dead, not that the batch flaked.
UNKNOWN_SYMBOL = "symbol returned no data after retries"


@dataclass(frozen=True)
class ScreenParams:
    """Every threshold, in one place, so the screen is reproducible."""

    min_dollar_volume: float = DEFAULT_MIN_DOLLAR_VOLUME
    min_price: float = DEFAULT_MIN_PRICE
    weight_6m: float = WEIGHT_6M
    weight_12m: float = WEIGHT_12M
    #: How far down the momentum ranking to check fundamentals before giving up.
    max_quality_checks: int = 80


# ---------------------------------------------------------------- data models

@dataclass(frozen=True)
class Factors:
    """What the price history alone tells us about a name."""

    ticker: str
    last_price: float
    ret_6m: float
    ret_12m: float
    median_dollar_volume: float
    bars: int


@dataclass(frozen=True)
class Quality:
    """Trailing profitability. `None` means the data was not available."""

    ticker: str
    net_income_ttm: float | None
    free_cash_flow_ttm: float | None

    @property
    def reason(self) -> str | None:
        """Why this name fails the quality gate, or None if it passes."""
        if self.net_income_ttm is None or self.free_cash_flow_ttm is None:
            return NO_FUNDAMENTALS
        if self.net_income_ttm <= 0:
            return NEGATIVE_EARNINGS
        if self.free_cash_flow_ttm <= 0:
            return NEGATIVE_FCF
        return None

    @property
    def passes(self) -> bool:
        return self.reason is None


@dataclass
class ScreenRow:
    """One name's full scorecard."""

    factors: Factors
    pctl_6m: float
    pctl_12m: float
    score: float
    momentum_rank: int
    quality: Quality | None = None

    @property
    def ticker(self) -> str:
        return self.factors.ticker

    @property
    def quality_checked(self) -> bool:
        return self.quality is not None

    @property
    def passes_quality(self) -> bool:
        return self.quality is not None and self.quality.passes


@dataclass
class ScreenResult:
    as_of: date
    params: ScreenParams
    universe_size: int
    ranked: list[ScreenRow] = field(default_factory=list)
    selected: list[ScreenRow] = field(default_factory=list)
    excluded: dict[str, str] = field(default_factory=dict)
    quality_checks: int = 0
    prices: PriceFetch | None = None
    snapshot: universe.Snapshot | None = None

    def row(self, ticker: str) -> ScreenRow | None:
        ticker = ticker.upper()
        for row in self.ranked:
            if row.ticker == ticker:
                return row
        return None

    @property
    def liquid_count(self) -> int:
        return len(self.ranked)


# -------------------------------------------------------------- pure factor math

def total_return(closes: pd.Series, lookback: int) -> float | None:
    """`close[-1] / close[-(lookback+1)] - 1`, or None without enough history.

    Uses split- and dividend-adjusted closes, so this is a total return.
    """
    closes = closes.dropna()
    if len(closes) <= lookback:
        return None
    return float(closes.iloc[-1]) / float(closes.iloc[-(lookback + 1)]) - 1.0


def median_dollar_volume(
    closes: pd.Series, volumes: pd.Series, window: int = LIQUIDITY_WINDOW
) -> float | None:
    """Median of `close x volume` over the last `window` bars.

    Median rather than mean: one earnings-day volume spike should not make an
    illiquid name look tradeable.
    """
    frame = pd.concat([closes, volumes], axis=1).dropna()
    if frame.empty:
        return None
    tail = frame.iloc[-window:]
    return float((tail.iloc[:, 0] * tail.iloc[:, 1]).median())


def compute_factors(ticker: str, closes: pd.Series, volumes: pd.Series) -> Factors | str:
    """Factors for one name, or an exclusion reason if the data will not support them."""
    closes = closes.dropna()
    if closes.empty:
        return NO_DATA
    ret_12m = total_return(closes, LOOKBACK_12M)
    ret_6m = total_return(closes, LOOKBACK_6M)
    if ret_6m is None or ret_12m is None:
        return SHORT_HISTORY
    dollar_volume = median_dollar_volume(closes, volumes)
    if dollar_volume is None:
        return NO_DATA
    return Factors(
        ticker=ticker,
        last_price=float(closes.iloc[-1]),
        ret_6m=ret_6m,
        ret_12m=ret_12m,
        median_dollar_volume=dollar_volume,
        bars=len(closes),
    )


def liquidity_reason(factors: Factors, params: ScreenParams) -> str | None:
    """Why this name fails the liquidity floor, or None if it clears it."""
    if factors.last_price < params.min_price:
        return ILLIQUID_PRICE
    if factors.median_dollar_volume < params.min_dollar_volume:
        return ILLIQUID_VOLUME
    return None


def percentile_ranks(values: Mapping[str, float]) -> dict[str, float]:
    """Cross-sectional percentile rank, 0-100, ties averaged.

    `rank / n * 100`, so the largest value scores 100 and the smallest scores
    `100 / n`. Computed across the whole liquid universe, not the top slice.
    """
    if not values:
        return {}
    series = pd.Series(values, dtype=float)
    return {k: float(v) for k, v in (series.rank(pct=True) * 100.0).items()}


def composite_score(pctl_6m: float, pctl_12m: float, params: ScreenParams) -> float:
    """`weight_6m x pctl_6m + weight_12m x pctl_12m` — a 0-100 momentum score."""
    return params.weight_6m * pctl_6m + params.weight_12m * pctl_12m


def rank_momentum(
    factors: Sequence[Factors], params: ScreenParams
) -> list[ScreenRow]:
    """Score and rank every (already liquid) name. Highest composite is rank 1."""
    ranks_6m = percentile_ranks({f.ticker: f.ret_6m for f in factors})
    ranks_12m = percentile_ranks({f.ticker: f.ret_12m for f in factors})

    rows = [
        ScreenRow(
            factors=f,
            pctl_6m=ranks_6m[f.ticker],
            pctl_12m=ranks_12m[f.ticker],
            score=composite_score(ranks_6m[f.ticker], ranks_12m[f.ticker], params),
            momentum_rank=0,
        )
        for f in factors
    ]
    # Ties break on ticker so a screen is reproducible run to run.
    rows.sort(key=lambda r: (-r.score, r.ticker))
    for position, row in enumerate(rows, start=1):
        row.momentum_rank = position
    return rows


# ---------------------------------------------------------------------- fetching

@dataclass
class PriceFetch:
    """Frames plus an account of how each name was obtained.

    `recovered` and `missing` are kept apart deliberately. A name that failed the
    bulk download and then succeeded on its own was a transient provider failure;
    a name that fails every attempt is either delisted or renamed, and the fix for
    that is the universe list, not another retry.
    """

    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    from_cache: tuple[str, ...] = ()
    topped_up: tuple[str, ...] = ()
    downloaded: tuple[str, ...] = ()
    recovered: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def network_calls(self) -> int:
        return len(self.topped_up) + len(self.downloaded) + len(self.recovered)


def _usable(frame: pd.DataFrame | None) -> bool:
    return (
        frame is not None
        and not frame.empty
        and "Close" in frame
        and bool(frame["Close"].notna().any())
    )


def bulk_download(
    tickers: Sequence[str], period: str | None = None, start: date | None = None
) -> dict[str, pd.DataFrame]:
    """Batched daily OHLCV. Names the provider does not return are simply absent."""
    if not tickers:
        return {}
    raw = yf.download(
        list(tickers),
        period=period,
        start=start.isoformat() if start else None,
        auto_adjust=True,
        group_by="ticker",
        progress=False,
        threads=True,
    )
    if raw is None or raw.empty:
        return {}

    frames: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        for ticker in tickers:
            if ticker not in level0:
                continue
            frame = raw[ticker].dropna(how="all")
            if _usable(frame):
                frames[ticker] = frame
    elif len(tickers) == 1:
        frame = raw.dropna(how="all")
        if _usable(frame):
            frames[tickers[0]] = frame
    return frames


def single_download(
    ticker: str, period: str | None = None, start: date | None = None
) -> pd.DataFrame | None:
    """One ticker on its own endpoint — the retry path when a bulk batch drops it."""
    try:
        frame = yf.Ticker(ticker).history(
            period=period, start=start.isoformat() if start else None, auto_adjust=True
        )
    except Exception:
        return None
    return frame if _usable(frame) else None


def fetch_prices(
    tickers: Sequence[str],
    period: str = "2y",
    *,
    refresh: bool = False,
    use_cache: bool = True,
    retries: int = 2,
    backoff: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    today: date | None = None,
    bulk: Callable[..., dict[str, pd.DataFrame]] | None = None,
    single: Callable[..., pd.DataFrame | None] | None = None,
    conn: sqlite3.Connection | None = None,
) -> PriceFetch:
    """Prices for the universe: cache first, then bulk, then individual retries.

    Anything already fetched today is served from the cache with no network call.
    Anything older is topped up from its last stored bar. Whatever the bulk call
    still drops is retried on its own with exponential backoff before being
    called missing.
    """
    today = today or date.today()
    bulk = bulk or bulk_download
    single = single or single_download
    tickers = list(dict.fromkeys(tickers))

    owns_conn = False
    if use_cache and conn is None:
        conn = pricecache.connect()
        owns_conn = True
    if not use_cache:
        conn = None

    try:
        cached = pricecache.read_frames(conn, tickers) if conn else {}
        state = pricecache.fetch_state(conn, tickers) if conn else {}

        stale = [
            t
            for t in tickers
            if refresh or not pricecache.is_fresh(state, t, today) or t not in cached
        ]
        from_cache = [t for t in tickers if t not in stale]

        # Warm names already have history and only need the days since their last
        # bar; cold names need the full window.
        warm = [t for t in stale if not refresh and _usable(cached.get(t))]
        cold = [t for t in stale if t not in set(warm)]

        fresh: dict[str, pd.DataFrame] = {}
        if cold:
            fresh.update(bulk(cold, period=period))
        if warm:
            since = min(state[t].last_bar for t in warm if state.get(t) and state[t].last_bar)
            fresh.update(bulk(warm, start=since))

        # Retry whatever the batch dropped, one at a time, backing off between.
        recovered: list[str] = []
        missing: list[str] = []
        for ticker in stale:
            if ticker in fresh:
                continue
            frame = None
            for attempt in range(retries):
                sleep(backoff * (2**attempt))
                start = state[ticker].last_bar if ticker in warm and state.get(ticker) else None
                frame = single(ticker, period=None if start else period, start=start)
                if frame is not None:
                    break
            if frame is not None:
                fresh[ticker] = frame
                recovered.append(ticker)
            else:
                missing.append(ticker)

        frames: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            if ticker in from_cache:
                frames[ticker] = cached[ticker]
                continue
            merged = pricecache.merge(cached.get(ticker), fresh.get(ticker))
            if _usable(merged):
                frames[ticker] = merged
                if conn:
                    pricecache.write_frame(conn, ticker, merged, today, commit=False)
        if conn:
            conn.commit()
            if missing:
                pricecache.mark_fetched(conn, missing, today)

        return PriceFetch(
            frames=frames,
            from_cache=tuple(from_cache),
            topped_up=tuple(t for t in warm if t in fresh),
            downloaded=tuple(t for t in cold if t in fresh),
            recovered=tuple(recovered),
            missing=tuple(missing),
        )
    finally:
        if owns_conn and conn is not None:
            conn.close()


def fetch_quality(ticker: str) -> Quality:
    """Trailing net income and free cash flow. Never raises — missing data is a reason."""
    try:
        info: dict[str, Any] = yf.Ticker(ticker).info or {}
    except Exception:
        return Quality(ticker, None, None)
    return Quality(
        ticker=ticker,
        net_income_ttm=info.get("netIncomeToCommon"),
        free_cash_flow_ttm=info.get("freeCashflow"),
    )


# ------------------------------------------------------------------ the pipeline

def run(
    tickers: Sequence[str],
    params: ScreenParams | None = None,
    top: int = 20,
    price_fetcher: Callable[[Sequence[str]], PriceFetch] | None = None,
    quality_fetcher: Callable[[str], Quality] | None = None,
    require: Iterable[str] = (),
    as_of: date | None = None,
    snapshot: universe.Snapshot | None = None,
) -> ScreenResult:
    """Run the whole screen. `require` forces a quality check on named tickers.

    `require` exists for `--explain`: a name ranked 300th never gets its
    fundamentals fetched during a normal run, but explaining it needs them.
    """
    params = params or ScreenParams()
    price_fetcher = price_fetcher or fetch_prices
    quality_fetcher = quality_fetcher or fetch_quality
    result = ScreenResult(
        as_of=as_of or date.today(),
        params=params,
        universe_size=len(tickers),
        snapshot=snapshot,
    )

    fetched = price_fetcher(list(tickers))
    result.prices = fetched
    frames = fetched.frames
    unresolved = set(fetched.missing)

    liquid: list[Factors] = []
    for ticker in tickers:
        frame = frames.get(ticker)
        if frame is None or "Close" not in frame:
            result.excluded[ticker] = UNKNOWN_SYMBOL if ticker in unresolved else NO_DATA
            continue
        factors = compute_factors(ticker, frame["Close"], frame.get("Volume", pd.Series(dtype=float)))
        if isinstance(factors, str):
            result.excluded[ticker] = factors
            continue
        reason = liquidity_reason(factors, params)
        if reason:
            result.excluded[ticker] = reason
            continue
        liquid.append(factors)

    result.ranked = rank_momentum(liquid, params)

    forced = {t.upper() for t in require}
    for row in result.ranked:
        wanted = row.ticker in forced
        budget_left = (
            len(result.selected) < top and result.quality_checks < params.max_quality_checks
        )
        if not wanted and not budget_left:
            continue
        row.quality = quality_fetcher(row.ticker)
        result.quality_checks += 1
        if row.passes_quality and len(result.selected) < top:
            result.selected.append(row)

    return result


# --------------------------------------------------------------------- rendering

def _pctl(value: float) -> str:
    return f"{value:.1f}"


def render_table(result: ScreenResult, top: int = 20) -> str:
    """The ranked table, with the numbers that produced each rank."""
    params = result.params
    out: list[str] = []
    out.append(f"# Screen — momentum + quality — {result.as_of}")
    out.append("")
    if result.snapshot is not None:
        out.append(f"Universe: {result.snapshot.describe()}")
        stale = result.snapshot.warning()
        if stale:
            out.append("")
            out.append(f"> **{stale}**")
            out.append("")
    out.append(
        f"Universe {result.universe_size} · liquid {result.liquid_count} · "
        f"showing {min(top, len(result.selected))} that also pass quality"
    )
    out.append(
        f"Score = {params.weight_6m:g} x pctl(6m return) + "
        f"{params.weight_12m:g} x pctl(12m return), ranked across all "
        f"{result.liquid_count} liquid names."
    )
    out.append(
        f"Liquidity floor: median 63-day dollar volume >= "
        f"{fmt_compact(params.min_dollar_volume)} and price >= "
        f"{fmt_compact(params.min_price)}. "
        "Quality gate: trailing net income > 0 and trailing FCF > 0."
    )
    out.append("")

    if not result.selected:
        out.append("*No name cleared every gate.*")
        out.append("")
    else:
        out.append(
            "| # | Ticker | 6m ret | 12m ret | 6m pctl | 12m pctl | Score | "
            "Net income (ttm) | FCF (ttm) | Median $ vol | Price |"
        )
        out.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for position, row in enumerate(result.selected[:top], start=1):
            f = row.factors
            q = row.quality
            out.append(
                f"| {position} | {row.ticker} | {fmt_pct(f.ret_6m)} | {fmt_pct(f.ret_12m)} "
                f"| {_pctl(row.pctl_6m)} | {_pctl(row.pctl_12m)} | {row.score:.1f} "
                f"| {fmt_compact(q.net_income_ttm if q else None)} "
                f"| {fmt_compact(q.free_cash_flow_ttm if q else None)} "
                f"| {fmt_compact(f.median_dollar_volume)} | {fmt_compact(f.last_price)} |"
            )
        out.append("")

    if result.excluded:
        counts: dict[str, int] = {}
        for reason in result.excluded.values():
            counts[reason] = counts.get(reason, 0) + 1
        summary = ", ".join(f"{count} {reason}" for reason, count in sorted(counts.items()))
        out.append(f"Excluded before ranking: {summary}.")

    prices = result.prices
    if prices is not None:
        out.append(
            f"Prices: {len(prices.from_cache)} from cache, "
            f"{len(prices.topped_up)} topped up, {len(prices.downloaded)} downloaded in full."
        )
        if prices.recovered:
            out.append(
                f"Transient provider failures recovered on retry "
                f"({len(prices.recovered)}): {', '.join(sorted(prices.recovered))}."
            )
        if prices.missing:
            out.append(
                f"No data after retries — check for a renamed or delisted symbol "
                f"({len(prices.missing)}): {', '.join(sorted(prices.missing))}."
            )
    out.append(
        f"Fundamentals fetched for {result.quality_checks} name(s) — "
        "quality is checked walking down the ranking, not for the whole universe."
    )
    out.append("")
    out.append("A rank is a candidate, not a thesis. Run `thesis research TICKER` next.")
    return "\n".join(out) + "\n"


def explain(result: ScreenResult, ticker: str) -> str:
    """Why one name ranked where it did — or which gate removed it."""
    ticker = ticker.upper()
    params = result.params
    out: list[str] = [f"# Why {ticker} ranked where it did — {result.as_of}", ""]

    if ticker in result.excluded:
        reason = result.excluded[ticker]
        out.append(f"**{ticker} was excluded before ranking: {reason}.**")
        out.append("")
        if reason == UNKNOWN_SYMBOL:
            out.append(
                "The bulk download dropped it and every individual retry came back "
                "empty, so this is not a transient provider failure. The symbol has "
                "almost certainly been renamed or delisted — check it against the "
                "universe snapshot rather than re-running the screen."
            )
        elif reason in (NO_DATA, SHORT_HISTORY):
            out.append(
                f"The screen needs {LOOKBACK_12M + 1} daily closes to measure a "
                "12-month return. A recent listing, a delisting, or a symbol that "
                "has changed will all land here."
            )
        else:
            out.append(
                f"The liquidity floor is a median 63-day dollar volume of "
                f"{fmt_compact(params.min_dollar_volume)} and a price of at least "
                f"{fmt_compact(params.min_price)}."
            )
        return "\n".join(out) + "\n"

    row = result.row(ticker)
    if row is None:
        out.append(f"{ticker} is not in the screened universe.")
        return "\n".join(out) + "\n"

    f = row.factors
    n = result.liquid_count
    out.append("## The numbers")
    out.append("")
    out.append(f"- Last close: {fmt_compact(f.last_price)} ({f.bars} daily bars)")
    out.append(
        f"- 6-month return: {fmt_pct(f.ret_6m)} "
        f"= close / close {LOOKBACK_6M} trading days ago - 1"
    )
    out.append(
        f"- 12-month return: {fmt_pct(f.ret_12m)} "
        f"= close / close {LOOKBACK_12M} trading days ago - 1"
    )
    out.append(
        f"- Median 63-day dollar volume: {fmt_compact(f.median_dollar_volume)} "
        f"(floor {fmt_compact(params.min_dollar_volume)}) — cleared"
    )
    out.append("")
    out.append("## The score")
    out.append("")
    out.append(f"- 6-month return ranks in the **{_pctl(row.pctl_6m)}th** percentile of {n} liquid names")
    out.append(f"- 12-month return ranks in the **{_pctl(row.pctl_12m)}th** percentile")
    out.append(
        f"- Score = {params.weight_6m:g} x {_pctl(row.pctl_6m)} + "
        f"{params.weight_12m:g} x {_pctl(row.pctl_12m)} = **{row.score:.1f}**"
    )
    out.append(f"- That is momentum rank **{row.momentum_rank} of {n}**")
    out.append("")

    out.append("## The quality gate")
    out.append("")
    if row.quality is None:
        out.append(
            "Not checked — the run stopped fetching fundamentals before reaching "
            f"rank {row.momentum_rank}."
        )
    else:
        q = row.quality
        out.append(f"- Trailing net income: {fmt_compact(q.net_income_ttm)} (must be > 0)")
        out.append(f"- Trailing free cash flow: {fmt_compact(q.free_cash_flow_ttm)} (must be > 0)")
        out.append("")
        if q.passes:
            out.append("Both positive — the quality gate is cleared.")
        else:
            out.append(f"**Fails the quality gate: {q.reason}.**")
    out.append("")

    out.append("## Where it landed")
    out.append("")
    if row in result.selected:
        out.append(
            f"**Shown at position {result.selected.index(row) + 1}** of the table — "
            f"it is the {result.selected.index(row) + 1}th-highest momentum score "
            "among names that also pass quality."
        )
    elif row.quality is not None and not row.quality.passes:
        out.append(
            f"**Not shown.** Momentum rank {row.momentum_rank} of {n} would have "
            "placed it, but the quality gate removed it."
        )
    else:
        out.append(
            f"**Not shown.** Momentum rank {row.momentum_rank} of {n} is below the "
            "cut for the displayed table."
        )
    out.append("")
    out.append(
        "None of this is a thesis. It says this name has gone up and earns money; "
        "it says nothing about why, or whether that continues."
    )
    return "\n".join(out) + "\n"
