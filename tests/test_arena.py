"""The Arena: personas, packets, rule enforcement, fills, isolation, cost.

No network and no API calls — the model client, the price fetcher and the
next-open fetcher are all injected.
"""

from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from thesis import arena, config, journal, track

from conftest import GOOD_EXIT, GOOD_HORIZON, GOOD_INVALIDATION, GOOD_THESIS, d, spy_series

SPY = spy_series({"2026-07-01": 500.0, "2026-07-10": 550.0, "2026-07-20": 600.0})
CYCLE = d("2026-07-10")
FILL_DATE = d("2026-07-13")


@pytest.fixture
def arena_db(tmp_path, monkeypatch):
    """An arena database in a tmp dir, isolated from the human journal."""
    monkeypatch.setenv("THESIS_ARENA_DB", str(tmp_path / "arena.db"))
    monkeypatch.setenv("THESIS_DB", str(tmp_path / "journal.db"))
    conn = arena.connect()
    yield conn
    conn.close()


def buy(**over) -> arena.Decision:
    kwargs = dict(
        agent="value", action="buy", ticker="ACME", shares=10.0, bucket=journal.CORE,
        thesis=GOOD_THESIS, invalidation=GOOD_INVALIDATION, horizon=GOOD_HORIZON,
        exit_plan=GOOD_EXIT, stop_price=None, outcome=None, reasoning="Looks durable.",
    )
    kwargs.update(over)
    return arena.Decision(**kwargs)


# ============================================================== personas

def test_three_personas_with_distinct_mandates() -> None:
    assert set(arena.PERSONAS) == {"value", "momentum", "monk"}
    for name, persona in arena.PERSONAS.items():
        assert persona.name == name
        assert len(persona.system_prompt) > 500
        assert "simulated" in persona.system_prompt.lower()


def test_each_persona_states_its_defining_constraint() -> None:
    assert "years" in arena.VALUE.system_prompt
    assert "core" in arena.VALUE.system_prompt
    assert "stop" in arena.MOMENTUM.system_prompt.lower()
    assert "active" in arena.MOMENTUM.system_prompt
    assert "ONE TRADE PER MONTH" in arena.MONK.system_prompt
    assert "Cash is a legitimate position" in arena.MONK.system_prompt


def test_every_persona_is_told_the_rules_bind_and_are_not_repaired() -> None:
    for persona in arena.PERSONAS.values():
        prompt = persona.system_prompt
        assert "REJECTED" in prompt and "forfeit" in prompt
        assert "no brokerage" in prompt.lower() or "not connected to any brokerage" in prompt


# ============================================================== init

def test_init_creates_and_funds_every_agent(arena_db) -> None:
    created = arena.init(arena_db, cash=100_000.0, on_date=d("2026-07-01"))

    assert sorted(created) == ["momentum", "monk", "value"]
    for name in created:
        deposits = journal.deposits(arena_db, name)
        assert len(deposits) == 1
        assert deposits[0].amount == pytest.approx(100_000.0)


def test_init_is_idempotent(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    assert arena.init(arena_db, on_date=d("2026-07-01")) == []
    assert len(journal.deposits(arena_db, "value")) == 1


# ============================================================== isolation

def test_arena_books_never_touch_the_human_journal(arena_db, tmp_path) -> None:
    """The strongest guarantee here: a different file, not a different column."""
    arena.init(arena_db, on_date=d("2026-07-01"))
    outcome = arena.apply_decision(arena_db, buy(), 100.0, FILL_DATE, {})
    assert outcome.filled

    human = journal.connect()
    try:
        for book in (journal.REAL, journal.PAPER):
            assert journal.deposits(human, book) == ()
            assert journal.holdings(human, book) == ()
        # And no arena book leaked in either.
        for name in arena.PERSONAS:
            assert journal.holdings(human, name) == ()
        assert human.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    finally:
        human.close()

    assert config.arena_db_path() != config.db_path()
    assert arena_db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_the_human_paper_book_is_invisible_to_agents(arena_db) -> None:
    human = journal.connect()
    journal.add_deposit(human, journal.PAPER, 10_000.0, d("2026-07-01"))
    human.close()

    arena.init(arena_db, on_date=d("2026-07-01"))
    assert journal.deposits(arena_db, journal.PAPER) == ()
    assert journal.cash_balance(journal.deposits(arena_db, "value"), []) == pytest.approx(100_000.0)


def test_agents_cannot_fund_each_other(arena_db) -> None:
    arena.init(arena_db, cash=100_000.0, on_date=d("2026-07-01"))
    arena.apply_decision(arena_db, buy(agent="value", shares=100.0), 100.0, FILL_DATE, {})

    assert journal.open_holdings(arena_db, "value")
    assert journal.open_holdings(arena_db, "momentum") == ()
    assert journal.open_holdings(arena_db, "monk") == ()
    momentum_cash = journal.cash_balance(
        journal.deposits(arena_db, "momentum"),
        [t for h in journal.holdings(arena_db, "momentum") for t in h.trades],
    )
    assert momentum_cash == pytest.approx(100_000.0)


def test_no_brokerage_or_execution_dependency_is_reachable() -> None:
    """Agents can never reach a brokerage: the module cannot even import one."""
    source = Path(arena.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    allowed = {
        "__future__", "json", "sqlite3", "dataclasses", "datetime", "pathlib",
        "typing", "pandas", "yfinance", "thesis",
    }
    assert imported <= allowed, f"unexpected imports in arena.py: {imported - allowed}"

    brokers = ("alpaca", "ibapi", "ib_insync", "robinhood", "tda", "schwab", "etrade", "ccxt")
    lowered = source.lower()
    for broker in brokers:
        assert broker not in lowered


# ============================================================== the rules bind

def test_a_valid_buy_is_filled_through_the_journal(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    outcome = arena.apply_decision(arena_db, buy(shares=100.0), 100.0, FILL_DATE, {})

    assert outcome.filled
    assert outcome.fill_price == pytest.approx(100.0)
    held = journal.find_open(arena_db, "value", "ACME")
    assert held is not None
    assert held.shares == pytest.approx(100.0)
    assert held.position.thesis == GOOD_THESIS
    assert held.position.opened_at.endswith("+00:00")  # timestamped like a human trade


@pytest.mark.parametrize(
    "field,value,rule",
    [
        ("thesis", "cheap", 1),
        ("invalidation", "", 1),
        ("horizon", "", 1),
        ("exit_plan", "", 1),
    ],
)
def test_rule_1_rejects_an_incomplete_plan(arena_db, field, value, rule) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    outcome = arena.apply_decision(arena_db, buy(**{field: value}), 100.0, FILL_DATE, {})

    assert outcome.status == "rejected"
    assert outcome.rule == rule
    assert journal.open_holdings(arena_db, "value") == ()


def test_rule_2_rejects_an_active_buy_without_a_stop(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    outcome = arena.apply_decision(
        arena_db, buy(agent="momentum", bucket=journal.ACTIVE, stop_price=None),
        100.0, FILL_DATE, {},
    )
    assert outcome.status == "rejected"
    assert outcome.rule == 2


def test_rule_2_rejects_a_stop_above_the_fill(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    outcome = arena.apply_decision(
        arena_db, buy(agent="momentum", bucket=journal.ACTIVE, stop_price=120.0),
        100.0, FILL_DATE, {},
    )
    assert outcome.status == "rejected"
    assert outcome.rule == 2


def test_rule_3_rejects_an_oversized_position(arena_db) -> None:
    """$100k book, a $30k Core buy is 30% — over the 20% cap."""
    arena.init(arena_db, cash=100_000.0, on_date=d("2026-07-01"))
    outcome = arena.apply_decision(arena_db, buy(shares=300.0), 100.0, FILL_DATE, {})

    assert outcome.status == "rejected"
    assert outcome.rule == 3
    assert "cap" in outcome.reason


def test_rule_4_rejects_leverage(arena_db) -> None:
    arena.init(arena_db, cash=100_000.0, on_date=d("2026-07-01"))
    outcome = arena.apply_decision(arena_db, buy(shares=100_000.0), 100.0, FILL_DATE, {})
    assert outcome.status == "rejected"
    assert outcome.rule in (3, 4)  # too big for the cap and for the cash


def test_rule_4_rejects_an_option_symbol(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    outcome = arena.apply_decision(
        arena_db, buy(ticker="AAPL250117C00200000"), 100.0, FILL_DATE, {}
    )
    assert outcome.status == "rejected"
    assert outcome.rule == 4


def test_rule_4_rejects_shorting_a_name_not_held(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    outcome = arena.apply_decision(
        arena_db, buy(action="sell", ticker="NONE", outcome="Closing out."), 100.0, FILL_DATE, {}
    )
    assert outcome.status == "rejected"
    assert "no open position" in outcome.reason


def test_a_sell_needs_an_honest_outcome_line(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    arena.apply_decision(arena_db, buy(shares=100.0), 100.0, FILL_DATE, {})

    outcome = arena.apply_decision(
        arena_db, buy(action="sell", shares=100.0, outcome="won"), 130.0, d("2026-07-20"), {}
    )
    assert outcome.status == "rejected"
    assert journal.find_open(arena_db, "value", "ACME") is not None  # still held


def test_a_good_sell_closes_the_position(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    arena.apply_decision(arena_db, buy(shares=100.0), 100.0, FILL_DATE, {})

    outcome = arena.apply_decision(
        arena_db,
        buy(action="sell", shares=100.0, outcome="Re-rated on the print; thesis played out."),
        130.0, d("2026-07-20"), {},
    )
    assert outcome.filled
    assert journal.find_open(arena_db, "value", "ACME") is None
    closed = journal.closed_holdings(arena_db, "value")
    assert closed[0].realized_pnl == pytest.approx(3_000.0)


def test_a_rejected_decision_is_never_repaired(arena_db) -> None:
    """The whole point: an illegal decision forfeits, it does not get downgraded."""
    arena.init(arena_db, cash=100_000.0, on_date=d("2026-07-01"))
    before = journal.cash_balance(journal.deposits(arena_db, "value"), [])

    outcome = arena.apply_decision(arena_db, buy(shares=300.0), 100.0, FILL_DATE, {})

    assert outcome.status == "rejected"
    assert journal.holdings(arena_db, "value") == ()
    after_trades = [t for h in journal.holdings(arena_db, "value") for t in h.trades]
    assert journal.cash_balance(journal.deposits(arena_db, "value"), after_trades) == before


def test_rejections_are_logged_with_the_rule_that_refused_them(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    cycle = arena.start_cycle(arena_db, CYCLE, "test-model")
    outcome = arena.apply_decision(arena_db, buy(shares=300.0), 100.0, FILL_DATE, {})
    arena.record_decision(arena_db, cycle, outcome)

    row = arena.decision_log(arena_db)[0]
    assert row["status"] == "rejected"
    assert row["rule"] == 3
    assert row["agent"] == "value"
    assert row["reasoning"] == "Looks durable."
    assert "cap" in row["reason"]


# ============================================================== fills

def frame(rows: dict[str, float]) -> pd.DataFrame:
    index = pd.DatetimeIndex([pd.Timestamp(k) for k in rows])
    return pd.DataFrame({"Open": list(rows.values())}, index=index)


def test_the_fill_is_the_next_sessions_open() -> None:
    fetch = lambda t, start, end: frame({"2026-07-13": 101.5, "2026-07-14": 103.0})
    assert arena.next_session_open("ACME", d("2026-07-10"), fetch) == (d("2026-07-13"), 101.5)


def test_a_session_on_the_cycle_date_itself_is_not_a_fill() -> None:
    """Decisions are made on the close; the fill must be strictly later."""
    fetch = lambda t, start, end: frame({"2026-07-10": 99.0, "2026-07-13": 101.5})
    assert arena.next_session_open("ACME", d("2026-07-10"), fetch) == (d("2026-07-13"), 101.5)


def test_no_next_open_yet_means_no_fill() -> None:
    assert arena.next_session_open("ACME", d("2026-07-10"), lambda *a: frame({})) is None
    assert arena.next_session_open("ACME", d("2026-07-10"), lambda *a: None) is None


def test_a_fetch_failure_does_not_crash_the_cycle() -> None:
    def boom(*args):
        raise RuntimeError("network down")

    assert arena.next_session_open("ACME", d("2026-07-10"), boom) is None


# ============================================================== pending orders

def opens(rows: dict[str, float]):
    """A fetcher returning only sessions inside the requested [start, end) window."""

    def fetch(ticker, start, end):
        inside = {k: v for k, v in rows.items() if start <= d(k) < end}
        return frame(inside)

    return fetch


def place(arena_db, cycle_date: date, decision: arena.Decision) -> int:
    cycle = arena.start_cycle(arena_db, cycle_date, "test-model")
    return arena.record_decision(
        arena_db, cycle, arena.Outcome(decision, "pending", "awaiting open")
    )


def test_a_pending_order_fills_at_the_open_after_its_own_decision_date(arena_db) -> None:
    """Regression: fills used the *current* cycle date, so an old order either
    stayed pending forever or would have filled at a much later open."""
    arena.init(arena_db, on_date=d("2026-07-25"))
    arena.record_cycle_review(arena_db, "value", d("2026-08-01"))
    place(arena_db, d("2026-08-01"), buy(shares=10.0))

    # Sessions exist on the 3rd (the correct fill) and later.
    fetch = opens({"2026-08-03": 101.0, "2026-08-04": 150.0, "2026-08-10": 900.0})
    (order, outcome), = arena.fill_pending(arena_db, {}, fetch)

    assert order.decided_on == d("2026-08-01")
    assert outcome.filled
    assert outcome.fill_date == d("2026-08-03")
    assert outcome.fill_price == pytest.approx(101.0)

    held = journal.find_open(arena_db, "value", "ACME")
    assert held.avg_cost == pytest.approx(101.0)
    assert held.trades[0].trade_date == d("2026-08-03")


def test_the_fill_is_never_the_newest_future_open(arena_db) -> None:
    """Two cycles, two orders: each takes the open after its own date, not a shared one."""
    arena.init(arena_db, on_date=d("2026-07-25"))
    for day in ("2026-08-01", "2026-08-10"):
        arena.record_cycle_review(arena_db, "value", d(day))
    place(arena_db, d("2026-08-01"), buy(ticker="ACME", shares=5.0))
    place(arena_db, d("2026-08-10"), buy(ticker="WILE", shares=5.0))

    fetch = opens({"2026-08-03": 100.0, "2026-08-11": 200.0})
    results = dict(
        (order.decision.ticker, outcome) for order, outcome in arena.fill_pending(arena_db, {}, fetch)
    )

    assert results["ACME"].fill_date == d("2026-08-03")
    assert results["ACME"].fill_price == pytest.approx(100.0)
    assert results["WILE"].fill_date == d("2026-08-11")
    assert results["WILE"].fill_price == pytest.approx(200.0)


def test_an_order_with_no_session_yet_stays_pending(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-25"))
    order_id = place(arena_db, d("2026-08-10"), buy(shares=5.0))

    (_, outcome), = arena.fill_pending(arena_db, {}, opens({}))
    assert outcome.status == "pending"
    assert "no session has opened since 2026-08-10" in outcome.reason

    row = arena_db.execute(
        "SELECT status, fill_price FROM arena_decisions WHERE id = ?", (order_id,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["fill_price"] is None


def test_filling_updates_the_original_row_rather_than_duplicating_it(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-25"))
    arena.record_cycle_review(arena_db, "value", d("2026-08-01"))
    order_id = place(arena_db, d("2026-08-01"), buy(shares=5.0))

    arena.fill_pending(arena_db, {}, opens({"2026-08-03": 100.0}))

    rows = arena_db.execute("SELECT * FROM arena_decisions").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == order_id
    assert rows[0]["status"] == "filled"
    assert rows[0]["fill_date"] == "2026-08-03"
    assert arena.pending_orders(arena_db) == []


def test_orders_fill_oldest_decision_first(arena_db) -> None:
    """A later order must be judged against the position the earlier one created."""
    arena.init(arena_db, on_date=d("2026-07-25"))
    for day in ("2026-08-01", "2026-08-10"):
        arena.record_cycle_review(arena_db, "value", d(day))
    place(arena_db, d("2026-08-10"), buy(shares=5.0))   # inserted first, decided later
    place(arena_db, d("2026-08-01"), buy(shares=5.0))

    order_dates = [o.decided_on for o in arena.pending_orders(arena_db)]
    assert order_dates == [d("2026-08-01"), d("2026-08-10")]


# ================================= pending exposure counts toward the caps

def test_a_duplicate_order_across_cycles_is_rejected_by_the_cap(arena_db) -> None:
    """Regression: splitting one name across two cycles slipped past the 10% cap
    because pending exposure was invisible to the cap check."""
    arena.init(arena_db, cash=100_000.0, on_date=d("2026-07-25"))
    for day in ("2026-08-01", "2026-08-10"):
        arena.record_cycle_review(arena_db, "momentum", d(day))
    # 16 sh then 20 sh of the same Active name at ~$450 = $16,200 — over 10% of $100k.
    place(arena_db, d("2026-08-01"),
          buy(agent="momentum", ticker="AMD", shares=16.0,
              bucket=journal.ACTIVE, stop_price=410.0))
    place(arena_db, d("2026-08-10"),
          buy(agent="momentum", ticker="AMD", shares=20.0,
              bucket=journal.ACTIVE, stop_price=430.0))

    fetch = opens({"2026-08-03": 450.0, "2026-08-11": 450.0})
    results = [outcome for _, outcome in arena.fill_pending(arena_db, {"AMD": 450.0}, fetch)]

    assert results[0].filled, results[0].reason
    assert results[1].status == "rejected"
    assert results[1].rule == 3
    assert "active names cap at 10%" in results[1].reason
    held = journal.find_open(arena_db, "momentum", "AMD")
    assert held.shares == pytest.approx(16.0), "the rejected order must not have been filled"


def test_a_second_order_for_one_name_is_refused_after_the_first_fills(arena_db) -> None:
    """Two orders that each have room alone but not together: the first fills,
    the second is judged against the position it created."""
    arena.init(arena_db, cash=100_000.0, on_date=d("2026-07-25"))
    arena.record_cycle_review(arena_db, "momentum", d("2026-08-01"))
    for _ in range(2):
        place(arena_db, d("2026-08-01"),
              buy(agent="momentum", ticker="AMD", shares=12.0,
                  bucket=journal.ACTIVE, stop_price=410.0))

    fetch = opens({"2026-08-03": 450.0})
    results = [o for _, o in arena.fill_pending(arena_db, {"AMD": 450.0}, fetch)]

    # 12 x 450 = $5,400 each; one clears the 10% cap, $10,800 together does not.
    assert results[0].filled
    assert results[1].status == "rejected"
    assert results[1].rule == 3
    assert journal.find_open(arena_db, "momentum", "AMD").shares == pytest.approx(12.0)


def test_an_older_unfillable_order_still_ties_up_that_names_cap(arena_db) -> None:
    """An earlier order that cannot resolve is a live commitment in the same name,
    so a later order in that name is judged against it."""
    arena.init(arena_db, cash=100_000.0, on_date=d("2026-06-25"))
    for day in ("2026-07-01", "2026-08-01"):
        arena.record_cycle_review(arena_db, "momentum", d(day))
    # Decided in July; no session in its window, so it never resolves.
    place(arena_db, d("2026-07-01"),
          buy(agent="momentum", ticker="AMD", shares=12.0,
              bucket=journal.ACTIVE, stop_price=410.0))
    place(arena_db, d("2026-08-01"),
          buy(agent="momentum", ticker="AMD", shares=12.0,
              bucket=journal.ACTIVE, stop_price=410.0))

    fetch = opens({"2026-08-03": 450.0})
    results = [o for _, o in arena.fill_pending(arena_db, {"AMD": 450.0}, fetch)]

    assert results[0].status == "pending"          # July order, still unresolvable
    assert results[1].status == "rejected", "the live commitment must count"
    assert results[1].rule == 3
    assert "10.8%" in results[1].reason            # 5,400 committed + 5,400 new


def test_a_later_order_never_reserves_against_an_earlier_fill(arena_db) -> None:
    """Regression: a fill on the 3rd was charged for capital committed on the 10th,
    so the whole of cycle 1 was rejected by a decision that did not exist yet."""
    arena.init(arena_db, cash=100_000.0, on_date=d("2026-07-25"))
    for day in ("2026-08-01", "2026-08-10"):
        arena.record_cycle_review(arena_db, "momentum", d(day))
    place(arena_db, d("2026-08-01"),
          buy(agent="momentum", ticker="AMD", shares=16.0,
              bucket=journal.ACTIVE, stop_price=410.0))
    place(arena_db, d("2026-08-10"),   # decided later; cannot resolve in this pass
          buy(agent="momentum", ticker="AMD", shares=20.0,
              bucket=journal.ACTIVE, stop_price=430.0))

    fetch = opens({"2026-08-03": 450.0})
    results = [o for _, o in arena.fill_pending(arena_db, {"AMD": 450.0}, fetch)]

    # 16 x 450 = $7,200, comfortably inside the 10% cap on its own.
    assert results[0].filled, results[0].reason
    assert results[0].fill_date == d("2026-08-03")
    assert results[1].status == "pending"
    # And the later order is caught next pass, against the position this created.
    later = opens({"2026-08-03": 450.0, "2026-08-11": 450.0})
    tail = [o for _, o in arena.fill_pending(arena_db, {"AMD": 450.0}, later)]
    assert tail[0].status == "rejected"
    assert tail[0].rule == 3


def test_reserved_against_prices_siblings_and_skips_the_excluded_order() -> None:
    orders = [
        arena.PendingOrder(1, "value", d("2026-08-01"), buy(ticker="AAA", shares=10.0)),
        arena.PendingOrder(2, "value", d("2026-08-01"), buy(ticker="BBB", shares=4.0)),
        arena.PendingOrder(3, "value", d("2026-08-01"), buy(ticker="CCC", shares=2.0)),
    ]
    fills = {1: (d("2026-08-03"), 100.0), 2: None, 3: None}
    cash, per_ticker = arena.reserved_against(
        orders, fills, {"BBB": 50.0}, exclude=1
    )

    assert per_ticker == {"BBB": 200.0}          # priced at the last close
    assert "CCC" not in per_ticker               # unpriceable reserves nothing
    assert cash == pytest.approx(200.0)


def test_a_sell_order_reserves_nothing() -> None:
    orders = [
        arena.PendingOrder(
            1, "value", d("2026-08-01"),
            buy(action="sell", ticker="AAA", shares=10.0, outcome="Closing per plan."),
        )
    ]
    cash, per_ticker = arena.reserved_against(orders, {}, {"AAA": 100.0}, exclude=99)
    assert cash == 0.0 and per_ticker == {}


def test_a_filled_order_is_marked_so_later_orders_can_be_valued(arena_db) -> None:
    """Regression: after a fill the new holding had no price, so computing equity
    for the next order raised and the order was rejected for the wrong reason."""
    arena.init(arena_db, cash=100_000.0, on_date=d("2026-07-25"))
    arena.record_cycle_review(arena_db, "value", d("2026-08-01"))
    place(arena_db, d("2026-08-01"), buy(ticker="ACME", shares=5.0))
    place(arena_db, d("2026-08-01"), buy(ticker="WILE", shares=5.0))

    # No prices supplied at all — the pass must mark each fill at its own open.
    fetch = opens({"2026-08-03": 100.0})
    results = [o for _, o in arena.fill_pending(arena_db, {}, fetch)]

    assert all(o.filled for o in results), [o.reason for o in results]
    assert journal.find_open(arena_db, "value", "WILE") is not None


def test_the_human_journal_is_unaffected_by_the_reservation_parameters(conn) -> None:
    """A human logs a fill after the fact — there is nothing pending to reserve."""
    journal.add_deposit(conn, journal.REAL, 10_000.0, d("2026-07-01"))
    from conftest import buy_request

    plain = journal.validate_buy(
        buy_request(shares=10.0, price=100.0), journal.load_state(conn, journal.REAL), {}
    )
    explicit = journal.validate_buy(
        buy_request(shares=10.0, price=100.0), journal.load_state(conn, journal.REAL), {},
        reserved_cash=0.0, reserved_exposure={},
    )
    assert plain.cash == explicit.cash
    assert plain.position_pct == explicit.position_pct


def test_reserving_cash_can_trip_the_no_margin_rule(conn) -> None:
    journal.add_deposit(conn, journal.REAL, 1_000.0, d("2026-07-01"))
    from conftest import buy_request

    request = buy_request(shares=1.0, price=150.0)
    journal.validate_buy(request, journal.load_state(conn, journal.REAL), {})  # fine
    with pytest.raises(journal.RuleViolation) as exc:
        journal.validate_buy(
            request, journal.load_state(conn, journal.REAL), {}, reserved_cash=900.0
        )
    assert exc.value.rule == 4


# ============================================================== packet: pending

def test_the_packet_lists_the_agents_pending_orders(arena_db) -> None:
    """Regression: momentum re-bought AMD/DELL/PANW in cycle 2 because its packet
    said it was entirely in cash."""
    arena.init(arena_db, on_date=d("2026-07-25"))
    order = arena.PendingOrder(
        1, "momentum", d("2026-08-01"),
        buy(agent="momentum", ticker="AMD", shares=16.0,
            bucket=journal.ACTIVE, stop_price=410.0),
    )
    packet = make_packet(
        arena_db, "momentum", prices={"AMD": 450.0}, pending=[order]
    )

    assert "YOUR PENDING ORDERS" in packet
    assert "BUY 16 AMD — decided 2026-08-01" in packet
    assert "$7,200.00" in packet  # 16 x 450, so it can see the capital committed
    assert "DO NOT order the same name again" in packet
    assert "counted together" in packet


def test_the_packet_says_so_when_nothing_is_pending(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-25"))
    packet = make_packet(arena_db)
    assert "None. Every order you have placed has been resolved." in packet


def test_pending_orders_are_scoped_to_the_asking_agent(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-25"))
    place(arena_db, d("2026-08-01"), buy(agent="momentum", ticker="AMD", shares=5.0,
                                         bucket=journal.ACTIVE, stop_price=400.0))
    place(arena_db, d("2026-08-01"), buy(agent="value", ticker="ACME", shares=5.0))

    assert [o.decision.ticker for o in arena.pending_orders(arena_db, "momentum")] == ["AMD"]
    assert [o.decision.ticker for o in arena.pending_orders(arena_db, "value")] == ["ACME"]
    assert len(arena.pending_orders(arena_db)) == 2


# ======================================== the cycle counts as the weekly review

def test_a_cycle_records_the_weekly_review_at_the_cycle_date(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-25"))
    arena.record_cycle_review(arena_db, "value", d("2026-08-01"))

    review = journal.last_review(arena_db, "value")
    assert review is not None
    assert review.review_date == d("2026-08-01")
    assert journal.last_review(arena_db, "momentum") is None  # per agent


def test_recording_the_same_cycle_review_twice_is_a_no_op(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-25"))
    arena.record_cycle_review(arena_db, "value", d("2026-08-01"))
    arena.record_cycle_review(arena_db, "value", d("2026-08-01"))
    count = arena_db.execute(
        "SELECT COUNT(*) FROM reviews WHERE book = 'value'"
    ).fetchone()[0]
    assert count == 1


def test_an_agent_that_skips_cycles_still_trips_the_review_gate(arena_db) -> None:
    """Rule 6 stays real: the cycle review is stamped at the cycle date, not today."""
    arena.init(arena_db, on_date=d("2026-07-01"))
    arena.record_cycle_review(arena_db, "value", d("2026-07-01"))
    place(arena_db, d("2026-07-01"), buy(shares=5.0))
    arena.fill_pending(arena_db, {}, opens({"2026-07-02": 100.0}))
    assert journal.find_open(arena_db, "value", "ACME") is not None

    # Next order comes 40 days later with no review in between.
    place(arena_db, d("2026-08-10"), buy(ticker="WILE", shares=5.0))
    results = [o for _, o in arena.fill_pending(arena_db, {"ACME": 100.0},
                                                opens({"2026-08-11": 100.0}))]
    assert results[0].status == "rejected"
    assert results[0].rule == 6


# ============================================================== cost estimate

def test_cost_estimate_prices_a_cycle() -> None:
    estimate = arena.estimate_cost([100_000, 100_000, 100_000], "claude-opus-4-8")

    assert estimate.input_tokens == 300_000
    assert estimate.output_tokens == 3 * arena.ASSUMED_OUTPUT_TOKENS
    assert estimate.input_cost == pytest.approx(300_000 / 1e6 * 5.00)
    assert estimate.output_cost == pytest.approx(3_600 / 1e6 * 25.00)
    assert estimate.total == pytest.approx(1.59)
    assert "$1.59" in estimate.render()


def test_cost_estimate_says_so_when_the_model_is_not_priced() -> None:
    estimate = arena.estimate_cost([1_000], "some-future-model")
    assert estimate.priced is False
    assert estimate.total == 0.0
    assert "cost unknown" in estimate.render()


def test_token_counting_uses_the_api_when_available() -> None:
    class Client:
        class messages:
            @staticmethod
            def count_tokens(**kwargs):
                return type("R", (), {"input_tokens": 4242})()

    assert arena.count_tokens(Client(), "m", "system", "packet") == 4242


def test_token_counting_falls_back_rather_than_failing() -> None:
    class Broken:
        class messages:
            @staticmethod
            def count_tokens(**kwargs):
                raise RuntimeError("offline")

    assert arena.count_tokens(Broken(), "m", "x" * 350, "y" * 350) == 200


# ============================================================== the packet

def make_packet(arena_db, agent: str = "value", **over) -> str:
    kwargs = dict(
        agent=agent,
        holdings=journal.holdings(arena_db, agent),
        deposits=journal.deposits(arena_db, agent),
        prices={"ACME": 130.0},
        screen_table="| # | Ticker |\n|---|---|\n| 1 | ACME |",
        briefs={"ACME": "# ACME — Research Brief\n\nBusiness: rocket skates."},
        as_of=CYCLE,
        pending=(),
    )
    kwargs.update(over)
    return arena.build_packet(**kwargs)


def test_the_packet_carries_account_screen_and_briefs(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    packet = make_packet(arena_db)

    assert "WEEKLY PACKET" in packet
    assert "$100,000.00" in packet
    assert "YOUR OPEN POSITIONS" in packet
    assert "None. You are entirely in cash." in packet
    assert "SCREEN" in packet and "ACME" in packet
    assert "Brief: ACME" in packet and "rocket skates" in packet


def test_the_packet_states_the_caps_in_dollars(arena_db) -> None:
    arena.init(arena_db, cash=100_000.0, on_date=d("2026-07-01"))
    packet = make_packet(arena_db)
    assert "Core $20,000.00 per name" in packet
    assert "Active $10,000.00 per name" in packet


def test_the_packet_shows_open_positions_with_their_theses(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    arena.apply_decision(arena_db, buy(shares=100.0), 100.0, FILL_DATE, {})
    packet = make_packet(arena_db)

    assert "ACME (core): 100 sh @ $100.00 cost" in packet
    assert "now $130.00" in packet
    assert GOOD_THESIS in packet
    assert f"invalidation: {GOOD_INVALIDATION}" in packet


def test_the_packet_tells_the_monk_when_it_last_traded(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    assert "You have not traded yet." in make_packet(arena_db, "monk")
    packet = make_packet(arena_db, "monk", last_trade=d("2026-07-01"))
    assert "most recent trade was 2026-07-01 (9 days ago)" in packet


def test_every_agent_gets_an_identical_screen_and_brief_section(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    packets = {a: make_packet(arena_db, a) for a in arena.PERSONAS}

    shared = {a: p.split("## SCREEN", 1)[1] for a, p in packets.items()}
    assert len(set(shared.values())) == 1, "agents saw different screens or briefs"


def test_briefs_are_loaded_from_disk_newest_first(tmp_path) -> None:
    (tmp_path / "ACME_2026-07-01.md").write_text("old", encoding="utf-8")
    (tmp_path / "ACME_2026-07-09.md").write_text("new", encoding="utf-8")
    (tmp_path / "WILE_2026-07-09.md").write_text("wile", encoding="utf-8")

    found = arena.briefs_for(["ACME", "MISSING"], tmp_path)
    assert found == {"ACME": "new"}


# ============================================================== the model call

class FakeClient:
    """Stands in for the Anthropic client; records the request it was given."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.seen: dict = {}
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.seen = kwargs
            text = json.dumps(self.outer.payload)
            block = type("B", (), {"type": "text", "text": text})()
            usage = type("U", (), {"input_tokens": 1000, "output_tokens": 200})()
            return type("R", (), {"content": [block], "usage": usage})()


def test_the_agent_is_asked_for_schema_constrained_json() -> None:
    client = FakeClient({"reasoning": "Quiet week.", "decisions": []})
    answer = arena.ask_agent(client, arena.VALUE, "packet", "claude-opus-4-8")

    assert answer.reasoning == "Quiet week."
    assert answer.decisions == []
    fmt = client.seen["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"] is arena.DECISION_SCHEMA
    assert client.seen["system"] == arena.VALUE.system_prompt
    assert client.seen["thinking"] == {"type": "adaptive"}


def test_decisions_are_parsed_into_typed_records() -> None:
    client = FakeClient({
        "reasoning": "Adding a name.",
        "decisions": [{
            "action": "BUY", "ticker": "acme", "shares": 10, "bucket": "Core",
            "thesis": GOOD_THESIS, "invalidation": GOOD_INVALIDATION,
            "horizon": GOOD_HORIZON, "exit_plan": GOOD_EXIT,
            "stop_price": None, "outcome": None, "reasoning": "cheap",
        }],
    })
    answer = arena.ask_agent(client, arena.VALUE, "packet", "claude-opus-4-8")

    decision = answer.decisions[0]
    assert decision.action == "buy"       # normalized
    assert decision.ticker == "ACME"      # normalized
    assert decision.bucket == "core"      # normalized
    assert decision.stop_price is None
    assert answer.input_tokens == 1000


def test_an_absurd_number_of_decisions_is_capped() -> None:
    many = [{"action": "buy", "ticker": f"A{i}", "shares": 1, "bucket": "core",
             "thesis": "x", "invalidation": "y", "horizon": "z", "exit_plan": "w",
             "stop_price": None, "outcome": None, "reasoning": "r"} for i in range(50)]
    client = FakeClient({"reasoning": "busy", "decisions": many})
    answer = arena.ask_agent(client, arena.VALUE, "packet", "claude-opus-4-8")
    assert len(answer.decisions) == arena.MAX_DECISIONS_PER_CYCLE


def test_the_schema_requires_the_whole_written_plan() -> None:
    item = arena.DECISION_SCHEMA["properties"]["decisions"]["items"]
    for field in ("thesis", "invalidation", "horizon", "exit_plan", "stop_price"):
        assert field in item["required"]
    assert item["additionalProperties"] is False
    assert item["properties"]["action"]["enum"] == ["buy", "sell"]


# ============================================================== the scoreboard

def test_the_scoreboard_ranks_agents_against_spy_and_the_human(arena_db) -> None:
    arena.init(arena_db, cash=10_000.0, on_date=d("2026-07-01"))
    arena.apply_decision(arena_db, buy(shares=10.0), 100.0, d("2026-07-10"), {})

    cards = []
    for name in arena.agents(arena_db):
        report = track.build_report(
            name, journal.deposits(arena_db, name), journal.holdings(arena_db, name),
            {"ACME": 130.0}, SPY, as_of=d("2026-07-20"),
        )
        cards.append(arena.build_scorecard(name, arena.PERSONAS[name].summary, report))

    text = arena.render_scoreboard(cards, d("2026-07-20"))
    assert "Arena scoreboard" in text
    assert "| value |" in text
    assert "Edge" in text and "SPY" in text
    for name in arena.PERSONAS:
        assert name in text


def test_the_scoreboard_marks_the_human_book(arena_db) -> None:
    report = track.build_report(
        journal.PAPER, [journal.Deposit(1, journal.PAPER, d("2026-07-01"), 10_000.0, "")],
        [], {}, SPY, as_of=d("2026-07-20"),
    )
    card = arena.build_scorecard("you (paper)", "your own paper book", report)
    assert card.is_human
    assert "**you (paper)**" in arena.render_scoreboard([card], d("2026-07-20"))


def test_the_decision_log_shows_reasoning_and_forfeits(arena_db) -> None:
    arena.init(arena_db, on_date=d("2026-07-01"))
    cycle = arena.start_cycle(arena_db, CYCLE, "m")
    arena.record_decision(
        arena_db, cycle, arena.apply_decision(arena_db, buy(shares=300.0), 100.0, FILL_DATE, {})
    )
    arena.record_decision(
        arena_db, cycle, arena.apply_decision(arena_db, buy(shares=10.0), 100.0, FILL_DATE, {})
    )

    text = arena.render_decisions(arena.decision_log(arena_db))
    assert "REJECTED" in text and "forfeited" in text and "rule 3" in text
    assert "FILLED" in text and "next session's open" in text
    assert "Looks durable." in text
    assert GOOD_INVALIDATION in text


def test_the_decision_log_is_empty_before_any_cycle(arena_db) -> None:
    assert "No decisions recorded yet" in arena.render_decisions(arena.decision_log(arena_db))
