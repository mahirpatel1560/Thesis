"""Performance vs. same-dated SPY deposits + the exportable track record.

Rule 7: no number appears without its benchmark. The mechanism is a *mirror* —
every dollar the journal put to work is simultaneously put into SPY on the same
date, and taken back out in the same proportion on the same date. Comparing a
position to "SPY over the same window" then needs no hand-waving: it is the same
money, the same days.

Three mirrors, one function each:

* the account mirror runs on deposits — "what if the deposits had just bought SPY"
* a position mirror runs on that position's trades
* a bucket's benchmark is the sum of its positions' mirrors

Everything here is pure except `spy_history`; the CLI does the fetching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

import pandas as pd

from thesis import journal
from thesis.data import market
from thesis.journal import (
    Deposit,
    Holding,
    Position,
    SHARE_EPS,
    Trade,
)

BENCHMARK = "SPY"


# ------------------------------------------------------------------ SPY mirror

@dataclass
class SpyMirror:
    """A SPY position that shadows real cash flows, average-cost accounted."""

    shares: float = 0.0
    cost: float = 0.0
    realized: float = 0.0
    contributed: float = 0.0
    withdrawn: float = 0.0

    def buy_dollars(self, dollars: float, price: float) -> None:
        if dollars <= 0 or price <= 0:
            return
        self.shares += dollars / price
        self.cost += dollars
        self.contributed += dollars

    def sell_dollars(self, dollars: float, price: float) -> None:
        """Take a dollar amount back out (mirrors a withdrawal)."""
        if dollars <= 0 or price <= 0 or self.shares <= 0:
            return
        self.sell_shares(min(dollars / price, self.shares), price)

    def sell_fraction(self, fraction: float, price: float) -> None:
        """Exit the same proportion the real position exited."""
        if fraction <= 0 or self.shares <= 0:
            return
        self.sell_shares(self.shares * min(fraction, 1.0), price)

    def sell_shares(self, shares: float, price: float) -> None:
        if shares <= 0 or self.shares <= 0:
            return
        avg = self.cost / self.shares
        self.realized += shares * (price - avg)
        self.cost -= shares * avg
        self.shares -= shares
        self.withdrawn += shares * price
        if self.shares < SHARE_EPS:
            self.shares, self.cost = 0.0, 0.0

    def value(self, price: float) -> float:
        return self.shares * price

    def unrealized(self, price: float) -> float:
        return self.shares * price - self.cost

    def total_pnl(self, price: float) -> float:
        return self.realized + self.unrealized(price)

    def return_pct(self, price: float) -> float | None:
        if self.contributed <= 0:
            return None
        return self.total_pnl(price) / self.contributed


def mirror_deposits(deposits: Iterable[Deposit], spy: pd.Series) -> SpyMirror:
    """Same deposits, same dates, into SPY. Withdrawals sell SPY back out."""
    mirror = SpyMirror()
    for deposit in sorted(deposits, key=lambda d: (d.deposit_date, d.id or 0)):
        price = market.price_on(spy, deposit.deposit_date, BENCHMARK)
        if deposit.amount > 0:
            mirror.buy_dollars(deposit.amount, price)
        else:
            mirror.sell_dollars(-deposit.amount, price)
    return mirror


def mirror_trades(trades: Iterable[Trade], spy: pd.Series) -> SpyMirror:
    """Same dollars, same dates, into SPY — exited in the same proportions."""
    mirror = SpyMirror()
    shares = 0.0
    for trade in sorted(trades, key=lambda t: (t.trade_date, t.id or 0)):
        price = market.price_on(spy, trade.trade_date, BENCHMARK)
        if trade.side == "buy":
            mirror.buy_dollars(trade.dollars, price)
            shares += trade.shares
        else:
            fraction = trade.shares / shares if shares > SHARE_EPS else 1.0
            mirror.sell_fraction(fraction, price)
            shares -= trade.shares
    return mirror


# ----------------------------------------------------------------- report model

@dataclass
class Line:
    """One position, its numbers, and its SPY mirror's numbers side by side."""

    ticker: str
    bucket: str
    status: str
    shares: float
    avg_cost: float
    entry_date: date
    exit_date: date | None
    held_days: int
    last_price: float
    invested: float
    market_value: float
    pnl: float
    pnl_pct: float | None
    spy_pnl: float
    spy_pct: float | None
    edge_pp: float | None
    stop_price: float | None
    stop_breached: bool
    position: Position

    @property
    def beat_spy(self) -> bool:
        return self.pnl > self.spy_pnl


@dataclass
class BucketLine:
    bucket: str
    n_open: int
    n_closed: int
    invested: float
    value: float
    pnl: float
    pnl_pct: float | None
    spy_pnl: float
    spy_pct: float | None
    edge_pp: float | None


@dataclass
class Account:
    contributed: float
    withdrawn: float
    net_deposits: float
    equity: float
    cash: float
    settled: float
    unsettled: float
    pnl: float
    pnl_pct: float | None
    spy_equity: float
    spy_pnl: float
    spy_pct: float | None
    edge_pp: float | None


@dataclass
class Stats:
    n_closed: int
    wins: int
    losses: int
    win_rate: float | None
    avg_win: float | None
    avg_loss: float | None
    expectancy: float | None
    spy_expectancy: float | None
    beat_spy: int
    beat_spy_rate: float | None


@dataclass
class TrackReport:
    book: str
    as_of: date
    spy_as_of: date
    spy_price: float
    account: Account
    buckets: list[BucketLine] = field(default_factory=list)
    open_lines: list[Line] = field(default_factory=list)
    closed_lines: list[Line] = field(default_factory=list)
    stats: Stats | None = None

    @property
    def is_paper(self) -> bool:
        return self.book == journal.PAPER

    @property
    def all_lines(self) -> list[Line]:
        return self.open_lines + self.closed_lines


# ------------------------------------------------------------------- assembly

def _pct_diff(a: float | None, b: float | None) -> float | None:
    """Edge in percentage points."""
    if a is None or b is None:
        return None
    return (a - b) * 100.0


def _line(holding: Holding, price: float, spy: pd.Series, spy_now: float, as_of: date) -> Line:
    mirror = mirror_trades(holding.trades, spy)
    pnl = holding.total_pnl(price)
    pnl_pct = holding.return_pct(price)
    spy_pnl = mirror.total_pnl(spy_now)
    spy_pct = mirror.return_pct(spy_now)
    return Line(
        ticker=holding.ticker,
        bucket=holding.bucket,
        status=holding.position.status,
        shares=holding.shares,
        avg_cost=holding.avg_cost,
        entry_date=holding.entry_date,
        exit_date=holding.exit_date,
        held_days=holding.held_days(as_of),
        last_price=price,
        invested=holding.invested,
        market_value=holding.market_value(price),
        pnl=pnl,
        pnl_pct=pnl_pct,
        spy_pnl=spy_pnl,
        spy_pct=spy_pct,
        edge_pp=_pct_diff(pnl_pct, spy_pct),
        stop_price=holding.position.stop_price,
        stop_breached=holding.stop_breached(price),
        position=holding.position,
    )


def _bucket_line(bucket: str, lines: Sequence[Line]) -> BucketLine:
    invested = sum(line.invested for line in lines)
    pnl = sum(line.pnl for line in lines)
    spy_pnl = sum(line.spy_pnl for line in lines)
    pnl_pct = pnl / invested if invested > 0 else None
    spy_pct = spy_pnl / invested if invested > 0 else None
    return BucketLine(
        bucket=bucket,
        n_open=sum(1 for line in lines if line.status == "open"),
        n_closed=sum(1 for line in lines if line.status == "closed"),
        invested=invested,
        value=sum(line.market_value for line in lines),
        pnl=pnl,
        pnl_pct=pnl_pct,
        spy_pnl=spy_pnl,
        spy_pct=spy_pct,
        edge_pp=_pct_diff(pnl_pct, spy_pct),
    )


def _stats(closed: Sequence[Line]) -> Stats:
    pnls = [line.pnl for line in closed]
    spy_pnls = [line.spy_pnl for line in closed]
    beat = sum(1 for line in closed if line.beat_spy)
    return Stats(
        n_closed=len(closed),
        wins=sum(1 for p in pnls if p > 0),
        losses=sum(1 for p in pnls if p <= 0),
        win_rate=journal.win_rate(pnls),
        avg_win=journal.average_win(pnls),
        avg_loss=journal.average_loss(pnls),
        expectancy=journal.expectancy(pnls),
        spy_expectancy=journal.expectancy(spy_pnls),
        beat_spy=beat,
        beat_spy_rate=(beat / len(closed)) if closed else None,
    )


def build_report(
    book: str,
    deposits: Sequence[Deposit],
    holdings: Sequence[Holding],
    prices: Mapping[str, float],
    spy: pd.Series,
    as_of: date | None = None,
) -> TrackReport:
    """Compute the whole track record. Pure — no DB, no network.

    `prices` must cover every ticker with open shares; `spy` must reach back to
    the first cash flow.
    """
    as_of = as_of or journal.today()
    spy_now = float(spy.iloc[-1])
    spy_as_of = pd.Timestamp(spy.index[-1]).date()

    def mark(holding: Holding) -> float:
        """Price to value a holding at. A flat position needs one only for display."""
        if holding.shares > 0:
            return prices[holding.ticker]
        sells = [t.price for t in holding.trades if t.side == "sell"]
        return prices.get(holding.ticker, sells[-1] if sells else 0.0)

    lines = [_line(h, mark(h), spy, spy_now, as_of) for h in holdings if h.trades]
    open_lines = [line for line in lines if line.status == "open"]
    closed_lines = [line for line in lines if line.status == "closed"]

    all_trades = [t for h in holdings for t in h.trades]
    cash = journal.cash_balance(deposits, all_trades)
    unsettled = journal.unsettled_proceeds(all_trades, as_of)
    account_equity = journal.equity(deposits, holdings, prices)

    contributed = sum(d.amount for d in deposits if d.amount > 0)
    withdrawn = -sum(d.amount for d in deposits if d.amount < 0)
    net_deposits = contributed - withdrawn

    mirror = mirror_deposits(deposits, spy)
    pnl = account_equity - net_deposits
    pnl_pct = pnl / contributed if contributed > 0 else None
    spy_pnl = mirror.total_pnl(spy_now)
    spy_pct = mirror.return_pct(spy_now)

    account = Account(
        contributed=contributed,
        withdrawn=withdrawn,
        net_deposits=net_deposits,
        equity=account_equity,
        cash=cash,
        settled=cash - unsettled,
        unsettled=unsettled,
        pnl=pnl,
        pnl_pct=pnl_pct,
        spy_equity=mirror.value(spy_now),
        spy_pnl=spy_pnl,
        spy_pct=spy_pct,
        edge_pp=_pct_diff(pnl_pct, spy_pct),
    )

    buckets = [
        _bucket_line(bucket, [line for line in lines if line.bucket == bucket])
        for bucket in journal.BUCKETS
        if any(line.bucket == bucket for line in lines)
    ]

    return TrackReport(
        book=book,
        as_of=as_of,
        spy_as_of=spy_as_of,
        spy_price=spy_now,
        account=account,
        buckets=buckets,
        open_lines=open_lines,
        closed_lines=closed_lines,
        stats=_stats(closed_lines),
    )


def spy_history(start: date, end: date | None = None) -> pd.Series:
    """SPY daily closes from `start` (padded back so an as-of lookup always lands)."""
    pad = start - timedelta(days=10)
    return market.get_closes(BENCHMARK, start=pad.isoformat(), end=end.isoformat() if end else None)


def first_flow_date(deposits: Sequence[Deposit], holdings: Sequence[Holding]) -> date | None:
    """Earliest date any money moved — how far back the benchmark must reach."""
    dates = [d.deposit_date for d in deposits]
    dates += [t.trade_date for h in holdings for t in h.trades]
    return min(dates) if dates else None


# --------------------------------------------------------------------- render

def _zeroed(value: float, resolution: float) -> float:
    """Collapse float residue to a true zero so nothing renders as "-$0.00"."""
    return 0.0 if abs(value) < resolution / 2 else value


def money(value: float | None) -> str:
    if value is None:
        return "n/a"
    value = _zeroed(value, 0.01)
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def signed_money(value: float | None) -> str:
    if value is None:
        return "n/a"
    value = _zeroed(value, 0.01)
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{_zeroed(value * 100, 0.01):+.2f}%"


def points(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{_zeroed(value, 0.01):+.2f} pp"


def pct_plain(value: float | None) -> str:
    """Unsigned percent, for rates that cannot be negative (win rate)."""
    if value is None:
        return "n/a"
    return f"{_zeroed(value * 100, 0.1):.1f}%"


def _shares(value: float) -> str:
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def render_markdown(report: TrackReport) -> str:
    """The deliverable: a track record where every number sits beside SPY."""
    acct = report.account
    out: list[str] = []

    label = "PAPER — simulated money" if report.is_paper else "REAL money"
    out.append(f"# Track Record — {label}")
    out.append("")
    out.append(
        f"*As of {report.as_of} · benchmark {BENCHMARK} @ {money(report.spy_price)} "
        f"(close {report.spy_as_of}) · book `{report.book}`*"
    )
    out.append("")
    if report.is_paper:
        out.append(
            "> **Paper book.** No real capital. Same rules, same math, same benchmark "
            "as the real book — practice results, not a real track record."
        )
        out.append("")
    out.append(
        f"Benchmark method: every deposit buys {BENCHMARK} on the same date; every "
        "position is mirrored by the same dollars into "
        f"{BENCHMARK} on the same dates, exited in the same proportions. "
        "Beating cash is not the bar."
    )
    out.append("")

    # -- account
    out.append("## Account")
    out.append("")
    out.append(f"| | This book | {BENCHMARK} (same deposits, same dates) |")
    out.append("|---|---|---|")
    out.append(f"| Deposited | {money(acct.contributed)} | {money(acct.contributed)} |")
    if acct.withdrawn:
        out.append(f"| Withdrawn | {money(acct.withdrawn)} | {money(acct.withdrawn)} |")
    out.append(f"| Value now | {money(acct.equity)} | {money(acct.spy_equity)} |")
    out.append(f"| P&L | {signed_money(acct.pnl)} | {signed_money(acct.spy_pnl)} |")
    out.append(f"| Return | {pct(acct.pnl_pct)} | {pct(acct.spy_pct)} |")
    out.append(f"| **Edge vs {BENCHMARK}** | **{points(acct.edge_pp)}** | |")
    out.append("")
    out.append(
        f"Cash {money(acct.cash)} — settled {money(acct.settled)}, "
        f"unsettled {money(acct.unsettled)} (T+1)."
    )
    out.append("")

    # -- buckets
    if report.buckets:
        out.append("## By bucket")
        out.append("")
        out.append(
            f"| Bucket | Open | Closed | Invested | P&L | Return | "
            f"{BENCHMARK} P&L | {BENCHMARK} return | Edge |"
        )
        out.append("|---|---|---|---|---|---|---|---|---|")
        for b in report.buckets:
            out.append(
                f"| {b.bucket.title()} | {b.n_open} | {b.n_closed} | {money(b.invested)} "
                f"| {signed_money(b.pnl)} | {pct(b.pnl_pct)} | {signed_money(b.spy_pnl)} "
                f"| {pct(b.spy_pct)} | {points(b.edge_pp)} |"
            )
        out.append("")

    # -- open positions
    out.append("## Open positions")
    out.append("")
    if report.open_lines:
        out.append(
            f"| Ticker | Bucket | Shares | Avg cost | Last | Value | P&L | Return | "
            f"{BENCHMARK} same dates | Edge | Held | Stop |"
        )
        out.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for line in report.open_lines:
            stop = money(line.stop_price) if line.stop_price else "—"
            if line.stop_breached:
                stop += " **BREACHED**"
            out.append(
                f"| {line.ticker} | {line.bucket.title()} | {_shares(line.shares)} "
                f"| {money(line.avg_cost)} | {money(line.last_price)} "
                f"| {money(line.market_value)} | {signed_money(line.pnl)} "
                f"| {pct(line.pnl_pct)} | {pct(line.spy_pct)} | {points(line.edge_pp)} "
                f"| {line.held_days}d | {stop} |"
            )
    else:
        out.append("*No open positions.*")
    out.append("")

    # -- closed trades
    out.append("## Closed trades")
    out.append("")
    if report.closed_lines:
        out.append(
            f"| Ticker | Bucket | Entry | Exit | Held | Invested | P&L | Return "
            f"| {BENCHMARK} P&L | {BENCHMARK} return | Edge |"
        )
        out.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for line in report.closed_lines:
            out.append(
                f"| {line.ticker} | {line.bucket.title()} | {line.entry_date} "
                f"| {line.exit_date} | {line.held_days}d | {money(line.invested)} "
                f"| {signed_money(line.pnl)} | {pct(line.pnl_pct)} "
                f"| {signed_money(line.spy_pnl)} | {pct(line.spy_pct)} "
                f"| {points(line.edge_pp)} |"
            )
    else:
        out.append("*No closed trades yet.*")
    out.append("")

    # -- stats
    stats = report.stats
    if stats and stats.n_closed:
        out.append("## Closed-trade statistics")
        out.append("")
        out.append(f"| Metric | This book | {BENCHMARK} |")
        out.append("|---|---|---|")
        out.append(f"| Closed trades | {stats.n_closed} | {stats.n_closed} |")
        out.append(
            f"| Win rate | {pct_plain(stats.win_rate)} ({stats.wins}W / {stats.losses}L) "
            f"| {pct_plain(stats.beat_spy_rate)} of trades beat {BENCHMARK} "
            f"({stats.beat_spy}/{stats.n_closed}) |"
        )
        out.append(f"| Average win | {signed_money(stats.avg_win)} | — |")
        out.append(f"| Average loss | {signed_money(stats.avg_loss)} | — |")
        out.append(
            f"| Expectancy / trade | {signed_money(stats.expectancy)} "
            f"| {signed_money(stats.spy_expectancy)} |"
        )
        out.append("")
        if stats.n_closed < 20:
            out.append(
                f"> {stats.n_closed} closed trade(s) is too small a sample to separate "
                "skill from luck. The number that matters at this stage is whether every "
                "position had a written thesis and an honest outcome."
            )
            out.append("")

    # -- the theses, timestamped
    out.append("## Trade log — theses as written, at the time")
    out.append("")
    if not report.all_lines:
        out.append("*Nothing logged yet.*")
        out.append("")
    for line in report.closed_lines + report.open_lines:
        pos = line.position
        state = (
            f"closed {line.exit_date}" if line.status == "closed" else f"open, {line.held_days}d"
        )
        out.append(
            f"### {line.ticker} — {line.bucket.title()} — {state} "
            f"— {signed_money(line.pnl)} ({pct(line.pnl_pct)} vs {BENCHMARK} {pct(line.spy_pct)})"
        )
        out.append("")
        out.append(f"- **Opened (UTC):** {pos.opened_at} · entry {line.entry_date}")
        if pos.closed_at:
            out.append(f"- **Closed (UTC):** {pos.closed_at}")
        out.append(f"- **Thesis:** {pos.thesis}")
        out.append(f"- **Invalidation:** {pos.invalidation}")
        out.append(f"- **Horizon:** {pos.horizon}")
        out.append(f"- **Exit plan:** {pos.exit_plan}")
        if pos.stop_price:
            out.append(f"- **Stop:** {money(pos.stop_price)}")
        if pos.outcome:
            out.append(f"- **What actually happened:** {pos.outcome}")
        out.append("")

    return "\n".join(out).rstrip() + "\n"
