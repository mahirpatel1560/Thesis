"""Positions, theses, and the 7 account rules â€” Week 2.

Every rule in CLAUDE.md is enforced here in code, with a test proving it.

Two layers, deliberately separated:

* **Pure math** â€” no DB, no network, no clock. Every dollar in the track record
  flows through these functions, so they can be tested exhaustively. A P&L bug
  invalidates the entire track record, so the money math never touches I/O.
* **Ledger** â€” SQLite. `trades` is the cash-flow truth (every buy and sell);
  `positions` holds the timestamped thesis and the plan. Share counts and cost
  basis are *derived* from trades rather than stored, so there is exactly one
  source of truth for money.

Two books share one implementation: ``real`` and ``paper``. A paper position is
fake money and real discipline â€” identical rules, identical math, identical SPY
benchmark. The books never mix: caps, cash, the review gate, and P&L are all
computed per book.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from thesis import config

# ------------------------------------------------------------------ constants

REAL = "real"
PAPER = "paper"
BOOKS = (REAL, PAPER)

CORE = "core"
ACTIVE = "active"
BUCKETS = (CORE, ACTIVE)

#: Rule 3 â€” max share of account equity a single name may occupy, per bucket.
BUCKET_CAPS = {CORE: 0.20, ACTIVE: 0.10}

#: Rule 6 â€” `log buy` is blocked once the last review is older than this.
REVIEW_MAX_AGE_DAYS = 9

#: How many days before that deadline the CLI starts warning. A rule you only
#: hear about at the moment it blocks you is a trap, not a discipline.
REVIEW_WARN_WITHIN_DAYS = 2

#: Rule 5 â€” cash account settlement. Sale proceeds are usable T+1.
SETTLEMENT_BUSINESS_DAYS = 1

#: Rule 1 â€” a thesis is prose, not a shrug. Enforced so "good co" can't pass.
MIN_THESIS_CHARS = 40
MIN_PLAN_CHARS = 10

#: Rule 4 â€” a plain listed equity symbol. Option (OCC) symbols carry digits.
TICKER_RE = re.compile(r"^[A-Z]{1,5}([.-][A-Z])?$")

#: Fractional shares mean float share counts; below this a position is closed.
SHARE_EPS = 1e-9
#: Cash comparisons tolerate sub-cent float residue.
CASH_EPS = 1e-6


def book_name(paper: bool) -> str:
    """`--paper` -> which book. The only place the flag becomes a book."""
    return PAPER if paper else REAL


#: A book name is a partition key, not a rule. `BOOKS` are the two human books;
#: the arena adds one book per agent persona in its own database file. Validating
#: the *shape* here rather than membership lets the arena reuse this module
#: verbatim â€” which is the point: agent trades must go through the same code.
BOOK_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def validate_book(book: str) -> str:
    """Check a book name is a well-formed slug. Raises ValueError otherwise."""
    if not isinstance(book, str) or not BOOK_RE.match(book):
        raise ValueError(
            f"invalid book name {book!r} â€” expected a lowercase slug such as "
            f"{REAL!r}, {PAPER!r}, or an arena persona"
        )
    return book


# ----------------------------------------------------------------- exceptions

class RuleViolation(ValueError):
    """A journal rule refused the operation.

    `rule` is the CLAUDE.md rule number, or None for journal discipline that
    isn't one of the seven numbered rules (e.g. a sell with no outcome note).
    """

    def __init__(self, rule: int | None, message: str) -> None:
        prefix = f"rule {rule}: " if rule else ""
        super().__init__(prefix + message)
        self.rule = rule
        self.message = message


#: How a refusal is announced, everywhere. The CLI and the Discord bot both
#: render through this, so a rule that refuses a trade says the same words no
#: matter which surface the trade came from — asserted by a test.
REFUSAL_PREFIX = "REFUSED"


def refusal_text(violation: RuleViolation) -> str:
    """The canonical one-line rendering of a rule refusal."""
    return f"{REFUSAL_PREFIX} — {violation}"


# ---------------------------------------------------------------- date helpers

def today() -> date:
    return date.today()


def now_stamp() -> str:
    """UTC timestamp for the journal â€” this is what makes a thesis timestamped."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def next_business_day(from_date: date, days: int = 1) -> date:
    """Skip weekends. Market holidays are not modeled â€” verify with your broker."""
    out = from_date
    while days > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            days -= 1
    return out


def settlement_date(trade_date: date) -> date:
    """When a sale's proceeds become usable cash (T+1)."""
    return next_business_day(trade_date, SETTLEMENT_BUSINESS_DAYS)


# ----------------------------------------------------------------- data models

@dataclass(frozen=True)
class Deposit:
    id: int | None
    book: str
    deposit_date: date
    amount: float  # negative = withdrawal
    note: str = ""


@dataclass(frozen=True)
class Trade:
    id: int | None
    book: str
    position_id: int | None
    ticker: str
    side: str  # "buy" | "sell"
    shares: float
    price: float
    trade_date: date
    settles_on: date
    logged_at: str = ""

    @property
    def dollars(self) -> float:
        return self.shares * self.price


@dataclass(frozen=True)
class Position:
    """The thesis record. Share counts and cost live in `trades`."""

    id: int | None
    book: str
    ticker: str
    bucket: str
    entry_date: date
    opened_at: str
    thesis: str
    invalidation: str
    horizon: str
    exit_plan: str
    stop_price: float | None = None
    status: str = "open"
    closed_at: str | None = None
    outcome: str | None = None


@dataclass(frozen=True)
class Review:
    id: int | None
    book: str
    review_date: date
    reviewed_at: str
    note: str = ""


@dataclass
class Holding:
    """A position joined to its trades, with every number derived.

    Realized P&L uses average-cost accounting, walked chronologically, so a
    partial sell and a later add-on both land on the right basis.
    """

    position: Position
    trades: tuple[Trade, ...]

    shares: float = field(init=False, default=0.0)
    avg_cost: float = field(init=False, default=0.0)
    realized_pnl: float = field(init=False, default=0.0)
    invested: float = field(init=False, default=0.0)
    proceeds: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.trades = tuple(sorted(self.trades, key=lambda t: (t.trade_date, t.id or 0)))
        shares = cost = realized = invested = proceeds = 0.0
        for trade in self.trades:
            if trade.side == "buy":
                shares += trade.shares
                cost += trade.dollars
                invested += trade.dollars
            else:
                avg = cost / shares if shares > SHARE_EPS else 0.0
                realized += trade.shares * (trade.price - avg)
                cost -= trade.shares * avg
                shares -= trade.shares
                proceeds += trade.dollars
        if abs(shares) < SHARE_EPS:
            shares, cost = 0.0, 0.0
        self.shares = shares
        self.avg_cost = cost / shares if shares > 0 else 0.0
        self.realized_pnl = realized
        self.invested = invested
        self.proceeds = proceeds

    # -- passthroughs so callers can treat a Holding as the position it is
    @property
    def ticker(self) -> str:
        return self.position.ticker

    @property
    def bucket(self) -> str:
        return self.position.bucket

    @property
    def book(self) -> str:
        return self.position.book

    @property
    def is_open(self) -> bool:
        return self.position.status == "open"

    @property
    def cost_basis(self) -> float:
        """Cost of the shares still held."""
        return self.shares * self.avg_cost

    @property
    def entry_date(self) -> date:
        return self.trades[0].trade_date if self.trades else self.position.entry_date

    @property
    def exit_date(self) -> date | None:
        sells = [t.trade_date for t in self.trades if t.side == "sell"]
        return max(sells) if sells and not self.is_open else None

    def market_value(self, price: float) -> float:
        return self.shares * price

    def unrealized_pnl(self, price: float) -> float:
        return self.shares * (price - self.avg_cost)

    def total_pnl(self, price: float) -> float:
        """Realized plus unrealized â€” the full P&L of this thesis so far."""
        return self.realized_pnl + self.unrealized_pnl(price)

    def return_pct(self, price: float) -> float | None:
        """P&L over dollars invested. None if nothing was ever invested."""
        if self.invested <= 0:
            return None
        return self.total_pnl(price) / self.invested

    def held_days(self, as_of: date | None = None) -> int:
        end = self.exit_date or as_of or today()
        return (end - self.entry_date).days

    def stop_breached(self, price: float) -> bool:
        stop = self.position.stop_price
        return stop is not None and self.shares > 0 and price <= stop


# ------------------------------------------------------------- pure money math

def cost_basis(shares: float, price: float) -> float:
    return shares * price


def market_value(shares: float, price: float) -> float:
    return shares * price


def realized_pnl(shares: float, entry_price: float, exit_price: float) -> float:
    return shares * (exit_price - entry_price)


def return_pct(entry_price: float, exit_price: float) -> float:
    """Per-share return. Entry price of zero is not a trade."""
    if entry_price <= 0:
        raise ValueError("entry price must be positive")
    return exit_price / entry_price - 1.0


def cash_balance(deposits: Iterable[Deposit], trades: Iterable[Trade]) -> float:
    """Book cash: deposits, less what buys took, plus what sells returned.

    Trade-date accounting â€” this is the balance, not the *settled* balance.
    """
    cash = sum(d.amount for d in deposits)
    for trade in trades:
        cash += -trade.dollars if trade.side == "buy" else trade.dollars
    return cash


def unsettled_proceeds(trades: Iterable[Trade], as_of: date) -> float:
    """Sale proceeds not yet settled as of a date (rule 5, T+1)."""
    return sum(t.dollars for t in trades if t.side == "sell" and t.settles_on > as_of)


def settled_cash(
    deposits: Iterable[Deposit], trades: Iterable[Trade], as_of: date
) -> float:
    """Cash a cash account may spend today without a good-faith violation."""
    trades = list(trades)
    return cash_balance(deposits, trades) - unsettled_proceeds(trades, as_of)


def equity(
    deposits: Iterable[Deposit],
    holdings: Iterable[Holding],
    prices: Mapping[str, float],
) -> float:
    """Account equity: cash plus open positions marked to market.

    Raises if a held ticker has no price â€” a track record that silently marks a
    position at cost is worse than one that refuses to compute.
    """
    holdings = list(holdings)
    all_trades = [t for h in holdings for t in h.trades]
    total = cash_balance(deposits, all_trades)
    for holding in holdings:
        if holding.shares <= 0:
            continue
        price = prices.get(holding.ticker)
        if price is None:
            raise ValueError(f"no current price for {holding.ticker} â€” cannot value the book")
        total += holding.market_value(price)
    return total


def exposure(
    holdings: Iterable[Holding], prices: Mapping[str, float], ticker: str
) -> float:
    """Current market value of everything held in one name."""
    total = 0.0
    for holding in holdings:
        if holding.ticker != ticker or holding.shares <= 0:
            continue
        price = prices.get(ticker)
        if price is None:
            raise ValueError(f"no current price for {ticker} â€” cannot size the position")
        total += holding.market_value(price)
    return total


def cap_for(bucket: str) -> float:
    try:
        return BUCKET_CAPS[bucket]
    except KeyError:
        raise ValueError(f"unknown bucket {bucket!r} â€” expected one of {BUCKETS}") from None


def win_rate(pnls: Sequence[float]) -> float | None:
    """Share of closed trades that made money. None with no closed trades."""
    if not pnls:
        return None
    return sum(1 for p in pnls if p > 0) / len(pnls)


def expectancy(pnls: Sequence[float]) -> float | None:
    """Average dollars per closed trade â€” the number that compounds."""
    if not pnls:
        return None
    return sum(pnls) / len(pnls)


def average_win(pnls: Sequence[float]) -> float | None:
    wins = [p for p in pnls if p > 0]
    return sum(wins) / len(wins) if wins else None


def average_loss(pnls: Sequence[float]) -> float | None:
    losses = [p for p in pnls if p <= 0]
    return sum(losses) / len(losses) if losses else None


# ---------------------------------------------------------------- buy requests

@dataclass
class BuyRequest:
    book: str
    ticker: str
    bucket: str
    shares: float
    price: float
    thesis: str
    invalidation: str
    horizon: str
    exit_plan: str
    stop_price: float | None = None
    trade_date: date = field(default_factory=today)

    def __post_init__(self) -> None:
        self.book = self.book.strip().lower()
        self.ticker = self.ticker.strip().upper()
        self.bucket = self.bucket.strip().lower()
        self.thesis = self.thesis.strip()
        self.invalidation = self.invalidation.strip()
        self.horizon = self.horizon.strip()
        self.exit_plan = self.exit_plan.strip()
        self.trade_date = parse_date(self.trade_date)

    @property
    def cost(self) -> float:
        return self.shares * self.price


@dataclass
class BookState:
    """Everything the rules need to judge a trade, for one book."""

    book: str
    deposits: tuple[Deposit, ...]
    holdings: tuple[Holding, ...]  # open *and* closed â€” cash needs every trade
    last_review: date | None = None

    @property
    def open_holdings(self) -> tuple[Holding, ...]:
        return tuple(h for h in self.holdings if h.is_open and h.shares > 0)

    @property
    def all_trades(self) -> tuple[Trade, ...]:
        return tuple(t for h in self.holdings for t in h.trades)


@dataclass
class BuyCheck:
    """A buy that passed every rule. `warnings` are advisory (rule 5)."""

    request: BuyRequest
    cost: float
    account_equity: float
    position_pct: float
    cash: float
    settled: float
    warnings: tuple[str, ...] = ()


# -------------------------------------------------------------- the seven rules

def check_instrument(req: BuyRequest) -> None:
    """Rule 4 â€” shares only. No options, no shorting. Hard-coded."""
    validate_book(req.book)
    if req.bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {req.bucket!r} â€” expected one of {BUCKETS}")
    if not TICKER_RE.match(req.ticker):
        raise RuleViolation(
            4,
            f"{req.ticker!r} is not a plain equity symbol. Shares only in v1 â€” "
            "no options, no futures, no derivatives.",
        )
    if req.shares <= 0:
        raise RuleViolation(4, "shares must be positive â€” no shorting in v1")
    if req.price <= 0:
        raise RuleViolation(4, "entry price must be positive")


def check_written_plan(req: BuyRequest) -> None:
    """Rule 1 â€” no position without a thesis, invalidation, horizon, exit plan."""
    if len(req.thesis) < MIN_THESIS_CHARS:
        raise RuleViolation(
            1,
            f"thesis must be a written argument of at least {MIN_THESIS_CHARS} "
            f"characters (got {len(req.thesis)}). Why does this make money?",
        )
    fields = (
        ("invalidation trigger", req.invalidation),
        ("time horizon", req.horizon),
        ("exit plan", req.exit_plan),
    )
    for label, value in fields:
        if len(value) < MIN_PLAN_CHARS:
            raise RuleViolation(
                1,
                f"{label} is required (at least {MIN_PLAN_CHARS} characters) "
                "before a position can exist",
            )


def check_stop(req: BuyRequest) -> None:
    """Rule 2 â€” Active trades log a hard stop at entry."""
    if req.bucket != ACTIVE:
        return
    if req.stop_price is None:
        raise RuleViolation(2, "Active-bucket trades require a stop level at entry")
    if req.stop_price <= 0:
        raise RuleViolation(2, "stop level must be positive")
    if req.stop_price >= req.price:
        raise RuleViolation(
            2,
            f"stop ${req.stop_price:,.2f} is not below the entry price "
            f"${req.price:,.2f} â€” that stop would fire immediately",
        )


def check_position_cap(
    bucket: str, existing_value: float, new_cost: float, account_equity: float
) -> float:
    """Rule 3 â€” <=20% per Core name, <=10% per Active name. Returns resulting %.

    A buy converts cash into stock at the same value, so account equity is
    unchanged by the trade; the resulting weight is measured against it.
    """
    cap = cap_for(bucket)
    if account_equity <= 0:
        raise RuleViolation(
            3,
            "cannot size a position against zero account equity â€” "
            "record a deposit first (`thesis deposit AMOUNT`)",
        )
    pct = (existing_value + new_cost) / account_equity
    if pct > cap + CASH_EPS:
        raise RuleViolation(
            3,
            f"{bucket} names cap at {cap:.0%} of the account; this buy would make it "
            f"{pct:.1%} (${existing_value + new_cost:,.2f} of ${account_equity:,.2f}). "
            "Size down.",
        )
    return pct


def check_no_margin(cost: float, cash: float) -> None:
    """Rule 4 â€” no margin. You cannot spend cash the book does not have."""
    if cost > cash + CASH_EPS:
        raise RuleViolation(
            4,
            f"this buy costs ${cost:,.2f} but the book holds ${cash:,.2f} cash. "
            "No margin in v1.",
        )


def unsettled_warning(
    cost: float, settled: float, trades: Sequence[Trade], as_of: date
) -> str | None:
    """Rule 5 â€” warn (don't block) when a buy reaches into unsettled proceeds."""
    if cost <= settled + CASH_EPS:
        return None
    shortfall = cost - settled
    pending = sorted(
        (t for t in trades if t.side == "sell" and t.settles_on > as_of),
        key=lambda t: t.settles_on,
    )
    detail = ", ".join(
        f"${t.dollars:,.2f} from {t.ticker} sold {t.trade_date} (settles {t.settles_on})"
        for t in pending
    )
    return (
        f"cash-account warning: ${shortfall:,.2f} of this buy uses unsettled funds "
        f"[{detail}]. Selling these shares before settlement is a good-faith "
        "violation. Market holidays are not modeled â€” verify with your broker."
    )


@dataclass(frozen=True)
class ReviewStatus:
    """Where a book stands against rule 6, for both the gate and the banner.

    One computation feeds both, so a warning can never disagree with the block
    it is warning about.
    """

    book: str
    open_positions: int
    last_review: date | None
    days_since: int | None
    #: Days left before rule 6 blocks buying. Negative once it already has.
    days_remaining: int | None
    state: str  # "ok" | "due_soon" | "overdue" | "never"

    @property
    def blocking(self) -> bool:
        return self.state in ("overdue", "never")

    @property
    def needs_notice(self) -> bool:
        return self.state != "ok"


def review_status(
    open_positions: int,
    last_review: date | None,
    as_of: date,
    book: str = REAL,
) -> ReviewStatus:
    """Rule 6, as a status rather than an exception. Pure."""
    days_since = (as_of - last_review).days if last_review else None
    remaining = REVIEW_MAX_AGE_DAYS - days_since if days_since is not None else None

    if open_positions == 0:
        state = "ok"  # nothing is held, so there is nothing to review yet
    elif last_review is None:
        state = "never"
    elif days_since > REVIEW_MAX_AGE_DAYS:
        state = "overdue"
    elif remaining <= REVIEW_WARN_WITHIN_DAYS:
        state = "due_soon"
    else:
        state = "ok"

    return ReviewStatus(
        book=book,
        open_positions=open_positions,
        last_review=last_review,
        days_since=days_since,
        days_remaining=remaining,
        state=state,
    )


def check_review_gate(
    open_count: int, last_review: date | None, as_of: date
) -> None:
    """Rule 6 â€” a stale review blocks new buys."""
    status = review_status(open_count, last_review, as_of)
    if status.state == "never":
        raise RuleViolation(
            6,
            f"you hold {open_count} open position(s) and have never run "
            "`thesis review` â€” review before buying again",
        )
    if status.state == "overdue":
        raise RuleViolation(
            6,
            f"last review was {status.days_since} days ago "
            f"(max {REVIEW_MAX_AGE_DAYS}) â€” run `thesis review` before buying again",
        )


def book_review_status(
    conn: sqlite3.Connection, book: str, as_of: date | None = None
) -> ReviewStatus:
    """Rule 6 status for one book, read from the journal."""
    review = last_review(conn, book)
    return review_status(
        open_positions=len(open_holdings(conn, book)),
        last_review=review.review_date if review else None,
        as_of=as_of or today(),
        book=book,
    )


def validate_buy(
    req: BuyRequest,
    state: BookState,
    prices: Mapping[str, float],
    as_of: date | None = None,
    reserved_cash: float = 0.0,
    reserved_exposure: Mapping[str, float] | None = None,
) -> BuyCheck:
    """Run every rule against a proposed buy. Raises RuleViolation on the first fail.

    Checked in this order: instrument sanity (4), written plan (1), stop (2),
    review freshness (6), margin (4), position cap (3), then the unsettled-funds
    warning (5). `prices` must cover every open holding's ticker.

    Rules are evaluated as of the trade date unless `as_of` says otherwise.

    `reserved_cash` and `reserved_exposure` account for capital already committed
    to orders that have been placed but have not executed. A human logging a fill
    by hand has none — the trade is already done by the time it is entered — so
    both default to nothing and the human path is unchanged. The arena does have
    them: an order decided last week and not yet filled is real exposure, and a
    cap that ignores it would let an agent commit to the same name twice and only
    discover the breach after both fills landed.
    """
    as_of = as_of or req.trade_date
    reserved_exposure = reserved_exposure or {}
    check_instrument(req)
    check_written_plan(req)
    check_stop(req)
    check_review_gate(len(state.open_holdings), state.last_review, as_of)

    trades = list(state.all_trades)
    cash = cash_balance(state.deposits, trades) - reserved_cash
    check_no_margin(req.cost, cash)

    # The new lot is valued at its entry price; existing shares at market.
    marks = {**prices, req.ticker: prices.get(req.ticker, req.price)}
    account_equity = equity(state.deposits, state.holdings, marks)
    existing = exposure(state.open_holdings, marks, req.ticker) + float(
        reserved_exposure.get(req.ticker, 0.0)
    )
    pct = check_position_cap(req.bucket, existing, req.cost, account_equity)

    settled = cash - unsettled_proceeds(trades, as_of)
    warning = unsettled_warning(req.cost, settled, trades, as_of)

    return BuyCheck(
        request=req,
        cost=req.cost,
        account_equity=account_equity,
        position_pct=pct,
        cash=cash,
        settled=settled,
        warnings=(warning,) if warning else (),
    )


def validate_sell(holding: Holding, shares: float, price: float, outcome: str) -> None:
    """Rule 4 (no shorting) plus the journal's outcome requirement."""
    if shares <= 0:
        raise RuleViolation(4, "shares sold must be positive")
    if price <= 0:
        raise RuleViolation(4, "exit price must be positive")
    if shares > holding.shares + SHARE_EPS:
        raise RuleViolation(
            4,
            f"you hold {holding.shares:g} shares of {holding.ticker}, cannot sell "
            f"{shares:g} â€” no shorting in v1",
        )
    if len(outcome.strip()) < MIN_PLAN_CHARS:
        raise RuleViolation(
            None,
            "a close needs one line on what actually happened vs. the thesis "
            f"(at least {MIN_PLAN_CHARS} characters)",
        )


# --------------------------------------------------------------- ledger (SQLite)

SCHEMA = """
CREATE TABLE IF NOT EXISTS deposits (
    id           INTEGER PRIMARY KEY,
    book         TEXT NOT NULL CHECK (book <> ''),
    deposit_date TEXT NOT NULL,
    amount       REAL NOT NULL,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS positions (
    id           INTEGER PRIMARY KEY,
    book         TEXT NOT NULL CHECK (book <> ''),
    ticker       TEXT NOT NULL,
    bucket       TEXT NOT NULL CHECK (bucket IN ('core','active')),
    entry_date   TEXT NOT NULL,
    opened_at    TEXT NOT NULL,
    thesis       TEXT NOT NULL,
    invalidation TEXT NOT NULL,
    horizon      TEXT NOT NULL,
    exit_plan    TEXT NOT NULL,
    stop_price   REAL,
    status       TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    closed_at    TEXT,
    outcome      TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY,
    book        TEXT NOT NULL CHECK (book <> ''),
    position_id INTEGER NOT NULL REFERENCES positions(id),
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('buy','sell')),
    shares      REAL NOT NULL CHECK (shares > 0),
    price       REAL NOT NULL CHECK (price > 0),
    trade_date  TEXT NOT NULL,
    settles_on  TEXT NOT NULL,
    logged_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY,
    book        TEXT NOT NULL CHECK (book <> ''),
    review_date TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id           INTEGER PRIMARY KEY,
    book         TEXT NOT NULL CHECK (book <> ''),
    snapshot_date TEXT NOT NULL,
    equity       REAL NOT NULL,
    spy_equity   REAL,
    UNIQUE (book, snapshot_date)
);

CREATE INDEX IF NOT EXISTS idx_positions_book ON positions (book, status);
CREATE INDEX IF NOT EXISTS idx_trades_position ON trades (position_id);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the journal database with the schema applied."""
    db = Path(path) if path else config.db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# -- deposits

def add_deposit(
    conn: sqlite3.Connection,
    book: str,
    amount: float,
    on_date: date | None = None,
    note: str = "",
) -> Deposit:
    """Record a deposit (or a withdrawal, as a negative amount)."""
    validate_book(book)
    if amount == 0:
        raise ValueError("deposit amount cannot be zero")
    on_date = parse_date(on_date) if on_date else today()
    cur = conn.execute(
        "INSERT INTO deposits (book, deposit_date, amount, note) VALUES (?,?,?,?)",
        (book, on_date.isoformat(), float(amount), note),
    )
    conn.commit()
    return Deposit(cur.lastrowid, book, on_date, float(amount), note)


def deposits(conn: sqlite3.Connection, book: str) -> tuple[Deposit, ...]:
    rows = conn.execute(
        "SELECT * FROM deposits WHERE book = ? ORDER BY deposit_date, id", (book,)
    ).fetchall()
    return tuple(
        Deposit(r["id"], r["book"], parse_date(r["deposit_date"]), r["amount"], r["note"])
        for r in rows
    )


# -- positions & trades

def _position(row: sqlite3.Row) -> Position:
    return Position(
        id=row["id"],
        book=row["book"],
        ticker=row["ticker"],
        bucket=row["bucket"],
        entry_date=parse_date(row["entry_date"]),
        opened_at=row["opened_at"],
        thesis=row["thesis"],
        invalidation=row["invalidation"],
        horizon=row["horizon"],
        exit_plan=row["exit_plan"],
        stop_price=row["stop_price"],
        status=row["status"],
        closed_at=row["closed_at"],
        outcome=row["outcome"],
    )


def _trade(row: sqlite3.Row) -> Trade:
    return Trade(
        id=row["id"],
        book=row["book"],
        position_id=row["position_id"],
        ticker=row["ticker"],
        side=row["side"],
        shares=row["shares"],
        price=row["price"],
        trade_date=parse_date(row["trade_date"]),
        settles_on=parse_date(row["settles_on"]),
        logged_at=row["logged_at"],
    )


def holdings(
    conn: sqlite3.Connection, book: str, status: str | None = None
) -> tuple[Holding, ...]:
    """Every position in a book, joined to its trades. `status` filters open/closed."""
    sql = "SELECT * FROM positions WHERE book = ?"
    params: list[object] = [book]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY entry_date, id"
    positions = [_position(r) for r in conn.execute(sql, params).fetchall()]

    by_position: dict[int, list[Trade]] = {}
    for row in conn.execute(
        "SELECT t.* FROM trades t JOIN positions p ON p.id = t.position_id "
        "WHERE t.book = ? ORDER BY t.trade_date, t.id",
        (book,),
    ).fetchall():
        by_position.setdefault(row["position_id"], []).append(_trade(row))

    return tuple(
        Holding(position=p, trades=tuple(by_position.get(p.id or -1, ()))) for p in positions
    )


def open_holdings(conn: sqlite3.Connection, book: str) -> tuple[Holding, ...]:
    return tuple(h for h in holdings(conn, book, status="open") if h.shares > 0)


def closed_holdings(conn: sqlite3.Connection, book: str) -> tuple[Holding, ...]:
    return holdings(conn, book, status="closed")


def find_open(conn: sqlite3.Connection, book: str, ticker: str) -> Holding | None:
    """The open lot for a ticker, if any. One open lot per name per book."""
    ticker = ticker.upper()
    for holding in open_holdings(conn, book):
        if holding.ticker == ticker:
            return holding
    return None


def load_state(conn: sqlite3.Connection, book: str) -> BookState:
    """Assemble everything the rules need for one book."""
    review = last_review(conn, book)
    return BookState(
        book=book,
        deposits=deposits(conn, book),
        holdings=holdings(conn, book),
        last_review=review.review_date if review else None,
    )


def _record_trade(
    conn: sqlite3.Connection,
    book: str,
    position_id: int,
    ticker: str,
    side: str,
    shares: float,
    price: float,
    trade_date: date,
) -> Trade:
    settles = settlement_date(trade_date) if side == "sell" else trade_date
    stamp = now_stamp()
    cur = conn.execute(
        "INSERT INTO trades (book, position_id, ticker, side, shares, price, "
        "trade_date, settles_on, logged_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            book,
            position_id,
            ticker,
            side,
            float(shares),
            float(price),
            trade_date.isoformat(),
            settles.isoformat(),
            stamp,
        ),
    )
    return Trade(
        cur.lastrowid, book, position_id, ticker, side, shares, price, trade_date, settles, stamp
    )


def log_buy(conn: sqlite3.Connection, check: BuyCheck) -> Holding:
    """Write a validated buy. Adding to a held name extends that thesis.

    A second buy in the same name does not open a second lot: shares and cost
    basis combine (average cost), the add-on is appended to the thesis with its
    own date, and the freshly stated invalidation/horizon/exit plan/stop replace
    the old ones â€” the current plan is the one that governs.
    """
    req = check.request
    existing = find_open(conn, req.book, req.ticker)
    if existing is None:
        stamp = now_stamp()
        cur = conn.execute(
            "INSERT INTO positions (book, ticker, bucket, entry_date, opened_at, thesis, "
            "invalidation, horizon, exit_plan, stop_price, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?, 'open')",
            (
                req.book,
                req.ticker,
                req.bucket,
                req.trade_date.isoformat(),
                stamp,
                req.thesis,
                req.invalidation,
                req.horizon,
                req.exit_plan,
                req.stop_price,
            ),
        )
        position_id = int(cur.lastrowid)
    else:
        position_id = int(existing.position.id or 0)
        if existing.bucket != req.bucket:
            raise RuleViolation(
                None,
                f"{req.ticker} is already open in the {existing.bucket} bucket â€” "
                "close it before re-opening it in another bucket",
            )
        thesis = (
            f"{existing.position.thesis}\n\n"
            f"[added {req.trade_date} â€” {req.shares:g} sh @ ${req.price:,.2f}] {req.thesis}"
        )
        conn.execute(
            "UPDATE positions SET thesis = ?, invalidation = ?, horizon = ?, "
            "exit_plan = ?, stop_price = ? WHERE id = ?",
            (
                thesis,
                req.invalidation,
                req.horizon,
                req.exit_plan,
                req.stop_price,
                position_id,
            ),
        )

    _record_trade(
        conn, req.book, position_id, req.ticker, "buy", req.shares, req.price, req.trade_date
    )
    conn.commit()
    return get_holding(conn, position_id)


def log_sell(
    conn: sqlite3.Connection,
    holding: Holding,
    shares: float,
    price: float,
    outcome: str,
    trade_date: date | None = None,
) -> Holding:
    """Close (or partially close) a position, recording thesis-vs-reality.

    A partial sell leaves the position open; the outcome note is kept until the
    final share is gone, when the position is stamped closed.
    """
    trade_date = parse_date(trade_date) if trade_date else today()
    validate_sell(holding, shares, price, outcome)
    position_id = int(holding.position.id or 0)
    _record_trade(
        conn, holding.book, position_id, holding.ticker, "sell", shares, price, trade_date
    )

    remaining = holding.shares - shares
    if remaining <= SHARE_EPS:
        conn.execute(
            "UPDATE positions SET status = 'closed', closed_at = ?, outcome = ? WHERE id = ?",
            (now_stamp(), outcome.strip(), position_id),
        )
    else:
        note = (
            f"[partial {trade_date} â€” sold {shares:g} sh @ ${price:,.2f}] {outcome.strip()}"
        )
        prior = holding.position.outcome
        conn.execute(
            "UPDATE positions SET outcome = ? WHERE id = ?",
            (f"{prior}\n{note}" if prior else note, position_id),
        )
    conn.commit()
    return get_holding(conn, position_id)


def get_holding(conn: sqlite3.Connection, position_id: int) -> Holding:
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    if row is None:
        raise LookupError(f"no position with id {position_id}")
    trades = tuple(
        _trade(r)
        for r in conn.execute(
            "SELECT * FROM trades WHERE position_id = ? ORDER BY trade_date, id",
            (position_id,),
        ).fetchall()
    )
    return Holding(position=_position(row), trades=trades)


# -- reviews (rule 6)

def record_review(
    conn: sqlite3.Connection, book: str, note: str = "", on_date: date | None = None
) -> Review:
    validate_book(book)
    on_date = parse_date(on_date) if on_date else today()
    stamp = now_stamp()
    cur = conn.execute(
        "INSERT INTO reviews (book, review_date, reviewed_at, note) VALUES (?,?,?,?)",
        (book, on_date.isoformat(), stamp, note),
    )
    conn.commit()
    return Review(cur.lastrowid, book, on_date, stamp, note)


def last_review(conn: sqlite3.Connection, book: str) -> Review | None:
    row = conn.execute(
        "SELECT * FROM reviews WHERE book = ? ORDER BY review_date DESC, id DESC LIMIT 1",
        (book,),
    ).fetchone()
    if row is None:
        return None
    return Review(
        row["id"], row["book"], parse_date(row["review_date"]), row["reviewed_at"], row["note"]
    )


def days_since_review(
    conn: sqlite3.Connection, book: str, as_of: date | None = None
) -> int | None:
    review = last_review(conn, book)
    if review is None:
        return None
    return ((as_of or today()) - review.review_date).days


def review_is_stale(
    conn: sqlite3.Connection, book: str, as_of: date | None = None
) -> bool:
    """True when rule 6 would block a buy (and a review is actually owed)."""
    if not open_holdings(conn, book):
        return False
    age = days_since_review(conn, book, as_of)
    return age is None or age > REVIEW_MAX_AGE_DAYS


# -- equity snapshots

def snapshot_equity(
    conn: sqlite3.Connection,
    book: str,
    value: float,
    spy_value: float | None = None,
    on_date: date | None = None,
) -> None:
    """Persist the day's equity next to its SPY benchmark (rule 7)."""
    on_date = parse_date(on_date) if on_date else today()
    conn.execute(
        "INSERT INTO equity_snapshots (book, snapshot_date, equity, spy_equity) "
        "VALUES (?,?,?,?) ON CONFLICT (book, snapshot_date) "
        "DO UPDATE SET equity = excluded.equity, spy_equity = excluded.spy_equity",
        (book, on_date.isoformat(), float(value), spy_value),
    )
    conn.commit()
