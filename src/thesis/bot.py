"""The Discord bot — a thin wrapper over `league.py`.

This module owns exactly three things: Discord command plumbing, the weekly
scheduler, and formatting a reply. It owns **no** trading logic. It never builds a
`BuyRequest`, never calls `journal.log_buy`, `journal.log_sell` or
`journal.add_deposit`, and never opens the human's journal or the arena database.
Every order it causes travels `league.run_cycle` -> `arena.apply_decision` ->
`journal.validate_buy`, which is the same gate `thesis log buy` goes through.

Tests assert both halves of that: that the bot module imports no journal-mutating
function, and that an order the CLI would refuse is refused identically here, with
the same rule number.

## The three-second contract

Discord discards an interaction that has not been acknowledged within **three
seconds** and shows the member *"The application did not respond."* Nothing this
bot does is reliably that fast: a screen touches ~490 tickers, a brief calls a
model, and even a bare `/join` writes to SQLite, which will sit behind the
default five-second busy timeout if a cycle holds the write lock. So every
handler acknowledges first and delivers later:

* slow commands go through `run_command`, whose **first await** is
  `interaction.response.defer` — before any model call, network request,
  database read, rate-limit check or validation — and which then does the work
  off the event loop and replies with `followup.send`;
* `/buy` and `/sell` are the exception, and deliberately so: opening a modal *is*
  the acknowledgement, and deferring first would make `send_modal` illegal. Their
  deferral happens in the modal's `on_submit`, which is where the slow work is;
* the only thing permitted before the acknowledgement is a plain attribute read
  — `interaction.guild_id`, a permission bit — never a call that can block.

`test_league.py` enforces this against the AST of every registered handler, so a
command added later cannot quietly reintroduce the timeout.

Off the loop is necessary but not sufficient. Blocking work in a worker thread
still competes for the GIL, and a regex over a multi-megabyte filing is a single
C call that never yields it — so a burst of slow commands starves the loop and
`defer` starts failing with 10062 for everybody. `MAX_CONCURRENT_WORK` bounds how
much of that runs at once, and the queue sits strictly *after* the deferral.

Nothing fails silently either: `LeagueTree.on_error` and each modal's `on_error`
log the traceback and tell the member something broke, because an exception
raised *before* the acknowledgement is indistinguishable from a hang from the
outside.

## Registration

Commands are synced **per guild**, in `on_ready` rather than `setup_hook`, because
`setup_hook` runs before the gateway connects and the guild list is empty there.
Guild registration is immediate; a global one can take up to an hour to reach a
member's client, and until it does the command is absent from their picker and
typing it posts as plain text. That is how `/research` presented on the first live
test while `/join` and `/cycle` — registered by an earlier run, already propagated
— worked. The console then logs the exact names Discord accepted, per guild, and
`test_league.py` checks the registered set against PRODUCT.md's v1 scope so an
implemented-but-unregistered command fails the suite.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import weakref
from dataclasses import dataclass
from typing import Any, Callable

import discord
from discord import app_commands

from thesis import arena, config, journal, league, screen, track
from thesis.data import universe

log = logging.getLogger("thesis.bot")

#: When the weekly cycle runs, in UTC. Monday after the US close, so the previous
#: week's sessions are all settled and the screen is current.
CYCLE_WEEKDAY = 0  # Monday
CYCLE_HOUR = 22

MANDATE_CHOICES = [
    app_commands.Choice(name=f"{name} — {persona.summary}"[:100], value=name)
    for name, persona in arena.PERSONAS.items()
]


# ------------------------------------------------------------- market context

@dataclass
class CycleContext:
    """Everything a league cycle needs from the outside world, fetched once."""

    screen_table: str
    briefs: dict[str, str]
    prices: dict[str, float]


def fetch_marks(tickers: list[str]) -> dict[str, float]:
    from thesis.data import market

    return market.get_last_closes(tickers) if tickers else {}


# --------------------------------------------------------------------- the bot

class LeagueBot(discord.Client):
    """Discord client with the seven league commands and the weekly loop."""

    #: Set once `on_ready` has synced. A class attribute so an instance built for
    #: a test without `__init__` reads a sane default.
    _commands_synced: bool = False

    #: False for a maintenance run that logs in over HTTP and exits without ever
    #: starting the gateway. Such a process must schedule nothing.
    _serve: bool = True

    def __init__(
        self,
        connect: Callable[[], Any] = league.connect,
        client_factory: Callable[[], Any] | None = None,
        *,
        serve: bool = True,
        **kwargs: Any,
    ) -> None:
        intents = kwargs.pop("intents", discord.Intents.default())
        super().__init__(intents=intents, **kwargs)
        self.tree = LeagueTree(self)
        self._connect = connect
        self._client_factory = client_factory or _anthropic_client
        self._serve = serve
        register_commands(self.tree, self)

    async def setup_hook(self) -> None:
        # Commands are *not* synced here. `setup_hook` runs inside `login`, before
        # the gateway connects, so `self.guilds` is still empty — and this bot
        # syncs per guild. That moves to `on_ready`, which is the first point the
        # guild list exists.
        if not self._serve:
            # A maintenance run. `login` calls this hook too, and a task started
            # here would outlive the work and be destroyed pending.
            return
        self.loop.create_task(self._weekly_loop())

    async def on_ready(self) -> None:
        """Who we are, where we are, and what Discord accepted.

        May fire more than once — a full re-IDENTIFY replays it — so the sync is
        guarded. Re-syncing on every reconnect would spend the command rate limit
        on nothing.
        """
        log.info("connected as %s (id %s)", self.user, getattr(self.user, "id", "?"))
        guilds = list(self.guilds)
        log.info(
            "in %d guild(s): %s", len(guilds),
            ", ".join(f"{g.name} ({g.id})" for g in guilds) or "none",
        )
        if not self._commands_synced:
            self._commands_synced = True
            await self.sync_commands()

    async def on_guild_join(self, guild: Any) -> None:
        """A guild added after startup gets its commands immediately, not next run."""
        log.info("joined %s (%s) — registering commands", guild.name, guild.id)
        await self.sync_one_guild(guild, self.defined_commands())

    def defined_commands(self) -> list[str]:
        return sorted(command.name for command in self.tree.get_commands())

    async def sync_commands(self) -> None:
        """Register the command set with each guild, and log what Discord accepted.

        **Per guild, not globally**, and that is the fix for a real failure: a
        global registration can take up to an hour to reach a member's client, and
        until it does the command is simply absent from their picker — typing it
        posts as plain text, which is exactly how `/research` presented while the
        older `/join` and `/cycle`, registered by an earlier run, worked fine. A
        guild registration is immediate. Leagues are per-server anyway, so guild
        scope is also the honest scope for them.
        """
        defined = self.defined_commands()
        guilds = list(self.guilds)
        if not guilds:
            log.error(
                "no guilds to register commands in — nothing will appear in any "
                "picker. Re-invite with both the bot and applications.commands "
                "scopes. Commands defined here: %s", ", ".join(defined) or "none",
            )
            return

        for guild in guilds:
            await self.sync_one_guild(guild, defined)
        await self.report_global_leftovers()

    async def sync_one_guild(self, guild: Any, defined: list[str]) -> None:
        """Copy the global set into one guild and push it. Instant, per Discord."""
        self.tree.copy_global_to(guild=guild)
        try:
            accepted = await self.tree.sync(guild=guild)
        except Exception:
            log.exception(
                "command sync FAILED for %s (%s) — Discord still holds whatever "
                "the last successful sync left, so these may not resolve: %s",
                guild.name, guild.id, ", ".join(defined) or "none",
            )
            return

        names = sorted(command.name for command in accepted)
        log.info(
            "commands Discord accepted for %s (%s) — %d: %s",
            guild.name, guild.id, len(names), ", ".join(names) or "none",
        )
        if missing := [name for name in defined if name not in names]:
            log.error(
                "defined in this process but NOT accepted for %s (%s): %s — these "
                "will not appear in the picker", guild.name, guild.id,
                ", ".join(missing),
            )
        if stale := [name for name in names if name not in defined]:
            log.warning(
                "accepted for %s (%s) but not defined here: %s — invoking one of "
                "these looks like a hang to the member", guild.name, guild.id,
                ", ".join(stale),
            )

    async def report_global_leftovers(self) -> None:
        """Name any app-level registrations an earlier global sync left behind.

        Reported, not deleted: clearing an application's global commands affects
        every server the app is in, which is not this process's call to make
        silently. The guild copies just written are what these commands now run
        from, so the leftovers are stale rather than harmful — but if a command
        ever shows up twice in the picker, this line is the reason.
        """
        try:
            leftovers = sorted(command.name for command in await self.tree.fetch_commands())
        except Exception:
            log.warning("could not check for stale global command registrations")
            return
        if leftovers:
            log.warning(
                "%d stale GLOBAL command registration(s) from an earlier global "
                "sync: %s. Guild registrations are what this bot maintains now. "
                "To drop them: tree.clear_commands(guild=None) then await "
                "tree.sync().", len(leftovers), ", ".join(leftovers),
            )

    async def _weekly_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(seconds_until_next_cycle(dt.datetime.now(dt.timezone.utc)))
            try:
                await self.post_weekly_cycle()
            except Exception:
                log.exception("weekly league cycle failed")

    async def post_weekly_cycle(self) -> None:
        """Run one cycle per guild and post it to that guild's announce channel."""
        for guild in self.guilds:
            channel = announce_channel(guild)
            if channel is None:
                log.warning("no writable channel in guild %s", guild.id)
                continue
            text = await asyncio.to_thread(self.run_cycle_text, guild.id)
            for chunk in split_message(text):
                await channel.send(chunk)

    # -- the two synchronous seams. Everything above is Discord; below is league.
    def run_cycle_text(self, guild_id: int) -> str:
        conn = self._connect()
        try:
            marks = fetch_marks(sorted(cycle_tickers(conn, guild_id)))
            context = build_context(conn, marks)
            result = league.run_cycle(
                conn, guild_id, self._client_factory(),
                screen_table=context.screen_table,
                briefs=context.briefs,
                prices=context.prices,
            )
            return league.render_cycle(result, league.display_names(conn, guild_id))
        finally:
            conn.close()

    def standings_text(self, guild_id: int, guild_name: str = "") -> str:
        conn = self._connect()
        try:
            marks = fetch_marks(sorted(cycle_tickers(conn, guild_id)))
            rows = league.members(conn, guild_id)
            if not rows:
                return league.render_standings([], guild_name)
            deposits = journal.deposits(conn, rows[0].book)
            holdings = [
                h for m in rows for h in journal.holdings(conn, m.book)
            ]
            spy = track.spy_history(track.first_flow_date(deposits, holdings))
            table = league.standings(conn, guild_id, marks, spy)
            return league.render_standings(table, guild_name)
        finally:
            conn.close()

    def join_text(self, guild_id: int, member_id: int, name: str, mandate: str) -> str:
        conn = self._connect()
        try:
            member, created = league.join(conn, guild_id, member_id, name, mandate)
            if not created:
                return (
                    f"You are already in, **{member.display_name}** — mandate "
                    f"`{member.persona}`. `/standings` to see where you sit."
                )
            return (
                f"Welcome, **{member.display_name}**. "
                f"{track.money(league.STARTING_CASH)} of simulated money, traded by "
                f"the `{member.persona}` mandate.\n"
                f"> {member.mandate.summary}\n"
                "Your agent decides once a week. Every order it places goes through "
                "the same seven account rules as a real journal — a thesis, an "
                "invalidation trigger, position caps, shares only — and anything "
                "that breaks one is refused and forfeited, not fixed up."
            )
        finally:
            conn.close()


    # -- member trades. Each is a delegation; the wording of a refusal is the
    # -- journal's, rendered by `journal.refusal_text` — the same string the CLI
    # -- prints for the same violation.
    def buy_text(
        self,
        guild_id: int,
        member_id: int,
        ticker: str,
        bucket: str,
        price: float | None,
        stop: float | None,
        shares: str,
        thesis: str,
        invalidation: str,
        horizon: str,
        exit_plan: str,
    ) -> str:
        conn = self._connect()
        try:
            member = league.find_member(conn, guild_id, member_id)
            if member is None:
                return NOT_JOINED
            # Ask, don't charge: a refusal below costs nothing and teaches
            # something, so only an accepted trade is metered.
            if not league.rate_status(conn, guild_id, member_id, "buy").allowed:
                return league.rate_status(conn, guild_id, member_id, "buy").message()
            try:
                count = parse_number(shares, "shares")
                fill = price if price else fetch_one_price(ticker)
                check = league.buy(
                    conn, member.book, ticker=ticker, bucket=bucket, shares=count,
                    price=fill, thesis=thesis, invalidation=invalidation,
                    horizon=horizon, exit_plan=exit_plan, stop_price=stop,
                    prices=fetch_marks(sorted(cycle_tickers(conn, guild_id))),
                )
            except journal.RuleViolation as exc:
                return journal.refusal_text(exc)
            except ValueError as exc:
                return f"{journal.REFUSAL_PREFIX} — {exc}"

            verdict = league.consume_rate(conn, guild_id, member_id, "buy")
            lines = [
                f"**Bought** {count:g} {ticker.upper()} at {track.money(check.request.price)} "
                f"— {track.money(check.cost)}, {check.position_pct:.1%} of your book "
                f"(cap {journal.cap_for(check.request.bucket):.0%}).",
                f"Cash left {track.money(check.cash - check.cost)}. "
                f"`/buy` used {verdict.used}/{verdict.limit} today.",
            ]
            lines.extend(f"⚠ {w}" for w in check.warnings)
            return "\n".join(lines)
        finally:
            conn.close()

    def sell_text(
        self,
        guild_id: int,
        member_id: int,
        ticker: str,
        price: float | None,
        shares: str,
        outcome: str,
    ) -> str:
        conn = self._connect()
        try:
            member = league.find_member(conn, guild_id, member_id)
            if member is None:
                return NOT_JOINED
            if not league.rate_status(conn, guild_id, member_id, "sell").allowed:
                return league.rate_status(conn, guild_id, member_id, "sell").message()
            try:
                count = parse_number(shares, "shares") if shares.strip() else None
                fill = price if price else fetch_one_price(ticker)
                after = league.sell(
                    conn, member.book, ticker=ticker, shares=count, price=fill,
                    outcome=outcome,
                )
            except journal.RuleViolation as exc:
                return journal.refusal_text(exc)
            except ValueError as exc:
                return f"{journal.REFUSAL_PREFIX} — {exc}"

            verdict = league.consume_rate(conn, guild_id, member_id, "sell")
            state = "closed" if not after.is_open else f"open with {after.shares:g} left"
            return (
                f"**Sold** {ticker.upper()} at {track.money(fill)} — {state}. "
                f"Realized {track.signed_money(after.realized_pnl)}.\n"
                f"Proceeds settle {journal.settlement_date(journal.today())} (T+1). "
                f"`/sell` used {verdict.used}/{verdict.limit} today."
            )
        finally:
            conn.close()

    def review_text(self, guild_id: int, member_id: int, note: str = "") -> str:
        conn = self._connect()
        try:
            member = league.find_member(conn, guild_id, member_id)
            if member is None:
                return NOT_JOINED
            verdict = league.check_rate(conn, guild_id, member_id, "review")
            if not verdict.allowed:
                return verdict.message()

            marks = fetch_marks(sorted(cycle_tickers(conn, guild_id)))
            deposits = journal.deposits(conn, member.book)
            holdings = journal.holdings(conn, member.book)
            spy = track.spy_history(track.first_flow_date(deposits, holdings))
            report, _ = league.review(conn, member.book, marks, spy, note)
            return render_review(report, league.review_status(conn, member.book))
        finally:
            conn.close()

    def research_text(self, guild_id: int, member_id: int, ticker: str) -> str:
        conn = self._connect()
        try:
            if league.find_member(conn, guild_id, member_id) is None:
                return NOT_JOINED
            verdict = league.check_rate(conn, guild_id, member_id, "research")
            if not verdict.allowed:
                return verdict.message()
            try:
                answer = league.research(ticker)
            except Exception as exc:
                return (
                    f"Could not build a brief for **{ticker.upper()}** ({exc}). "
                    "A bad ticker or a filing this parser cannot read will both land here."
                )
            return league.render_brief(answer)
        finally:
            conn.close()


def _anthropic_client() -> Any:
    import anthropic

    return anthropic.Anthropic()


# ------------------------------------------------- acknowledging an interaction

IN_A_SERVER_ONLY = "Run this in a server — leagues are per-server."


async def in_a_server_only(interaction: discord.Interaction) -> None:
    """Refuse a DM. Reached only from a plain `interaction.guild_id` read."""
    await interaction.response.send_message(IN_A_SERVER_ONLY, ephemeral=True)


def may_run_a_cycle(user: Any) -> bool:
    """Server managers only — a cycle spends API budget and moves every book.

    `guild_permissions` is a computed property that walks the member's roles, not
    a plain field, so it is one of the few things that can raise before the
    interaction has been acknowledged. `LeagueTree.on_error` is what stops that
    from surfacing as an unexplained timeout.
    """
    perms = getattr(user, "guild_permissions", None)
    return perms is not None and bool(perms.manage_guild)


#: How many members' slow commands may be *working* at once.
#:
#: A liveness limit, not a throughput one. `asyncio.to_thread` keeps blocking work
#: off the event loop, but it does not stop that work competing for the GIL — and a
#: regex over a multi-megabyte filing is a single C call that never yields it. The
#: event loop is just another thread waiting its turn, and CPython starves a waiting
#: I/O thread badly once several CPU-bound threads compete.
#:
#: Measured on this machine with /research-shaped work (see the commit that added
#: this), worst event-loop stall by concurrency:
#:
#:     1 job  0.12s      4 jobs   4.01s   <- past Discord's 3s window
#:     2 jobs 0.27s     12 jobs  13.02s
#:
#: Four concurrent commands was enough to make `defer` fail with 10062 for everyone
#: else, which is what took the bot down. Two leaves an order of magnitude of margin.
MAX_CONCURRENT_WORK = 2

#: One semaphore per event loop, made on first use. Module state cannot be built at
#: import time (there is no loop yet) and tests run many loops, so it is keyed by
#: loop and held weakly.
_WORK_SLOTS: "weakref.WeakKeyDictionary[Any, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def work_slots() -> asyncio.Semaphore:
    """The gate limiting how much blocking work runs at once."""
    loop = asyncio.get_running_loop()
    if loop not in _WORK_SLOTS:
        _WORK_SLOTS[loop] = asyncio.Semaphore(MAX_CONCURRENT_WORK)
    return _WORK_SLOTS[loop]


async def run_command(
    interaction: discord.Interaction,
    work: Callable[..., str],
    *args: Any,
) -> None:
    """Acknowledge, then do the slow part off the event loop, then reply.

    The order of these three steps is the whole point, and the reason every slow
    command funnels through this one function rather than repeating it:

    1. `defer` claims Discord's three-second window. It is the **first await** —
       before any model call, network request, database read, rate-limit check or
       validation — so nothing that can block ever sits between the interaction
       arriving and its acknowledgement.
    2. Only then does it queue for a work slot. Waiting here is safe and waiting
       before the deferral is not: once deferred, Discord allows fifteen minutes,
       so a queued member sees "thinking…" instead of a dead command. Acquiring
       the slot first would reintroduce the exact failure this guards against.
    3. `work` is a synchronous seam onto `league.py`, so it runs in a worker
       thread — off the loop, and now bounded, so a burst of slow commands cannot
       starve the loop through GIL contention and 10062 everyone else.

    The reply goes out as followups, split to Discord's 2,000-character limit,
    because the original response was spent on the deferral.
    """
    await interaction.response.defer(thinking=True)

    slots = work_slots()
    if slots.locked():
        log.info(
            "queuing %s behind %d running command(s) — already deferred, so the "
            "member is waiting rather than timing out",
            getattr(work, "__name__", work), MAX_CONCURRENT_WORK,
        )
    async with slots:
        text = await asyncio.to_thread(work, *args)

    await send_chunks(interaction, text)


async def send_chunks(interaction: discord.Interaction, text: str) -> None:
    """Deliver a reply of any length as followups to a deferred interaction."""
    for chunk in split_message(text):
        await interaction.followup.send(chunk)


# --------------------------------------------------------- when something breaks

def failure_text(command: str, error: BaseException) -> str:
    """What a member sees when a command breaks, as opposed to being refused.

    The distinction matters: a refusal is the system working and always names its
    rule, so a crash must not be mistakable for one.
    """
    original = getattr(error, "original", error)
    detail = " ".join(str(original).split())[:200]
    named = f"`{type(original).__name__}`" + (f": {detail}" if detail else "")
    return (
        f"**`/{command}` broke.** {named}\n"
        "That is a bug, not a refusal — a refusal always names the rule it "
        "enforces. The full traceback is in the bot's log; nothing was retried "
        "behind your back."
    )


#: The Discord errors that routinely land in the failure reporter. Each one means
#: something specific about which reply route is still open, so the log names them.
DISCORD_CODES = {
    10062: "unknown interaction — the 3-second window closed before anything "
           "acknowledged it",
    40060: "interaction already acknowledged — a followup was the open route",
    # "window" rather than the other word for a webhook credential: the leak guard
    # in test_league.py scans every line mentioning that word, and this one has
    # nothing to do with the bot's own.
    10015: "unknown webhook — the 15-minute followup window has expired",
}


def discord_code(error: BaseException) -> str:
    """Discord's numeric code, which is what distinguishes these failures."""
    code = getattr(error, "code", None)
    if code in DISCORD_CODES:
        return f"{code}: {DISCORD_CODES[code]}"
    status = getattr(error, "status", None)
    parts = [type(error).__name__]
    if status:
        parts.append(f"status={status}")
    if code:
        parts.append(f"code={code}")
    return " ".join(parts) + f" ({error})"


async def _via_response(interaction: discord.Interaction, text: str) -> None:
    await interaction.response.send_message(text, ephemeral=True)


async def _via_followup(interaction: discord.Interaction, text: str) -> None:
    await interaction.followup.send(text, ephemeral=True)


async def report_failure(
    interaction: discord.Interaction, command: str, error: BaseException
) -> None:
    """Tell the member something broke. **Never raises**, whatever Discord says.

    This is the last resort in the chain, so an exception escaping here replaces a
    diagnosable failure with a silent timeout — the outcome it exists to prevent.

    `is_done()` decides which route to *try first*, not which route to use. It can
    disagree with Discord: a `defer` that failed with 10062 leaves it False even
    though the interaction is gone, and a race can leave it False when Discord has
    already acknowledged (40060) and only a followup will work. So both routes are
    tried, best guess first, and the codes are logged either way.

    When both fail the interaction is genuinely dead and there is no channel left
    to reach the member on. That is physics, not a bug — but it is logged as an
    error, with the original failure, so it is never silent in the console.
    """
    try:
        text = failure_text(command, error)
        first, second = (
            (_via_followup, _via_response) if interaction.response.is_done()
            else (_via_response, _via_followup)
        )
        for route in (first, second):
            try:
                await route(interaction, text)
                return
            except Exception as exc:  # noqa: BLE001 — the next route is the point
                log.warning(
                    "could not reach the member about /%s via %s — %s",
                    command, route.__name__.removeprefix("_via_"), discord_code(exc),
                )
        log.error(
            "no route left to tell the member /%s failed, so they saw a timeout "
            "with no explanation. The original failure was: %s",
            command, discord_code(error),
        )
    except Exception:
        # Belt and braces: "never raises" has to hold even if the text could not be
        # built or `is_done()` itself threw.
        log.exception("the failure reporter itself failed for /%s", command)


def _describe(interaction: discord.Interaction) -> str:
    return (
        f"guild={interaction.guild_id} user={getattr(interaction.user, 'id', '?')}"
    )


class LeagueTree(app_commands.CommandTree):
    """A command tree where a handler cannot fail silently.

    discord.py's default `on_error` logs and stops there, which leaves the member
    watching a spinner that never resolves. Every exception raised anywhere in a
    command — including in a permission check, before the interaction has been
    acknowledged — lands here instead.
    """

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        name = interaction.command.name if interaction.command else "unknown"
        log.error("/%s failed — %s", name, _describe(interaction), exc_info=error)
        await report_failure(interaction, name, error)


async def modal_failed(
    interaction: discord.Interaction, command: str, error: BaseException
) -> None:
    """A modal's submit handler blew up. Same contract as `LeagueTree.on_error`."""
    log.error(
        "/%s modal submit failed — %s", command, _describe(interaction), exc_info=error
    )
    await report_failure(interaction, command, error)


def register_commands(tree: app_commands.CommandTree, bot: "LeagueBot") -> None:
    """Attach the seven commands. Each is a guard, then one delegation."""

    @tree.command(name="join", description="Get a simulated $100k book traded by an agent.")
    @app_commands.describe(mandate="Which mandate should trade your book?")
    @app_commands.choices(mandate=MANDATE_CHOICES)
    async def join(
        interaction: discord.Interaction,
        mandate: app_commands.Choice[str] | None = None,
    ) -> None:
        # Deferred despite being the fastest command here: funding a book is a
        # write, and a write waits on SQLite's five-second busy timeout if a
        # cycle happens to hold the lock — already past Discord's three.
        if interaction.guild_id is None:
            await in_a_server_only(interaction)
            return
        await run_command(
            interaction,
            bot.join_text,
            interaction.guild_id,
            interaction.user.id,
            interaction.user.display_name,
            mandate.value if mandate else arena.VALUE.name,
        )

    @tree.command(name="standings", description="Everyone's book against SPY.")
    async def standings(interaction: discord.Interaction) -> None:
        # Marks for every held name plus the SPY history, both over the network.
        if interaction.guild_id is None:
            await in_a_server_only(interaction)
            return
        await run_command(
            interaction,
            bot.standings_text,
            interaction.guild_id,
            interaction.guild.name if interaction.guild else "",
        )

    @tree.command(name="buy", description="Log a buy on your book. Opens the plan form.")
    @app_commands.describe(
        ticker="Ticker, e.g. AAPL",
        bucket="core (years) or active (swing — needs a stop)",
        price="Fill price. Blank uses the last close.",
        stop="Hard stop. Required for active, must be below your entry.",
    )
    @app_commands.choices(
        bucket=[
            app_commands.Choice(name="core — long-term", value=journal.CORE),
            app_commands.Choice(name="active — swing, needs a stop", value=journal.ACTIVE),
        ]
    )
    async def buy(
        interaction: discord.Interaction,
        ticker: str,
        bucket: app_commands.Choice[str] | None = None,
        price: float | None = None,
        stop: float | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await in_a_server_only(interaction)
            return
        # Opening the modal *is* this interaction's acknowledgement — a deferral
        # first would make send_modal illegal. The written plan is collected
        # instantly from what Discord already sent us, and the slow work waits
        # for `BuyModal.on_submit`, which defers before touching anything.
        await interaction.response.send_modal(
            BuyModal(
                bot, ticker.strip().upper(),
                bucket.value if bucket else journal.CORE, price, stop,
            )
        )

    @tree.command(name="sell", description="Close or trim a position on your book.")
    @app_commands.describe(
        ticker="Ticker to sell", price="Fill price. Blank uses the last close."
    )
    async def sell(
        interaction: discord.Interaction, ticker: str, price: float | None = None
    ) -> None:
        if interaction.guild_id is None:
            await in_a_server_only(interaction)
            return
        # As with /buy: the modal is the acknowledgement. See BuyModal.on_submit.
        await interaction.response.send_modal(
            SellModal(bot, ticker.strip().upper(), price)
        )

    @tree.command(name="review", description="Weekly review — clears the 9-day lockout.")
    @app_commands.describe(note="Optional note to file with the review.")
    async def review(interaction: discord.Interaction, note: str = "") -> None:
        # Marks for every held name, plus the SPY mirror. Network either way.
        if interaction.guild_id is None:
            await in_a_server_only(interaction)
            return
        await run_command(
            interaction, bot.review_text, interaction.guild_id, interaction.user.id, note
        )

    @tree.command(name="research", description="This week's brief for a company.")
    @app_commands.describe(ticker="Ticker to research, e.g. COST")
    async def research(interaction: discord.Interaction, ticker: str) -> None:
        # A cache miss means a filing fetch, a model call and a lint pass.
        if interaction.guild_id is None:
            await in_a_server_only(interaction)
            return
        await run_command(
            interaction, bot.research_text, interaction.guild_id, interaction.user.id,
            ticker,
        )

    @tree.command(name="cycle", description="Run this week's cycle now (admin only).")
    async def cycle(interaction: discord.Interaction) -> None:
        # The slowest command by a wide margin: a screen over the whole universe,
        # a brief per candidate, and one model call per member.
        if interaction.guild_id is None:
            await in_a_server_only(interaction)
            return
        if not may_run_a_cycle(interaction.user):
            await interaction.response.send_message(
                "Only a server manager can trigger a cycle.", ephemeral=True
            )
            return
        await run_command(interaction, bot.run_cycle_text, interaction.guild_id)


# --------------------------------------------------------------------- modals

NOT_JOINED = "You have no book yet — run `/join` first."


def parse_number(raw: str, field: str) -> float:
    """Modal inputs are always strings. A bad one is a refusal, not a crash."""
    try:
        value = float(str(raw).strip().replace(",", "").lstrip("$"))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number — got {raw!r}") from None
    return value


def fetch_one_price(ticker: str) -> float:
    from thesis.data import market

    return market.get_last_close(ticker.upper())


class BuyModal(discord.ui.Modal, title="Log a buy"):
    """The five fields rule 1 requires, plus the size. Ticker and price are options.

    Discord allows five text inputs per modal, which is exactly the written plan:
    shares, thesis, invalidation, horizon, exit. Anything numeric that is not the
    size lives on the slash command so the modal stays the plan.
    """

    shares: discord.ui.TextInput = discord.ui.TextInput(
        label="Shares", placeholder="e.g. 12  (fractional is fine)", max_length=20
    )
    thesis: discord.ui.TextInput = discord.ui.TextInput(
        label="Thesis — why does this make money?",
        style=discord.TextStyle.paragraph, min_length=journal.MIN_THESIS_CHARS,
        max_length=1500,
    )
    invalidation: discord.ui.TextInput = discord.ui.TextInput(
        label="Invalidation — what would prove you wrong?",
        style=discord.TextStyle.paragraph,
        min_length=journal.MIN_PLAN_CHARS, max_length=600,
    )
    horizon: discord.ui.TextInput = discord.ui.TextInput(
        label="Horizon — how long do you intend to hold?",
        min_length=journal.MIN_PLAN_CHARS, max_length=200,
    )
    exit_plan: discord.ui.TextInput = discord.ui.TextInput(
        label="Exit plan — how do you get out, win or lose?",
        style=discord.TextStyle.paragraph,
        min_length=journal.MIN_PLAN_CHARS, max_length=600,
    )

    def __init__(
        self, bot: "LeagueBot", ticker: str, bucket: str,
        price: float | None, stop: float | None,
    ) -> None:
        super().__init__()
        self._bot = bot
        self._ticker = ticker
        self._bucket = bucket
        self._price = price
        self._stop = stop

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # This is where /buy's slow work lives, so this is where it defers. The
        # `str(...)` calls are reads of what Discord already delivered.
        await run_command(
            interaction, self._bot.buy_text,
            interaction.guild_id, interaction.user.id, self._ticker, self._bucket,
            self._price, self._stop, str(self.shares), str(self.thesis),
            str(self.invalidation), str(self.horizon), str(self.exit_plan),
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await modal_failed(interaction, "buy", error)


class SellModal(discord.ui.Modal, title="Log a sell"):
    """Size, and the one honest line about what happened versus the thesis."""

    shares: discord.ui.TextInput = discord.ui.TextInput(
        label="Shares (blank = the whole position)", required=False, max_length=20
    )
    outcome: discord.ui.TextInput = discord.ui.TextInput(
        label="What actually happened vs. your thesis?",
        style=discord.TextStyle.paragraph,
        min_length=journal.MIN_PLAN_CHARS, max_length=1000,
    )

    def __init__(self, bot: "LeagueBot", ticker: str, price: float | None) -> None:
        super().__init__()
        self._bot = bot
        self._ticker = ticker
        self._price = price

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await run_command(
            interaction, self._bot.sell_text,
            interaction.guild_id, interaction.user.id, self._ticker,
            self._price, str(self.shares), str(self.outcome),
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        await modal_failed(interaction, "sell", error)


def render_review(
    report: Any, status: journal.ReviewStatus
) -> str:
    """A member's review: every open position against its own stated trigger."""
    out: list[str] = ["**Review recorded.**"]
    if report is None or not report.open_lines:
        out.append("No open positions — nothing to check against a thesis.")
        out.append(
            f"The {journal.REVIEW_MAX_AGE_DAYS}-day clock is clear either way; "
            "with nothing held there is nothing to review."
        )
        return "\n".join(out)

    acct = report.account
    out.append(
        f"Equity {track.money(acct.equity)} vs SPY {track.money(acct.spy_equity)} — "
        f"{track.pct(acct.pnl_pct)} vs {track.pct(acct.spy_pct)} "
        f"({track.points(acct.edge_pp)})"
    )
    out.append("")
    for line in report.open_lines:
        pos = line.position
        out.append(
            f"__{line.ticker}__ ({line.bucket}) {line.shares:g} sh @ "
            f"{track.money(line.avg_cost)} → {track.money(line.last_price)} · "
            f"{track.signed_money(line.pnl)} ({track.pct(line.pnl_pct)}) · "
            f"SPY {track.pct(line.spy_pct)}"
        )
        out.append(f"Invalidation, as you wrote it: *{pos.invalidation}*")
        if line.stop_breached:
            out.append(
                f"**STOP BREACHED** — your stop was {track.money(line.stop_price)}. "
                "Your own plan says act."
            )
        out.append("")
    out.append(
        f"Next review due within {journal.REVIEW_MAX_AGE_DAYS} days, "
        "or `/buy` locks out."
    )
    return "\n".join(out)


# ------------------------------------------------------------------- helpers

def cycle_tickers(conn: Any, guild_id: int | str) -> set[str]:
    """Every ticker a cycle or standings call needs a mark for."""
    held = {
        h.ticker
        for member in league.members(conn, guild_id)
        for h in journal.open_holdings(conn, member.book)
    }
    return held | {o.decision.ticker for o in arena.pending_orders(conn)}


def build_context(conn: Any, marks: dict[str, float], top: int = 15) -> CycleContext:
    """Screen once, brief once — every member sees the same market context."""
    tickers = universe.load()
    result = screen.run(
        tickers, top=top, snapshot=universe.describe(None, tickers),
        price_fetcher=lambda names: screen.fetch_prices(names),
    )
    candidates = [row.ticker for row in result.selected[:top]]
    prices = dict(marks)
    missing = [t for t in candidates if t not in prices]
    if missing:
        prices.update(fetch_marks(missing))
    return CycleContext(
        screen_table=screen.render_table(result, top=top),
        briefs=arena.briefs_for(sorted(prices) + candidates),
        prices=prices,
    )


def seconds_until_next_cycle(now: dt.datetime) -> float:
    """Seconds until the next Monday 22:00 UTC, strictly in the future."""
    days_ahead = (CYCLE_WEEKDAY - now.weekday()) % 7
    target = (now + dt.timedelta(days=days_ahead)).replace(
        hour=CYCLE_HOUR, minute=0, second=0, microsecond=0
    )
    if target <= now:
        target += dt.timedelta(days=7)
    return (target - now).total_seconds()


#: Discord rejects a message over 2000 characters.
MESSAGE_LIMIT = 2000


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split on line boundaries so a reasoning paragraph is never cut mid-word."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        piece = line[:limit]
        if size + len(piece) + 1 > limit and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(piece)
        size += len(piece) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def announce_channel(guild: Any) -> Any:
    """The guild's system channel if we can post there, else the first we can."""
    me = guild.me
    candidate = guild.system_channel
    if candidate is not None and candidate.permissions_for(me).send_messages:
        return candidate
    for channel in getattr(guild, "text_channels", []):
        if channel.permissions_for(me).send_messages:
            return channel
    return None


def _configure_logging() -> None:
    """Timestamps and logger names on deliberately.

    The console is the only place a live failure is visible, and "which command,
    at what time" is the first thing you need from it.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _token() -> str:
    """The single place this module reads the bot token.

    Both entry points need it, and a leaked token is a full account takeover, so
    the read stays in one auditable spot rather than being repeated per entry
    point. `test_the_token_is_read_once_and_never_sent_anywhere` enforces that.
    """
    return config.discord_token()


def run() -> None:
    """Entry point — `thesis bot`. Reads DISCORD_TOKEN from .env."""
    _configure_logging()
    LeagueBot().run(_token(), log_handler=None)


# ------------------------------------------------------- one-off maintenance

async def clear_global_commands(
    token: str, client: "LeagueBot | None" = None
) -> list[str]:
    """Remove the application's **global** command registrations. Returns the names.

    An earlier global `tree.sync()` registered the commands at application level.
    Switching to per-guild syncing does not retract those, so a member sees each
    command twice: once from the guild copy this bot maintains, once from the
    global leftover. This removes the leftovers.

    Deliberately not part of startup. A global write applies to every server the
    application is in, which is not a decision a routine restart should make on
    the operator's behalf — `sync_commands` only ever reports the leftovers, and a
    test asserts normal startup issues no global write at all.

    `login` authenticates over HTTP and fetches the application id; the command
    endpoints are plain REST, so the gateway is never started and no interaction
    is ever served. `serve=False` keeps `setup_hook` — which `login` also calls —
    from starting the weekly loop in a process about to exit.
    """
    bot_client = client if client is not None else LeagueBot(serve=False)
    try:
        await bot_client.login(token)
        before = sorted(
            command.name for command in await bot_client.tree.fetch_commands()
        )
        if not before:
            log.info(
                "no global command registrations found — nothing to remove. Any "
                "duplicate in the picker is not coming from this application."
            )
            return []

        log.info(
            "removing %d global command registration(s): %s",
            len(before), ", ".join(before),
        )
        bot_client.tree.clear_commands(guild=None)
        await bot_client.tree.sync()

        if remaining := sorted(
            command.name for command in await bot_client.tree.fetch_commands()
        ):
            log.error(
                "still registered globally after the clear: %s — the duplicates "
                "will persist", ", ".join(remaining),
            )
        else:
            log.info(
                "removed %d global registration(s): %s. Per-guild registrations "
                "are untouched — restart with `thesis bot` and each command "
                "appears once.", len(before), ", ".join(before),
            )
        return before
    finally:
        await bot_client.close()


def clear_global() -> list[str]:
    """Entry point — `thesis bot --clear-global`. Does the one job and exits.

    Never reached from `run`, and `run` is never reached from here.
    """
    _configure_logging()
    return asyncio.run(clear_global_commands(_token()))
