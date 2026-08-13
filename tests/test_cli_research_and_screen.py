"""CLI tests for `thesis screen` and the lint gate on `thesis research`."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from thesis import brief, cli, lint, screen
from thesis.data import universe

from test_screen import FRAMES, QUALITY, UNIVERSE

runner = CliRunner()


def text(result) -> str:
    combined = result.output or ""
    try:
        if result.stderr:
            combined += result.stderr
    except (ValueError, AttributeError):
        pass
    return combined


# --------------------------------------------------------------------- screen

def stub_prices(only: set[str] | None = None):
    """A `fetch_prices` stand-in that accepts whatever keywords the CLI passes."""

    def fetch(tickers, **kwargs) -> screen.PriceFetch:
        wanted = [t for t in tickers if t in FRAMES and (only is None or t in only)]
        return screen.PriceFetch(
            frames={t: FRAMES[t] for t in wanted}, downloaded=tuple(wanted)
        )

    return fetch


@pytest.fixture
def offline_screen(monkeypatch):
    """Point the screen's fetchers at the synthetic frames from test_screen."""
    monkeypatch.setattr(screen, "fetch_prices", stub_prices())
    monkeypatch.setattr(
        screen,
        "fetch_quality",
        lambda ticker: QUALITY.get(ticker, screen.Quality(ticker, None, None)),
    )
    original_load = universe.load
    monkeypatch.setattr(
        universe,
        "load",
        lambda path=None: tuple(UNIVERSE) if path is None else original_load(path),
    )


def test_screen_prints_a_ranked_table(offline_screen) -> None:
    result = runner.invoke(cli.app, ["screen"])
    body = text(result)

    assert result.exit_code == 0, body
    assert "Screen — momentum + quality" in body
    assert "| 1 | AAA |" in body
    assert "Score = 0.5 x pctl(6m return)" in body
    # Names that failed the quality gate are ranked but not shown.
    assert "| BBB |" not in body


def test_screen_top_limits_the_table(offline_screen) -> None:
    result = runner.invoke(cli.app, ["screen", "--top", "1"])
    body = text(result)
    assert "| 1 | AAA |" in body
    assert "| 2 |" not in body


def test_screen_explain_shows_one_names_arithmetic(offline_screen) -> None:
    result = runner.invoke(cli.app, ["screen", "--explain", "BBB"])
    body = text(result)

    assert result.exit_code == 0, body
    assert "Why BBB ranked where it did" in body
    assert "Fails the quality gate" in body
    assert screen.NEGATIVE_EARNINGS in body
    # The table is still printed, so the explanation has context.
    assert "| 1 | AAA |" in body


def test_screen_explain_forces_a_quality_check_outside_the_top(offline_screen) -> None:
    result = runner.invoke(cli.app, ["screen", "--top", "1", "--explain", "EEE"])
    body = text(result)
    assert "Trailing net income" in body
    assert "Not checked" not in body


def test_screen_accepts_a_custom_universe_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(screen, "fetch_prices", stub_prices())
    monkeypatch.setattr(screen, "fetch_quality", lambda ticker: QUALITY[ticker])
    path = tmp_path / "watchlist.txt"
    path.write_text("AAA\nDDD\n", encoding="utf-8")

    result = runner.invoke(cli.app, ["screen", "--universe", str(path)])
    body = text(result)
    assert result.exit_code == 0, body
    assert "Universe 2" in body
    assert "| AAA |" in body


def test_screen_reports_an_unreadable_universe_file() -> None:
    result = runner.invoke(cli.app, ["screen", "--universe", "no-such-file.txt"])
    assert result.exit_code == 1
    assert "error" in text(result).lower()


def test_screen_output_is_stamped_with_the_snapshot_date(offline_screen) -> None:
    body = text(runner.invoke(cli.app, ["screen"]))
    assert f"snapshot {universe.AS_OF}" in body
    assert "days old" in body


def test_screen_warns_when_the_snapshot_has_gone_stale(offline_screen, monkeypatch) -> None:
    """Staleness should be caught by the calendar, not by a wall of 404s."""
    stale = (date.fromisoformat(universe.AS_OF) - timedelta(days=200)).isoformat()
    monkeypatch.setattr(universe, "AS_OF", stale)

    body = text(runner.invoke(cli.app, ["screen"]))
    assert "days old" in body
    assert "thesis universe --check" in body


# -------------------------------------------------------- thesis universe

def test_a_read_only_command_does_not_create_a_journal(offline_screen, tmp_path) -> None:
    """`screen` must not leave a journal.db behind just to check the review clock."""
    from thesis import config

    assert not config.db_path().exists()
    result = runner.invoke(cli.app, ["screen"])

    assert result.exit_code == 0, text(result)
    assert not config.db_path().exists()


def test_the_review_banner_still_fires_once_a_journal_exists(offline_screen) -> None:
    from thesis import journal

    conn = journal.connect()
    journal.add_deposit(conn, journal.REAL, 1_000.0, date(2026, 7, 1))
    check = journal.validate_buy(
        journal.BuyRequest(
            book=journal.REAL, ticker="AAA", bucket=journal.CORE, shares=1.0, price=100.0,
            thesis="A" * 60, invalidation="B" * 20, horizon="C" * 20, exit_plan="D" * 20,
            trade_date=date(2026, 7, 1),
        ),
        journal.load_state(conn, journal.REAL),
        {},
    )
    journal.log_buy(conn, check)
    conn.close()

    assert "REVIEW REQUIRED" in text(runner.invoke(cli.app, ["screen"]))


def test_universe_reports_provenance_and_upkeep(offline_screen) -> None:
    result = runner.invoke(cli.app, ["universe"])
    body = text(result)

    assert result.exit_code == 0, body
    assert universe.AS_OF in body
    assert "ticker change(s) applied" in body
    assert "renamed  BK" in body and "BNY" in body
    assert "retired  HES" in body
    assert "thesis universe --check" in body


def test_universe_check_passes_when_every_symbol_resolves(monkeypatch) -> None:
    monkeypatch.setattr(universe, "load", lambda path=None: ("AAA", "BBB"))
    monkeypatch.setattr(
        screen,
        "fetch_prices",
        lambda tickers, **kw: screen.PriceFetch(frames={t: FRAMES["AAA"] for t in tickers}),
    )

    result = runner.invoke(cli.app, ["universe", "--check"])
    assert result.exit_code == 0, text(result)
    assert "All 2 symbols returned data" in text(result)


def test_universe_check_names_the_dead_symbols_and_says_what_to_do(monkeypatch) -> None:
    monkeypatch.setattr(universe, "load", lambda path=None: ("AAA", "GONE"))
    monkeypatch.setattr(
        screen,
        "fetch_prices",
        lambda tickers, **kw: screen.PriceFetch(
            frames={"AAA": FRAMES["AAA"]}, missing=("GONE",)
        ),
    )

    result = runner.invoke(cli.app, ["universe", "--check"])
    body = text(result)

    assert result.exit_code == 1, body
    assert "1 symbol(s) returned no data" in body
    assert "GONE" in body
    assert "universe.RENAMED" in body and "universe.RETIRED" in body
    assert "retrying will not fix it" in body


def test_universe_can_audit_a_custom_list(monkeypatch, tmp_path) -> None:
    path = tmp_path / "list.txt"
    path.write_text("AAPL\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["universe", "--universe", str(path)])
    body = text(result)

    assert result.exit_code == 0, body
    assert "list.txt" in body
    assert "no snapshot date" in body


def test_screen_liquidity_floor_is_adjustable(monkeypatch) -> None:
    """Raising the floor should throw names out — the gate is really wired up."""
    monkeypatch.setattr(screen, "fetch_prices", stub_prices(only={"AAA"}))
    monkeypatch.setattr(screen, "fetch_quality", lambda ticker: QUALITY["AAA"])
    monkeypatch.setattr(universe, "load", lambda path=None: ("AAA",))

    ok = runner.invoke(cli.app, ["screen"])
    assert "| 1 | AAA |" in text(ok)

    strict = runner.invoke(cli.app, ["screen", "--min-dollar-volume", "1e12"])
    assert "No name cleared every gate" in text(strict)


# ------------------------------------------------------------------- research

def _result(report: lint.LintReport, tmp_path, attempts: int = 1) -> brief.BriefResult:
    path = tmp_path / "ACME_brief.md"
    path.write_text("# brief", encoding="utf-8")
    return brief.BriefResult(
        ticker="ACME",
        path=path,
        model="fake-model",
        input_tokens=100,
        output_tokens=200,
        lint=report,
        attempts=attempts,
    )


def test_research_reports_a_clean_lint_and_exits_zero(monkeypatch, tmp_path) -> None:
    clean = lint.lint_brief("Acme sells rocket skates [10-K 1].")
    monkeypatch.setattr(brief, "generate", lambda *a, **k: _result(clean, tmp_path))

    result = runner.invoke(cli.app, ["research", "ACME"])
    assert result.exit_code == 0, text(result)
    assert "Brief lint: clean" in text(result)


def test_research_fails_loudly_when_the_lint_does_not_clear(monkeypatch, tmp_path) -> None:
    dirty = lint.lint_brief(
        "Revenue grew 14% in fiscal 2025.\n\nInvestors should buy the shares."
    )
    monkeypatch.setattr(brief, "generate", lambda *a, **k: _result(dirty, tmp_path, attempts=2))

    result = runner.invoke(cli.app, ["research", "ACME"])
    body = text(result)

    assert result.exit_code == 1
    assert "violation" in body
    assert "no source tag" in body
    assert "Do not share it as a" in body
    assert "2 drafts" in body  # the regeneration is visible in the summary


def test_research_surfaces_the_regeneration_notice(monkeypatch, tmp_path) -> None:
    """The user must see that a redraft happened, not just the final result."""
    captured: list[str] = []

    def fake_generate(ticker, **kwargs):
        kwargs["on_notice"]("lint found 2 violation(s) in draft 1 — regenerating once:")
        captured.append(ticker)
        return _result(lint.LintReport(), tmp_path, attempts=2)

    monkeypatch.setattr(brief, "generate", fake_generate)
    result = runner.invoke(cli.app, ["research", "ACME"])

    assert captured == ["ACME"]
    assert "regenerating once" in text(result)
    assert result.exit_code == 0
