"""The Discord league: joining, standings, the weekly cycle, and the guarantee
that the bot layer cannot construct a trade the CLI would refuse.

No Discord connection and no API calls — the model client and the price/session
fetchers are injected.
"""

from __future__ import annotations

import ast
import asyncio
import datetime as dt
import json
import logging
import re
import textwrap
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import discord
import pandas as pd
import pytest

from thesis import arena, bot, config, journal, league, track

from conftest import GOOD_EXIT, GOOD_HORIZON, GOOD_INVALIDATION, GOOD_THESIS, d, spy_series

SPY = spy_series({"2026-08-01": 600.0, "2026-08-03": 610.0, "2026-08-11": 620.0})
GUILD = 111222333444555666
ALICE = 999888777666555444
BOB = 555444333222111000


@pytest.fixture
def league_db(tmp_path, monkeypatch):
    """A league database isolated from the journal and the arena."""
    monkeypatch.setenv("THESIS_LEAGUE_DB", str(tmp_path / "league.db"))
    monkeypatch.setenv("THESIS_ARENA_DB", str(tmp_path / "arena.db"))
    monkeypatch.setenv("THESIS_DB", str(tmp_path / "journal.db"))
    conn = league.connect()
    yield conn
    conn.close()


def order(ticker="ACME", shares=10.0, bucket=journal.CORE, stop=None, **over):
    kwargs = dict(
        agent="", action="buy", ticker=ticker, shares=shares, bucket=bucket,
        thesis=GOOD_THESIS, invalidation=GOOD_INVALIDATION, horizon=GOOD_HORIZON,
        exit_plan=GOOD_EXIT, stop_price=stop, outcome=None, reasoning="Screen leader.",
    )
    kwargs.update(over)
    return arena.Decision(**kwargs)


def sessions(rows: dict[str, float]):
    def fetch(ticker, start, end):
        inside = {k: v for k, v in rows.items() if start <= d(k) < end}
        if not inside:
            return pd.DataFrame()
        return pd.DataFrame(
            {"Open": list(inside.values())},
            index=pd.DatetimeIndex([pd.Timestamp(k) for k in inside]),
        )

    return fetch


# ============================================================== /join

def test_join_creates_and_funds_a_book(league_db) -> None:
    member, created = league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))

    assert created is True
    assert member.display_name == "alice"
    assert member.persona == arena.VALUE.name
    deposits = journal.deposits(league_db, member.book)
    assert len(deposits) == 1
    assert deposits[0].amount == pytest.approx(league.STARTING_CASH)


def test_a_book_name_is_always_a_legal_journal_book(league_db) -> None:
    """Discord snowflakes are far longer than a book name may be."""
    member, _ = league.join(league_db, GUILD, ALICE, "alice")
    assert journal.validate_book(member.book) == member.book
    assert len(member.book) <= 32
    assert str(ALICE) not in member.book


def test_joining_twice_does_not_double_fund(league_db) -> None:
    first, created_first = league.join(league_db, GUILD, ALICE, "alice")
    second, created_second = league.join(league_db, GUILD, ALICE, "alice again")

    assert created_first is True and created_second is False
    assert first.book == second.book
    assert len(journal.deposits(league_db, first.book)) == 1


def test_a_member_may_choose_a_mandate(league_db) -> None:
    member, _ = league.join(league_db, GUILD, ALICE, "alice", persona="momentum")
    assert member.persona == "momentum"
    assert member.mandate is arena.MOMENTUM


def test_an_unknown_mandate_is_refused(league_db) -> None:
    with pytest.raises(ValueError, match="unknown mandate"):
        league.join(league_db, GUILD, ALICE, "alice", persona="wizard")


def test_each_member_gets_their_own_book(league_db) -> None:
    alice, _ = league.join(league_db, GUILD, ALICE, "alice")
    bobby, _ = league.join(league_db, GUILD, BOB, "bob")
    assert alice.book != bobby.book
    assert {m.display_name for m in league.members(league_db, GUILD)} == {"alice", "bob"}


# ============================================================== guild scoping

def test_leagues_are_scoped_per_guild(league_db) -> None:
    other_guild = 777
    league.join(league_db, GUILD, ALICE, "alice")
    league.join(league_db, other_guild, BOB, "bob")

    assert [m.display_name for m in league.members(league_db, GUILD)] == ["alice"]
    assert [m.display_name for m in league.members(league_db, other_guild)] == ["bob"]


def test_the_same_person_in_two_servers_gets_two_books(league_db) -> None:
    here, _ = league.join(league_db, GUILD, ALICE, "alice")
    there, _ = league.join(league_db, 777, ALICE, "alice")
    assert here.book != there.book


# ============================================================== isolation

def test_league_books_live_in_their_own_database(league_db) -> None:
    member, _ = league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    outcome = league.place_order(
        league_db, member.book, order(shares=10.0), 100.0, d("2026-08-03")
    )
    assert outcome.filled

    assert config.league_db_path() != config.db_path()
    assert config.league_db_path() != config.arena_db_path()

    human = journal.connect()
    try:
        for book in (journal.REAL, journal.PAPER, member.book):
            assert journal.holdings(human, book) == ()
        assert human.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    finally:
        human.close()

    ring = arena.connect()
    try:
        assert ring.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
        assert journal.holdings(ring, member.book) == ()
    finally:
        ring.close()

    assert league_db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1


def test_members_cannot_fund_each_other(league_db) -> None:
    alice, _ = league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    bobby, _ = league.join(league_db, GUILD, BOB, "bob", on_date=d("2026-08-01"))
    league.place_order(league_db, alice.book, order(shares=10.0), 100.0, d("2026-08-03"))

    assert journal.open_holdings(league_db, alice.book)
    assert journal.open_holdings(league_db, bobby.book) == ()
    bob_cash = journal.cash_balance(journal.deposits(league_db, bobby.book), [])
    assert bob_cash == pytest.approx(league.STARTING_CASH)


# ================== the guarantee: no trade the CLI would refuse

ILLEGAL_ORDERS = [
    ("rule 1 — no thesis", order(thesis="cheap"), 1),
    ("rule 1 — no invalidation", order(invalidation=""), 1),
    ("rule 1 — no horizon", order(horizon=""), 1),
    ("rule 1 — no exit plan", order(exit_plan=""), 1),
    ("rule 2 — active with no stop", order(bucket=journal.ACTIVE, stop=None), 2),
    ("rule 2 — stop above entry", order(bucket=journal.ACTIVE, stop=150.0), 2),
    ("rule 3 — over the core cap", order(shares=300.0), 3),
    ("rule 4 — option symbol", order(ticker="AAPL250117C00200000"), 4),
    ("rule 4 — negative shares", order(shares=-5.0), 4),
    ("rule 4 — more cash than the book has", order(shares=100_000.0), 4),
]


@pytest.mark.parametrize("label,decision,rule", ILLEGAL_ORDERS, ids=[o[0] for o in ILLEGAL_ORDERS])
def test_the_bot_cannot_construct_a_trade_the_cli_would_refuse(
    league_db, conn, label, decision, rule
) -> None:
    """Route the same order through the league and through the CLI's own journal
    path, and require both to refuse it for the same reason.

    This is the load-bearing test of the whole bot layer: the league is a wrapper,
    so anything the CLI rejects it must reject identically.
    """
    member, _ = league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))

    # --- the league / bot path
    league_outcome = league.place_order(
        league_db, member.book, decision, 100.0, d("2026-08-03")
    )

    # --- the CLI path: same rules, same amount of money, a plain journal book
    journal.add_deposit(conn, journal.REAL, league.STARTING_CASH, d("2026-08-01"))
    request = journal.BuyRequest(
        book=journal.REAL, ticker=decision.ticker, bucket=decision.bucket or journal.CORE,
        shares=decision.shares, price=100.0, thesis=decision.thesis,
        invalidation=decision.invalidation, horizon=decision.horizon,
        exit_plan=decision.exit_plan, stop_price=decision.stop_price,
        trade_date=d("2026-08-03"),
    )
    with pytest.raises((journal.RuleViolation, ValueError)) as exc:
        check = journal.validate_buy(request, journal.load_state(conn, journal.REAL), {})
        journal.log_buy(conn, check)

    cli_rule = getattr(exc.value, "rule", None)

    assert league_outcome.status == "rejected", f"the league accepted {label}"
    assert league_outcome.rule == cli_rule, (
        f"{label}: league said rule {league_outcome.rule}, CLI said rule {cli_rule}"
    )
    assert league_outcome.rule == rule
    assert journal.holdings(league_db, member.book) == ()


def test_a_legal_order_is_accepted_by_both_paths(league_db, conn) -> None:
    """The mirror of the test above — the wrapper must not be refusing everything."""
    member, _ = league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    outcome = league.place_order(
        league_db, member.book, order(shares=10.0), 100.0, d("2026-08-03")
    )
    assert outcome.filled

    journal.add_deposit(conn, journal.REAL, league.STARTING_CASH, d("2026-08-01"))
    request = journal.BuyRequest(
        book=journal.REAL, ticker="ACME", bucket=journal.CORE, shares=10.0, price=100.0,
        thesis=GOOD_THESIS, invalidation=GOOD_INVALIDATION, horizon=GOOD_HORIZON,
        exit_plan=GOOD_EXIT, trade_date=d("2026-08-03"),
    )
    journal.log_buy(conn, journal.validate_buy(request, journal.load_state(conn, journal.REAL), {}))

    league_held = journal.find_open(league_db, member.book, "ACME")
    cli_held = journal.find_open(conn, journal.REAL, "ACME")
    assert league_held.shares == cli_held.shares
    assert league_held.avg_cost == cli_held.avg_cost


def test_an_order_cannot_be_placed_on_someone_elses_book(league_db) -> None:
    """A model that names another member in its JSON still trades only its own book."""
    alice, _ = league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    bobby, _ = league.join(league_db, GUILD, BOB, "bob", on_date=d("2026-08-01"))

    hijack = order(shares=10.0, agent=bobby.book)
    league.place_order(league_db, alice.book, hijack, 100.0, d("2026-08-03"))

    assert journal.find_open(league_db, alice.book, "ACME") is not None
    assert journal.find_open(league_db, bobby.book, "ACME") is None


def bot_code_calls() -> set[str]:
    """Every `x.y(...)` attribute called in bot.py, from the AST — code, not prose."""
    tree = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def bot_qualified_calls() -> set[str]:
    """`module.function` pairs called in bot.py, e.g. 'journal.connect'."""
    tree = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
    pairs: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            pairs.add(f"{node.func.value.id}.{node.func.attr}")
    return pairs


def bot_qualified_refs() -> set[str]:
    """`module.attr` pairs *referenced* in bot.py, whether called or passed along."""
    tree = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
    return {
        f"{node.value.id}.{node.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }


def bot_names() -> set[str]:
    """Every identifier referenced in bot.py's code (docstrings excluded)."""
    tree = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    return names


def test_the_bot_module_holds_no_trade_logic() -> None:
    """The bot may not write to a book directly — it must go through the league."""
    banned = {"log_buy", "log_sell", "add_deposit", "validate_buy", "log_sell"}
    assert not (bot_code_calls() & banned), (
        f"bot.py calls journal mutators: {bot_code_calls() & banned}"
    )
    # It must not construct the request object itself.
    assert "BuyRequest" not in bot_names()
    # Nor open the human's journal or the arena's book — only the league's.
    refs = bot_qualified_refs()
    assert "journal.connect" not in refs
    assert "arena.connect" not in refs
    assert "league.connect" in refs, "the bot's only database handle is the league's"


def test_the_bot_never_writes_to_a_book_itself() -> None:
    """bot.py may read holdings and use pure helpers; it may not write a row.

    Stated as a ban on writers rather than an allowlist of readers, so adding a
    harmless pure helper does not fail the test but adding a write does.
    """
    writers = {
        "log_buy", "log_sell", "add_deposit", "record_review", "snapshot_equity",
        "connect", "record_trade", "log_sell",
    }
    journal_calls = {
        call.split(".", 1)[1]
        for call in bot_qualified_calls()
        if call.startswith("journal.")
    }
    assert not (journal_calls & writers), (
        f"bot.py writes to a book directly: {journal_calls & writers}"
    )


def test_the_league_module_defines_no_rule_of_its_own() -> None:
    """Caps, cash and stops live in journal.py. The league must not restate them."""
    source = Path(league.__file__).read_text(encoding="utf-8")
    for smell in ("0.20", "0.10", "BUCKET_CAPS", "MIN_THESIS_CHARS", "REVIEW_MAX_AGE"):
        assert smell not in source, f"league.py appears to reimplement a rule: {smell}"


# ============================================================== /standings

def test_standings_rank_members_and_end_with_spy(league_db) -> None:
    alice, _ = league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    bobby, _ = league.join(league_db, GUILD, BOB, "bob", on_date=d("2026-08-01"))
    # alice wins: bought at 100, now 130.
    league.place_order(league_db, alice.book, order(shares=10.0), 100.0, d("2026-08-03"))

    rows = league.standings(
        league_db, GUILD, {"ACME": 130.0}, SPY, as_of=d("2026-08-11")
    )

    assert [r.label for r in rows[:2]] == ["alice", "bob"]
    assert rows[0].pnl_pct > rows[1].pnl_pct
    assert rows[-1].is_benchmark
    assert "SPY" in rows[-1].label


def test_standings_are_empty_before_anyone_joins(league_db) -> None:
    assert league.standings(league_db, GUILD, {}, SPY) == []
    assert "Nobody has joined yet" in league.render_standings([], "Test")


def test_standings_only_show_the_asking_guild(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    league.join(league_db, 777, BOB, "bob", on_date=d("2026-08-01"))

    rows = league.standings(league_db, GUILD, {}, SPY, as_of=d("2026-08-11"))
    assert [r.label for r in rows if not r.is_benchmark] == ["alice"]


def test_rendered_standings_fit_a_discord_message(league_db) -> None:
    for i in range(25):
        league.join(league_db, GUILD, 1000 + i, f"member{i:02d}", on_date=d("2026-08-01"))
    rows = league.standings(league_db, GUILD, {}, SPY, as_of=d("2026-08-11"))
    text = league.render_standings(rows, "Test Server")

    assert "```" in text
    assert "mandate" in text
    for chunk in bot.split_message(text):
        assert len(chunk) <= bot.MESSAGE_LIMIT


# ============================================================== weekly cycle

class FakeClient:
    """Returns a canned decision payload per call, and records the model used."""

    def __init__(self, *payloads: dict) -> None:
        self.payloads = list(payloads)
        self.models: list[str] = []
        self.systems: list[str] = []
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.models.append(kwargs["model"])
            self.outer.systems.append(kwargs["system"])
            payload = (
                self.outer.payloads.pop(0)
                if self.outer.payloads
                else {"reasoning": "Nothing to do.", "decisions": []}
            )
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            usage = type("U", (), {"input_tokens": 500, "output_tokens": 100})()
            return type("R", (), {"content": [block], "usage": usage})()


def payload(reasoning: str, *decisions: dict) -> dict:
    return {"reasoning": reasoning, "decisions": list(decisions)}


def raw_order(ticker="ACME", shares=10.0, bucket="core", stop=None) -> dict:
    return {
        "action": "buy", "ticker": ticker, "shares": shares, "bucket": bucket,
        "thesis": GOOD_THESIS, "invalidation": GOOD_INVALIDATION,
        "horizon": GOOD_HORIZON, "exit_plan": GOOD_EXIT, "stop_price": stop,
        "outcome": None, "reasoning": "Screen leader.",
    }


def test_a_cycle_places_orders_and_captures_reasoning(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    client = FakeClient(payload("Buying the leader.", raw_order()))

    result = league.run_cycle(
        league_db, GUILD, client, screen_table="| # | Ticker |\n|---|---|\n| 1 | ACME |",
        briefs={}, prices={"ACME": 100.0}, as_of=d("2026-08-01"),
        fetcher=sessions({}),
    )

    assert result.reasoning == {"alice": "Buying the leader."}
    assert result.ordered == {"alice": 1}
    assert len(result.pending) == 1        # no session after 08-01 yet
    assert result.model == config.LEAGUE_MODEL


def test_league_cycles_run_on_sonnet(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    client = FakeClient()
    league.run_cycle(
        league_db, GUILD, client, screen_table="", briefs={}, prices={},
        as_of=d("2026-08-01"), fetcher=sessions({}),
    )
    assert client.models == ["claude-sonnet-5"]
    assert config.LEAGUE_MODEL == "claude-sonnet-5"


def test_each_member_is_asked_with_their_own_mandate(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", persona="value", on_date=d("2026-08-01"))
    league.join(league_db, GUILD, BOB, "bob", persona="momentum", on_date=d("2026-08-01"))
    client = FakeClient()

    league.run_cycle(
        league_db, GUILD, client, screen_table="", briefs={}, prices={},
        as_of=d("2026-08-01"), fetcher=sessions({}),
    )
    assert client.systems == [arena.VALUE.system_prompt, arena.MOMENTUM.system_prompt]


def test_an_order_fills_next_cycle_at_the_open_after_its_decision(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    fetch = sessions({"2026-08-03": 101.0, "2026-08-11": 200.0})

    first = league.run_cycle(
        league_db, GUILD, FakeClient(payload("Buying.", raw_order())),
        screen_table="", briefs={}, prices={"ACME": 100.0}, as_of=d("2026-08-01"),
        fetcher=sessions({}),
    )
    assert len(first.pending) == 1

    second = league.run_cycle(
        league_db, GUILD, FakeClient(), screen_table="", briefs={},
        prices={"ACME": 105.0}, as_of=d("2026-08-10"), fetcher=fetch,
    )
    assert len(second.filled) == 1
    _, order_obj, outcome = second.filled[0]
    assert order_obj.decided_on == d("2026-08-01")
    assert outcome.fill_date == d("2026-08-03")
    assert outcome.fill_price == pytest.approx(101.0)


def test_a_rule_breaking_order_is_reported_as_refused(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    league.run_cycle(
        league_db, GUILD, FakeClient(payload("Going big.", raw_order(shares=300.0))),
        screen_table="", briefs={}, prices={"ACME": 100.0}, as_of=d("2026-08-01"),
        fetcher=sessions({}),
    )
    result = league.run_cycle(
        league_db, GUILD, FakeClient(), screen_table="", briefs={},
        prices={"ACME": 100.0}, as_of=d("2026-08-10"),
        fetcher=sessions({"2026-08-03": 100.0}),
    )

    assert len(result.rejected) == 1
    _, _, outcome = result.rejected[0]
    assert outcome.rule == 3
    assert journal.holdings(league_db, "m1") == ()


def test_one_members_failure_does_not_kill_the_cycle(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    league.join(league_db, GUILD, BOB, "bob", on_date=d("2026-08-01"))

    class Flaky(FakeClient):
        def __init__(self):
            super().__init__()
            self.calls = 0

        class _Messages(FakeClient._Messages):
            def create(self, **kwargs):
                self.outer.calls += 1
                if self.outer.calls == 1:
                    raise RuntimeError("rate limited")
                return super().create(**kwargs)

    client = Flaky()
    client.messages = Flaky._Messages(client)
    result = league.run_cycle(
        league_db, GUILD, client, screen_table="", briefs={}, prices={},
        as_of=d("2026-08-01"), fetcher=sessions({}),
    )
    assert "no answer this cycle" in result.reasoning["alice"]
    assert result.reasoning["bob"] == "Nothing to do."


def test_a_cycle_records_the_weekly_review_so_rule_six_stays_satisfied(league_db) -> None:
    member, _ = league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    league.run_cycle(
        league_db, GUILD, FakeClient(), screen_table="", briefs={}, prices={},
        as_of=d("2026-08-01"), fetcher=sessions({}),
    )
    review = journal.last_review(league_db, member.book)
    assert review is not None and review.review_date == d("2026-08-01")


# ============================================================== the weekly post

def test_the_weekly_post_carries_every_agents_reasoning(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    league.join(league_db, GUILD, BOB, "bob", persona="monk", on_date=d("2026-08-01"))
    client = FakeClient(
        payload("Semis are extended but the trend is intact.", raw_order()),
        payload("Cash is a position. Nothing clears the bar this month."),
    )
    result = league.run_cycle(
        league_db, GUILD, client, screen_table="", briefs={}, prices={"ACME": 100.0},
        as_of=d("2026-08-01"), fetcher=sessions({}),
    )
    text = league.render_cycle(result, league.display_names(league_db, GUILD))

    assert "League cycle — 2026-08-01" in text
    assert "claude-sonnet-5" in text
    assert "__alice__" in text and "__bob__" in text
    assert "Semis are extended but the trend is intact." in text
    assert "Cash is a position." in text
    assert "No trades this week." in text          # bob ordered nothing
    assert "Simulated money" in text


def test_the_weekly_post_names_refusals_with_their_rule(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    league.run_cycle(
        league_db, GUILD, FakeClient(payload("Big swing.", raw_order(shares=300.0))),
        screen_table="", briefs={}, prices={"ACME": 100.0}, as_of=d("2026-08-01"),
        fetcher=sessions({}),
    )
    result = league.run_cycle(
        league_db, GUILD, FakeClient(), screen_table="", briefs={},
        prices={"ACME": 100.0}, as_of=d("2026-08-10"),
        fetcher=sessions({"2026-08-03": 100.0}),
    )
    text = league.render_cycle(result, league.display_names(league_db, GUILD))

    assert "Refused by the account rules" in text
    assert "forfeited, not repaired" in text
    assert "rule 3" in text
    assert "alice" in text


def test_the_post_says_so_when_the_league_is_empty(league_db) -> None:
    result = league.run_cycle(
        league_db, GUILD, FakeClient(), screen_table="", briefs={}, prices={},
        as_of=d("2026-08-01"), fetcher=sessions({}),
    )
    assert "Nobody has joined yet" in league.render_cycle(result, {})


def test_a_long_post_is_split_into_sendable_chunks() -> None:
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(120))
    chunks = bot.split_message(text)

    assert len(chunks) > 1
    assert all(len(c) <= bot.MESSAGE_LIMIT for c in chunks)
    assert "".join(c.replace("\n", "") for c in chunks) == text.replace("\n", "")


# ============================================================== the schedule

def test_the_cycle_is_scheduled_for_monday_evening_utc() -> None:
    # A Wednesday -> the coming Monday.
    wednesday = dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.timezone.utc)
    seconds = bot.seconds_until_next_cycle(wednesday)
    landing = wednesday + dt.timedelta(seconds=seconds)
    assert landing.weekday() == bot.CYCLE_WEEKDAY
    assert landing.hour == bot.CYCLE_HOUR


def test_the_schedule_never_returns_a_past_or_zero_delay() -> None:
    # Exactly on the mark: must roll to next week rather than fire immediately.
    on_time = dt.datetime(2026, 8, 10, bot.CYCLE_HOUR, 0, tzinfo=dt.timezone.utc)
    seconds = bot.seconds_until_next_cycle(on_time)
    assert seconds > 0
    assert seconds == pytest.approx(7 * 24 * 3600)

    for hour in range(0, 24, 3):
        now = dt.datetime(2026, 8, 10, hour, 30, tzinfo=dt.timezone.utc)
        assert bot.seconds_until_next_cycle(now) > 0


# ============================================================== the token

def test_the_token_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "  abc123  ")
    assert config.discord_token() == "abc123"


def test_a_missing_token_is_a_clear_refusal_to_start(monkeypatch) -> None:
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN is not set"):
        config.discord_token()


def test_an_empty_token_is_treated_as_missing(monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_TOKEN", "   ")
    with pytest.raises(RuntimeError, match="DISCORD_TOKEN is not set"):
        config.discord_token()


def test_the_token_is_read_once_and_never_sent_anywhere() -> None:
    """A leaked bot token is a full account takeover, so it must not reach a reply."""
    source = Path(bot.__file__).read_text(encoding="utf-8")
    assert source.count("config.discord_token()") == 1, "read the token in exactly one place"

    for number, line in enumerate(source.splitlines(), start=1):
        if "token" not in line.lower():
            continue
        for leak in ("send(", "followup", "log.info", "log.warning", "print("):
            assert leak not in line, f"line {number} may leak the token: {line.strip()}"


# ==================================== /buy and /sell: the member's own trades

@pytest.fixture
def offline(monkeypatch):
    """No network: marks and last closes are fixed."""
    monkeypatch.setattr(bot, "fetch_marks", lambda tickers: {t: 100.0 for t in tickers})
    monkeypatch.setattr(bot, "fetch_one_price", lambda ticker: 100.0)


def seam(connect=league.connect) -> bot.LeagueBot:
    """A LeagueBot with only its database seam wired — no Discord, no client."""
    instance = bot.LeagueBot.__new__(bot.LeagueBot)
    instance._connect = connect
    return instance


PLAN = dict(
    shares="10", thesis=GOOD_THESIS, invalidation=GOOD_INVALIDATION,
    horizon=GOOD_HORIZON, exit_plan=GOOD_EXIT,
)


def test_a_complete_buy_is_logged(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    text = seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)

    assert "**Bought** 10 ACME at $100.00" in text
    assert "1.0% of your book" in text
    held = journal.find_open(league_db, "m1", "ACME")
    assert held.shares == pytest.approx(10.0)
    assert held.position.thesis == GOOD_THESIS
    assert held.position.opened_at.endswith("+00:00")


def test_buying_without_a_book_says_to_join(league_db, offline) -> None:
    assert "run `/join` first" in seam().buy_text(
        GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN
    )


def test_a_buy_uses_the_last_close_when_no_price_is_given(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, None, None, **PLAN)
    assert journal.find_open(league_db, "m1", "ACME").avg_cost == pytest.approx(100.0)


def test_non_numeric_shares_are_refused_not_crashed(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    text = seam().buy_text(
        GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **{**PLAN, "shares": "ten"}
    )
    assert text.startswith(journal.REFUSAL_PREFIX)
    assert "must be a number" in text
    assert journal.holdings(league_db, "m1") == ()


REFUSALS = [
    ("rule 1 — thesis too short", {"thesis": "cheap"}, 1),
    ("rule 1 — no invalidation", {"invalidation": ""}, 1),
    ("rule 1 — no horizon", {"horizon": ""}, 1),
    ("rule 1 — no exit plan", {"exit_plan": ""}, 1),
    ("rule 3 — over the cap", {"shares": "300"}, 3),
    ("rule 4 — negative shares", {"shares": "-5"}, 4),
]


@pytest.mark.parametrize("label,override,rule", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_a_refused_buy_uses_the_exact_cli_wording(
    league_db, conn, offline, label, override, rule
) -> None:
    """Same rule, same sentence, whichever surface the trade came from."""
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    plan = {**PLAN, **override}
    text = seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **plan)

    # Reproduce the same violation through the CLI's own path and compare strings.
    journal.add_deposit(conn, journal.REAL, league.STARTING_CASH, d("2026-08-01"))
    request = journal.BuyRequest(
        book=journal.REAL, ticker="ACME", bucket=journal.CORE,
        shares=float(plan["shares"]), price=100.0, thesis=plan["thesis"],
        invalidation=plan["invalidation"], horizon=plan["horizon"],
        exit_plan=plan["exit_plan"], trade_date=d("2026-08-01"),
    )
    with pytest.raises(journal.RuleViolation) as exc:
        journal.validate_buy(request, journal.load_state(conn, journal.REAL), {})

    assert text == journal.refusal_text(exc.value), label
    assert text.startswith(f"{journal.REFUSAL_PREFIX} — rule {rule}:")
    assert journal.holdings(league_db, "m1") == ()


def test_an_active_buy_without_a_stop_is_refused_with_rule_two(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    text = seam().buy_text(GUILD, ALICE, "ACME", journal.ACTIVE, 100.0, None, **PLAN)
    assert text.startswith(f"{journal.REFUSAL_PREFIX} — rule 2:")
    assert "require a stop level" in text


def test_an_active_buy_with_a_stop_below_entry_is_accepted(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    text = seam().buy_text(GUILD, ALICE, "ACME", journal.ACTIVE, 100.0, 92.0, **PLAN)
    assert "**Bought**" in text
    assert journal.find_open(league_db, "m1", "ACME").position.stop_price == 92.0


def test_the_cli_and_the_bot_share_one_refusal_renderer() -> None:
    """Not a coincidence of formatting — one function, used by both."""
    from pathlib import Path as P

    from thesis import cli

    assert "journal.refusal_text" in P(cli.__file__).read_text(encoding="utf-8")
    assert "journal.refusal_text" in P(bot.__file__).read_text(encoding="utf-8")
    violation = journal.RuleViolation(3, "core names cap at 20%")
    assert journal.refusal_text(violation) == "REFUSED — rule 3: core names cap at 20%"


def test_a_sell_closes_the_position_and_records_the_outcome(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)

    text = seam().sell_text(
        GUILD, ALICE, "ACME", 130.0, "", "Re-rated on the print; thesis played out."
    )
    assert "**Sold** ACME at $130.00 — closed" in text
    assert "+$300.00" in text
    assert "T+1" in text
    assert journal.find_open(league_db, "m1", "ACME") is None


def test_a_partial_sell_leaves_the_position_open(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)
    text = seam().sell_text(
        GUILD, ALICE, "ACME", 130.0, "4", "Took some off into strength."
    )
    assert "open with 6 left" in text


# -- /sell held to the same byte-identical standard as /buy ------------------

#: Every violation `journal.validate_sell` can raise, in the order it checks
#: them. The fifth thing that can refuse a sell — holding nothing in that name —
#: is deliberately absent; see the test below it for why it cannot be pinned.
SELL_REFUSALS = [
    ("rule 4 — more shares than held", {"shares": "25"}, 4),
    ("rule 4 — negative shares", {"shares": "-5"}, 4),
    ("rule 4 — negative price", {"price": -5.0}, 4),
    ("no honest outcome line", {"outcome": "won"}, None),
]

GOOD_OUTCOME = "Re-rated on the services print; the thesis played out early."


def cli_book_holding_ten_acme(conn) -> journal.Holding:
    """A plain journal book holding exactly what the league member holds.

    The over-sell message interpolates the held size and ticker, so the two books
    must match for a string comparison to mean anything.
    """
    journal.add_deposit(conn, journal.REAL, league.STARTING_CASH, d("2026-08-01"))
    request = journal.BuyRequest(
        book=journal.REAL, ticker="ACME", bucket=journal.CORE, shares=10.0,
        price=100.0, thesis=GOOD_THESIS, invalidation=GOOD_INVALIDATION,
        horizon=GOOD_HORIZON, exit_plan=GOOD_EXIT, trade_date=d("2026-08-01"),
    )
    journal.log_buy(
        conn, journal.validate_buy(request, journal.load_state(conn, journal.REAL), {})
    )
    holding = journal.find_open(conn, journal.REAL, "ACME")
    assert holding is not None and holding.shares == pytest.approx(10.0)
    return holding


@pytest.mark.parametrize(
    "label,override,rule", SELL_REFUSALS, ids=[r[0] for r in SELL_REFUSALS]
)
def test_a_refused_sell_uses_the_exact_cli_wording(
    league_db, conn, offline, label, override, rule
) -> None:
    """Same rule, same sentence, whichever surface the sell came from.

    The mirror of `test_a_refused_buy_uses_the_exact_cli_wording`: route the same
    illegal sell through `/sell` and through the CLI's own `journal.validate_sell`,
    and require the rendered refusals to be equal — not merely similar.
    """
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)

    args = {"ticker": "ACME", "price": 130.0, "shares": "", "outcome": GOOD_OUTCOME}
    args.update(override)

    # --- the /sell path
    text = seam().sell_text(
        GUILD, ALICE, args["ticker"], args["price"], args["shares"], args["outcome"]
    )

    # --- the CLI path, from an identical book
    holding = cli_book_holding_ten_acme(conn)
    count = float(args["shares"]) if str(args["shares"]).strip() else holding.shares
    with pytest.raises(journal.RuleViolation) as exc:
        journal.validate_sell(holding, count, args["price"], args["outcome"])

    assert text == journal.refusal_text(exc.value), label
    assert exc.value.rule == rule
    assert text.startswith(journal.REFUSAL_PREFIX)

    # Neither book moved.
    assert journal.find_open(league_db, "m1", "ACME").shares == pytest.approx(10.0)
    assert journal.find_open(conn, journal.REAL, "ACME").shares == pytest.approx(10.0)


def test_the_over_sell_refusal_quotes_the_real_held_size(league_db, conn, offline) -> None:
    """The interpolated numbers have to match too, or equality is accidental."""
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)

    text = seam().sell_text(GUILD, ALICE, "ACME", 130.0, "25", GOOD_OUTCOME)

    assert "you hold 10 shares of ACME, cannot sell 25" in text
    assert "no shorting in v1" in text
    holding = cli_book_holding_ten_acme(conn)
    with pytest.raises(journal.RuleViolation) as exc:
        journal.validate_sell(holding, 25.0, 130.0, GOOD_OUTCOME)
    assert text == journal.refusal_text(exc.value)


def test_a_legal_sell_lands_identically_on_both_paths(league_db, conn, offline) -> None:
    """The mirror assertion — the wrapper must not be refusing everything."""
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)
    seam().sell_text(GUILD, ALICE, "ACME", 130.0, "", GOOD_OUTCOME)

    holding = cli_book_holding_ten_acme(conn)
    journal.validate_sell(holding, 10.0, 130.0, GOOD_OUTCOME)
    journal.log_sell(conn, holding, 10.0, 130.0, GOOD_OUTCOME, d("2026-08-01"))

    league_closed = journal.closed_holdings(league_db, "m1")
    cli_closed = journal.closed_holdings(conn, journal.REAL)
    assert len(league_closed) == len(cli_closed) == 1
    assert league_closed[0].realized_pnl == pytest.approx(cli_closed[0].realized_pnl)
    assert league_closed[0].realized_pnl == pytest.approx(300.0)
    assert journal.find_open(league_db, "m1", "ACME") is None
    assert journal.find_open(conn, journal.REAL, "ACME") is None


def test_the_no_position_refusal_is_league_only_and_deliberately_so(
    league_db, offline
) -> None:
    """One sell-side refusal cannot be pinned to CLI equality, and this records why.

    Holding nothing in a name never reaches `journal.validate_sell` — there is no
    holding to validate. The CLI reports it as a plain error (`error: no open ACME
    position in the real book`) and exits 1; the league raises an unnumbered
    `RuleViolation` so the member gets the same REFUSED shape as every other
    rejection. The two messages differ on purpose, and no equality assertion
    would be honest here.
    """
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    text = seam().sell_text(GUILD, ALICE, "ACME", 130.0, "", GOOD_OUTCOME)

    assert text.startswith(journal.REFUSAL_PREFIX)
    assert "no open ACME position" in text
    assert "your book is empty" in text
    # Unnumbered: it is journal discipline, not one of the seven rules.
    assert "rule" not in text


def test_every_validate_sell_violation_is_enumerated() -> None:
    """If a new refusal is added to validate_sell, this fails until it is pinned."""
    import inspect

    source = inspect.getsource(journal.validate_sell)
    raised = source.count("raise RuleViolation")
    assert raised == len(SELL_REFUSALS), (
        f"journal.validate_sell raises {raised} refusals but SELL_REFUSALS "
        f"enumerates {len(SELL_REFUSALS)} — add the missing case"
    )


def test_selling_a_name_you_do_not_hold_is_refused(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    text = seam().sell_text(GUILD, ALICE, "NONE", 100.0, "", "Closing it out now.")
    assert text.startswith(journal.REFUSAL_PREFIX)
    assert "no open NONE position" in text


def test_a_sell_needs_an_honest_outcome_line(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)
    text = seam().sell_text(GUILD, ALICE, "ACME", 130.0, "", "won")
    assert text.startswith(journal.REFUSAL_PREFIX)
    assert "what actually happened" in text
    assert journal.find_open(league_db, "m1", "ACME") is not None


# ==================================== /review and the 9-day lockout

def test_review_clears_the_lockout(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)

    stale = journal.today() - dt.timedelta(days=12)
    journal.record_review(league_db, "m1", "discord review", stale)
    assert league.review_status(league_db, "m1").state == "overdue"

    text = seam().review_text(GUILD, ALICE, "back from holiday")
    assert "**Review recorded.**" in text
    assert league.review_status(league_db, "m1").state == "ok"


def test_a_stale_review_locks_out_buying(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)
    journal.record_review(
        league_db, "m1", "discord review", journal.today() - dt.timedelta(days=12)
    )

    text = seam().buy_text(GUILD, ALICE, "WILE", journal.CORE, 100.0, None, **PLAN)
    assert text.startswith(f"{journal.REFUSAL_PREFIX} — rule 6:")
    assert "thesis review" in text
    assert journal.find_open(league_db, "m1", "WILE") is None


def test_nine_days_is_still_inside_the_window(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)
    journal.record_review(
        league_db, "m1", "discord review",
        journal.today() - dt.timedelta(days=journal.REVIEW_MAX_AGE_DAYS),
    )
    text = seam().buy_text(GUILD, ALICE, "WILE", journal.CORE, 100.0, None, **PLAN)
    assert "**Bought**" in text


def test_the_weekly_cycle_review_does_not_clear_a_members_lockout(league_db, offline) -> None:
    """Otherwise a running agent keeps the lockout permanently open and `/review`
    is decorative. A person who trades by hand has to look at their own book."""
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)

    # The agent's cycle stamps a review today — but it is not a human review.
    arena.record_cycle_review(league_db, "m1", journal.today())
    assert journal.last_review(league_db, "m1") is not None
    assert league.last_human_review(league_db, "m1") is None
    assert league.review_status(league_db, "m1").state == "never"

    text = seam().buy_text(GUILD, ALICE, "WILE", journal.CORE, 100.0, None, **PLAN)
    assert text.startswith(f"{journal.REFUSAL_PREFIX} — rule 6:")


def test_review_shows_each_position_against_its_own_trigger(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.ACTIVE, 100.0, 92.0, **PLAN)

    text = seam().review_text(GUILD, ALICE)
    assert "__ACME__" in text
    assert "Invalidation, as you wrote it:" in text
    assert GOOD_INVALIDATION in text
    assert f"within {journal.REVIEW_MAX_AGE_DAYS} days" in text


def test_review_with_an_empty_book_says_there_is_nothing_to_check(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    text = seam().review_text(GUILD, ALICE)
    assert "nothing to review" in text or "nothing to check" in text


# ==================================== /research: the weekly cached brief

BRIEF_BODY = "# ACME — Research Brief\n\n## 1. Business\nThey sell rocket skates [10-K 1].\n"


class FakeBriefResult:
    def __init__(self, path, model, ok=True):
        self.path = path
        self.model = model
        self.ticker = path.stem.split("_")[0]
        self.lint = type("L", (), {"ok": ok, "render": lambda self: "2 violations"})()
        self.input_tokens = 1000
        self.output_tokens = 500


@pytest.fixture
def league_briefs(tmp_path, monkeypatch):
    directory = tmp_path / "briefs" / "league"
    monkeypatch.setattr(league, "briefs_dir", lambda: directory)
    return directory


def test_a_cache_miss_generates_on_sonnet_into_the_league_store(league_briefs) -> None:
    calls: list[dict] = []

    def generator(ticker, **kwargs):
        calls.append({"ticker": ticker, **kwargs})
        kwargs["briefs_dir"].mkdir(parents=True, exist_ok=True)
        path = kwargs["briefs_dir"] / f"{ticker}_{journal.today()}.md"
        path.write_text(BRIEF_BODY, encoding="utf-8")
        return FakeBriefResult(path, kwargs["model"])

    answer = league.research("acme", generator=generator)

    assert calls[0]["model"] == "claude-sonnet-5" == config.LEAGUE_MODEL
    assert calls[0]["briefs_dir"] == league_briefs
    assert answer.cached is False
    assert answer.servable
    assert "rocket skates" in answer.text


def test_a_second_call_in_the_same_week_is_served_from_cache(league_briefs) -> None:
    league_briefs.mkdir(parents=True)
    (league_briefs / f"ACME_{journal.today()}.md").write_text(BRIEF_BODY, encoding="utf-8")

    def generator(ticker, **kwargs):
        raise AssertionError("must not regenerate within the same week")

    answer = league.research("ACME", generator=generator)
    assert answer.cached is True
    assert "rocket skates" in answer.text
    assert "cached this week" in league.render_brief(answer)


def test_last_weeks_brief_is_not_reused(league_briefs) -> None:
    league_briefs.mkdir(parents=True)
    old = journal.today() - dt.timedelta(days=9)
    (league_briefs / f"ACME_{old}.md").write_text("stale\n", encoding="utf-8")
    regenerated: list[str] = []

    def generator(ticker, **kwargs):
        regenerated.append(ticker)
        path = kwargs["briefs_dir"] / f"{ticker}_{journal.today()}.md"
        path.write_text(BRIEF_BODY, encoding="utf-8")
        return FakeBriefResult(path, kwargs["model"])

    answer = league.research("ACME", generator=generator)
    assert regenerated == ["ACME"]
    assert answer.cached is False


def test_the_cache_key_is_the_iso_week() -> None:
    monday = date(2026, 8, 10)
    sunday = date(2026, 8, 16)
    next_monday = date(2026, 8, 17)
    assert league.week_key(monday) == league.week_key(sunday)
    assert league.week_key(monday) != league.week_key(next_monday)


def test_a_brief_that_fails_the_lint_gate_is_not_served(league_briefs) -> None:
    """An unsourced brief posted to a room of people is worse than no brief."""

    def generator(ticker, **kwargs):
        kwargs["briefs_dir"].mkdir(parents=True, exist_ok=True)
        path = kwargs["briefs_dir"] / f"{ticker}_{journal.today()}.md"
        path.write_text("Revenue grew 14%.\n", encoding="utf-8")
        return FakeBriefResult(path, kwargs["model"], ok=False)

    answer = league.research("ACME", generator=generator)

    assert answer.lint_ok is False
    assert answer.servable is False
    text = league.render_brief(answer)
    assert "failed its citation check" in text
    assert "not being served" in text
    assert "Revenue grew 14%" not in text


def test_league_briefs_never_land_in_the_human_store() -> None:
    """Sonnet-generated league briefs must not be mistaken for the user's research."""
    assert league.briefs_dir() != config.BRIEFS_DIR
    assert league.briefs_dir().parent == config.BRIEFS_DIR


def test_research_needs_a_book(league_db, league_briefs) -> None:
    assert "run `/join` first" in seam().research_text(GUILD, ALICE, "ACME")


def test_a_long_brief_is_truncated_to_fit_a_message(league_briefs) -> None:
    answer = league.BriefAnswer("ACME", "x\n" * 4000, None, cached=True)
    text = league.render_brief(answer)
    assert "truncated" in text
    assert len(text) < 2200


# ==================================== per-user daily rate limits

def test_a_limit_is_per_user_per_command_per_day(league_db) -> None:
    for i in range(1, league.DAILY_LIMITS["research"] + 1):
        verdict = league.check_rate(league_db, GUILD, ALICE, "research")
        assert verdict.allowed, f"attempt {i} should be allowed"
    assert not league.check_rate(league_db, GUILD, ALICE, "research").allowed

    # A different command is unaffected...
    assert league.check_rate(league_db, GUILD, ALICE, "buy").allowed
    # ...and so is a different member.
    assert league.check_rate(league_db, GUILD, BOB, "research").allowed


def test_the_limit_resets_the_next_day(league_db) -> None:
    today = d("2026-08-10")
    for _ in range(league.DAILY_LIMITS["research"] + 2):
        league.check_rate(league_db, GUILD, ALICE, "research", on_day=today)
    assert not league.check_rate(league_db, GUILD, ALICE, "research", on_day=today).allowed
    assert league.check_rate(
        league_db, GUILD, ALICE, "research", on_day=d("2026-08-11")
    ).allowed


def test_a_refused_buy_does_not_consume_the_allowance(league_db, offline) -> None:
    """A refusal is pure validation with no API cost, and for someone learning the
    discipline it is the teaching moment — so it must not be rationed.

    This inverts an earlier test that asserted the opposite; the behaviour was
    changed deliberately, not the assertion weakened.
    """
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))

    for _ in range(league.DAILY_LIMITS["buy"] * 3):   # far past the cap
        text = seam().buy_text(
            GUILD, ALICE, "ACME", journal.CORE, 100.0, None,
            **{**PLAN, "thesis": "cheap"},            # refused every time
        )
        assert text.startswith(f"{journal.REFUSAL_PREFIX} — rule 1:")

    assert league.usage_today(league_db, GUILD, ALICE).get("buy", 0) == 0
    # And a good buy still goes through afterwards, at a fresh count.
    good = seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)
    assert "**Bought**" in good
    assert f"used 1/{league.DAILY_LIMITS['buy']} today" in good


def test_a_refused_sell_does_not_consume_the_allowance(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)

    for _ in range(league.DAILY_LIMITS["sell"] * 3):
        text = seam().sell_text(GUILD, ALICE, "ACME", 130.0, "", "won")
        assert text.startswith(journal.REFUSAL_PREFIX)

    assert league.usage_today(league_db, GUILD, ALICE).get("sell", 0) == 0
    good = seam().sell_text(
        GUILD, ALICE, "ACME", 130.0, "", "Re-rated on the print; thesis played out."
    )
    assert "**Sold**" in good
    assert f"used 1/{league.DAILY_LIMITS['sell']} today" in good


#: Distinct legal symbols — rule 4 refuses anything with a digit in it.
SPARE_TICKERS = [
    "AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH",
    "III", "JJJ", "KKK", "LLL", "MMM", "NNN",
]


def clear_the_review_gate(conn, book: str = "m1") -> None:
    """A fresh human review, so rule 6 does not stand in for the rate limit.

    Once a member holds anything, rule 6 blocks the next buy until they review —
    which is correct, and would otherwise mask what these tests are measuring.
    """
    journal.record_review(conn, book, "discord review", journal.today())


def test_only_accepted_trades_increment_the_counter(league_db, offline) -> None:
    """The counter tracks trades placed, not attempts made."""
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    usage = lambda cmd: league.usage_today(league_db, GUILD, ALICE).get(cmd, 0)

    assert usage("buy") == 0
    seam().buy_text(GUILD, ALICE, "AAA", journal.CORE, 100.0, None,
                    **{**PLAN, "shares": "300"})      # rule 3
    assert usage("buy") == 0, "a refusal must leave the counter alone"

    seam().buy_text(GUILD, ALICE, "AAA", journal.CORE, 100.0, None, **PLAN)
    assert usage("buy") == 1, "an acceptance must increment it"

    clear_the_review_gate(league_db)
    seam().buy_text(GUILD, ALICE, "BBB", journal.CORE, 100.0, None, **PLAN)
    assert usage("buy") == 2


def test_the_cap_still_binds_on_accepted_buys(league_db, offline) -> None:
    """Charging only on acceptance must not turn the cap off."""
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    cap = league.DAILY_LIMITS["buy"]

    for i in range(cap):
        clear_the_review_gate(league_db)
        text = seam().buy_text(
            GUILD, ALICE, SPARE_TICKERS[i], journal.CORE, 10.0, None,
            **{**PLAN, "shares": "5"},
        )
        assert "**Bought**" in text, f"buy {i + 1} of {cap} should be allowed: {text}"

    clear_the_review_gate(league_db)
    blocked = seam().buy_text(
        GUILD, ALICE, "ZZZ", journal.CORE, 10.0, None, **{**PLAN, "shares": "5"}
    )
    assert "Daily limit reached for `/buy`" in blocked
    assert league.usage_today(league_db, GUILD, ALICE)["buy"] == cap
    assert journal.find_open(league_db, "m1", "ZZZ") is None


def test_exactly_the_cap_many_trades_get_through(league_db, offline) -> None:
    """Off-by-one guard: the cap is the number of accepted trades, not one fewer."""
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    cap = league.DAILY_LIMITS["buy"]
    accepted = 0
    for i in range(cap + 4):
        clear_the_review_gate(league_db)
        if "**Bought**" in seam().buy_text(
            GUILD, ALICE, SPARE_TICKERS[i], journal.CORE, 10.0, None,
            **{**PLAN, "shares": "5"},
        ):
            accepted += 1
    assert accepted == cap


def test_research_still_charges_on_attempt(league_db, league_briefs) -> None:
    """A /research miss generates a brief and spends money, so the attempt is the
    billable event — unlike a buy, where the refusal is free."""
    assert "research" not in league.CHARGE_ON_ACCEPTANCE
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))

    # A member with no book still burns the attempt on /buy? No — but /research
    # charges as soon as the command runs.
    seam().research_text(GUILD, ALICE, "NOPE")
    assert league.usage_today(league_db, GUILD, ALICE).get("research", 0) == 1


def test_rate_status_reads_without_charging(league_db) -> None:
    for _ in range(5):
        verdict = league.rate_status(league_db, GUILD, ALICE, "buy")
        assert verdict.used == 0
        assert verdict.charged is False
    assert league.usage_today(league_db, GUILD, ALICE) == {}


def test_consume_rate_charges_exactly_once(league_db) -> None:
    first = league.consume_rate(league_db, GUILD, ALICE, "buy")
    assert first.used == 1 and first.charged is True
    second = league.consume_rate(league_db, GUILD, ALICE, "buy")
    assert second.used == 2
    assert league.usage_today(league_db, GUILD, ALICE)["buy"] == 2


def test_the_two_metering_styles_agree_on_where_the_cap_falls(league_db) -> None:
    """charge-on-attempt uses <=, charge-on-acceptance uses < — both permit
    exactly `limit` uses."""
    for used in range(0, 12):
        attempt = league.RateVerdict("buy", limit=8, used=used, charged=True)
        # `used` under charge-on-attempt already includes the current attempt.
        acceptance = league.RateVerdict("buy", limit=8, used=used - 1, charged=False)
        assert attempt.allowed == acceptance.allowed, used


def test_charge_on_acceptance_covers_exactly_buy_and_sell() -> None:
    assert league.CHARGE_ON_ACCEPTANCE == {"buy", "sell"}
    for command in league.CHARGE_ON_ACCEPTANCE:
        assert command in league.DAILY_LIMITS


def test_the_limit_message_names_the_cap_and_the_reset(league_db) -> None:
    verdict = league.RateVerdict("research", limit=3, used=4)
    assert not verdict.allowed
    assert verdict.remaining == 0
    text = verdict.message()
    assert "/research" in text and "3 per day" in text and "midnight UTC" in text


def test_a_successful_reply_reports_the_remaining_allowance(league_db, offline) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    text = seam().buy_text(GUILD, ALICE, "ACME", journal.CORE, 100.0, None, **PLAN)
    assert f"used 1/{league.DAILY_LIMITS['buy']} today" in text


def test_usage_is_scoped_per_guild(league_db) -> None:
    for _ in range(league.DAILY_LIMITS["research"] + 1):
        league.check_rate(league_db, GUILD, ALICE, "research")
    assert not league.check_rate(league_db, GUILD, ALICE, "research").allowed
    assert league.check_rate(league_db, 777, ALICE, "research").allowed


def test_usage_today_reports_what_was_spent(league_db) -> None:
    league.check_rate(league_db, GUILD, ALICE, "buy")
    league.check_rate(league_db, GUILD, ALICE, "buy")
    league.check_rate(league_db, GUILD, ALICE, "review")
    assert league.usage_today(league_db, GUILD, ALICE) == {"buy": 2, "review": 1}


def test_every_command_has_a_limit() -> None:
    for command in ("buy", "sell", "review", "research", "standings", "join"):
        assert league.DAILY_LIMITS[command] > 0


# ==================================== the modals

def test_the_buy_modal_collects_exactly_the_written_plan() -> None:
    """Discord allows five inputs; the plan rule 1 requires is exactly five."""
    modal = bot.BuyModal(None, "MU", journal.ACTIVE, 850.0, 780.0)

    assert len(modal.children) == 5, "Discord caps a modal at five text inputs"
    for field in ("shares", "thesis", "invalidation", "horizon", "exit_plan"):
        assert isinstance(getattr(modal, field), discord.ui.TextInput), field
    # Everything numeric that is not the size lives on the slash command, so the
    # modal stays the written plan.
    assert modal._ticker == "MU" and modal._bucket == journal.ACTIVE
    assert modal._price == 850.0 and modal._stop == 780.0


def test_the_modal_enforces_the_same_minimum_lengths_as_the_journal() -> None:
    modal = bot.BuyModal(None, "MU", journal.CORE, None, None)
    assert modal.thesis.min_length == journal.MIN_THESIS_CHARS
    for field in (modal.invalidation, modal.horizon, modal.exit_plan):
        assert field.min_length == journal.MIN_PLAN_CHARS


def test_the_sell_modal_asks_for_size_and_an_outcome() -> None:
    modal = bot.SellModal(None, "MU", 900.0)
    assert len(modal.children) == 2
    assert modal.shares.required is False       # blank = the whole position
    assert modal.outcome.min_length == journal.MIN_PLAN_CHARS


def test_parse_number_accepts_human_input_and_refuses_nonsense() -> None:
    assert bot.parse_number("12", "shares") == 12.0
    assert bot.parse_number(" 1,250 ", "shares") == 1250.0
    assert bot.parse_number("$99.50", "price") == 99.50
    for bad in ("ten", "", "12 shares", None):
        with pytest.raises(ValueError, match="must be a number"):
            bot.parse_number(bad, "shares")


# ============================================================== bot seams

def test_join_reply_explains_the_rules_bind(league_db) -> None:
    client = bot.LeagueBot.__new__(bot.LeagueBot)
    client._connect = league.connect
    text = client.join_text(GUILD, ALICE, "alice", "momentum")

    assert "alice" in text
    assert "$100,000.00" in text
    assert "momentum" in text
    assert "refused and forfeited" in text


def test_join_reply_is_idempotent(league_db) -> None:
    client = bot.LeagueBot.__new__(bot.LeagueBot)
    client._connect = league.connect
    client.join_text(GUILD, ALICE, "alice", "value")
    again = client.join_text(GUILD, ALICE, "alice", "value")
    assert "already in" in again


def test_the_benchmark_row_is_not_truncated(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    rows = league.standings(league_db, GUILD, {}, SPY, as_of=d("2026-08-11"))
    text = league.render_standings(rows, "Test")

    assert "SPY benchmark" in text
    assert "SPY (same" not in text          # the old label did not fit the column
    assert "same deposits on the same dates" in text


def test_thesis_bot_refuses_to_start_without_a_token(monkeypatch) -> None:
    """The CLI must fail fast, before any Discord connection is attempted."""
    from typer.testing import CliRunner

    from thesis import cli

    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    started: list[bool] = []
    monkeypatch.setattr(bot, "run", lambda: started.append(True))

    result = CliRunner().invoke(cli.app, ["bot"])
    combined = (result.output or "") + (getattr(result, "stderr", "") or "")

    assert result.exit_code == 1
    assert "DISCORD_TOKEN is not set" in combined
    assert started == [], "the bot must not start without a token"


def test_standings_seam_renders_without_a_discord_connection(league_db) -> None:
    league.join(league_db, GUILD, ALICE, "alice", on_date=d("2026-08-01"))
    client = bot.LeagueBot.__new__(bot.LeagueBot)
    client._connect = league.connect

    text = client.standings_text(GUILD, "Test Server")
    assert "Standings — Test Server" in text
    assert "alice" in text


# ================================ the three-second interaction contract
#
# Discord discards an interaction that has not been acknowledged within three
# seconds and tells the member "The application did not respond." Every handler
# therefore acknowledges before it works. That is a property of the source, so —
# in the style of the wrapper-guarantee tests above — it is checked against the
# source rather than hoped for.

BOT_TREE = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Calls that acknowledge the interaction. `run_command` counts because its own
#: first statement is the deferral, which `test_run_command_defers_first` pins
#: independently — so this is a shorthand, not a circular argument.
ACK_CALLS = frozenset(
    {"defer", "send_message", "send_modal", "in_a_server_only", "run_command"}
)

#: Modules where essentially every function reaches SQLite, the network, or a
#: model. None of them may be called before the acknowledgement.
#:
#: This is a denylist, and the honest limitation of these tests: a *newly
#: invented* way to block — a module nobody has imported yet — would not be
#: caught. It covers everything this package can currently reach, and the last
#: three are here to catch the obvious ways a future handler might block without
#: going through the league at all.
SLOW_MODULES = frozenset(
    {
        "league", "journal", "arena", "track", "screen", "universe", "market",
        "brief", "lint", "pdf", "edgar", "pricecache",
        "time", "requests", "httpx", "urllib", "sqlite3", "yfinance", "anthropic",
    }
)

#: Slow helpers defined in bot.py itself, plus plain file access.
SLOW_HELPERS = frozenset(
    {"fetch_marks", "fetch_one_price", "cycle_tickers", "build_context", "open"}
)

#: The seven commands the league exposes. Listed so that adding an eighth fails
#: this file until it is covered, rather than slipping past unchecked.
EXPECTED_COMMANDS = frozenset(
    {"join", "standings", "buy", "sell", "review", "research", "cycle"}
)


def slow_call(call: ast.Call) -> str | None:
    """The name of the blocking thing this call reaches, if it is one."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id if func.id in SLOW_HELPERS else None
    if isinstance(func, ast.Attribute):
        if func.attr.endswith("_text"):  # a synchronous seam onto league.py
            return func.attr
        if func.attr == "to_thread":
            return "asyncio.to_thread"
        if isinstance(func.value, ast.Name) and func.value.id in SLOW_MODULES:
            return f"{func.value.id}.{func.attr}"
    return None


def is_ack(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in ACK_CALLS
    return isinstance(func, ast.Attribute) and func.attr in ACK_CALLS


def calls_in_evaluation_order(node: ast.AST):
    """Nested calls first, then the call containing them.

    Evaluation order, not source order, and the difference matters: in
    `run_command(interaction, seam, fetch_marks(...))` the fetch runs *before*
    the deferral, so a left-to-right reading would clear a real violation.
    """
    for child in ast.iter_child_nodes(node):
        yield from calls_in_evaluation_order(child)
    if isinstance(node, ast.Call):
        yield node


def _scan(where: str, node: ast.AST, acked: bool, found: list[str]) -> bool:
    for call in calls_in_evaluation_order(node):
        reached = slow_call(call)
        if reached and not acked:
            found.append(
                f"{where} line {call.lineno}: calls {reached} before the "
                "interaction is acknowledged"
            )
        if is_ack(call):
            acked = True
    return acked


def violations(where: str, body: list[ast.stmt], acked: bool = False) -> list[str]:
    """Every place `body` does blocking work on a path that has not acknowledged.

    Branches are treated conservatively: an acknowledgement inside an `if` does
    not count for the fall-through path, because the branch may not be taken.
    """
    found: list[str] = []
    for statement in body:
        if isinstance(statement, ast.If):
            branch = _scan(where, statement.test, acked, found)
            found += violations(where, statement.body, branch)
            found += violations(where, statement.orelse, branch)
            continue
        acked = _scan(where, statement, acked, found)
    return found


def command_handlers() -> dict[str, ast.AsyncFunctionDef]:
    """Every `@tree.command(...)` handler in bot.py, keyed by its Discord name."""
    found: dict[str, ast.AsyncFunctionDef] = {}
    for node in ast.walk(BOT_TREE):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "command"
            ):
                named = [
                    kw.value.value for kw in decorator.keywords
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant)
                ]
                found[named[0] if named else node.name] = node
    return found


def modal_submits() -> dict[str, ast.AsyncFunctionDef]:
    """Each modal's `on_submit` — where /buy's and /sell's slow work actually is."""
    return {
        node.name: item
        for node in ast.walk(BOT_TREE)
        if isinstance(node, ast.ClassDef) and node.name.endswith("Modal")
        for item in node.body
        if isinstance(item, ast.AsyncFunctionDef) and item.name == "on_submit"
    }


def test_every_registered_command_is_covered_by_this_contract() -> None:
    """A new command must be added to EXPECTED_COMMANDS to get past this file."""
    assert set(command_handlers()) == set(EXPECTED_COMMANDS)


def test_a_real_client_still_registers_all_seven_commands() -> None:
    """Built for real, offline — no token, no gateway, no network.

    Everything else here reads the source. This one proves the source actually
    assembles: that the decorators still compose, that the seven commands keep
    their options, and that the tree in use is the one that handles errors. A
    decorator-ordering mistake shows up here and nowhere else.
    """
    client = bot.LeagueBot()
    assert isinstance(client.tree, bot.LeagueTree)
    assert isinstance(client.tree, discord.app_commands.CommandTree)
    assert sorted(c.name for c in client.tree.get_commands()) == sorted(
        EXPECTED_COMMANDS
    )

    buy = next(c for c in client.tree.get_commands() if c.name == "buy")
    assert [p.name for p in buy.parameters] == ["ticker", "bucket", "price", "stop"]
    assert [p.required for p in buy.parameters] == [True, False, False, False]

    assert type(client.tree).on_error is not discord.app_commands.CommandTree.on_error
    for modal in (bot.BuyModal, bot.SellModal):
        assert "on_error" in modal.__dict__, f"{modal.__name__} would fail silently"


@pytest.mark.parametrize("name", sorted(EXPECTED_COMMANDS))
def test_no_command_works_before_acknowledging_the_interaction(name: str) -> None:
    handler = command_handlers()[name]
    assert violations(f"/{name}", handler.body) == []


@pytest.mark.parametrize("modal", ["BuyModal", "SellModal"])
def test_no_modal_submit_works_before_acknowledging_the_interaction(modal: str) -> None:
    assert violations(f"{modal}.on_submit", modal_submits()[modal].body) == []


def test_run_command_defers_first() -> None:
    """The single place the ordering lives, so it is asserted literally."""
    function = next(
        node for node in ast.walk(BOT_TREE)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_command"
    )
    body = [
        statement for statement in function.body
        if not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
        )
    ]  # drop the docstring
    first = body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Await)
    call = first.value.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Attribute) and call.func.attr == "defer", (
        "run_command must defer before anything else — it is the only thing "
        "standing between a slow command and Discord's three-second window"
    )


def called_names(node: ast.AST) -> set[str]:
    """Bare function names called anywhere inside `node`."""
    return {
        call.func.id for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def called_attrs(node: ast.AST) -> set[str]:
    return {
        call.func.attr for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }


def test_only_the_modal_openers_skip_the_deferral() -> None:
    """/buy and /sell may not defer: opening a modal *is* the acknowledgement.

    Deferring first would make `send_modal` illegal, so the exception is real —
    but it is exactly two commands wide, and their deferral moves to `on_submit`.
    """
    handlers = command_handlers()
    for name in ("buy", "sell"):
        assert "send_modal" in called_attrs(handlers[name]), (
            f"/{name} must open its modal to acknowledge"
        )
        assert "run_command" not in called_names(handlers[name]), (
            f"/{name} cannot defer — send_modal must be the initial response"
        )

    for name in sorted(EXPECTED_COMMANDS - {"buy", "sell"}):
        assert "run_command" in called_names(handlers[name]), (
            f"/{name} must go through run_command"
        )

    # And both modals defer, so nothing is actually exempt from the contract.
    for modal in ("BuyModal", "SellModal"):
        assert "run_command" in called_names(modal_submits()[modal])


GOOD_HANDLER = """
async def handler(interaction):
    if interaction.guild_id is None:
        await in_a_server_only(interaction)
        return
    await run_command(interaction, bot.review_text, interaction.guild_id)
"""

LATE_DEFER = """
async def handler(interaction):
    text = await asyncio.to_thread(bot.run_cycle_text, interaction.guild_id)
    await interaction.response.defer()
    await interaction.followup.send(text)
"""

SLOW_ARGUMENT = """
async def handler(interaction):
    await run_command(interaction, bot.buy_text, fetch_marks(["SPY"]))
"""

UNGUARDED_BRANCH = """
async def handler(interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message("no")
    text = await asyncio.to_thread(bot.join_text, interaction.guild_id)
"""


def parse_handler(source: str) -> list[ast.stmt]:
    return ast.parse(textwrap.dedent(source)).body[0].body


def test_the_ordering_check_passes_a_correct_handler() -> None:
    assert violations("good", parse_handler(GOOD_HANDLER)) == []


@pytest.mark.parametrize(
    "label, source",
    [
        ("work before the deferral", LATE_DEFER),
        ("slow work in an argument", SLOW_ARGUMENT),
        ("a branch that falls through unacknowledged", UNGUARDED_BRANCH),
    ],
)
def test_the_ordering_check_has_teeth(label: str, source: str) -> None:
    """Each of these is a way the timeout comes back. The checker must see all three.

    `SLOW_ARGUMENT` is the subtle one: the deferral is textually first but the
    argument is evaluated before the call, so the fetch really does run first.
    """
    assert violations("bad", parse_handler(source)), f"missed: {label}"


# ---------------------------------------------------- the ordering, in behaviour

class FakeResponse:
    """Records what a handler did to the interaction, in order."""

    def __init__(self, log: list[str]) -> None:
        self._log = log
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs: object) -> None:
        self._log.append("defer")
        self._done = True

    async def send_message(self, text: str, **kwargs: object) -> None:
        self._log.append(f"send_message:{text}")
        self._done = True

    async def send_modal(self, modal: object) -> None:
        self._log.append(f"send_modal:{type(modal).__name__}")
        self._done = True


class FakeFollowup:
    def __init__(self, log: list[str]) -> None:
        self._log = log

    async def send(self, text: str, **kwargs: object) -> None:
        self._log.append(f"followup:{text}")


class FakeInteraction:
    """Enough of a discord.Interaction to prove ordering, with no Discord."""

    def __init__(self, guild_id: int | None = GUILD, manage_guild: bool = True) -> None:
        self.calls: list[str] = []
        self.response = FakeResponse(self.calls)
        self.followup = FakeFollowup(self.calls)
        self.guild_id = guild_id
        self.guild = SimpleNamespace(name="Test Server")
        self.user = SimpleNamespace(
            id=ALICE, display_name="alice",
            guild_permissions=SimpleNamespace(manage_guild=manage_guild),
        )
        self.command = SimpleNamespace(name="cycle")


def test_run_command_defers_before_the_work_runs() -> None:
    """The ordering the AST asserts, observed end to end."""
    interaction = FakeInteraction()

    def work() -> str:
        interaction.calls.append("work")
        return "done"

    asyncio.run(bot.run_command(interaction, work))
    assert interaction.calls == ["defer", "work", "followup:done"]


def test_a_long_reply_arrives_as_several_followups() -> None:
    interaction = FakeInteraction()
    asyncio.run(bot.run_command(interaction, lambda: "x\n" * 1500))
    assert interaction.calls[0] == "defer"
    assert len(interaction.calls) > 2, "a 3,000-character reply needs splitting"


def test_a_dm_is_refused_without_touching_the_database() -> None:
    interaction = FakeInteraction(guild_id=None)
    asyncio.run(bot.in_a_server_only(interaction))
    assert interaction.calls == [f"send_message:{bot.IN_A_SERVER_ONLY}"]


def test_only_a_server_manager_may_run_a_cycle() -> None:
    assert bot.may_run_a_cycle(FakeInteraction(manage_guild=True).user)
    assert not bot.may_run_a_cycle(FakeInteraction(manage_guild=False).user)
    assert not bot.may_run_a_cycle(SimpleNamespace()), "a DM author has no permissions"


# --------------------------------------------------------- nothing fails silently

def tree_only() -> bot.LeagueTree:
    """A LeagueTree with no client — `on_error` needs nothing else."""
    return bot.LeagueTree.__new__(bot.LeagueTree)


def raised(error: BaseException) -> BaseException:
    """The same exception, but actually raised, so it carries a traceback.

    discord.py hands `on_error` an exception that has been through a `raise`. One
    built by calling its constructor has an empty `__traceback__` and logs as a
    bare one-liner, so testing with that would quietly stop proving the traceback
    reaches the log at all.
    """
    try:
        raise error
    except BaseException as caught:  # noqa: BLE001 - handing it straight back
        return caught


def test_a_crash_reaches_the_member_instead_of_timing_out(caplog) -> None:
    interaction = FakeInteraction()
    with caplog.at_level(logging.ERROR, logger="thesis.bot"):
        asyncio.run(
            tree_only().on_error(interaction, raised(RuntimeError("yfinance died")))
        )

    said = " ".join(interaction.calls)
    assert "yfinance died" in said, "the member is told what broke"
    assert "RuntimeError" in said
    assert "/cycle" in said
    assert "Traceback" in caplog.text, "the log carries the traceback"


def test_a_crash_is_never_mistakable_for_a_rule_refusal() -> None:
    """A refusal is the system working; a crash is not. They must not read alike."""
    text = bot.failure_text("buy", RuntimeError("boom"))
    assert not text.startswith(journal.REFUSAL_PREFIX)
    assert "not a refusal" in text


def test_a_crash_before_the_acknowledgement_still_gets_a_reply() -> None:
    """The case that produced "did not respond": nothing had answered Discord yet."""
    interaction = FakeInteraction()
    asyncio.run(
        tree_only().on_error(interaction, raised(RuntimeError("permission lookup")))
    )
    assert interaction.calls[0].startswith("send_message:")


def test_a_crash_after_the_acknowledgement_replies_as_a_followup() -> None:
    interaction = FakeInteraction()
    asyncio.run(interaction.response.defer())
    asyncio.run(tree_only().on_error(interaction, raised(RuntimeError("mid-flight"))))
    assert interaction.calls[0] == "defer"
    assert interaction.calls[1].startswith("followup:")


def test_the_discord_wrapper_exception_is_unwrapped_for_the_member() -> None:
    """discord.py wraps a callback error; the member wants the cause, not the wrapper."""
    inner = ValueError("no session data for ACME")
    wrapped = SimpleNamespace(original=inner)
    assert "no session data for ACME" in bot.failure_text("research", wrapped)
    assert "ValueError" in bot.failure_text("research", wrapped)


def test_a_dead_interaction_is_logged_rather_than_raised(caplog) -> None:
    """15 minutes on, the token is gone. That must not become a second exception."""
    interaction = FakeInteraction()

    async def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("404 Not Found (error code: 10015): Unknown Webhook")

    interaction.response.send_message = refuse
    with caplog.at_level(logging.ERROR, logger="thesis.bot"):
        asyncio.run(bot.report_failure(interaction, "cycle", RuntimeError("original")))
    assert "could not deliver the failure notice" in caplog.text


@pytest.mark.parametrize("modal, command", [("BuyModal", "buy"), ("SellModal", "sell")])
def test_a_modal_crash_reaches_the_member(modal: str, command: str, caplog) -> None:
    instance = getattr(bot, modal).__new__(getattr(bot, modal))
    interaction = FakeInteraction()
    with caplog.at_level(logging.ERROR, logger="thesis.bot"):
        asyncio.run(instance.on_error(interaction, raised(RuntimeError("modal broke"))))
    assert "modal broke" in " ".join(interaction.calls)
    assert f"/{command} modal submit failed" in caplog.text
    assert "Traceback" in caplog.text


# ------------------------------------------------------------- startup visibility

GUILD_NAME = "UIUC Investing"
GUILD_ROW = SimpleNamespace(name=GUILD_NAME, id=777)


class FakeTree:
    """Records what was synced where, with no Discord connection.

    `synced=None` makes `sync` raise, standing in for a rate limit or a payload
    Discord rejects.
    """

    def __init__(
        self,
        defined: list[str],
        synced: list[str] | None = None,
        globals_left: tuple[str, ...] = (),
    ) -> None:
        self._defined = defined
        self._synced = defined if synced is None else synced
        self._globals_left = globals_left
        self.copied_to: list[int] = []
        self.synced_scopes: list[int | None] = []

    def get_commands(self, **kwargs: object) -> list[object]:
        return [SimpleNamespace(name=name) for name in self._defined]

    def copy_global_to(self, *, guild: object) -> None:
        self.copied_to.append(guild.id)

    async def sync(self, *, guild: object | None = None) -> list[object]:
        self.synced_scopes.append(None if guild is None else guild.id)
        if self._synced is None:
            raise RuntimeError("429 Too Many Requests")
        return [SimpleNamespace(name=name) for name in self._synced]

    async def fetch_commands(self, *, guild: object | None = None) -> list[object]:
        return [SimpleNamespace(name=name) for name in self._globals_left]


def bot_with(tree: FakeTree, guilds: list[object] | None = None) -> bot.LeagueBot:
    """A LeagueBot with no gateway. `guilds` shadows discord.Client's property."""
    present = [GUILD_ROW] if guilds is None else guilds

    class Offline(bot.LeagueBot):
        user = "ThesisLeague#4242"

        def __init__(self) -> None:  # no token, no connection
            pass

    Offline.guilds = present  # a plain attribute wins over the base property
    client = Offline()
    client.tree = tree
    return client


def test_startup_syncs_per_guild_rather_than_globally() -> None:
    """The fix for /research: a guild sync lands at once, a global one may not."""
    tree = FakeTree(sorted(EXPECTED_COMMANDS))
    asyncio.run(bot_with(tree).sync_commands())

    assert tree.copied_to == [GUILD_ROW.id], "the global set must be copied in"
    assert GUILD_ROW.id in tree.synced_scopes, "the guild itself was never synced"


def test_every_guild_the_bot_is_in_gets_the_commands() -> None:
    rooms = [SimpleNamespace(name="One", id=1), SimpleNamespace(name="Two", id=2)]
    tree = FakeTree(sorted(EXPECTED_COMMANDS))
    asyncio.run(bot_with(tree, guilds=rooms).sync_commands())
    assert tree.copied_to == [1, 2]
    assert [scope for scope in tree.synced_scopes if scope is not None] == [1, 2]


def test_startup_logs_the_names_discord_accepted(caplog) -> None:
    """"Log the exact list of command names Discord accepted after sync."""
    with caplog.at_level(logging.INFO, logger="thesis.bot"):
        asyncio.run(bot_with(FakeTree(sorted(EXPECTED_COMMANDS))).sync_commands())

    assert f"commands Discord accepted for {GUILD_NAME} (777) — 7:" in caplog.text
    for name in EXPECTED_COMMANDS:
        assert name in caplog.text
    assert "research" in caplog.text, "the command that started all this"


def test_a_command_that_did_not_register_is_an_error_in_the_console(caplog) -> None:
    """The failure mode being instrumented: defined here, absent at Discord."""
    with caplog.at_level(logging.INFO, logger="thesis.bot"):
        asyncio.run(
            bot_with(FakeTree(["join", "research"], synced=["join"])).sync_commands()
        )
    assert "NOT accepted" in caplog.text
    assert "research" in caplog.text
    assert "will not appear in the picker" in caplog.text
    assert any(record.levelno == logging.ERROR for record in caplog.records)


def test_a_stale_registration_is_called_out(caplog) -> None:
    """A command Discord still offers but this process cannot serve — a hang."""
    with caplog.at_level(logging.INFO, logger="thesis.bot"):
        asyncio.run(
            bot_with(FakeTree(["join"], synced=["join", "gone"])).sync_commands()
        )
    assert "not defined here" in caplog.text
    assert "gone" in caplog.text


def test_a_failed_sync_says_so_loudly(caplog) -> None:
    tree = FakeTree(["join", "cycle"])
    tree._synced = None
    with caplog.at_level(logging.INFO, logger="thesis.bot"):
        asyncio.run(bot_with(tree).sync_commands())
    assert "command sync FAILED" in caplog.text
    assert GUILD_NAME in caplog.text
    assert "Traceback" in caplog.text


def test_leftover_global_registrations_are_named(caplog) -> None:
    """If a command ever shows twice in the picker, the console says why."""
    tree = FakeTree(sorted(EXPECTED_COMMANDS), globals_left=("join", "cycle"))
    with caplog.at_level(logging.INFO, logger="thesis.bot"):
        asyncio.run(bot_with(tree).sync_commands())
    assert "stale GLOBAL command registration(s)" in caplog.text
    assert "cycle, join" in caplog.text  # sorted, so the log reads the same each run


def test_nothing_global_is_deleted_behind_the_users_back() -> None:
    """Clearing an app's global commands hits every server it is in.

    So it is reported and not done. `sync(guild=None)` is the global write, and it
    must never be issued by a startup that was only asked to register per guild.
    """
    tree = FakeTree(sorted(EXPECTED_COMMANDS), globals_left=("join",))
    asyncio.run(bot_with(tree).sync_commands())
    assert None not in tree.synced_scopes, "a global sync would rewrite every server"


def test_a_bot_in_no_guilds_is_an_error_not_a_silent_no_op(caplog) -> None:
    """With guild-scoped commands and no guilds, nothing can appear anywhere."""
    tree = FakeTree(sorted(EXPECTED_COMMANDS))
    with caplog.at_level(logging.INFO, logger="thesis.bot"):
        asyncio.run(bot_with(tree, guilds=[]).sync_commands())
    assert "no guilds to register commands in" in caplog.text
    assert "applications.commands" in caplog.text
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert tree.synced_scopes == [], "nothing to sync, so nothing was attempted"


def test_a_guild_joined_later_is_synced_at_once(caplog) -> None:
    """Otherwise a new server waits for a restart before any command works."""
    tree = FakeTree(sorted(EXPECTED_COMMANDS))
    newcomer = SimpleNamespace(name="Late Joiner", id=4242)
    with caplog.at_level(logging.INFO, logger="thesis.bot"):
        asyncio.run(bot_with(tree).on_guild_join(newcomer))
    assert tree.copied_to == [4242]
    assert tree.synced_scopes == [4242]
    assert "Late Joiner" in caplog.text


def test_startup_logs_who_we_are_and_where(caplog) -> None:
    client = bot_with(FakeTree(sorted(EXPECTED_COMMANDS)))
    with caplog.at_level(logging.INFO, logger="thesis.bot"):
        asyncio.run(client.on_ready())
    assert "ThesisLeague#4242" in caplog.text
    assert f"{GUILD_NAME} (777)" in caplog.text
    assert "in 1 guild(s)" in caplog.text


def test_on_ready_syncs_once_however_often_it_fires(caplog) -> None:
    """A reconnect replays on_ready; re-syncing would burn the command rate limit."""
    tree = FakeTree(sorted(EXPECTED_COMMANDS))
    client = bot_with(tree)
    with caplog.at_level(logging.INFO, logger="thesis.bot"):
        asyncio.run(client.on_ready())
        asyncio.run(client.on_ready())
    assert tree.synced_scopes == [GUILD_ROW.id], "synced twice on one connection"


def test_commands_are_not_synced_in_setup_hook() -> None:
    """`setup_hook` runs before the gateway, so `self.guilds` is empty there.

    Syncing from it is why a per-guild sync silently registers nothing — the loop
    has no guilds to iterate. Asserted against the source, since the failure is
    invisible at runtime: it logs an error and moves on.
    """
    hook = next(
        node for node in ast.walk(BOT_TREE)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "setup_hook"
    )
    assert "sync_commands" not in called_attrs(hook) | called_names(hook)

    ready = next(
        node for node in ast.walk(BOT_TREE)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_ready"
    )
    assert "sync_commands" in called_attrs(ready), (
        "the sync has to happen where the guild list exists"
    )


# --------------------------------------------- registration vs PRODUCT.md's scope

def v1_commands() -> set[str]:
    """The slash commands PRODUCT.md's v1 scope says ship, read from the file.

    Taken from the document rather than restated here, so the two cannot drift:
    if a command is added to the scope and never registered — which is the whole
    bug this came from — the suite fails instead of a member finding out.
    """
    product = (REPO_ROOT / "PRODUCT.md").read_text(encoding="utf-8")
    scope = product.split("## v1 scope", 1)
    assert len(scope) == 2, "PRODUCT.md no longer has a v1 scope section"
    listed = scope[1].split("**Explicitly deferred:**", 1)[0]
    return set(re.findall(r"`/([a-z][a-z0-9_-]*)", listed))


def test_product_md_still_lists_the_commands_we_think_it_does() -> None:
    """Guards the parser itself: a reformat must not quietly empty the set."""
    assert v1_commands() == set(EXPECTED_COMMANDS)


def test_every_command_in_the_v1_scope_is_actually_registered() -> None:
    """The headline failure: /research existed, worked, and was never in a picker.

    Built for real rather than read from source, because "implemented" and
    "registered on the tree" are different things and only the tree decides
    whether Discord is ever told about a command.
    """
    registered = {c.name for c in bot.LeagueBot().tree.get_commands()}
    missing = sorted(v1_commands() - registered)
    assert missing == [], f"in PRODUCT.md's v1 scope but never registered: {missing}"


def test_nothing_is_registered_that_the_scope_does_not_claim() -> None:
    registered = {c.name for c in bot.LeagueBot().tree.get_commands()}
    assert sorted(registered - v1_commands()) == []


def test_research_is_registered() -> None:
    """Named on its own, because this is the one that was missing."""
    registered = {c.name for c in bot.LeagueBot().tree.get_commands()}
    assert "research" in registered


def test_every_command_fits_what_discord_will_accept() -> None:
    """One oversized description makes Discord reject the whole sync payload.

    Which would present as *every* new command missing from the picker — the same
    symptom, from a cause a registration test alone would not find.
    """
    for command in bot.LeagueBot().tree.get_commands():
        assert re.fullmatch(r"[a-z0-9_-]{1,32}", command.name), command.name
        assert 1 <= len(command.description) <= 100, command.name
        for parameter in command.parameters:
            assert 1 <= len(parameter.description) <= 100, (
                f"/{command.name} {parameter.name}"
            )
            assert re.fullmatch(r"[a-z0-9_-]{1,32}", parameter.name), parameter.name
            for choice in getattr(parameter, "choices", ()):
                assert 1 <= len(choice.name) <= 100, choice.name
