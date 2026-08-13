"""End-to-end CLI tests for log/review/track (no network — prices are stubbed)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from typer.testing import CliRunner

from thesis import cli, config, journal, track
from thesis.data import market

from conftest import (
    GOOD_EXIT,
    GOOD_HORIZON,
    GOOD_INVALIDATION,
    GOOD_THESIS,
    d,
    spy_series,
)

SPY = spy_series({"2026-07-01": 500.0, "2026-07-10": 550.0, "2026-07-20": 600.0})
LAST_CLOSES = {"ACME": 130.0, "WILE": 60.0}

runner = CliRunner()


@pytest.fixture(autouse=True)
def offline(monkeypatch, conn, tmp_path):
    """Stub every network call the journal commands make, and sandbox reports/."""
    monkeypatch.setattr(
        market, "get_last_closes", lambda tickers: {t.upper(): LAST_CLOSES[t.upper()] for t in tickers}
    )
    monkeypatch.setattr(track, "spy_history", lambda start, end=None: SPY)
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    return conn


def text(result) -> str:
    """Everything the command printed, stdout and stderr alike."""
    combined = result.output or ""
    try:
        if result.stderr:
            combined += result.stderr
    except (ValueError, AttributeError):  # click merges the streams in some versions
        pass
    return combined


def invoke(*args: str):
    return runner.invoke(cli.app, list(args))


BUY_ACME = (
    "log", "buy", "ACME",
    "--bucket", "core",
    "--shares", "10",
    "--price", "100",
    "--thesis", GOOD_THESIS,
    "--invalidation", GOOD_INVALIDATION,
    "--horizon", GOOD_HORIZON,
    "--exit-plan", GOOD_EXIT,
    "--date", "2026-07-10",
    "--yes",
)


def fund(book_flag: tuple[str, ...] = ()) -> None:
    result = invoke("deposit", "10000", "--date", "2026-07-01", *book_flag)
    assert result.exit_code == 0, text(result)


# ------------------------------------------------------------------- deposits

def test_deposit_reports_the_new_cash_balance(conn) -> None:
    result = invoke("deposit", "1000", "--date", "2026-07-01", "--note", "paycheck")
    assert result.exit_code == 0, text(result)
    assert "$1,000.00" in text(result)
    assert journal.deposits(conn, journal.REAL)[0].note == "paycheck"


def test_a_buy_into_an_unfunded_book_is_refused() -> None:
    result = invoke(*BUY_ACME)
    assert result.exit_code == 1
    assert "no money in it" in text(result)


# ------------------------------------------------------------------ rule 1 e2e

def test_cli_refuses_a_buy_with_a_one_word_thesis() -> None:
    fund()
    result = invoke(
        "log", "buy", "ACME", "--bucket", "core", "--shares", "10", "--price", "100",
        "--thesis", "cheap",
        "--invalidation", GOOD_INVALIDATION,
        "--horizon", GOOD_HORIZON,
        "--exit-plan", GOOD_EXIT,
        "--date", "2026-07-10", "--yes",
    )
    assert result.exit_code == 1
    assert "REFUSED" in text(result)
    assert "rule 1" in text(result)


def test_cli_refuses_a_buy_with_no_exit_plan() -> None:
    fund()
    result = invoke(
        "log", "buy", "ACME", "--bucket", "core", "--shares", "10", "--price", "100",
        "--thesis", GOOD_THESIS,
        "--invalidation", GOOD_INVALIDATION,
        "--horizon", GOOD_HORIZON,
        "--exit-plan", "n/a",
        "--date", "2026-07-10", "--yes",
    )
    assert result.exit_code == 1
    assert "rule 1" in text(result)


# ------------------------------------------------------------------ rule 2 e2e

def test_cli_refuses_an_active_trade_with_a_stop_above_entry() -> None:
    fund()
    result = invoke(
        "log", "buy", "WILE", "--bucket", "active", "--shares", "5", "--price", "60",
        "--stop", "70",
        "--thesis", GOOD_THESIS,
        "--invalidation", GOOD_INVALIDATION,
        "--horizon", "Two to six weeks.",
        "--exit-plan", GOOD_EXIT,
        "--date", "2026-07-10", "--yes",
    )
    assert result.exit_code == 1
    assert "rule 2" in text(result)


# ------------------------------------------------------------------ rule 3 e2e

def test_cli_refuses_an_oversized_core_position() -> None:
    fund()
    result = invoke(
        "log", "buy", "ACME", "--bucket", "core", "--shares", "30", "--price", "100",
        "--thesis", GOOD_THESIS,
        "--invalidation", GOOD_INVALIDATION,
        "--horizon", GOOD_HORIZON,
        "--exit-plan", GOOD_EXIT,
        "--date", "2026-07-10", "--yes",
    )
    assert result.exit_code == 1
    assert "rule 3" in text(result)
    assert "Size down" in text(result)


# ------------------------------------------------------------------ happy path

def test_a_complete_buy_is_logged_with_a_timestamped_thesis(conn) -> None:
    fund()
    result = invoke(*BUY_ACME)
    assert result.exit_code == 0, text(result)
    assert "Logged: ACME" in text(result)
    assert "10.0% of $10,000.00 equity" in text(result)

    held = journal.find_open(conn, journal.REAL, "ACME")
    assert held is not None
    assert held.shares == pytest.approx(10.0)
    assert held.avg_cost == pytest.approx(100.0)
    assert held.position.thesis == GOOD_THESIS
    assert held.position.opened_at.endswith("+00:00")


def test_the_confirmation_prompt_can_decline(conn) -> None:
    fund()
    args = [a for a in BUY_ACME if a != "--yes"]
    result = runner.invoke(cli.app, args, input="n\n")
    assert result.exit_code == 0
    assert "Not logged" in text(result)
    assert journal.find_open(conn, journal.REAL, "ACME") is None


def test_a_missing_thesis_is_re_prompted_until_it_is_real(conn) -> None:
    """Interactive discipline: a short answer is rejected at the prompt, not silently."""
    fund()
    args = [
        "log", "buy", "ACME", "--bucket", "core", "--shares", "10", "--price", "100",
        "--invalidation", GOOD_INVALIDATION,
        "--horizon", GOOD_HORIZON,
        "--exit-plan", GOOD_EXIT,
        "--date", "2026-07-10", "--yes",
    ]
    result = runner.invoke(cli.app, args, input=f"nope\n{GOOD_THESIS}\n")
    assert result.exit_code == 0, text(result)
    assert "too short" in text(result)
    assert journal.find_open(conn, journal.REAL, "ACME").position.thesis == GOOD_THESIS


# ------------------------------------------------------------------ rule 6 e2e

def test_rule_6_blocks_the_next_buy_until_a_review_runs(conn) -> None:
    fund()
    assert invoke(*BUY_ACME).exit_code == 0

    blocked = invoke(
        "log", "buy", "WILE", "--bucket", "core", "--shares", "5", "--price", "60",
        "--thesis", GOOD_THESIS,
        "--invalidation", GOOD_INVALIDATION,
        "--horizon", GOOD_HORIZON,
        "--exit-plan", GOOD_EXIT,
        "--date", "2026-07-20", "--yes",
    )
    assert blocked.exit_code == 1
    assert "rule 6" in text(blocked)
    assert "thesis review" in text(blocked)

    reviewed = invoke("review", "--yes")
    assert reviewed.exit_code == 0, text(reviewed)
    assert "Review recorded" in text(reviewed)

    allowed = invoke(
        "log", "buy", "WILE", "--bucket", "core", "--shares", "5", "--price", "60",
        "--thesis", GOOD_THESIS,
        "--invalidation", GOOD_INVALIDATION,
        "--horizon", GOOD_HORIZON,
        "--exit-plan", GOOD_EXIT,
        "--date", "2026-07-20", "--yes",
    )
    assert allowed.exit_code == 0, text(allowed)
    assert journal.find_open(conn, journal.REAL, "WILE") is not None


def test_review_peek_does_not_satisfy_rule_6(conn) -> None:
    fund()
    invoke(*BUY_ACME)
    result = invoke("review", "--peek")
    assert result.exit_code == 0
    assert "no review recorded" in text(result)
    assert journal.last_review(conn, journal.REAL) is None


def test_review_shows_the_thesis_and_the_spy_comparison() -> None:
    fund()
    invoke(*BUY_ACME)
    result = invoke("review", "--yes")
    body = text(result)
    assert GOOD_THESIS in body
    assert GOOD_INVALIDATION in body
    assert "same dollars in SPY" in body
    assert "edge" in body


def test_the_invalidation_is_echoed_immediately_before_the_question() -> None:
    """The answer must be given against the trigger as written, not from memory."""
    fund()
    invoke(*BUY_ACME)
    body = text(runner.invoke(cli.app, ["review"], input="n\n"))

    trigger = body.index(GOOD_INVALIDATION)
    question = body.index("Has exactly that happened?")
    assert trigger < question, "the trigger must be shown before the question"

    # Nothing from the position block may come between them.
    between = body[trigger + len(GOOD_INVALIDATION) : question]
    for other in ("Thesis:", "Horizon:", "Exit plan:", "same dollars in SPY", "Stop "):
        assert other not in between, f"{other!r} sits between the trigger and the question"


def test_the_trigger_is_still_shown_when_not_prompting() -> None:
    """--yes and --peek skip the question; they must not skip the trigger."""
    fund()
    invoke(*BUY_ACME)
    for flag in ("--yes", "--peek"):
        body = text(invoke("review", flag))
        assert GOOD_INVALIDATION in body, flag
        assert "as written at entry" in body, flag


def test_answering_no_records_a_review_with_no_flag(conn) -> None:
    fund()
    invoke(*BUY_ACME)
    runner.invoke(cli.app, ["review"], input="n\n")

    review = journal.last_review(conn, journal.REAL)
    assert review is not None
    assert "invalidation fired" not in review.note


def test_answering_yes_records_the_flag_in_the_review_note(conn) -> None:
    """Flag state lives in the review's free-text note — nowhere else."""
    fund()
    invoke(*BUY_ACME)
    runner.invoke(cli.app, ["review"], input="y\n")

    review = journal.last_review(conn, journal.REAL)
    assert review is not None
    assert review.note == "ACME: invalidation fired."


# --------------------------------------------------------------------- selling

def test_sell_closes_the_position_and_records_the_outcome(conn) -> None:
    fund()
    invoke(*BUY_ACME)
    result = invoke(
        "log", "sell", "ACME", "--shares", "10", "--price", "130",
        "--outcome", "Re-rated on the services print; thesis played out.",
        "--date", "2026-07-20", "--yes",
    )
    assert result.exit_code == 0, text(result)
    assert "closed" in text(result)
    assert "+$300.00" in text(result)

    closed = journal.closed_holdings(conn, journal.REAL)
    assert len(closed) == 1
    assert closed[0].realized_pnl == pytest.approx(300.0)
    assert closed[0].position.outcome.startswith("Re-rated")


def test_sell_refuses_a_lazy_outcome_note() -> None:
    fund()
    invoke(*BUY_ACME)
    result = invoke(
        "log", "sell", "ACME", "--shares", "10", "--price", "130",
        "--outcome", "won", "--date", "2026-07-20", "--yes",
    )
    assert result.exit_code == 1
    assert "what actually happened" in text(result)


def test_selling_something_you_do_not_hold_is_refused() -> None:
    fund()
    result = invoke(
        "log", "sell", "ACME", "--shares", "1", "--price", "130",
        "--outcome", "Closing a position that does not exist.", "--yes",
    )
    assert result.exit_code == 1
    assert "no open ACME position" in text(result)


# ----------------------------------------------------------------------- track

def test_track_prints_every_number_next_to_spy() -> None:
    fund()
    invoke(*BUY_ACME)
    result = invoke("track")
    body = text(result)

    assert result.exit_code == 0, body
    assert "# Track Record — REAL money" in body
    assert "SPY (same deposits, same dates)" in body
    assert "Edge vs SPY" in body
    assert "Trade log — theses as written, at the time" in body
    assert GOOD_THESIS in body


def test_track_on_an_empty_book_says_so() -> None:
    result = invoke("track")
    assert result.exit_code == 0
    assert "Nothing logged" in text(result)


def test_track_export_writes_a_markdown_report(conn) -> None:
    fund()
    invoke(*BUY_ACME)
    result = invoke("track", "--export")
    assert result.exit_code == 0, text(result)

    path = config.REPORTS_DIR / f"track_real_{journal.today()}.md"
    assert path.exists()
    assert "Track Record" in path.read_text(encoding="utf-8")
    assert "SPY" in path.read_text(encoding="utf-8")


def test_track_export_also_writes_a_pdf(conn) -> None:
    fund()
    invoke(*BUY_ACME)
    result = invoke("track", "--export")
    assert result.exit_code == 0, text(result)

    path = config.REPORTS_DIR / f"track_real_{journal.today()}.pdf"
    assert path.exists()
    assert path.read_bytes().startswith(b"%PDF-")
    assert path.stat().st_size > 3_000
    assert ".pdf" in text(result)


def test_a_paper_export_produces_a_pdf_stamped_paper(conn) -> None:
    fund(("--paper",))
    invoke(*BUY_ACME, "--paper")
    result = invoke("track", "--paper", "--export")
    assert result.exit_code == 0, text(result)

    path = config.REPORTS_DIR / f"track_paper_{journal.today()}.pdf"
    assert path.exists()
    assert path.read_bytes().startswith(b"%PDF-")
    assert "stamped PAPER" in text(result)


def test_the_two_books_export_to_separate_pdfs(conn) -> None:
    fund()
    fund(("--paper",))
    invoke(*BUY_ACME)
    invoke(*BUY_ACME, "--paper")
    invoke("track", "--export")
    invoke("track", "--paper", "--export")

    real = config.REPORTS_DIR / f"track_real_{journal.today()}.pdf"
    paper = config.REPORTS_DIR / f"track_paper_{journal.today()}.pdf"
    assert real.exists() and paper.exists()
    assert real.read_bytes() != paper.read_bytes()


def test_track_writes_an_equity_snapshot_next_to_the_benchmark(conn) -> None:
    fund()
    invoke(*BUY_ACME)
    invoke("track")
    row = conn.execute(
        "SELECT book, equity, spy_equity FROM equity_snapshots WHERE book = 'real'"
    ).fetchone()
    assert row["equity"] == pytest.approx(9_000.0 + 10 * 130.0)
    assert row["spy_equity"] == pytest.approx(10_000.0 / 500.0 * 600.0)


# ------------------------------------------------------- review discipline

def review_days_ago(days: int, book: str = journal.REAL) -> None:
    """Record a review dated `days` before today, relative to the real clock."""
    conn = journal.connect()
    journal.record_review(conn, book, on_date=journal.today() - timedelta(days=days))
    conn.close()


def test_review_banner_warns_before_the_deadline_not_only_at_it() -> None:
    fund()
    invoke(*BUY_ACME)
    review_days_ago(8)  # one day left

    body = text(invoke("track"))
    assert "Review due in 1 day" in body
    assert "last review 8 days ago" in body
    assert "REVIEW OVERDUE" not in body


def test_review_banner_is_silent_while_the_review_is_fresh() -> None:
    fund()
    invoke(*BUY_ACME)
    review_days_ago(3)

    body = text(invoke("track"))
    assert "Review due" not in body
    assert "REVIEW OVERDUE" not in body


def test_an_overdue_review_is_announced_on_an_unrelated_command() -> None:
    """The point of the banner: you hear about it while doing something else."""
    fund()
    invoke(*BUY_ACME)
    review_days_ago(12)

    body = text(invoke("track"))
    assert "REVIEW OVERDUE" in body
    assert "3 day(s) past the 9-day limit" in body
    assert "thesis log buy" in body and "is blocked" in body


def test_a_book_that_has_never_been_reviewed_says_so() -> None:
    fund()
    invoke(*BUY_ACME)
    body = text(invoke("track"))
    assert "REVIEW REQUIRED" in body
    assert "never been reviewed" in body


def test_the_banner_does_not_fire_before_anything_is_held() -> None:
    fund()
    assert "REVIEW" not in text(invoke("track")).upper().replace("REVIEWED", "")


def test_the_hard_block_past_nine_days_still_stands() -> None:
    """The banner is a warning; rule 6 is still the wall."""
    fund()
    invoke(*BUY_ACME)
    review_days_ago(12)

    result = invoke(
        "log", "buy", "WILE", "--bucket", "core", "--shares", "5", "--price", "60",
        "--thesis", GOOD_THESIS,
        "--invalidation", GOOD_INVALIDATION,
        "--horizon", GOOD_HORIZON,
        "--exit-plan", GOOD_EXIT,
        "--yes",
    )
    assert result.exit_code == 1
    body = text(result)
    assert "REVIEW OVERDUE" in body  # the banner
    assert "REFUSED" in body and "rule 6" in body  # and the block


def test_a_due_soon_warning_does_not_block_a_buy() -> None:
    fund()
    invoke(*BUY_ACME)
    review_days_ago(8)

    result = invoke(
        "log", "buy", "WILE", "--bucket", "core", "--shares", "5", "--price", "60",
        "--thesis", GOOD_THESIS,
        "--invalidation", GOOD_INVALIDATION,
        "--horizon", GOOD_HORIZON,
        "--exit-plan", GOOD_EXIT,
        "--yes",
    )
    assert result.exit_code == 0, text(result)
    assert "Review due in 1 day" in text(result)


def test_the_banner_names_the_paper_book_and_its_flag() -> None:
    fund(("--paper",))
    invoke(*BUY_ACME, "--paper")
    review_days_ago(12, journal.PAPER)

    body = text(invoke("track", "--paper"))
    assert "REVIEW OVERDUE — paper book" in body
    assert "`thesis review --paper`" in body


def test_each_book_keeps_its_own_review_clock_in_the_banner() -> None:
    fund()
    fund(("--paper",))
    invoke(*BUY_ACME)
    invoke(*BUY_ACME, "--paper")
    review_days_ago(1, journal.REAL)
    review_days_ago(12, journal.PAPER)

    body = text(invoke("track"))
    assert "REVIEW OVERDUE — paper book" in body
    assert "real book" not in body


# ------------------------------------------------------------------ paper flag

def test_the_paper_flag_keeps_practice_trades_out_of_the_real_book(conn) -> None:
    fund(("--paper",))
    result = invoke(*BUY_ACME, "--paper")
    assert result.exit_code == 0, text(result)
    assert "[paper book]" in text(result)

    assert journal.find_open(conn, journal.PAPER, "ACME") is not None
    assert journal.find_open(conn, journal.REAL, "ACME") is None

    real = invoke("track")
    assert "Nothing logged" in text(real)

    paper = invoke("track", "--paper")
    assert "# Track Record — PAPER — simulated money" in text(paper)
    assert "SPY" in text(paper)


def test_paper_and_real_books_track_the_same_way_through_the_cli(conn) -> None:
    """Same trade, both books: the reported numbers must agree line for line."""
    fund()
    fund(("--paper",))
    assert invoke(*BUY_ACME).exit_code == 0
    assert invoke(*BUY_ACME, "--paper").exit_code == 0

    def body(*args: str) -> str:
        markdown = text(invoke("track", *args))
        return markdown.split("## Account", 1)[1].split("## Trade log", 1)[0]

    assert body("--paper") == body()


def test_paper_export_is_filed_separately() -> None:
    fund(("--paper",))
    invoke(*BUY_ACME, "--paper")
    assert invoke("track", "--paper", "--export").exit_code == 0
    assert (config.REPORTS_DIR / f"track_paper_{journal.today()}.md").exists()
    assert not (config.REPORTS_DIR / f"track_real_{journal.today()}.md").exists()
