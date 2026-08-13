"""SPY benchmark math and the track report — including rule 7 as a structural test."""

from __future__ import annotations

import re

import pytest

from thesis import journal, track
from thesis.data import market

from conftest import FLAT_SPY, d, deposit, holding, spy_series, trade

# SPY doubles-ish over the window: $500 -> $550 -> $600.
SPY = spy_series({"2026-07-01": 500.0, "2026-07-10": 550.0, "2026-07-20": 600.0})


# ============================================================ as-of pricing

def test_price_on_is_as_of_the_last_close() -> None:
    assert market.price_on(SPY, d("2026-07-10")) == pytest.approx(550.0)
    # A weekend/holiday date falls back to the previous close.
    assert market.price_on(SPY, d("2026-07-15")) == pytest.approx(550.0)
    assert market.price_on(SPY, d("2026-07-25")) == pytest.approx(600.0)


def test_price_on_refuses_a_date_before_the_history_starts() -> None:
    with pytest.raises(ValueError, match="on or before"):
        market.price_on(SPY, d("2026-06-01"), "SPY")


# ============================================================ the mirror

def test_mirror_buys_and_sells_average_cost() -> None:
    mirror = track.SpyMirror()
    mirror.buy_dollars(500.0, 500.0)  # 1 share
    mirror.buy_dollars(550.0, 550.0)  # 1 share
    assert mirror.shares == pytest.approx(2.0)
    assert mirror.cost == pytest.approx(1050.0)

    mirror.sell_fraction(0.5, 600.0)  # sell 1 share at 600, avg cost 525
    assert mirror.shares == pytest.approx(1.0)
    assert mirror.realized == pytest.approx(75.0)
    assert mirror.total_pnl(600.0) == pytest.approx(150.0)
    assert mirror.return_pct(600.0) == pytest.approx(150.0 / 1050.0)


def test_mirror_deposits_buys_spy_on_the_same_dates() -> None:
    mirror = track.mirror_deposits(
        [deposit(1000.0, "2026-07-01"), deposit(550.0, "2026-07-10", id=2)], SPY
    )
    assert mirror.shares == pytest.approx(2.0 + 1.0)
    assert mirror.contributed == pytest.approx(1550.0)
    assert mirror.value(600.0) == pytest.approx(1800.0)
    assert mirror.total_pnl(600.0) == pytest.approx(250.0)


def test_mirror_deposits_sells_spy_for_a_withdrawal() -> None:
    mirror = track.mirror_deposits(
        [deposit(1000.0, "2026-07-01"), deposit(-550.0, "2026-07-10", id=2)], SPY
    )
    # $550 out at $550/share = 1 share sold, bought at $500 -> $50 realized.
    assert mirror.shares == pytest.approx(1.0)
    assert mirror.realized == pytest.approx(50.0)
    assert mirror.total_pnl(600.0) == pytest.approx(150.0)


def test_mirror_trades_matches_dollars_and_dates() -> None:
    """$500 into ACME on 07-10 is $500 into SPY on 07-10 — same money, same days."""
    mirror = track.mirror_trades([trade("buy", 5, 100.0, "2026-07-10")], SPY)
    assert mirror.shares == pytest.approx(500.0 / 550.0)
    assert mirror.total_pnl(600.0) == pytest.approx(500.0 * (600.0 / 550.0 - 1.0))


def test_mirror_trades_exits_in_the_same_proportion() -> None:
    """Selling 40% of the position sells 40% of the mirror, on the same date."""
    trades = [
        trade("buy", 5, 100.0, "2026-07-10", id=1),
        trade("sell", 2, 130.0, "2026-07-20", id=2),
    ]
    mirror = track.mirror_trades(trades, SPY)
    bought = 500.0 / 550.0
    assert mirror.shares == pytest.approx(bought * 0.6)
    assert mirror.realized == pytest.approx(bought * 0.4 * (600.0 - 550.0))


def test_mirror_trades_on_a_flat_tape_earns_nothing() -> None:
    """The control case: if SPY does not move, the benchmark P&L is exactly zero."""
    mirror = track.mirror_trades([trade("buy", 5, 100.0, "2026-07-10")], FLAT_SPY)
    assert mirror.total_pnl(500.0) == pytest.approx(0.0)


# ============================================================ the report

def _report(holdings, deposits=None, prices=None, spy=SPY, book=journal.REAL):
    return track.build_report(
        book,
        deposits or [deposit(1000.0, "2026-07-01")],
        holdings,
        prices or {"ACME": 130.0},
        spy,
        as_of=d("2026-07-20"),
    )


OPEN_POSITION = [trade("buy", 5, 100.0, "2026-07-10")]
CLOSED_POSITION = [
    trade("buy", 5, 100.0, "2026-07-10", id=1),
    trade("sell", 5, 130.0, "2026-07-20", id=2),
]


def test_report_open_position_against_its_spy_mirror() -> None:
    report = _report([holding(OPEN_POSITION)])
    line = report.open_lines[0]

    assert line.pnl == pytest.approx(150.0)
    assert line.pnl_pct == pytest.approx(0.30)
    assert line.spy_pnl == pytest.approx(500.0 * (600.0 / 550.0 - 1.0))
    assert line.spy_pct == pytest.approx(600.0 / 550.0 - 1.0)
    assert line.edge_pp == pytest.approx((0.30 - (600.0 / 550.0 - 1.0)) * 100)
    assert line.beat_spy is True


def test_report_account_shows_the_cost_of_sitting_in_cash() -> None:
    """The position beat SPY, but half the account never left cash — so it trailed."""
    report = _report([holding(OPEN_POSITION)])
    acct = report.account

    assert acct.cash == pytest.approx(500.0)
    assert acct.equity == pytest.approx(500.0 + 650.0)
    assert acct.pnl == pytest.approx(150.0)
    assert acct.pnl_pct == pytest.approx(0.15)
    assert acct.spy_equity == pytest.approx(1200.0)  # $1,000 -> 2 shares -> $600 each
    assert acct.spy_pnl == pytest.approx(200.0)
    assert acct.spy_pct == pytest.approx(0.20)
    assert acct.edge_pp == pytest.approx(-5.0)
    assert report.open_lines[0].beat_spy is True  # the pick won; the account did not


def test_report_closed_trade_and_statistics() -> None:
    report = _report([holding(CLOSED_POSITION, status="closed")], prices={})
    line = report.closed_lines[0]
    stats = report.stats

    assert line.pnl == pytest.approx(150.0)
    assert line.exit_date == d("2026-07-20")
    assert line.held_days == 10
    assert stats.n_closed == 1
    assert stats.wins == 1
    assert stats.win_rate == pytest.approx(1.0)
    assert stats.expectancy == pytest.approx(150.0)
    assert stats.spy_expectancy == pytest.approx(line.spy_pnl)
    assert stats.beat_spy == 1
    assert stats.beat_spy_rate == pytest.approx(1.0)


def test_report_counts_a_loss_that_still_beat_spy() -> None:
    """On a falling tape, losing less than SPY is a relative win — track both."""
    falling = spy_series({"2026-07-01": 600.0, "2026-07-10": 600.0, "2026-07-20": 400.0})
    losing = [
        trade("buy", 5, 100.0, "2026-07-10", id=1),
        trade("sell", 5, 95.0, "2026-07-20", id=2),
    ]
    report = _report([holding(losing, status="closed")], prices={}, spy=falling)
    line = report.closed_lines[0]

    assert line.pnl == pytest.approx(-25.0)
    assert line.spy_pnl == pytest.approx(500.0 * (400.0 / 600.0 - 1.0))
    assert line.beat_spy is True
    assert report.stats.wins == 0
    assert report.stats.win_rate == pytest.approx(0.0)
    assert report.stats.beat_spy_rate == pytest.approx(1.0)


def test_report_splits_buckets_and_benchmarks_each_one() -> None:
    core = holding(OPEN_POSITION, id=1, ticker="ACME", bucket=journal.CORE)
    swing = holding(
        [trade("buy", 2, 50.0, "2026-07-10", ticker="WILE", position_id=2, id=9)],
        id=2,
        ticker="WILE",
        bucket=journal.ACTIVE,
        stop_price=45.0,
    )
    report = _report(
        [core, swing],
        deposits=[deposit(2000.0, "2026-07-01")],
        prices={"ACME": 130.0, "WILE": 60.0},
    )

    buckets = {b.bucket: b for b in report.buckets}
    assert buckets[journal.CORE].invested == pytest.approx(500.0)
    assert buckets[journal.CORE].pnl == pytest.approx(150.0)
    assert buckets[journal.ACTIVE].invested == pytest.approx(100.0)
    assert buckets[journal.ACTIVE].pnl == pytest.approx(20.0)
    # Every bucket carries its own SPY comparison (rule 7).
    for bucket in report.buckets:
        assert bucket.spy_pnl == pytest.approx(bucket.invested * (600.0 / 550.0 - 1.0))
        assert bucket.edge_pp is not None


def test_report_flags_a_breached_stop() -> None:
    swing = holding(OPEN_POSITION, bucket=journal.ACTIVE, stop_price=95.0)
    report = _report([swing], prices={"ACME": 90.0})
    assert report.open_lines[0].stop_breached is True
    assert "BREACHED" in track.render_markdown(report)


def test_report_survives_an_empty_book() -> None:
    report = _report([], deposits=[deposit(1000.0, "2026-07-01")], prices={})
    assert report.account.equity == pytest.approx(1000.0)
    assert report.open_lines == []
    assert report.stats.n_closed == 0
    assert "No open positions" in track.render_markdown(report)


def test_unsettled_cash_appears_in_the_report() -> None:
    report = _report([holding(CLOSED_POSITION, status="closed")], prices={})
    assert report.account.unsettled == pytest.approx(650.0)  # sold 07-20, settles 07-21
    assert report.account.settled == pytest.approx(500.0)
    assert "unsettled" in track.render_markdown(report)


# ============================================================ rule 7

def _table_headers(markdown: str) -> list[str]:
    """Header row of every markdown table (the row above the |---|---| divider)."""
    lines = markdown.splitlines()
    return [
        lines[i]
        for i in range(len(lines) - 1)
        if lines[i].startswith("|") and re.match(r"^\|[\s:-]*\|", lines[i + 1] or "")
        and set(lines[i + 1].replace("|", "").strip()) <= {"-", " ", ":"}
    ]


def test_rule_7_every_table_of_numbers_carries_its_spy_benchmark() -> None:
    core = holding(OPEN_POSITION, id=1)
    closed = holding(
        [
            trade("buy", 2, 50.0, "2026-07-10", ticker="WILE", position_id=2, id=9),
            trade("sell", 2, 60.0, "2026-07-20", ticker="WILE", position_id=2, id=10),
        ],
        id=2,
        ticker="WILE",
        bucket=journal.ACTIVE,
        status="closed",
    )
    report = _report(
        [core, closed],
        deposits=[deposit(2000.0, "2026-07-01")],
        prices={"ACME": 130.0, "WILE": 60.0},
    )
    markdown = track.render_markdown(report)

    headers = _table_headers(markdown)
    assert len(headers) >= 4  # account, buckets, open, closed, stats
    for header in headers:
        assert "SPY" in header, f"table without a benchmark column: {header}"


def test_rule_7_no_performance_line_lacks_a_benchmark() -> None:
    report = _report([holding(OPEN_POSITION), holding(CLOSED_POSITION, id=2, status="closed")])
    for line in report.all_lines:
        assert line.spy_pnl is not None
        assert line.spy_pct is not None
        assert line.edge_pp is not None
    assert report.account.spy_equity is not None
    assert report.account.edge_pp is not None


def test_render_includes_the_timestamped_thesis_and_the_outcome() -> None:
    closed = holding(
        CLOSED_POSITION,
        status="closed",
        closed_at="2026-07-20T20:00:00+00:00",
        outcome="Re-rated on the services print — thesis played out early.",
    )
    markdown = track.render_markdown(_report([closed], prices={}))

    assert "Trade log — theses as written, at the time" in markdown
    assert "2026-07-10T14:00:00+00:00" in markdown  # opened_at, as recorded
    assert "Re-rated on the services print" in markdown
    assert "**Invalidation:**" in markdown
    assert "**Exit plan:**" in markdown


def test_render_warns_about_small_sample_sizes() -> None:
    markdown = track.render_markdown(_report([holding(CLOSED_POSITION, status="closed")], prices={}))
    assert "too small a sample" in markdown


def test_formatters_never_render_a_negative_zero() -> None:
    """A same-day mirror leaves float residue; it must not print as a loss."""
    residue = -1e-13
    assert track.money(residue) == "$0.00"
    assert track.signed_money(residue) == "+$0.00"
    assert track.pct(residue) == "+0.00%"
    assert track.points(residue) == "+0.00 pp"
    assert track.pct_plain(residue) == "0.0%"
    # Real values are untouched.
    assert track.signed_money(-12.5) == "-$12.50"
    assert track.pct(-0.031) == "-3.10%"
    assert track.money(None) == "n/a"


def test_a_same_day_position_shows_a_flat_benchmark_not_a_loss() -> None:
    report = _report([holding([trade("buy", 5, 100.0, "2026-07-20")])])
    markdown = track.render_markdown(report)
    assert "-$0.00" not in markdown
    assert "-0.00%" not in markdown


def test_first_flow_date_reaches_back_to_the_earliest_money_movement() -> None:
    assert track.first_flow_date([deposit(100.0, "2026-07-05")], []) == d("2026-07-05")
    assert track.first_flow_date([], [holding(OPEN_POSITION)]) == d("2026-07-10")
    assert track.first_flow_date([], []) is None
