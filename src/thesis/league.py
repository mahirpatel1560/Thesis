"""The Discord league — a hosted Arena where each member gets an agent-run book.

This is the domain layer. It knows nothing about Discord: it takes guild and
member ids as opaque integers and returns plain data. `bot.py` is the only module
that touches the Discord API, and it holds no trade logic of its own.

**Nothing here implements a trading rule.** A league order is turned into a
`journal.BuyRequest` and pushed through `arena.apply_decision`, which calls
`journal.validate_buy` — the same function the CLI's `thesis log buy` uses. There
is no league-specific cap, no league-specific cash check, and no path that writes
a position without passing that gate. A test asserts the bot cannot construct a
trade the CLI would refuse.

Isolation is physical: league books live in `.cache/league.db`, a different file
from both `journal.db` and `arena.db`.

Books are keyed per guild, so two Discord servers run independent leagues. A
member's book name is a short slug (`m1`, `m2`, …) derived from its row, because
`journal.BOOK_RE` caps a book name at 32 characters and a Discord snowflake pair
is far longer than that.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from thesis import arena, config, journal, track

#: Every member starts with the same simulated capital.
STARTING_CASH = 100_000.0

LEAGUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS league_members (
    id           INTEGER PRIMARY KEY,
    guild_id     TEXT NOT NULL,
    member_id    TEXT NOT NULL,
    display_name TEXT NOT NULL,
    persona      TEXT NOT NULL,
    book         TEXT NOT NULL UNIQUE,
    joined_at    TEXT NOT NULL,
    UNIQUE (guild_id, member_id)
);

CREATE INDEX IF NOT EXISTS idx_league_members_guild ON league_members (guild_id);

CREATE TABLE IF NOT EXISTS league_usage (
    id        INTEGER PRIMARY KEY,
    guild_id  TEXT NOT NULL,
    member_id TEXT NOT NULL,
    command   TEXT NOT NULL,
    day       TEXT NOT NULL,
    used      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (guild_id, member_id, command, day)
);
"""

#: Per-user, per-day caps. Two jobs: keep one member from burning the API budget
#: with `/research`, and keep the journal's discipline from being brute-forced —
#: someone who needs sixteen buys a day is not writing sixteen theses.
#:
#: Commands split into two metering styles, and which one a command gets depends
#: on whether a rejected attempt costs anything:
#:
#: * **Charge on acceptance** (`buy`, `sell`) — a refusal here is pure validation
#:   against the seven rules. It calls no API and writes no row, and for someone
#:   learning the discipline the refusal *is* the lesson. Charging for it would
#:   ration the teaching and push a member to guess less carefully, not more.
#: * **Charge on attempt** (`research`, and the rest) — a `/research` miss
#:   generates a brief, which costs real money whether the member likes the
#:   result or not, so the attempt is the billable event.
CHARGE_ON_ACCEPTANCE = frozenset({"buy", "sell"})

DAILY_LIMITS: dict[str, int] = {
    "buy": 8,
    "sell": 8,
    "review": 6,
    "research": 3,      # each miss is a brief generation, so this one costs money
    "standings": 40,
    "join": 3,
}


@dataclass(frozen=True)
class RateVerdict:
    command: str
    limit: int
    used: int
    #: True when `used` counts an attempt that has already been charged
    #: (charge-on-attempt), False when it is the balance *before* charging
    #: (charge-on-acceptance). Decides whether the cap compares with <= or <.
    charged: bool = True

    @property
    def allowed(self) -> bool:
        return self.used <= self.limit if self.charged else self.used < self.limit

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def message(self) -> str:
        return (
            f"Daily limit reached for `/{self.command}` — {self.limit} per day. "
            "It resets at midnight UTC."
        )


def rate_status(
    conn: sqlite3.Connection,
    guild_id: int | str,
    member_id: int | str,
    command: str,
    on_day: date | None = None,
    limit: int | None = None,
) -> RateVerdict:
    """What the member has spent so far today. Reads only — charges nothing.

    The first half of charge-on-acceptance: ask whether there is room, do the
    work, and only then call `consume_rate`.
    """
    cap = DAILY_LIMITS.get(command, 20) if limit is None else limit
    day = (on_day or journal.today()).isoformat()
    row = conn.execute(
        "SELECT used FROM league_usage WHERE guild_id = ? AND member_id = ? "
        "AND command = ? AND day = ?",
        (str(guild_id), str(member_id), command, day),
    ).fetchone()
    return RateVerdict(
        command=command, limit=cap, used=row[0] if row else 0, charged=False
    )


def consume_rate(
    conn: sqlite3.Connection,
    guild_id: int | str,
    member_id: int | str,
    command: str,
    on_day: date | None = None,
    limit: int | None = None,
) -> RateVerdict:
    """Charge one use and report the new balance. Call this only after success."""
    return check_rate(conn, guild_id, member_id, command, on_day, limit)


def check_rate(
    conn: sqlite3.Connection,
    guild_id: int | str,
    member_id: int | str,
    command: str,
    on_day: date | None = None,
    limit: int | None = None,
) -> RateVerdict:
    """Charge one use immediately and return the verdict — charge-on-attempt.

    Correct for commands where making the attempt is itself the cost: a
    `/research` miss generates a brief and spends money regardless of what the
    member does with it. **Wrong for `/buy` and `/sell`**, where a refusal is pure
    validation — those use `rate_status` then `consume_rate` so only accepted
    trades are charged. See `CHARGE_ON_ACCEPTANCE`.
    """
    cap = DAILY_LIMITS.get(command, 20) if limit is None else limit
    day = (on_day or journal.today()).isoformat()
    conn.execute(
        "INSERT INTO league_usage (guild_id, member_id, command, day, used) "
        "VALUES (?,?,?,?,1) ON CONFLICT (guild_id, member_id, command, day) "
        "DO UPDATE SET used = used + 1",
        (str(guild_id), str(member_id), command, day),
    )
    conn.commit()
    used = conn.execute(
        "SELECT used FROM league_usage WHERE guild_id = ? AND member_id = ? "
        "AND command = ? AND day = ?",
        (str(guild_id), str(member_id), command, day),
    ).fetchone()[0]
    return RateVerdict(command=command, limit=cap, used=used)


def usage_today(
    conn: sqlite3.Connection,
    guild_id: int | str,
    member_id: int | str,
    on_day: date | None = None,
) -> dict[str, int]:
    day = (on_day or journal.today()).isoformat()
    rows = conn.execute(
        "SELECT command, used FROM league_usage WHERE guild_id = ? AND member_id = ? "
        "AND day = ?",
        (str(guild_id), str(member_id), day),
    ).fetchall()
    return {r["command"]: r["used"] for r in rows}


@dataclass(frozen=True)
class Member:
    id: int
    guild_id: str
    member_id: str
    display_name: str
    persona: str
    book: str
    joined_at: str

    @property
    def mandate(self) -> arena.Persona:
        return arena.PERSONAS[self.persona]


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the league database — journal + arena tables, plus the member roster.

    Reuses `arena.connect` deliberately: a league cycle *is* an arena cycle, so it
    gets the same decision log, the same cycle records and the same journal
    schema. Only the file and the model differ.
    """
    conn = arena.connect(path or config.league_db_path())
    conn.executescript(LEAGUE_SCHEMA)
    conn.commit()
    return conn


# --------------------------------------------------------------------- joining

def book_slug(row_id: int) -> str:
    """`m17` — short enough for `journal.BOOK_RE`, unique per roster row."""
    return f"m{row_id}"


def find_member(
    conn: sqlite3.Connection, guild_id: int | str, member_id: int | str
) -> Member | None:
    row = conn.execute(
        "SELECT * FROM league_members WHERE guild_id = ? AND member_id = ?",
        (str(guild_id), str(member_id)),
    ).fetchone()
    return _member(row) if row else None


def _member(row: sqlite3.Row) -> Member:
    return Member(
        id=row["id"],
        guild_id=row["guild_id"],
        member_id=row["member_id"],
        display_name=row["display_name"],
        persona=row["persona"],
        book=row["book"],
        joined_at=row["joined_at"],
    )


def members(conn: sqlite3.Connection, guild_id: int | str) -> list[Member]:
    rows = conn.execute(
        "SELECT * FROM league_members WHERE guild_id = ? ORDER BY id",
        (str(guild_id),),
    ).fetchall()
    return [_member(r) for r in rows]


def join(
    conn: sqlite3.Connection,
    guild_id: int | str,
    member_id: int | str,
    display_name: str,
    persona: str = arena.VALUE.name,
    cash: float = STARTING_CASH,
    on_date: date | None = None,
) -> tuple[Member, bool]:
    """Enrol a member and fund their book. Returns (member, created).

    Idempotent: joining twice returns the existing book rather than a second one
    or a second deposit.
    """
    if persona not in arena.PERSONAS:
        raise ValueError(
            f"unknown mandate {persona!r} — choose one of {sorted(arena.PERSONAS)}"
        )
    existing = find_member(conn, guild_id, member_id)
    if existing:
        return existing, False

    cur = conn.execute(
        "INSERT INTO league_members (guild_id, member_id, display_name, persona, "
        "book, joined_at) VALUES (?,?,?,?,?,?)",
        (str(guild_id), str(member_id), display_name, persona, "", journal.now_stamp()),
    )
    row_id = int(cur.lastrowid)
    book = book_slug(row_id)
    journal.validate_book(book)
    conn.execute("UPDATE league_members SET book = ? WHERE id = ?", (book, row_id))
    conn.commit()

    journal.add_deposit(
        conn, book, cash, on_date or journal.today(), f"league seed for {display_name}"
    )
    member = find_member(conn, guild_id, member_id)
    assert member is not None
    return member, True


# ---------------------------------------------------------------- the standings

@dataclass
class Standing:
    label: str
    kind: str  # "member" | "agent" | "benchmark"
    persona: str
    equity: float
    pnl: float
    pnl_pct: float | None
    open_positions: int
    rejected: int

    @property
    def is_benchmark(self) -> bool:
        return self.kind == "benchmark"


def standings(
    conn: sqlite3.Connection,
    guild_id: int | str,
    prices: Mapping[str, float],
    spy: pd.Series,
    as_of: date | None = None,
) -> list[Standing]:
    """One table: every member book, then the SPY mirror they are all measured against.

    SPY appears as a row rather than a column so a member can see at a glance
    whether they are above or below the line that actually matters.
    """
    rows: list[Standing] = []
    spy_pct: float | None = None
    spy_equity = 0.0

    for member in members(conn, guild_id):
        deposits = journal.deposits(conn, member.book)
        holdings = journal.holdings(conn, member.book)
        if not deposits:
            continue
        report = track.build_report(
            member.book, deposits, holdings, prices, spy, as_of=as_of
        )
        acct = report.account
        counts = conn.execute(
            "SELECT COALESCE(SUM(status = 'rejected'), 0) FROM arena_decisions "
            "WHERE agent = ?",
            (member.book,),
        ).fetchone()
        rows.append(
            Standing(
                label=member.display_name,
                kind="member",
                persona=member.persona,
                equity=acct.equity,
                pnl=acct.pnl,
                pnl_pct=acct.pnl_pct,
                open_positions=len(report.open_lines),
                rejected=counts[0] or 0,
            )
        )
        spy_pct = acct.spy_pct
        spy_equity = acct.spy_equity

    rows.sort(key=lambda s: (s.pnl_pct if s.pnl_pct is not None else float("-inf")), reverse=True)
    if rows:
        rows.append(
            Standing(
                label="SPY benchmark",
                kind="benchmark",
                persona="",
                equity=spy_equity,
                pnl=spy_equity - STARTING_CASH,
                pnl_pct=spy_pct,
                open_positions=0,
                rejected=0,
            )
        )
    return rows


def render_standings(rows: Sequence[Standing], guild_name: str = "") -> str:
    """Discord-ready standings. Plain text in a code block — no embeds needed."""
    title = f"Standings — {guild_name}" if guild_name else "Standings"
    if not rows:
        return f"**{title}**\nNobody has joined yet. Run `/join` to get a book."

    lines = [f"{'#':>2}  {'who':<18} {'mandate':<9} {'equity':>12} {'return':>8}"]
    lines.append("-" * 54)
    position = 0
    for row in rows:
        if row.is_benchmark:
            lines.append("-" * 54)
            lines.append(
                f"{'':>2}  {row.label[:18]:<18} {'':<9} "
                f"{track.money(row.equity):>12} {track.pct(row.pnl_pct):>8}"
            )
            continue
        position += 1
        lines.append(
            f"{position:>2}  {row.label[:18]:<18} {row.persona:<9} "
            f"{track.money(row.equity):>12} {track.pct(row.pnl_pct):>8}"
        )
    body = "\n".join(lines)
    return (
        f"**{title}**\n```\n{body}\n```\n"
        "*Simulated money. SPY is the same deposits on the same dates — "
        "beating cash is not the bar.*"
    )


# ------------------------------------------------------------- the weekly cycle

@dataclass
class CycleResult:
    cycle_id: int
    cycle_date: date
    model: str
    reasoning: dict[str, str]
    filled: list[tuple[str, arena.PendingOrder, arena.Outcome]]
    rejected: list[tuple[str, arena.PendingOrder, arena.Outcome]]
    pending: list[tuple[str, arena.PendingOrder, arena.Outcome]]
    ordered: dict[str, int]
    input_tokens: int = 0
    output_tokens: int = 0


def run_cycle(
    conn: sqlite3.Connection,
    guild_id: int | str,
    client: Any,
    screen_table: str,
    briefs: Mapping[str, str],
    prices: Mapping[str, float],
    as_of: date | None = None,
    model: str | None = None,
    fetcher: Callable[..., pd.DataFrame] | None = None,
) -> CycleResult:
    """One weekly league cycle, for every member of one guild.

    Identical in structure to an arena cycle, and it uses the same functions:
    settle outstanding orders first, then each member's agent gets its packet,
    returns schema-constrained JSON, and every order goes through the journal.
    """
    as_of = as_of or journal.today()
    model = model or config.LEAGUE_MODEL
    roster = members(conn, guild_id)
    books = {m.book for m in roster}

    # Settle what is outstanding before anyone decides anything new, so a member
    # sees the position an earlier order created rather than a phantom.
    settled = [
        (order.agent, order, outcome)
        for order, outcome in arena.fill_pending(conn, prices, fetcher)
        if order.agent in books
    ]

    cycle_id = arena.start_cycle(conn, as_of, model)
    result = CycleResult(
        cycle_id=cycle_id, cycle_date=as_of, model=model,
        reasoning={}, filled=[], rejected=[], pending=[], ordered={},
    )
    for agent, order, outcome in settled:
        _bucket(result, agent, order, outcome)

    for member in roster:
        packet = arena.build_packet(
            agent=member.book,
            holdings=journal.holdings(conn, member.book),
            deposits=journal.deposits(conn, member.book),
            prices=prices,
            screen_table=screen_table,
            briefs=briefs,
            as_of=as_of,
            last_trade=arena.last_trade_date(conn, member.book),
            pending=arena.pending_orders(conn, member.book),
        )
        try:
            answer = arena.ask_agent(client, member.mandate, packet, model)
        except Exception as exc:  # one member's failure must not kill the cycle
            result.reasoning[member.display_name] = f"(no answer this cycle: {exc})"
            continue

        result.input_tokens += answer.input_tokens
        result.output_tokens += answer.output_tokens
        result.reasoning[member.display_name] = answer.reasoning
        arena.record_reasoning(conn, cycle_id, member.book, answer.reasoning)
        # The packet showed every position with its thesis and stop — that is the
        # weekly review, so rule 6's clock is honestly satisfied.
        arena.record_cycle_review(conn, member.book, as_of)

        result.ordered[member.display_name] = len(answer.decisions)
        for decision in answer.decisions:
            # Force the order onto the member's own book. Whatever the model put
            # in its JSON, it cannot trade in someone else's name.
            decision.agent = member.book
            arena.record_decision(
                conn, cycle_id,
                arena.Outcome(
                    decision, "pending",
                    f"awaiting the first session open after {as_of}",
                ),
            )

    arena.finish_cycle(conn, cycle_id, result.input_tokens, result.output_tokens)
    for order, outcome in arena.fill_pending(conn, prices, fetcher):
        if order.agent in books:
            _bucket(result, order.agent, order, outcome)
    return result


def _bucket(
    result: CycleResult, agent: str, order: arena.PendingOrder, outcome: arena.Outcome
) -> None:
    target = {
        "filled": result.filled,
        "rejected": result.rejected,
        "pending": result.pending,
    }.get(outcome.status)
    if target is not None:
        target.append((agent, order, outcome))


# ------------------------------------------------- the member's own trades

#: The note an arena/league cycle stamps on the review it records for an agent.
CYCLE_REVIEW_NOTE = "arena cycle review"


def last_human_review(conn: sqlite3.Connection, book: str) -> date | None:
    """The most recent review a *person* ran, ignoring the weekly cycle's own.

    Rule 6 is a property of the book, so a running agent would otherwise keep the
    9-day lockout permanently cleared and `/review` would be decorative. A member
    who trades by hand has to look at their own positions.
    """
    row = conn.execute(
        "SELECT review_date FROM reviews WHERE book = ? AND note IS NOT ? "
        "ORDER BY review_date DESC, id DESC LIMIT 1",
        (book, CYCLE_REVIEW_NOTE),
    ).fetchone()
    return journal.parse_date(row[0]) if row else None


def human_state(
    conn: sqlite3.Connection, book: str
) -> journal.BookState:
    """The book's state as the *human* sees it — cycle reviews do not count."""
    return journal.BookState(
        book=book,
        deposits=journal.deposits(conn, book),
        holdings=journal.holdings(conn, book),
        last_review=last_human_review(conn, book),
    )


def buy(
    conn: sqlite3.Connection,
    book: str,
    ticker: str,
    bucket: str,
    shares: float,
    price: float,
    thesis: str,
    invalidation: str,
    horizon: str,
    exit_plan: str,
    stop_price: float | None = None,
    prices: Mapping[str, float] | None = None,
    on_date: date | None = None,
) -> journal.BuyCheck:
    """A member's own buy. Raises `journal.RuleViolation` exactly as the CLI does.

    Every check is the journal's. This function assembles a `BuyRequest` and hands
    it to `journal.validate_buy` — there is no league-side rule, and no attempt to
    fix up a request that fails one.
    """
    on_date = on_date or journal.today()
    request = journal.BuyRequest(
        book=book, ticker=ticker, bucket=bucket, shares=shares, price=price,
        thesis=thesis, invalidation=invalidation, horizon=horizon,
        exit_plan=exit_plan, stop_price=stop_price, trade_date=on_date,
    )
    marks = {**(prices or {}), request.ticker: price}
    check = journal.validate_buy(request, human_state(conn, book), marks, on_date)
    journal.log_buy(conn, check)
    return check


def sell(
    conn: sqlite3.Connection,
    book: str,
    ticker: str,
    shares: float | None,
    price: float,
    outcome: str,
    on_date: date | None = None,
) -> journal.Holding:
    """A member's own sell. Raises `journal.RuleViolation` exactly as the CLI does."""
    on_date = on_date or journal.today()
    holding = journal.find_open(conn, book, ticker.upper())
    if holding is None:
        open_names = ", ".join(h.ticker for h in journal.open_holdings(conn, book))
        raise journal.RuleViolation(
            None,
            f"you hold no open {ticker.upper()} position"
            + (f" — open: {open_names}" if open_names else " — your book is empty"),
        )
    count = holding.shares if shares in (None, 0) else float(shares)
    journal.validate_sell(holding, count, price, outcome)
    return journal.log_sell(conn, holding, count, price, outcome, on_date)


def review(
    conn: sqlite3.Connection,
    book: str,
    prices: Mapping[str, float],
    spy: pd.Series,
    note: str = "",
    on_date: date | None = None,
) -> tuple[track.TrackReport | None, journal.Review]:
    """Run and record a member's weekly review. Clears the 9-day lockout."""
    on_date = on_date or journal.today()
    deposits = journal.deposits(conn, book)
    holdings = journal.holdings(conn, book)
    report = (
        track.build_report(book, deposits, holdings, prices, spy, as_of=on_date)
        if deposits
        else None
    )
    entry = journal.record_review(conn, book, note or "discord review", on_date)
    return report, entry


def review_status(
    conn: sqlite3.Connection, book: str, on_date: date | None = None
) -> journal.ReviewStatus:
    """Rule 6 status for a member, counting human reviews only."""
    return journal.review_status(
        open_positions=len(journal.open_holdings(conn, book)),
        last_review=last_human_review(conn, book),
        as_of=on_date or journal.today(),
        book=book,
    )


def place_order(
    conn: sqlite3.Connection,
    book: str,
    decision: arena.Decision,
    fill_price: float,
    fill_date: date,
    prices: Mapping[str, float] | None = None,
) -> arena.Outcome:
    """Route one league order through the journal's rules. No rule lives here.

    This is the *only* way a league book acquires a position, and it is a
    delegation: `arena.apply_decision` -> `journal.validate_buy`. A rejected order
    keeps its rule number and is forfeited.
    """
    decision.agent = book
    return arena.apply_decision(conn, decision, fill_price, fill_date, prices or {})


# ----------------------------------------------------------------- the weekly post

def render_cycle(result: CycleResult, names: Mapping[str, str]) -> str:
    """The weekly auto-post: what each agent did, and why, in its own words."""
    out: list[str] = [
        f"**League cycle — {result.cycle_date}** *(model: {result.model})*",
        "",
    ]
    if not result.reasoning:
        out.append("Nobody has joined yet. Run `/join` to get a book.")
        return "\n".join(out)

    for who, reasoning in result.reasoning.items():
        out.append(f"__{who}__")
        out.append(reasoning.strip() or "*(no comment)*")
        ordered = result.ordered.get(who, 0)
        out.append(f"*Orders placed: {ordered}*" if ordered else "*No trades this week.*")
        out.append("")

    def label(agent: str) -> str:
        return names.get(agent, agent)

    if result.filled:
        out.append("**Filled** *(at the open after each order's decision date)*")
        for agent, order, outcome in result.filled:
            out.append(
                f"- {label(agent)}: {order.decision.action} {order.decision.shares:g} "
                f"{order.decision.ticker} @ {track.money(outcome.fill_price)} "
                f"on {outcome.fill_date}"
            )
        out.append("")
    if result.rejected:
        out.append("**Refused by the account rules** *(forfeited, not repaired)*")
        for agent, order, outcome in result.rejected:
            rule = f"rule {outcome.rule}: " if outcome.rule else ""
            out.append(
                f"- {label(agent)}: {order.decision.action} "
                f"{order.decision.shares:g} {order.decision.ticker} — {rule}"
                f"{outcome.reason}"
            )
        out.append("")
    if result.pending:
        out.append("**Placed, waiting on the next open**")
        for agent, order, _ in result.pending:
            out.append(
                f"- {label(agent)}: {order.decision.action} "
                f"{order.decision.shares:g} {order.decision.ticker}"
            )
        out.append("")

    out.append("Simulated money. `/standings` for the table.")
    return "\n".join(out)


# ------------------------------------------------- /research: weekly brief cache

def briefs_dir() -> Path:
    """Where league briefs live — separate from the human's own store.

    A league brief is generated on Sonnet; the human's are Opus. Keeping them apart
    means a brief's provenance is never ambiguous, and a cheaper league brief can
    never be mistaken for the user's own research later.
    """
    return config.BRIEFS_DIR / "league"


def week_key(day: date) -> str:
    """ISO year-week, e.g. `2026-W33`. The cache is per company, per week."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def cached_brief(ticker: str, day: date | None = None) -> Path | None:
    """This week's brief for a ticker, if one was already generated. Pure-ish."""
    day = day or journal.today()
    directory = briefs_dir()
    if not directory.exists():
        return None
    wanted = week_key(day)
    matches = sorted(directory.glob(f"{ticker.upper()}_*.md"))
    for path in reversed(matches):
        stamp = path.stem.split("_", 1)[-1]
        try:
            written = journal.parse_date(stamp)
        except ValueError:
            continue
        if week_key(written) == wanted:
            return path
    return None


@dataclass
class BriefAnswer:
    ticker: str
    text: str
    path: Path | None
    cached: bool
    model: str = ""
    lint_ok: bool = True
    lint_summary: str = ""

    @property
    def servable(self) -> bool:
        return self.lint_ok and bool(self.text)


def research(
    ticker: str,
    day: date | None = None,
    generator: Callable[..., Any] | None = None,
    model: str | None = None,
) -> BriefAnswer:
    """Serve this week's brief for a company, generating it on a cache miss.

    Generation runs on Sonnet and is lint-gated: a brief whose citations do not
    survive `lint.lint_brief` after its one retry is **not served**. Serving an
    unsourced brief to a room of people is worse than serving nothing.
    """
    ticker = ticker.upper()
    day = day or journal.today()

    hit = cached_brief(ticker, day)
    if hit is not None:
        return BriefAnswer(
            ticker=ticker, text=hit.read_text(encoding="utf-8"), path=hit, cached=True
        )

    from thesis import brief as brief_module

    generate = generator or brief_module.generate
    result = generate(
        ticker,
        model=model or config.LEAGUE_MODEL,
        briefs_dir=briefs_dir(),
    )
    ok = result.lint.ok
    return BriefAnswer(
        ticker=ticker,
        text=result.path.read_text(encoding="utf-8") if ok else "",
        path=result.path,
        cached=False,
        model=result.model,
        lint_ok=ok,
        lint_summary=result.lint.render(),
    )


def render_brief(answer: BriefAnswer, limit: int = 1_800) -> str:
    """A Discord-sized reply. The full brief is a file; this is the readable head."""
    if not answer.servable:
        return (
            f"**{answer.ticker}** — the generated brief failed its citation check, "
            "so it is not being served.\n"
            "A brief with untagged claims is worse than no brief. Try again later, "
            "or run `thesis research` locally to see the violations."
        )
    head = "cached this week" if answer.cached else f"generated just now on {answer.model}"
    body = answer.text.strip()
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + "\n\n*…truncated. Full brief on file.*"
    return f"**{answer.ticker}** *({head})*\n{body}"


def display_names(conn: sqlite3.Connection, guild_id: int | str) -> dict[str, str]:
    return {m.book: m.display_name for m in members(conn, guild_id)}


def guilds(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT guild_id FROM league_members ORDER BY guild_id"
        )
    ]
