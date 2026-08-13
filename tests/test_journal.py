"""The 7 account rules, plus every dollar of money math they depend on.

A P&L bug invalidates the entire track record, so the arithmetic is tested as
carefully as the rules.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from thesis import journal
from thesis.journal import RuleViolation

from conftest import (
    GOOD_EXIT,
    GOOD_HORIZON,
    GOOD_INVALIDATION,
    GOOD_THESIS,
    buy_request,
    d,
    deposit,
    holding,
    state,
    trade,
)


# =========================================================== money math

def test_holding_walks_average_cost() -> None:
    """Two buys at different prices, then two sells — average-cost accounting."""
    h = holding(
        [
            trade("buy", 10, 100.0, "2026-07-01", id=1),
            trade("buy", 10, 120.0, "2026-07-05", id=2),
            trade("sell", 5, 130.0, "2026-07-10", id=3),
        ]
    )
    assert h.shares == pytest.approx(15.0)
    assert h.avg_cost == pytest.approx(110.0)
    assert h.realized_pnl == pytest.approx(5 * (130.0 - 110.0))
    assert h.invested == pytest.approx(2200.0)
    assert h.proceeds == pytest.approx(650.0)
    assert h.cost_basis == pytest.approx(1650.0)
    # Unrealized at $115: 15 shares over an average cost of $110.
    assert h.unrealized_pnl(115.0) == pytest.approx(75.0)
    assert h.total_pnl(115.0) == pytest.approx(175.0)


def test_holding_fully_closed_pnl_is_proceeds_minus_invested() -> None:
    h = holding(
        [
            trade("buy", 10, 100.0, "2026-07-01", id=1),
            trade("buy", 10, 120.0, "2026-07-05", id=2),
            trade("sell", 5, 130.0, "2026-07-10", id=3),
            trade("sell", 15, 90.0, "2026-07-15", id=4),
        ],
        status="closed",
    )
    assert h.shares == 0.0
    assert h.total_pnl(0.0) == pytest.approx(2000.0 - 2200.0)
    assert h.realized_pnl == pytest.approx(-200.0)
    assert h.return_pct(0.0) == pytest.approx(-200.0 / 2200.0)
    assert h.exit_date == d("2026-07-15")
    assert h.held_days() == 14


def test_holding_orders_trades_chronologically_regardless_of_input() -> None:
    out_of_order = holding(
        [
            trade("sell", 5, 130.0, "2026-07-10", id=3),
            trade("buy", 10, 100.0, "2026-07-01", id=1),
        ]
    )
    assert out_of_order.shares == pytest.approx(5.0)
    assert out_of_order.realized_pnl == pytest.approx(150.0)


def test_realized_and_return_helpers() -> None:
    assert journal.realized_pnl(10, 100.0, 110.0) == pytest.approx(100.0)
    assert journal.return_pct(100.0, 110.0) == pytest.approx(0.10)
    assert journal.cost_basis(2.5, 40.0) == pytest.approx(100.0)
    with pytest.raises(ValueError):
        journal.return_pct(0.0, 10.0)


def test_cash_balance_counts_deposits_buys_and_sells() -> None:
    deposits = [deposit(1000.0, "2026-07-01"), deposit(-200.0, "2026-07-02", id=2)]
    trades = [
        trade("buy", 2, 100.0, "2026-07-03", id=1),
        trade("sell", 1, 150.0, "2026-07-04", id=2),
    ]
    assert journal.cash_balance(deposits, trades) == pytest.approx(1000 - 200 - 200 + 150)


def test_equity_is_cash_plus_open_positions_at_market() -> None:
    deposits = [deposit(1000.0)]
    open_h = holding([trade("buy", 4, 100.0, "2026-07-02")])
    value = journal.equity(deposits, [open_h], {"ACME": 150.0})
    assert value == pytest.approx(600.0 + 4 * 150.0)


def test_equity_refuses_to_value_a_position_with_no_price() -> None:
    open_h = holding([trade("buy", 4, 100.0, "2026-07-02")])
    with pytest.raises(ValueError, match="no current price"):
        journal.equity([deposit(1000.0)], [open_h], {})


def test_win_rate_and_expectancy() -> None:
    pnls = [100.0, -50.0, 25.0, -25.0]
    assert journal.win_rate(pnls) == pytest.approx(0.5)
    assert journal.expectancy(pnls) == pytest.approx(12.5)
    assert journal.average_win(pnls) == pytest.approx(62.5)
    assert journal.average_loss(pnls) == pytest.approx(-37.5)
    assert journal.win_rate([]) is None
    assert journal.expectancy([]) is None
    # A scratch trade is not a win.
    assert journal.win_rate([0.0]) == pytest.approx(0.0)


# =========================================================== rule 1

def test_rule_1_refuses_a_buy_with_no_thesis() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_written_plan(buy_request(thesis=""))
    assert exc.value.rule == 1


def test_rule_1_refuses_a_one_word_thesis() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_written_plan(buy_request(thesis="good company"))
    assert exc.value.rule == 1
    assert "written argument" in exc.value.message


def test_rule_1_refuses_whitespace_padding() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_written_plan(buy_request(thesis="   " * 30))
    assert exc.value.rule == 1


@pytest.mark.parametrize("field", ["invalidation", "horizon", "exit_plan"])
def test_rule_1_requires_invalidation_horizon_and_exit_plan(field: str) -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_written_plan(buy_request(**{field: ""}))
    assert exc.value.rule == 1


def test_rule_1_passes_with_the_full_written_plan() -> None:
    journal.check_written_plan(
        buy_request(
            thesis=GOOD_THESIS,
            invalidation=GOOD_INVALIDATION,
            horizon=GOOD_HORIZON,
            exit_plan=GOOD_EXIT,
        )
    )


def test_rule_1_is_enforced_end_to_end_by_validate_buy() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.validate_buy(
            buy_request(thesis="meh"), state([deposit(10_000.0)]), {}, d("2026-07-20")
        )
    assert exc.value.rule == 1


# =========================================================== rule 2

def test_rule_2_active_trade_without_a_stop_is_refused() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_stop(buy_request(bucket=journal.ACTIVE, stop_price=None))
    assert exc.value.rule == 2


def test_rule_2_stop_at_or_above_entry_is_refused() -> None:
    for stop in (100.0, 105.0):
        with pytest.raises(RuleViolation) as exc:
            journal.check_stop(buy_request(bucket=journal.ACTIVE, price=100.0, stop_price=stop))
        assert exc.value.rule == 2


def test_rule_2_active_trade_with_a_real_stop_passes() -> None:
    journal.check_stop(buy_request(bucket=journal.ACTIVE, price=100.0, stop_price=92.0))


def test_rule_2_core_needs_no_stop() -> None:
    journal.check_stop(buy_request(bucket=journal.CORE, stop_price=None))


# =========================================================== rule 3

def test_rule_3_core_name_caps_at_20_percent() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_position_cap(journal.CORE, 0.0, 210.0, 1000.0)
    assert exc.value.rule == 3
    assert "20%" in exc.value.message


def test_rule_3_core_at_exactly_20_percent_is_allowed() -> None:
    assert journal.check_position_cap(journal.CORE, 0.0, 200.0, 1000.0) == pytest.approx(0.20)


def test_rule_3_active_name_caps_at_10_percent() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_position_cap(journal.ACTIVE, 0.0, 110.0, 1000.0)
    assert exc.value.rule == 3
    assert journal.check_position_cap(journal.ACTIVE, 0.0, 100.0, 1000.0) == pytest.approx(0.10)


def test_rule_3_counts_shares_already_held_in_the_same_name() -> None:
    """A 15% position plus another 10% is a 25% position, cap or no cap."""
    with pytest.raises(RuleViolation) as exc:
        journal.check_position_cap(journal.CORE, 150.0, 100.0, 1000.0)
    assert exc.value.rule == 3


def test_rule_3_sizing_against_an_empty_account_is_refused() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_position_cap(journal.CORE, 0.0, 100.0, 0.0)
    assert exc.value.rule == 3
    assert "deposit" in exc.value.message


def test_rule_3_runs_inside_validate_buy_against_live_equity() -> None:
    """$1,000 account, 3 shares at $100 = 30% of it — refused for a Core name."""
    with pytest.raises(RuleViolation) as exc:
        journal.validate_buy(
            buy_request(shares=3.0, price=100.0),
            state([deposit(1000.0)]),
            {},
            d("2026-07-20"),
        )
    assert exc.value.rule == 3


def test_rule_3_add_on_to_an_existing_holding_is_measured_at_market() -> None:
    """Holding $180 of a name in a $1,000 book leaves $20 of Core headroom."""
    held = holding([trade("buy", 2, 90.0, "2026-07-05")])  # marked at $90 -> $180
    book = state([deposit(1000.0)], [held], last_review=d("2026-07-19"))
    prices = {"ACME": 90.0}

    check = journal.validate_buy(
        buy_request(shares=0.2, price=100.0), book, prices, d("2026-07-20")
    )
    assert check.position_pct == pytest.approx(0.20)

    with pytest.raises(RuleViolation) as exc:
        journal.validate_buy(
            buy_request(shares=0.5, price=100.0), book, prices, d("2026-07-20")
        )
    assert exc.value.rule == 3


# =========================================================== rule 4

@pytest.mark.parametrize(
    "symbol",
    ["AAPL240119C00190000", "AAPL 240119C00190", "SPY_CALL", "TOOLONGSYM", "aapl calls"],
)
def test_rule_4_refuses_anything_that_is_not_a_plain_equity_symbol(symbol: str) -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_instrument(buy_request(ticker=symbol))
    assert exc.value.rule == 4


def test_rule_4_allows_ordinary_and_share_class_symbols() -> None:
    for symbol in ("AAPL", "F", "BRK-B", "BF.B"):
        journal.check_instrument(buy_request(ticker=symbol))


@pytest.mark.parametrize("shares", [0.0, -1.0, -0.5])
def test_rule_4_refuses_zero_or_negative_shares_no_shorting(shares: float) -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_instrument(buy_request(shares=shares))
    assert exc.value.rule == 4


def test_rule_4_no_margin_a_buy_cannot_exceed_cash() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_no_margin(cost=500.0, cash=400.0)
    assert exc.value.rule == 4
    assert "No margin" in exc.value.message
    journal.check_no_margin(cost=400.0, cash=400.0)


def test_rule_4_margin_check_runs_inside_validate_buy() -> None:
    """A $10,000 buy in a $100 book is refused for cash before anything else."""
    with pytest.raises(RuleViolation) as exc:
        journal.validate_buy(
            buy_request(shares=100.0, price=100.0),
            state([deposit(100.0)]),
            {},
            d("2026-07-20"),
        )
    assert exc.value.rule == 4


def test_rule_4_cannot_sell_more_shares_than_held_no_shorting() -> None:
    held = holding([trade("buy", 3, 100.0, "2026-07-01")])
    with pytest.raises(RuleViolation) as exc:
        journal.validate_sell(held, shares=4.0, price=110.0, outcome="Sold the whole lot.")
    assert exc.value.rule == 4
    journal.validate_sell(held, shares=3.0, price=110.0, outcome="Sold the whole lot.")


def test_a_close_requires_an_outcome_line() -> None:
    held = holding([trade("buy", 3, 100.0, "2026-07-01")])
    with pytest.raises(RuleViolation) as exc:
        journal.validate_sell(held, shares=3.0, price=110.0, outcome="ok")
    assert exc.value.rule is None
    assert "what actually happened" in exc.value.message


# =========================================================== rule 5

def test_next_business_day_skips_the_weekend() -> None:
    assert journal.next_business_day(d("2026-07-24")) == d("2026-07-27")  # Fri -> Mon
    assert journal.next_business_day(d("2026-07-20")) == d("2026-07-21")  # Mon -> Tue


def test_settlement_is_t_plus_one() -> None:
    assert journal.settlement_date(d("2026-07-20")) == d("2026-07-21")


def test_unsettled_proceeds_only_counts_sales_not_yet_settled() -> None:
    trades = [
        trade("sell", 2, 100.0, "2026-07-20", id=1),  # settles 07-21
        trade("sell", 1, 100.0, "2026-07-17", id=2),  # settles 07-20
    ]
    assert journal.unsettled_proceeds(trades, d("2026-07-20")) == pytest.approx(200.0)
    assert journal.unsettled_proceeds(trades, d("2026-07-21")) == pytest.approx(0.0)


def test_settled_cash_excludes_unsettled_proceeds() -> None:
    deposits = [deposit(100.0, "2026-07-01")]
    trades = [
        trade("buy", 1, 100.0, "2026-07-02", id=1),
        trade("sell", 1, 150.0, "2026-07-20", id=2),
    ]
    assert journal.cash_balance(deposits, trades) == pytest.approx(150.0)
    assert journal.settled_cash(deposits, trades, d("2026-07-20")) == pytest.approx(0.0)
    assert journal.settled_cash(deposits, trades, d("2026-07-21")) == pytest.approx(150.0)


def _sold_out_book(last_review: str) -> journal.BookState:
    """$100 in, nearly all of it spent, then sold at $150 on 2026-07-20.

    Cash is $155 but only $5 of it has settled, so a $30 buy — comfortably
    inside the 20% Core cap on $155 of equity — still reaches into proceeds.
    """
    sold = holding(
        [
            trade("buy", 1, 95.0, "2026-07-02", id=1),
            trade("sell", 1, 150.0, "2026-07-20", id=2),
        ],
        status="closed",
    )
    return state([deposit(100.0, "2026-07-01")], [sold], last_review=d(last_review))


def test_rule_5_warns_when_a_buy_reaches_into_unsettled_funds() -> None:
    """Sell today, buy today with the proceeds: allowed, but warned about."""
    check = journal.validate_buy(
        buy_request(shares=0.2, price=150.0, trade_date=d("2026-07-20")),
        _sold_out_book("2026-07-20"),
        {},
        d("2026-07-20"),
    )
    assert check.cash == pytest.approx(155.0)
    assert check.settled == pytest.approx(5.0)
    assert check.warnings
    assert "unsettled" in check.warnings[0]
    assert "good-faith" in check.warnings[0]
    assert "settles 2026-07-21" in check.warnings[0]


def test_rule_5_is_silent_once_the_funds_have_settled() -> None:
    check = journal.validate_buy(
        buy_request(shares=0.2, price=150.0, trade_date=d("2026-07-21")),
        _sold_out_book("2026-07-21"),
        {},
        d("2026-07-21"),
    )
    assert check.settled == pytest.approx(155.0)
    assert check.warnings == ()


def test_rule_5_warns_but_never_blocks() -> None:
    """The warning is advisory — the trade still validates."""
    warning = journal.unsettled_warning(
        cost=140.0,
        settled=0.0,
        trades=[trade("sell", 1, 150.0, "2026-07-20")],
        as_of=d("2026-07-20"),
    )
    assert warning is not None
    assert journal.unsettled_warning(140.0, 200.0, [], d("2026-07-20")) is None


# =========================================================== rule 6

def test_rule_6_blocks_a_buy_when_the_last_review_is_stale() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_review_gate(1, d("2026-07-10"), d("2026-07-20"))  # 10 days
    assert exc.value.rule == 6
    assert "review" in exc.value.message


def test_rule_6_allows_a_buy_at_nine_days() -> None:
    journal.check_review_gate(1, d("2026-07-11"), d("2026-07-20"))  # 9 days


def test_rule_6_blocks_when_positions_are_held_and_no_review_ever_ran() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_review_gate(2, None, d("2026-07-20"))
    assert exc.value.rule == 6


def test_rule_6_does_not_trap_the_first_ever_buy() -> None:
    """Nothing is held, so there is nothing to review yet."""
    journal.check_review_gate(0, None, d("2026-07-20"))


def test_rule_6_gate_runs_inside_validate_buy() -> None:
    held = holding([trade("buy", 1, 10.0, "2026-07-01")])
    with pytest.raises(RuleViolation) as exc:
        journal.validate_buy(
            buy_request(),
            state([deposit(10_000.0)], [held], last_review=d("2026-07-01")),
            {"ACME": 10.0},
            d("2026-07-20"),
        )
    assert exc.value.rule == 6


def test_review_status_reports_days_remaining_before_the_block() -> None:
    status = journal.review_status(1, d("2026-07-13"), d("2026-07-20"))
    assert status.days_since == 7
    assert status.days_remaining == 2
    assert status.state == "due_soon"
    assert status.needs_notice is True
    assert status.blocking is False


@pytest.mark.parametrize(
    "review_day,expected",
    [
        ("2026-07-20", "ok"),        # today
        ("2026-07-16", "ok"),        # 4 days, 5 remaining
        ("2026-07-14", "ok"),        # 6 days, 3 remaining
        ("2026-07-13", "due_soon"),  # 7 days, 2 remaining
        ("2026-07-11", "due_soon"),  # 9 days, 0 remaining — last legal day
        ("2026-07-10", "overdue"),   # 10 days
    ],
)
def test_review_status_transitions_on_the_right_day(review_day: str, expected: str) -> None:
    assert journal.review_status(1, d(review_day), d("2026-07-20")).state == expected


def test_review_status_is_quiet_when_nothing_is_held() -> None:
    status = journal.review_status(0, None, d("2026-07-20"))
    assert status.state == "ok"
    assert status.needs_notice is False


def test_review_status_flags_a_book_that_has_never_been_reviewed() -> None:
    status = journal.review_status(2, None, d("2026-07-20"))
    assert status.state == "never"
    assert status.blocking is True
    assert status.days_since is None
    assert status.days_remaining is None


def test_the_warning_can_never_disagree_with_the_block() -> None:
    """Whatever the banner says is blocking must actually be blocked, and vice versa."""
    for days in range(0, 16):
        review_day = d("2026-07-20") - timedelta(days=days)
        status = journal.review_status(1, review_day, d("2026-07-20"))
        try:
            journal.check_review_gate(1, review_day, d("2026-07-20"))
            gate_blocked = False
        except RuleViolation:
            gate_blocked = True
        assert status.blocking == gate_blocked, f"disagreement at {days} days"

    # And the same for a book that has never been reviewed.
    never = journal.review_status(1, None, d("2026-07-20"))
    with pytest.raises(RuleViolation):
        journal.check_review_gate(1, None, d("2026-07-20"))
    assert never.blocking is True


def test_book_review_status_reads_the_journal(conn) -> None:
    journal.add_deposit(conn, journal.REAL, 100_000.0, d("2026-07-01"))
    check = journal.validate_buy(
        buy_request(shares=10.0, price=100.0, trade_date=d("2026-07-01")),
        journal.load_state(conn, journal.REAL),
        {},
    )
    journal.log_buy(conn, check)

    assert journal.book_review_status(conn, journal.REAL, d("2026-07-20")).state == "never"
    journal.record_review(conn, journal.REAL, on_date=d("2026-07-13"))

    status = journal.book_review_status(conn, journal.REAL, d("2026-07-20"))
    assert status.state == "due_soon"
    assert status.book == journal.REAL
    assert status.open_positions == 1
    # The other book holds nothing, so it owes nothing.
    assert journal.book_review_status(conn, journal.PAPER, d("2026-07-20")).state == "ok"


def test_review_staleness_is_tracked_in_the_database(conn) -> None:
    journal.add_deposit(conn, journal.REAL, 10_000.0, d("2026-07-01"))
    check = journal.validate_buy(
        buy_request(), journal.load_state(conn, journal.REAL), {}, d("2026-07-20")
    )
    journal.log_buy(conn, check)

    # Holding something with no review on file: stale.
    assert journal.review_is_stale(conn, journal.REAL, d("2026-07-20")) is True
    journal.record_review(conn, journal.REAL, on_date=d("2026-07-20"))
    assert journal.days_since_review(conn, journal.REAL, d("2026-07-25")) == 5
    assert journal.review_is_stale(conn, journal.REAL, d("2026-07-25")) is False
    assert journal.review_is_stale(conn, journal.REAL, d("2026-07-30")) is True


# =========================================================== ledger round trips

def test_buy_then_full_sell_round_trips_through_sqlite(conn) -> None:
    journal.add_deposit(conn, journal.REAL, 10_000.0, d("2026-07-01"))
    check = journal.validate_buy(
        buy_request(shares=10.0, price=100.0),
        journal.load_state(conn, journal.REAL),
        {},
        d("2026-07-20"),
    )
    opened = journal.log_buy(conn, check)
    assert opened.shares == pytest.approx(10.0)
    assert opened.position.opened_at.endswith("+00:00")  # timestamped thesis

    held = journal.find_open(conn, journal.REAL, "ACME")
    assert held is not None
    closed = journal.log_sell(
        conn, held, 10.0, 130.0, "Re-rated on the services print, thesis played out.",
        d("2026-07-24"),
    )
    assert closed.is_open is False
    assert closed.realized_pnl == pytest.approx(300.0)
    assert closed.position.outcome.startswith("Re-rated")
    assert journal.find_open(conn, journal.REAL, "ACME") is None


def test_partial_sell_leaves_the_position_open(conn) -> None:
    journal.add_deposit(conn, journal.REAL, 10_000.0, d("2026-07-01"))
    check = journal.validate_buy(
        buy_request(shares=10.0, price=100.0),
        journal.load_state(conn, journal.REAL),
        {},
        d("2026-07-20"),
    )
    journal.log_buy(conn, check)
    held = journal.find_open(conn, journal.REAL, "ACME")
    after = journal.log_sell(
        conn, held, 4.0, 120.0, "Took some off into strength, letting the rest run.",
        d("2026-07-22"),
    )
    assert after.is_open is True
    assert after.shares == pytest.approx(6.0)
    assert after.realized_pnl == pytest.approx(80.0)
    assert after.avg_cost == pytest.approx(100.0)
    assert "partial" in after.position.outcome


def test_adding_to_a_name_averages_in_and_keeps_one_lot(conn) -> None:
    journal.add_deposit(conn, journal.REAL, 100_000.0, d("2026-07-01"))
    first = journal.validate_buy(
        buy_request(shares=10.0, price=100.0),
        journal.load_state(conn, journal.REAL),
        {},
        d("2026-07-20"),
    )
    journal.log_buy(conn, first)
    journal.record_review(conn, journal.REAL, on_date=d("2026-07-20"))

    second = journal.validate_buy(
        buy_request(
            shares=10.0,
            price=120.0,
            trade_date=d("2026-07-22"),
            thesis="Doubling down now that the setup confirmed on volume.",
        ),
        journal.load_state(conn, journal.REAL),
        {"ACME": 120.0},
    )
    combined = journal.log_buy(conn, second)

    assert len(journal.open_holdings(conn, journal.REAL)) == 1
    assert combined.shares == pytest.approx(20.0)
    assert combined.avg_cost == pytest.approx(110.0)
    assert "[added 2026-07-22" in combined.position.thesis
    assert GOOD_THESIS in combined.position.thesis  # the original argument survives


def test_a_name_cannot_be_open_in_two_buckets_at_once(conn) -> None:
    journal.add_deposit(conn, journal.REAL, 100_000.0, d("2026-07-01"))
    check = journal.validate_buy(
        buy_request(shares=10.0, price=100.0),
        journal.load_state(conn, journal.REAL),
        {},
        d("2026-07-20"),
    )
    journal.log_buy(conn, check)
    journal.record_review(conn, journal.REAL, on_date=d("2026-07-20"))

    swing = journal.validate_buy(
        buy_request(shares=1.0, price=100.0, bucket=journal.ACTIVE, stop_price=90.0),
        journal.load_state(conn, journal.REAL),
        {"ACME": 100.0},
        d("2026-07-21"),
    )
    with pytest.raises(RuleViolation):
        journal.log_buy(conn, swing)


def test_stop_breach_is_detectable_on_an_open_position() -> None:
    held = holding(
        [trade("buy", 10, 100.0, "2026-07-01")], stop_price=92.0, bucket=journal.ACTIVE
    )
    assert held.stop_breached(95.0) is False
    assert held.stop_breached(92.0) is True
    assert held.stop_breached(88.0) is True


def test_deposits_reject_a_zero_amount(conn) -> None:
    with pytest.raises(ValueError):
        journal.add_deposit(conn, journal.REAL, 0.0)


def test_unknown_bucket_is_rejected() -> None:
    with pytest.raises(ValueError):
        journal.check_instrument(buy_request(bucket="yolo"))
    with pytest.raises(ValueError):
        journal.cap_for("yolo")


@pytest.mark.parametrize("bad", ["", "Real", "has space", "x" * 40, "1st", None])
def test_a_malformed_book_name_is_rejected(bad) -> None:
    with pytest.raises(ValueError, match="invalid book name"):
        journal.validate_book(bad)


def test_book_names_are_validated_by_shape_not_membership() -> None:
    """The arena adds a book per persona, so membership cannot be the check.

    A book is a partition key, not a rule — but it still has to be a slug, so a
    typo cannot silently open a brand-new book.
    """
    for good in (journal.REAL, journal.PAPER, "value", "momentum", "monk"):
        assert journal.validate_book(good) == good
    journal.check_instrument(buy_request(book="momentum"))
