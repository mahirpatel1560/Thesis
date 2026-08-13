"""The paper book: fake money, identical rules, identical math, identical benchmark.

Two things have to be true at once — a paper position must be treated *exactly*
like a real one by every rule and every calculation, and the two books must never
leak into each other.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from thesis import journal, track
from thesis.journal import RuleViolation

from conftest import buy_request, d, deposit, holding, spy_series, state, trade

SPY = spy_series({"2026-07-01": 500.0, "2026-07-10": 550.0, "2026-07-20": 600.0})
PRICES = {"ACME": 130.0}


def test_paper_flag_maps_to_the_paper_book() -> None:
    assert journal.book_name(paper=True) == journal.PAPER
    assert journal.book_name(paper=False) == journal.REAL
    assert set(journal.BOOKS) == {"real", "paper"}


def _run_book(conn, book: str) -> None:
    """The same history in whichever book: deposit, buy, review, partial sell."""
    journal.add_deposit(conn, book, 10_000.0, d("2026-07-01"))
    check = journal.validate_buy(
        buy_request(book=book, shares=20.0, price=100.0, trade_date=d("2026-07-10")),
        journal.load_state(conn, book),
        {},
    )
    journal.log_buy(conn, check)
    journal.record_review(conn, book, on_date=d("2026-07-15"))
    held = journal.find_open(conn, book, "ACME")
    journal.log_sell(
        conn, held, 10.0, 130.0, "Trimmed half into the print, thesis intact.", d("2026-07-20")
    )


def _report(conn, book: str) -> track.TrackReport:
    return track.build_report(
        book,
        journal.deposits(conn, book),
        journal.holdings(conn, book),
        PRICES,
        SPY,
        as_of=d("2026-07-20"),
    )


def test_paper_and_real_produce_identical_numbers(conn) -> None:
    """The whole point of the flag: practice results are computed the same way."""
    _run_book(conn, journal.REAL)
    _run_book(conn, journal.PAPER)

    real = _report(conn, journal.REAL)
    paper = _report(conn, journal.PAPER)

    assert asdict(paper.account) == asdict(real.account)
    assert [asdict(b) for b in paper.buckets] == [asdict(b) for b in real.buckets]
    assert asdict(paper.stats) == asdict(real.stats)

    def numbers(report: track.TrackReport) -> list[tuple]:
        return [
            (
                line.ticker,
                line.bucket,
                line.status,
                line.shares,
                line.avg_cost,
                line.pnl,
                line.pnl_pct,
                line.spy_pnl,
                line.spy_pct,
                line.edge_pp,
            )
            for line in report.all_lines
        ]

    assert numbers(paper) == numbers(real)


def test_paper_and_real_reports_render_identically_apart_from_the_label(conn) -> None:
    _run_book(conn, journal.REAL)
    _run_book(conn, journal.PAPER)

    def body(report: track.TrackReport) -> str:
        markdown = track.render_markdown(report)
        # Everything between the account table and the (timestamped) trade log.
        return markdown.split("## Account", 1)[1].split("## Trade log", 1)[0]

    assert body(_report(conn, journal.PAPER)) == body(_report(conn, journal.REAL))


def test_paper_report_is_labelled_so_it_cannot_pass_for_real(conn) -> None:
    _run_book(conn, journal.PAPER)
    markdown = track.render_markdown(_report(conn, journal.PAPER))

    assert markdown.startswith("# Track Record — PAPER — simulated money")
    assert "No real capital" in markdown
    assert _report(conn, journal.PAPER).is_paper is True

    _run_book(conn, journal.REAL)
    real_markdown = track.render_markdown(_report(conn, journal.REAL))
    assert real_markdown.startswith("# Track Record — REAL money")
    assert "PAPER" not in real_markdown


def test_paper_is_benchmarked_against_the_same_spy_mirror(conn) -> None:
    _run_book(conn, journal.PAPER)
    report = _report(conn, journal.PAPER)

    # $2,000 into SPY on 07-10 at $550, half taken out on 07-20 at $600.
    line = report.open_lines[0]
    assert line.spy_pct == pytest.approx(600.0 / 550.0 - 1.0)
    assert report.account.spy_equity == pytest.approx(10_000.0 / 500.0 * 600.0)
    assert report.account.edge_pp is not None


# ------------------------------------------------------------------- isolation

def test_books_do_not_share_cash(conn) -> None:
    journal.add_deposit(conn, journal.PAPER, 100_000.0, d("2026-07-01"))
    journal.add_deposit(conn, journal.REAL, 500.0, d("2026-07-01"))

    assert journal.cash_balance(journal.deposits(conn, journal.REAL), []) == pytest.approx(500.0)
    assert journal.cash_balance(
        journal.deposits(conn, journal.PAPER), []
    ) == pytest.approx(100_000.0)

    # A $1,000 buy is fine on paper and impossible for real — no cross-funding.
    paper_check = journal.validate_buy(
        buy_request(book=journal.PAPER, shares=10.0, price=100.0, trade_date=d("2026-07-10")),
        journal.load_state(conn, journal.PAPER),
        {},
    )
    assert paper_check.cost == pytest.approx(1000.0)

    with pytest.raises(RuleViolation) as exc:
        journal.validate_buy(
            buy_request(book=journal.REAL, shares=10.0, price=100.0, trade_date=d("2026-07-10")),
            journal.load_state(conn, journal.REAL),
            {},
        )
    assert exc.value.rule in (3, 4)  # too big for the account, and unfunded


def test_positions_and_reviews_stay_in_their_own_book(conn) -> None:
    _run_book(conn, journal.PAPER)

    assert journal.find_open(conn, journal.PAPER, "ACME") is not None
    assert journal.find_open(conn, journal.REAL, "ACME") is None
    assert journal.holdings(conn, journal.REAL) == ()
    assert journal.last_review(conn, journal.PAPER) is not None
    assert journal.last_review(conn, journal.REAL) is None


def test_a_paper_review_does_not_unlock_a_real_buy(conn) -> None:
    """Rule 6 is per book — practising discipline on paper is not doing it for real."""
    journal.add_deposit(conn, journal.REAL, 100_000.0, d("2026-07-01"))
    check = journal.validate_buy(
        buy_request(shares=10.0, price=100.0, trade_date=d("2026-07-01")),
        journal.load_state(conn, journal.REAL),
        {},
    )
    journal.log_buy(conn, check)
    journal.record_review(conn, journal.PAPER, on_date=d("2026-07-20"))

    assert journal.review_is_stale(conn, journal.REAL, d("2026-07-20")) is True
    with pytest.raises(RuleViolation) as exc:
        journal.validate_buy(
            buy_request(shares=1.0, price=100.0, trade_date=d("2026-07-20")),
            journal.load_state(conn, journal.REAL),
            {"ACME": 100.0},
        )
    assert exc.value.rule == 6

    journal.record_review(conn, journal.REAL, on_date=d("2026-07-20"))
    assert journal.review_is_stale(conn, journal.REAL, d("2026-07-20")) is False


def test_equity_snapshots_are_kept_per_book(conn) -> None:
    journal.snapshot_equity(conn, journal.REAL, 1_000.0, 990.0, d("2026-07-20"))
    journal.snapshot_equity(conn, journal.PAPER, 100_000.0, 99_000.0, d("2026-07-20"))
    rows = conn.execute(
        "SELECT book, equity, spy_equity FROM equity_snapshots ORDER BY book"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("paper", 100_000.0, 99_000.0),
        ("real", 1_000.0, 990.0),
    ]

    # Re-running track the same day updates rather than duplicating.
    journal.snapshot_equity(conn, journal.REAL, 1_010.0, 995.0, d("2026-07-20"))
    count = conn.execute(
        "SELECT COUNT(*) FROM equity_snapshots WHERE book = 'real'"
    ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------- the rules bind on paper too

def test_paper_buys_obey_rule_1(conn) -> None:
    journal.add_deposit(conn, journal.PAPER, 100_000.0, d("2026-07-01"))
    with pytest.raises(RuleViolation) as exc:
        journal.validate_buy(
            buy_request(book=journal.PAPER, thesis="feels right"),
            journal.load_state(conn, journal.PAPER),
            {},
        )
    assert exc.value.rule == 1


def test_paper_active_trades_obey_rule_2() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_stop(
            buy_request(book=journal.PAPER, bucket=journal.ACTIVE, stop_price=None)
        )
    assert exc.value.rule == 2


def test_paper_position_caps_are_not_relaxed(conn) -> None:
    """Fake money does not buy a bigger position — the sizing lesson is the point."""
    journal.add_deposit(conn, journal.PAPER, 1_000.0, d("2026-07-01"))
    with pytest.raises(RuleViolation) as exc:
        journal.validate_buy(
            buy_request(book=journal.PAPER, shares=3.0, price=100.0),
            journal.load_state(conn, journal.PAPER),
            {},
        )
    assert exc.value.rule == 3


def test_paper_book_still_refuses_options_and_shorts() -> None:
    with pytest.raises(RuleViolation) as exc:
        journal.check_instrument(buy_request(book=journal.PAPER, ticker="AAPL250117C00200000"))
    assert exc.value.rule == 4
    with pytest.raises(RuleViolation) as exc:
        journal.check_instrument(buy_request(book=journal.PAPER, shares=-5.0))
    assert exc.value.rule == 4


def test_paper_sells_still_require_an_honest_outcome() -> None:
    held = holding([trade("buy", 5, 100.0, "2026-07-10", book=journal.PAPER)], book=journal.PAPER)
    with pytest.raises(RuleViolation):
        journal.validate_sell(held, 5.0, 130.0, "won")


def test_rule_5_settlement_warning_applies_on_paper_too() -> None:
    """Practising a cash account means practising T+1, or the practice is a lie."""
    sold = holding(
        [
            trade("buy", 1, 95.0, "2026-07-02", id=1, book=journal.PAPER),
            trade("sell", 1, 150.0, "2026-07-20", id=2, book=journal.PAPER),
        ],
        status="closed",
        book=journal.PAPER,
    )
    book = state(
        [deposit(100.0, "2026-07-01", book=journal.PAPER)],
        [sold],
        last_review=d("2026-07-20"),
        book=journal.PAPER,
    )
    check = journal.validate_buy(
        buy_request(book=journal.PAPER, shares=0.2, price=150.0, trade_date=d("2026-07-20")),
        book,
        {},
    )
    assert check.warnings and "unsettled" in check.warnings[0]
