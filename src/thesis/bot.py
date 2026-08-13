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
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
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
    """Discord client with the three league commands and the weekly loop."""

    def __init__(
        self,
        connect: Callable[[], Any] = league.connect,
        client_factory: Callable[[], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        intents = kwargs.pop("intents", discord.Intents.default())
        super().__init__(intents=intents, **kwargs)
        self.tree = app_commands.CommandTree(self)
        self._connect = connect
        self._client_factory = client_factory or _anthropic_client
        register_commands(self.tree, self)

    async def setup_hook(self) -> None:
        await self.tree.sync()
        self.loop.create_task(self._weekly_loop())

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


def register_commands(tree: app_commands.CommandTree, bot: "LeagueBot") -> None:
    """Attach /join, /standings and /cycle. Each is a two-line delegation."""

    @tree.command(name="join", description="Get a simulated $100k book traded by an agent.")
    @app_commands.describe(mandate="Which mandate should trade your book?")
    @app_commands.choices(mandate=MANDATE_CHOICES)
    async def join(
        interaction: discord.Interaction,
        mandate: app_commands.Choice[str] | None = None,
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Run this in a server — leagues are per-server.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        text = await asyncio.to_thread(
            bot.join_text,
            interaction.guild_id,
            interaction.user.id,
            interaction.user.display_name,
            mandate.value if mandate else arena.VALUE.name,
        )
        await interaction.followup.send(text)

    @tree.command(name="standings", description="Everyone's book against SPY.")
    async def standings(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Run this in a server — leagues are per-server.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        text = await asyncio.to_thread(
            bot.standings_text,
            interaction.guild_id,
            interaction.guild.name if interaction.guild else "",
        )
        await interaction.followup.send(text)

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
            await interaction.response.send_message(
                "Run this in a server — leagues are per-server.", ephemeral=True
            )
            return
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
            await interaction.response.send_message(
                "Run this in a server — leagues are per-server.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            SellModal(bot, ticker.strip().upper(), price)
        )

    @tree.command(name="review", description="Weekly review — clears the 9-day lockout.")
    @app_commands.describe(note="Optional note to file with the review.")
    async def review(interaction: discord.Interaction, note: str = "") -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Run this in a server — leagues are per-server.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        text = await asyncio.to_thread(
            bot.review_text, interaction.guild_id, interaction.user.id, note
        )
        chunks = split_message(text)
        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    @tree.command(name="research", description="This week's brief for a company.")
    @app_commands.describe(ticker="Ticker to research, e.g. COST")
    async def research(interaction: discord.Interaction, ticker: str) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Run this in a server — leagues are per-server.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        text = await asyncio.to_thread(
            bot.research_text, interaction.guild_id, interaction.user.id, ticker
        )
        chunks = split_message(text)
        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)

    @tree.command(name="cycle", description="Run this week's cycle now (admin only).")
    async def cycle(interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Run this in a server — leagues are per-server.", ephemeral=True
            )
            return
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.manage_guild:
            await interaction.response.send_message(
                "Only a server manager can trigger a cycle.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        text = await asyncio.to_thread(bot.run_cycle_text, interaction.guild_id)
        chunks = split_message(text)
        await interaction.followup.send(chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)


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
        await interaction.response.defer(thinking=True)
        text = await asyncio.to_thread(
            self._bot.buy_text,
            interaction.guild_id, interaction.user.id, self._ticker, self._bucket,
            self._price, self._stop, str(self.shares), str(self.thesis),
            str(self.invalidation), str(self.horizon), str(self.exit_plan),
        )
        await interaction.followup.send(text)


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
        await interaction.response.defer(thinking=True)
        text = await asyncio.to_thread(
            self._bot.sell_text,
            interaction.guild_id, interaction.user.id, self._ticker,
            self._price, str(self.shares), str(self.outcome),
        )
        await interaction.followup.send(text)


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


def run() -> None:
    """Entry point — `thesis bot`. Reads DISCORD_TOKEN from .env."""
    logging.basicConfig(level=logging.INFO)
    token = config.discord_token()
    LeagueBot().run(token, log_handler=None)
