"""The Arena — a simulated LLM portfolio experiment.

Three agent personas each run a $100k simulated book. Every week each one
receives the same packet — its own positions, the latest screen, and the briefs
for names it holds or wants — and returns decisions as structured JSON.

The point of the experiment is that **agent trades are not special**. Every
decision is turned into a `journal.BuyRequest` and pushed through
`journal.validate_buy`, the same function the human's trades go through, so all
seven account rules bind identically: a written thesis, an invalidation trigger,
a horizon and an exit plan; a stop for Active names; position caps; shares only;
no margin; a fresh weekly review. A decision that fails is **rejected, logged
with the rule that refused it, and forfeited** — never repaired, never retried,
never quietly downgraded into something legal.

Isolation is physical, not merely logical: the arena lives in its own SQLite
file (`.arena/arena.db`), so an arena bug cannot reach the human's journal even
if it tried. And nothing here can reach a brokerage — the only outbound calls in
this module are to the market-data provider and the Claude API. Fills are
simulated at the next session's open.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd
import yfinance as yf

from thesis import config, journal, track
from thesis.data import market

#: Every agent starts with the same simulated capital, on the same date.
STARTING_CASH = 100_000.0

#: One regeneration is not offered here: a malformed or rule-breaking decision is
#: forfeited, not re-asked. Re-prompting until the model produces something legal
#: would launder a bad decision into a good-looking one.
MAX_DECISIONS_PER_CYCLE = 10


# --------------------------------------------------------------------- personas

@dataclass(frozen=True)
class Persona:
    name: str
    summary: str
    system_prompt: str


_SHARED_RULES = """\
## The account rules — these are enforced in code, not by trust

Your decisions are validated by the same journal code that governs a human
trader. A decision that breaks a rule is REJECTED and LOGGED with the reason,
and you forfeit that action for the week. Nothing is repaired for you.

1. Every buy needs a written thesis (40+ characters), an invalidation trigger, a
   time horizon, and an exit plan. All four. "It looks cheap" is not a thesis.
2. Active-bucket buys need a stop price, and it must be BELOW your entry price.
3. Position caps: no Core name above 20% of your account, no Active name above
   10%. Measured after the trade, counting shares you already hold in that name.
4. Shares only. No options, no shorting, no margin — you cannot spend cash you
   do not have, and you cannot sell what you do not hold.
5. Cash settles T+1. Selling and rebuying the same week may use unsettled funds.
6. Sells need one honest line on what actually happened versus your thesis.

You are managing a simulated book. It is not connected to any brokerage and no
real money moves. Be honest rather than flattering: a losing position described
accurately is worth more than a winning one described vaguely.

## How to answer

Return JSON only. `reasoning` is your overall read of the week. `decisions` is a
list — it may be empty, and an empty list is a legitimate answer. Do not invent
prices; the fill will happen at the next session's open, which you cannot see.
"""

VALUE = Persona(
    name="value",
    summary="Long-term quality + valuation. Core bucket, years-long horizons.",
    system_prompt=f"""\
You are a long-term value investor running a simulated $100,000 book.

You buy good businesses at sensible prices and hold them for years. You care
about durable competitive advantage, consistent free cash flow, a balance sheet
that survives a bad year, and a price that is not already paying for perfection.
You are deeply skeptical of momentum, narrative, and anything you cannot explain
in a paragraph to someone who does not follow markets.

Your positions belong in the `core` bucket. Your horizons are measured in years,
not weeks. You would rather hold cash than own something you do not understand,
and you do not trade to look busy — most weeks you should do nothing.

Your invalidation triggers should be about the BUSINESS deteriorating (margins,
returns on capital, competitive position), not about the share price falling.
A price drop in a business you still believe in is not an invalidation.

{_SHARED_RULES}""",
)

MOMENTUM = Persona(
    name="momentum",
    summary="Swing trades on confirmed strength. Active bucket, strict stops.",
    system_prompt=f"""\
You are a momentum swing trader running a simulated $100,000 book.

You buy strength and you cut losers fast. You want names already working — the
screen's momentum ranks are your primary hunting ground — with a specific
catalyst or setup you can name. You do not average down, you do not hold through
a broken thesis, and you do not marry a position.

Your positions belong in the `active` bucket, which means every buy needs a stop
price below your entry. Set it where the setup is genuinely broken, not at an
arbitrary round number. Your horizons are days to weeks.

Your invalidation triggers should be specific and checkable — a level breaking, a
moving average lost, a catalyst that fails to materialize by a date. "It stops
going up" is not checkable; "closes below the 50-day" is.

Respect the 10% Active cap: concentrated conviction is not the same as
recklessness, and a stop only protects you if the position was sized correctly
in the first place.

{_SHARED_RULES}""",
)

MONK = Persona(
    name="monk",
    summary="At most one trade per month. Cash is a position.",
    system_prompt=f"""\
You are an extremely patient investor running a simulated $100,000 book.

You may make AT MOST ONE TRADE PER MONTH. That is the defining constraint of
your mandate, and it is the whole point of your existence in this experiment.
Because you get so few actions, each one must clear a far higher bar than it
would for anyone else — the question is never "is this good?" but "is this the
single best thing I will see in the next month?"

Cash is a legitimate position, not a failure to act. Most weeks the correct
answer is an empty decisions list, and you should say so plainly rather than
manufacturing a rationale for activity. If you have already traded this month,
return no decisions and say why.

When you do act, your thesis should read like something you would be content to
be judged on in five years. Your invalidation triggers should be things that
would genuinely make you conclude you were wrong, not things that would merely
make you uncomfortable.

{_SHARED_RULES}""",
)

PERSONAS: dict[str, Persona] = {p.name: p for p in (VALUE, MOMENTUM, MONK)}


# ----------------------------------------------------------- decision contract

#: Structured-output schema. The API constrains the response to this shape, so
#: parsing cannot fail — but a well-formed decision can still be an illegal one,
#: which is what the journal rules are for.
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reasoning": {
            "type": "string",
            "description": "Your read of the week, before any specific decision.",
        },
        "decisions": {
            "type": "array",
            "description": "May be empty. An empty list is a legitimate answer.",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["buy", "sell"]},
                    "ticker": {"type": "string"},
                    "shares": {"type": "number"},
                    "bucket": {"type": "string", "enum": ["core", "active"]},
                    "thesis": {"type": "string"},
                    "invalidation": {"type": "string"},
                    "horizon": {"type": "string"},
                    "exit_plan": {"type": "string"},
                    "stop_price": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "outcome": {
                        "anyOf": [{"type": "string"}, {"type": "null"}],
                        "description": "For a sell: what happened vs. the thesis.",
                    },
                    "reasoning": {"type": "string"},
                },
                "required": [
                    "action", "ticker", "shares", "bucket", "thesis",
                    "invalidation", "horizon", "exit_plan", "stop_price",
                    "outcome", "reasoning",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reasoning", "decisions"],
    "additionalProperties": False,
}


@dataclass
class Decision:
    """One requested action, before the rules have had their say."""

    agent: str
    action: str
    ticker: str
    shares: float
    bucket: str
    thesis: str
    invalidation: str
    horizon: str
    exit_plan: str
    stop_price: float | None
    outcome: str | None
    reasoning: str

    @classmethod
    def from_json(cls, agent: str, raw: Mapping[str, Any]) -> "Decision":
        return cls(
            agent=agent,
            action=str(raw.get("action", "")).strip().lower(),
            ticker=str(raw.get("ticker", "")).strip().upper(),
            shares=float(raw.get("shares") or 0.0),
            bucket=str(raw.get("bucket", "")).strip().lower(),
            thesis=str(raw.get("thesis") or "").strip(),
            invalidation=str(raw.get("invalidation") or "").strip(),
            horizon=str(raw.get("horizon") or "").strip(),
            exit_plan=str(raw.get("exit_plan") or "").strip(),
            stop_price=None if raw.get("stop_price") in (None, 0) else float(raw["stop_price"]),
            outcome=(str(raw["outcome"]).strip() if raw.get("outcome") else None),
            reasoning=str(raw.get("reasoning") or "").strip(),
        )


@dataclass
class Outcome:
    """What the rules did with a decision. `status` is filled, rejected, or pending."""

    decision: Decision
    status: str
    reason: str = ""
    fill_price: float | None = None
    fill_date: date | None = None
    rule: int | None = None

    @property
    def filled(self) -> bool:
        return self.status == "filled"


# ------------------------------------------------------------------- the ledger

ARENA_SCHEMA = """
CREATE TABLE IF NOT EXISTS arena_agents (
    name       TEXT PRIMARY KEY,
    summary    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arena_cycles (
    id           INTEGER PRIMARY KEY,
    cycle_date   TEXT NOT NULL,
    model        TEXT NOT NULL,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arena_decisions (
    id           INTEGER PRIMARY KEY,
    cycle_id     INTEGER NOT NULL REFERENCES arena_cycles(id),
    agent        TEXT NOT NULL,
    action       TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    shares       REAL NOT NULL,
    bucket       TEXT NOT NULL,
    thesis       TEXT NOT NULL,
    invalidation TEXT NOT NULL,
    horizon      TEXT NOT NULL,
    exit_plan    TEXT NOT NULL,
    stop_price   REAL,
    outcome_note TEXT,
    reasoning    TEXT NOT NULL,
    status       TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    rule         INTEGER,
    fill_price   REAL,
    fill_date    TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arena_notes (
    id         INTEGER PRIMARY KEY,
    cycle_id   INTEGER NOT NULL REFERENCES arena_cycles(id),
    agent      TEXT NOT NULL,
    reasoning  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_arena_decisions_agent ON arena_decisions (agent);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the arena database — the journal schema plus the arena's own tables.

    A separate file from `journal.db` on purpose: the arena cannot touch the
    human's books because it is not looking at the same bytes.
    """
    conn = journal.connect(path or config.arena_db_path())
    conn.executescript(ARENA_SCHEMA)
    conn.commit()
    return conn


def init(
    conn: sqlite3.Connection,
    cash: float = STARTING_CASH,
    on_date: date | None = None,
) -> list[str]:
    """Create each persona's book and fund it. Idempotent — funds once per agent."""
    on_date = on_date or journal.today()
    created: list[str] = []
    for persona in PERSONAS.values():
        existing = conn.execute(
            "SELECT name FROM arena_agents WHERE name = ?", (persona.name,)
        ).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO arena_agents (name, summary, created_at) VALUES (?,?,?)",
            (persona.name, persona.summary, journal.now_stamp()),
        )
        journal.add_deposit(conn, persona.name, cash, on_date, "arena seed capital")
        created.append(persona.name)
    conn.commit()
    return created


def agents(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in conn.execute("SELECT name FROM arena_agents ORDER BY name")]


# ------------------------------------------------------------- packet assembly

def build_packet(
    agent: str,
    holdings: Sequence[journal.Holding],
    deposits: Sequence[journal.Deposit],
    prices: Mapping[str, float],
    screen_table: str,
    briefs: Mapping[str, str],
    as_of: date,
    last_trade: date | None = None,
    pending: Sequence[PendingOrder] = (),
) -> str:
    """The weekly packet. Pure — identical inputs give an identical packet.

    Every agent receives the same screen and the same brief corpus; only the
    positions and cash differ, because those are the agent's own.
    """
    parts: list[str] = []
    parts.append(f"# WEEKLY PACKET — agent `{agent}` — {as_of}")
    parts.append("")

    all_trades = [t for h in holdings for t in h.trades]
    cash = journal.cash_balance(deposits, all_trades)
    settled = cash - journal.unsettled_proceeds(all_trades, as_of)
    open_holdings = [h for h in holdings if h.is_open and h.shares > 0]
    try:
        equity = journal.equity(deposits, holdings, prices)
    except ValueError:
        equity = cash  # a held name with no price; the caller reports it

    parts.append("## YOUR ACCOUNT")
    parts.append(f"Equity: {track.money(equity)}")
    parts.append(f"Cash: {track.money(cash)} (settled and spendable: {track.money(settled)})")
    parts.append(
        f"Position caps at this equity: Core {track.money(equity * 0.20)} per name, "
        f"Active {track.money(equity * 0.10)} per name."
    )
    if last_trade:
        parts.append(f"Your most recent trade was {last_trade} ({(as_of - last_trade).days} days ago).")
    else:
        parts.append("You have not traded yet.")
    parts.append("")

    parts.append("## YOUR OPEN POSITIONS")
    if not open_holdings:
        parts.append("None. You are entirely in cash.")
    for holding in open_holdings:
        price = prices.get(holding.ticker)
        pos = holding.position
        line = (
            f"- {holding.ticker} ({holding.bucket}): {holding.shares:g} sh @ "
            f"{track.money(holding.avg_cost)} cost"
        )
        if price is not None:
            line += (
                f", now {track.money(price)}, "
                f"P&L {track.signed_money(holding.total_pnl(price))} "
                f"({track.pct(holding.return_pct(price))})"
            )
        parts.append(line)
        parts.append(f"    opened {holding.entry_date} · thesis: {pos.thesis}")
        parts.append(f"    invalidation: {pos.invalidation}")
        parts.append(f"    exit plan: {pos.exit_plan}")
        if pos.stop_price:
            breached = " — BREACHED" if price is not None and price <= pos.stop_price else ""
            parts.append(f"    stop: {track.money(pos.stop_price)}{breached}")
    parts.append("")

    parts.append("## YOUR PENDING ORDERS — placed, not yet filled")
    if not pending:
        parts.append("None. Every order you have placed has been resolved.")
    else:
        committed = 0.0
        for order in pending:
            price = prices.get(order.decision.ticker)
            dollars = order.decision.shares * price if price else None
            estimate = f", ~{track.money(dollars)} at the last close" if dollars else ""
            committed += dollars or 0.0
            parts.append(
                f"- {order.decision.action.upper()} {order.decision.shares:g} "
                f"{order.decision.ticker} — decided {order.decided_on}{estimate}"
            )
        parts.append("")
        parts.append(
            f"These are commitments you have already made (~{track.money(committed)} "
            "of capital). They fill at the open of the first session after the date "
            "they were decided. DO NOT order the same name again to 'make sure' it "
            "goes through — your pending and open exposure are counted together "
            "against the position caps, so a duplicate will simply be rejected and "
            "forfeited."
        )
    parts.append("")

    parts.append("## SCREEN — momentum + quality, run today")
    parts.append(screen_table.strip() or "(screen unavailable this cycle)")
    parts.append("")

    parts.append("## RESEARCH BRIEFS")
    if briefs:
        for ticker, text in briefs.items():
            parts.append(f"### Brief: {ticker}")
            parts.append(text.strip())
            parts.append("")
    else:
        parts.append("No briefs available for the names in play this cycle.")
        parts.append("")

    parts.append("## YOUR TASK")
    parts.append(
        "Decide what to do this week. Returning an empty decisions list is a "
        "legitimate answer and is often the right one. Anything you buy will be "
        "filled at the next session's open, which you cannot see — do not "
        "assume a price."
    )
    return "\n".join(parts)


def briefs_for(tickers: Iterable[str], briefs_dir: Path | None = None) -> dict[str, str]:
    """Load the most recent saved brief for each named ticker."""
    directory = briefs_dir or config.briefs_dir()
    if not directory.exists():
        return {}
    out: dict[str, str] = {}
    for ticker in dict.fromkeys(t.upper() for t in tickers):
        matches = sorted(directory.glob(f"{ticker}_*.md"))
        if matches:
            out[ticker] = matches[-1].read_text(encoding="utf-8", errors="replace")
    return out


# ------------------------------------------------------------------ cost model

#: Per million tokens, (input, output). From the model catalogue.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

#: A decision response is short — reasoning plus a handful of decisions.
ASSUMED_OUTPUT_TOKENS = 1_200


@dataclass
class CostEstimate:
    model: str
    input_tokens: int
    output_tokens: int
    input_cost: float
    output_cost: float
    priced: bool = True

    @property
    def total(self) -> float:
        return self.input_cost + self.output_cost

    def render(self) -> str:
        note = "" if self.priced else "  (model not in the price table — cost unknown)"
        return (
            f"Estimated cost for this cycle: ~{self.input_tokens:,} input + "
            f"~{self.output_tokens:,} output tokens on {self.model} "
            f"= ~${self.total:.2f}{note}"
        )


def estimate_cost(
    token_counts: Sequence[int],
    model: str,
    output_tokens_each: int = ASSUMED_OUTPUT_TOKENS,
) -> CostEstimate:
    """Price a cycle from per-agent input token counts. Pure."""
    input_tokens = int(sum(token_counts))
    output_tokens = output_tokens_each * len(token_counts)
    rates = MODEL_PRICING.get(model)
    per_in, per_out = rates if rates else (0.0, 0.0)
    return CostEstimate(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_cost=input_tokens / 1_000_000 * per_in,
        output_cost=output_tokens / 1_000_000 * per_out,
        priced=rates is not None,
    )


def count_tokens(client: Any, model: str, system: str, packet: str) -> int:
    """Exact input token count from the API (free), falling back to an estimate."""
    try:
        return int(
            client.messages.count_tokens(
                model=model,
                system=system,
                messages=[{"role": "user", "content": packet}],
            ).input_tokens
        )
    except Exception:
        # ~3.5 characters per token is a rough but honest stand-in.
        return int((len(system) + len(packet)) / 3.5)


# ---------------------------------------------------------------- the model call

@dataclass
class AgentResponse:
    agent: str
    reasoning: str
    decisions: list[Decision]
    input_tokens: int = 0
    output_tokens: int = 0


def ask_agent(
    client: Any,
    persona: Persona,
    packet: str,
    model: str,
) -> AgentResponse:
    """One agent, one packet, one structured answer.

    Uses the structured-output format so the response is schema-valid JSON by
    construction. Adaptive thinking is on; these are genuinely deliberative
    decisions and the packet is large.
    """
    response = client.messages.create(
        model=model,
        max_tokens=8_000,
        thinking={"type": "adaptive"},
        system=persona.system_prompt,
        output_config={"format": {"type": "json_schema", "schema": DECISION_SCHEMA}},
        messages=[{"role": "user", "content": packet}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "{}")
    payload = json.loads(text)
    raw = payload.get("decisions") or []
    return AgentResponse(
        agent=persona.name,
        reasoning=str(payload.get("reasoning") or "").strip(),
        decisions=[Decision.from_json(persona.name, d) for d in raw[:MAX_DECISIONS_PER_CYCLE]],
        input_tokens=getattr(response.usage, "input_tokens", 0),
        output_tokens=getattr(response.usage, "output_tokens", 0),
    )


# ------------------------------------------------------------------ simulated fills

def next_session_open(
    ticker: str, after: date, fetcher: Callable[..., pd.DataFrame] | None = None
) -> tuple[date, float] | None:
    """The open of the first session strictly after `after`, or None if not yet."""
    fetch = fetcher or _download_daily
    try:
        frame = fetch(ticker, after + timedelta(days=1), after + timedelta(days=10))
    except Exception:
        return None
    if frame is None or frame.empty or "Open" not in frame:
        return None
    opens = frame["Open"].dropna()
    if opens.empty:
        return None
    index = pd.DatetimeIndex(opens.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    for stamp, value in zip(index, opens):
        session = stamp.date()
        if session > after and float(value) > 0:
            return session, float(value)
    return None


def _download_daily(ticker: str, start: date, end: date) -> pd.DataFrame:
    return yf.Ticker(ticker).history(
        start=start.isoformat(), end=end.isoformat(), auto_adjust=True
    )


# --------------------------------------------------------------- rule enforcement

def apply_decision(
    conn: sqlite3.Connection,
    decision: Decision,
    fill_price: float,
    fill_date: date,
    prices: Mapping[str, float],
    reserved_cash: float = 0.0,
    reserved_exposure: Mapping[str, float] | None = None,
) -> Outcome:
    """Push one decision through the human journal's rules. Never repairs it.

    Returns an Outcome describing what happened. A rejected decision is
    forfeited: no retry, no downgrade, no partial fill.

    `reserved_cash` / `reserved_exposure` carry the agent's other still-unfilled
    orders, so caps and cash are checked against open **plus** pending exposure.
    """
    marks = {**prices, decision.ticker: fill_price}

    if decision.action == "sell":
        holding = journal.find_open(conn, decision.agent, decision.ticker)
        if holding is None:
            return Outcome(decision, "rejected", "no open position in that name")
        shares = decision.shares or holding.shares
        try:
            journal.validate_sell(holding, shares, fill_price, decision.outcome or "")
        except journal.RuleViolation as exc:
            return Outcome(decision, "rejected", exc.message, rule=exc.rule)
        journal.log_sell(
            conn, holding, shares, fill_price, decision.outcome or "", fill_date
        )
        return Outcome(decision, "filled", fill_price=fill_price, fill_date=fill_date)

    if decision.action != "buy":
        return Outcome(decision, "rejected", f"unknown action {decision.action!r}")

    request = journal.BuyRequest(
        book=decision.agent,
        ticker=decision.ticker,
        bucket=decision.bucket or journal.CORE,
        shares=decision.shares,
        price=fill_price,
        thesis=decision.thesis,
        invalidation=decision.invalidation,
        horizon=decision.horizon,
        exit_plan=decision.exit_plan,
        stop_price=decision.stop_price,
        trade_date=fill_date,
    )
    state = journal.load_state(conn, decision.agent)
    try:
        check = journal.validate_buy(
            request, state, marks, fill_date,
            reserved_cash=reserved_cash,
            reserved_exposure=reserved_exposure,
        )
        journal.log_buy(conn, check)
    except journal.RuleViolation as exc:
        return Outcome(decision, "rejected", exc.message, rule=exc.rule)
    except ValueError as exc:
        return Outcome(decision, "rejected", str(exc))
    return Outcome(decision, "filled", fill_price=fill_price, fill_date=fill_date)


def record_decision(
    conn: sqlite3.Connection, cycle_id: int, outcome: Outcome
) -> int:
    """Persist a decision and its fate — including every rejection and why."""
    d = outcome.decision
    cur = conn.execute(
        "INSERT INTO arena_decisions (cycle_id, agent, action, ticker, shares, bucket, "
        "thesis, invalidation, horizon, exit_plan, stop_price, outcome_note, reasoning, "
        "status, reason, rule, fill_price, fill_date, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cycle_id, d.agent, d.action, d.ticker, d.shares, d.bucket, d.thesis,
            d.invalidation, d.horizon, d.exit_plan, d.stop_price, d.outcome,
            d.reasoning, outcome.status, outcome.reason, outcome.rule,
            outcome.fill_price,
            outcome.fill_date.isoformat() if outcome.fill_date else None,
            journal.now_stamp(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


# ------------------------------------------------------------ pending orders

@dataclass(frozen=True)
class PendingOrder:
    """An order that was decided but has not executed yet.

    `decided_on` is the cycle date the agent made the call, and it — not today —
    is what determines the fill. An order decided on the 1st fills at the open on
    the 3rd even if it is discovered on the 11th; by then that open is history,
    not a guess.
    """

    id: int
    agent: str
    decided_on: date
    decision: Decision


def pending_orders(
    conn: sqlite3.Connection, agent: str | None = None
) -> list[PendingOrder]:
    """Unfilled orders, oldest decision first. Chronological order is required:
    an earlier order must become a position before a later one is judged."""
    sql = (
        "SELECT d.*, c.cycle_date FROM arena_decisions d "
        "JOIN arena_cycles c ON c.id = d.cycle_id WHERE d.status = 'pending'"
    )
    params: list[object] = []
    if agent:
        sql += " AND d.agent = ?"
        params.append(agent)
    sql += " ORDER BY c.cycle_date, d.id"

    orders: list[PendingOrder] = []
    for row in conn.execute(sql, params).fetchall():
        orders.append(
            PendingOrder(
                id=row["id"],
                agent=row["agent"],
                decided_on=journal.parse_date(row["cycle_date"]),
                decision=Decision(
                    agent=row["agent"],
                    action=row["action"],
                    ticker=row["ticker"],
                    shares=row["shares"],
                    bucket=row["bucket"],
                    thesis=row["thesis"],
                    invalidation=row["invalidation"],
                    horizon=row["horizon"],
                    exit_plan=row["exit_plan"],
                    stop_price=row["stop_price"],
                    outcome=row["outcome_note"],
                    reasoning=row["reasoning"],
                ),
            )
        )
    return orders


def settle_order(conn: sqlite3.Connection, order_id: int, outcome: Outcome) -> None:
    """Write a pending order's fate back onto its existing row."""
    conn.execute(
        "UPDATE arena_decisions SET status = ?, reason = ?, rule = ?, "
        "fill_price = ?, fill_date = ? WHERE id = ?",
        (
            outcome.status, outcome.reason, outcome.rule, outcome.fill_price,
            outcome.fill_date.isoformat() if outcome.fill_date else None,
            order_id,
        ),
    )
    conn.commit()


def reserved_against(
    orders: Sequence[PendingOrder],
    fills: Mapping[int, tuple[date, float] | None],
    prices: Mapping[str, float],
    exclude: int,
) -> tuple[float, dict[str, float]]:
    """Cash and per-ticker dollars committed by every *other* unfilled order. Pure.

    Priced at the order's own resolved fill where known, else at the last close.
    An order we cannot price at all reserves nothing — under-reserving is the
    honest failure here, because inventing a number would reject real orders on
    the strength of a guess.
    """
    cash = 0.0
    per_ticker: dict[str, float] = {}
    for order in orders:
        if order.id == exclude or order.decision.action != "buy":
            continue
        resolved = fills.get(order.id)
        price = resolved[1] if resolved else prices.get(order.decision.ticker)
        if price is None:
            continue
        dollars = order.decision.shares * price
        cash += dollars
        per_ticker[order.decision.ticker] = (
            per_ticker.get(order.decision.ticker, 0.0) + dollars
        )
    return cash, per_ticker


def fill_pending(
    conn: sqlite3.Connection,
    prices: Mapping[str, float] | None = None,
    fetcher: Callable[..., pd.DataFrame] | None = None,
    agent: str | None = None,
) -> list[tuple[PendingOrder, Outcome]]:
    """Fill every pending order at the open of the first session after it was decided.

    Processed oldest-decision-first, which is what makes the caps bind: an
    earlier order becomes a real position before a later one is judged, so a name
    split across two cycles is caught by the ordinary exposure check.

    Reservations cover the orders that *cannot* execute in this pass and were
    already outstanding when this one was decided — those were live commitments
    at fill time, so their capital counts against it. Two filters, both load
    bearing:

    * Orders that will each take their own turn are not reserved against one
      another. Doing so double-counts, and would refuse both halves of a pair
      where the first genuinely had room.
    * Orders decided *later* are never reserved. A fill on the 3rd cannot be
      charged for capital an agent committed on the 10th — the later order is
      instead judged against the position this one creates, which is what makes
      a name split across two cycles get caught.

    An order whose next session has not happened yet stays pending. An order that
    breaks a rule is rejected with that rule's number and forfeited.
    """
    orders = pending_orders(conn, agent)
    if not orders:
        return []

    # Resolve every fill up front so reservations can be priced at real opens.
    fills: dict[int, tuple[date, float] | None] = {
        order.id: next_session_open(order.decision.ticker, order.decided_on, fetcher)
        for order in orders
    }
    # Capital tied up in orders this pass cannot resolve.
    unresolvable = [o for o in orders if fills.get(o.id) is None]

    # A name filled during this pass has no fresh close yet; mark it at its fill
    # so the next order's equity can still be computed.
    marks = dict(prices or {})

    results: list[tuple[PendingOrder, Outcome]] = []
    for order in orders:
        resolved = fills.get(order.id)
        if resolved is None:
            results.append(
                (
                    order,
                    Outcome(
                        order.decision, "pending",
                        f"no session has opened since {order.decided_on} yet",
                    ),
                )
            )
            continue

        fill_date, fill_price = resolved
        already_committed = [
            o for o in unresolvable if o.decided_on <= order.decided_on
        ]
        reserved_cash, reserved_exposure = reserved_against(
            already_committed, fills, marks, exclude=order.id
        )
        outcome = apply_decision(
            conn, order.decision, fill_price, fill_date, marks,
            reserved_cash=reserved_cash, reserved_exposure=reserved_exposure,
        )
        settle_order(conn, order.id, outcome)
        if outcome.filled:
            marks.setdefault(order.decision.ticker, fill_price)
        results.append((order, outcome))
    return results


def record_cycle_review(
    conn: sqlite3.Connection, agent: str, cycle_date: date
) -> None:
    """Record that the agent reviewed its book this cycle (rule 6).

    Not a repair: the weekly packet *is* the review — it puts every open position
    in front of the agent with its thesis, invalidation trigger, exit plan and
    stop, which is exactly what `thesis review` shows a human. Stamped at the
    cycle date, so an agent that skips cycles still trips the 9-day gate.
    """
    existing = conn.execute(
        "SELECT id FROM reviews WHERE book = ? AND review_date = ?",
        (agent, cycle_date.isoformat()),
    ).fetchone()
    if existing:
        return
    journal.record_review(conn, agent, "arena cycle review", cycle_date)


def last_cycle_date(conn: sqlite3.Connection) -> date | None:
    """When a cycle last ran, or None if none ever has.

    Read by the bot on startup to work out whether a scheduled cycle was missed
    while the process was down. A read only — deciding what to do about it is not
    this module's business.
    """
    row = conn.execute(
        "SELECT cycle_date FROM arena_cycles ORDER BY cycle_date DESC, id DESC LIMIT 1"
    ).fetchone()
    return date.fromisoformat(row[0]) if row else None


def start_cycle(conn: sqlite3.Connection, cycle_date: date, model: str) -> int:
    cur = conn.execute(
        "INSERT INTO arena_cycles (cycle_date, model, created_at) VALUES (?,?,?)",
        (cycle_date.isoformat(), model, journal.now_stamp()),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_cycle(
    conn: sqlite3.Connection, cycle_id: int, input_tokens: int, output_tokens: int
) -> None:
    conn.execute(
        "UPDATE arena_cycles SET input_tokens = ?, output_tokens = ? WHERE id = ?",
        (input_tokens, output_tokens, cycle_id),
    )
    conn.commit()


def record_reasoning(
    conn: sqlite3.Connection, cycle_id: int, agent: str, reasoning: str
) -> None:
    conn.execute(
        "INSERT INTO arena_notes (cycle_id, agent, reasoning) VALUES (?,?,?)",
        (cycle_id, agent, reasoning),
    )
    conn.commit()


def last_trade_date(conn: sqlite3.Connection, agent: str) -> date | None:
    row = conn.execute(
        "SELECT MAX(trade_date) FROM trades WHERE book = ?", (agent,)
    ).fetchone()
    return journal.parse_date(row[0]) if row and row[0] else None


# ------------------------------------------------------------------- the report

@dataclass
class Scorecard:
    agent: str
    summary: str
    equity: float
    spy_equity: float
    pnl: float
    pnl_pct: float | None
    spy_pct: float | None
    edge_pp: float | None
    open_positions: int
    closed_trades: int
    decisions: int
    rejected: int

    @property
    def is_human(self) -> bool:
        return self.agent == "you (paper)"


def build_scorecard(
    agent: str,
    summary: str,
    report: track.TrackReport,
    decisions: int = 0,
    rejected: int = 0,
) -> Scorecard:
    acct = report.account
    return Scorecard(
        agent=agent,
        summary=summary,
        equity=acct.equity,
        spy_equity=acct.spy_equity,
        pnl=acct.pnl,
        pnl_pct=acct.pnl_pct,
        spy_pct=acct.spy_pct,
        edge_pp=acct.edge_pp,
        open_positions=len(report.open_lines),
        closed_trades=len(report.closed_lines),
        decisions=decisions,
        rejected=rejected,
    )


def render_scoreboard(cards: Sequence[Scorecard], as_of: date) -> str:
    """The scoreboard: every agent, the human, and SPY — same mirror, same dates."""
    out: list[str] = [f"# Arena scoreboard — {as_of}", ""]
    out.append(
        "Simulated money. Every agent trade passed the same seven account rules as "
        "the human's, and is benchmarked by the same SPY mirror — same dollars, "
        "same dates."
    )
    out.append("")
    out.append(
        "| Agent | Equity | Return | SPY | Edge | Open | Closed | Decisions | Rejected |"
    )
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for card in sorted(cards, key=lambda c: -(c.pnl_pct or float("-inf"))):
        label = f"**{card.agent}**" if card.is_human else card.agent
        out.append(
            f"| {label} | {track.money(card.equity)} | {track.pct(card.pnl_pct)} "
            f"| {track.pct(card.spy_pct)} | {track.points(card.edge_pp)} "
            f"| {card.open_positions} | {card.closed_trades} "
            f"| {card.decisions} | {card.rejected} |"
        )
    out.append("")
    for card in cards:
        if card.summary:
            out.append(f"- **{card.agent}** — {card.summary}")
    out.append("")
    out.append(
        "A rejected decision was refused by a rule and forfeited — it was never "
        "repaired into a legal trade. The decision log below shows each one."
    )
    return "\n".join(out) + "\n"


def decision_log(conn: sqlite3.Connection, agent: str | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT d.*, c.cycle_date FROM arena_decisions d "
        "JOIN arena_cycles c ON c.id = d.cycle_id"
    )
    params: list[object] = []
    if agent:
        sql += " WHERE d.agent = ?"
        params.append(agent)
    sql += " ORDER BY c.cycle_date, d.id"
    return conn.execute(sql, params).fetchall()


def render_decisions(rows: Sequence[sqlite3.Row]) -> str:
    """Every decision with its stated reasoning, and every rejection with its rule."""
    out: list[str] = ["## Decision log", ""]
    if not rows:
        out.append("*No decisions recorded yet.*")
        return "\n".join(out) + "\n"
    for row in rows:
        mark = {"filled": "FILLED", "rejected": "REJECTED", "pending": "PENDING"}.get(
            row["status"], row["status"].upper()
        )
        head = (
            f"### {row['cycle_date']} · {row['agent']} · {row['action']} "
            f"{row['shares']:g} {row['ticker']} — {mark}"
        )
        out.append(head)
        if row["status"] == "filled" and row["fill_price"]:
            out.append(
                f"- Filled at {track.money(row['fill_price'])} on {row['fill_date']} "
                "(next session's open)"
            )
        if row["status"] == "rejected":
            rule = f"rule {row['rule']}: " if row["rule"] else ""
            out.append(f"- **Refused — {rule}{row['reason']}.** Action forfeited.")
        if row["thesis"]:
            out.append(f"- Thesis: {row['thesis']}")
        if row["invalidation"]:
            out.append(f"- Invalidation: {row['invalidation']}")
        if row["exit_plan"]:
            out.append(f"- Exit plan: {row['exit_plan']}")
        if row["outcome_note"]:
            out.append(f"- Outcome: {row['outcome_note']}")
        if row["reasoning"]:
            out.append(f"- Reasoning: {row['reasoning']}")
        out.append("")
    return "\n".join(out)
