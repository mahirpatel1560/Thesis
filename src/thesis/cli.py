"""Typer CLI — the `thesis` command."""

from __future__ import annotations

import sqlite3
import sys
from typing import Sequence

import typer

from thesis import arena, brief, config, journal, pdf, screen, track
from thesis.data import market, universe
from thesis.journal import Holding, RuleViolation

# Windows consoles often default to cp1252, which can't print characters the
# model routinely emits (en dashes, minus signs). Never let display encoding
# crash a brief mid-stream.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(
    name="thesis",
    help="AI research briefs + disciplined trading journal, benchmarked against SPY.",
    no_args_is_help=True,
)

log_app = typer.Typer(help="Log trades into the journal.", no_args_is_help=True)
app.add_typer(log_app, name="log")

PAPER_OPT = typer.Option(
    False,
    "--paper",
    "-p",
    help="Use the paper book: practice positions, fake money — identical rules, "
    "identical math, identical SPY benchmark.",
)


# --------------------------------------------------------------------- helpers

def _book_banner(book: str) -> None:
    if book == journal.PAPER:
        typer.secho(
            "[paper book] simulated money — same rules, same benchmark",
            fg=typer.colors.MAGENTA,
        )


def _die(message: str) -> None:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def review_banner(status: journal.ReviewStatus) -> str:
    """The one-line reminder for a book's review clock. Pure, so it can be tested."""
    flag = " --paper" if status.book == journal.PAPER else ""
    blocked = f"`thesis log buy{flag}` is blocked until you run `thesis review{flag}`."
    if status.state == "never":
        return (
            f"REVIEW REQUIRED — {status.book} book holds {status.open_positions} "
            f"open position(s) and has never been reviewed. {blocked}"
        )
    if status.state == "overdue":
        overdue_by = -(status.days_remaining or 0)
        return (
            f"REVIEW OVERDUE — {status.book} book: last review {status.days_since} "
            f"days ago, {overdue_by} day(s) past the {journal.REVIEW_MAX_AGE_DAYS}-day "
            f"limit. {blocked}"
        )
    days = status.days_remaining or 0
    return (
        f"Review due in {days} day{'' if days == 1 else 's'} — {status.book} book: "
        f"last review {status.days_since} days ago, limit "
        f"{journal.REVIEW_MAX_AGE_DAYS}. Run `thesis review{flag}`."
    )


def _review_notice(conn: sqlite3.Connection, as_of=None) -> None:
    """Warn about any book whose weekly review is due soon or already overdue.

    Runs on every command, not just the journal ones: the point of rule 6 is that
    you find out you owe a review while doing something else, not at the moment
    it refuses a trade.
    """
    for book in journal.BOOKS:
        status = journal.book_review_status(conn, book, as_of)
        if not status.needs_notice:
            continue
        typer.secho(
            review_banner(status),
            fg=typer.colors.RED if status.blocking else typer.colors.YELLOW,
            bold=status.blocking,
            err=True,
        )


def _notice_only() -> None:
    """Review banner for commands that otherwise need no journal connection.

    Does nothing when no journal exists yet: `research` and `screen` are
    read-only, and a read-only command has no business creating a journal as a
    side effect of checking whether one is overdue.
    """
    if not config.db_path().exists():
        return
    try:
        conn = journal.connect()
    except Exception:
        return  # a broken journal must not stop `research` or `screen`
    try:
        _review_notice(conn)
    finally:
        conn.close()


def _refused(exc: RuleViolation) -> None:
    typer.secho(
        f"\n{journal.refusal_text(exc)}", fg=typer.colors.RED, bold=True, err=True
    )
    raise typer.Exit(1)


def _prices_for(holdings: Sequence[Holding], extra: str | None = None) -> dict[str, float]:
    """Last close for every ticker with open shares, plus one more if asked."""
    tickers = {h.ticker for h in holdings if h.shares > 0}
    if extra:
        tickers.add(extra.upper())
    if not tickers:
        return {}
    try:
        return market.get_last_closes(sorted(tickers))
    except Exception as exc:  # network, bad ticker, delisting
        _die(f"could not fetch current prices ({exc}). The book cannot be valued.")
        raise  # unreachable; keeps type checkers honest


def _spy_for(deposits: Sequence[journal.Deposit], holdings: Sequence[Holding]):
    start = track.first_flow_date(deposits, holdings)
    if start is None:
        return None
    try:
        return track.spy_history(start)
    except Exception as exc:
        _die(f"could not fetch the SPY benchmark ({exc}). Rule 7 needs it.")
        raise


def _prompt_price(label: str, last: float | None) -> float:
    """Prompt for a fill price, defaulting to the last close when we have one."""
    if last:
        return float(typer.prompt(label, type=float, default=last))
    return float(typer.prompt(label, type=float))


def _prompt_min(label: str, minimum: int, provided: str | None, hint: str = "") -> str:
    """Prompt until the answer is long enough to be a real answer."""
    if provided is not None:
        return provided.strip()
    if hint:
        typer.secho(f"  {hint}", fg=typer.colors.BRIGHT_BLACK)
    while True:
        value = str(typer.prompt(label)).strip()
        if len(value) >= minimum:
            return value
        typer.secho(
            f"  too short — {label.lower()} needs at least {minimum} characters. "
            "This is the rule, not a formality.",
            fg=typer.colors.YELLOW,
        )


def _build_report(conn: sqlite3.Connection, book: str) -> track.TrackReport | None:
    """Assemble the full report for a book, or None when nothing is logged."""
    deposits = journal.deposits(conn, book)
    holdings = journal.holdings(conn, book)
    if not deposits and not holdings:
        return None
    spy = _spy_for(deposits, holdings)
    if spy is None:
        return None
    prices = _prices_for(holdings)
    return track.build_report(book, deposits, holdings, prices, spy)


# -------------------------------------------------------------------- research

@app.command()
def research(
    ticker: str = typer.Argument(..., help="Ticker to research, e.g. AAPL"),
    peers: str = typer.Option(
        "",
        "--peers",
        help="Comma-separated peer tickers for the valuation comparison, e.g. MSFT,GOOGL",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Gather data and write the assembled packet without calling Claude.",
    ),
) -> None:
    """Generate a research brief from the 10-K, financials, and news."""
    _notice_only()
    peer_list = [p.strip().upper() for p in peers.split(",") if p.strip()]
    typer.echo(f"Gathering data for {ticker.upper()} (prices, financials, news, 10-K)...")

    def notice(message: str) -> None:
        typer.secho(f"\n\n{message}\n", fg=typer.colors.YELLOW, err=True)

    try:
        result = brief.generate(
            ticker,
            peers=peer_list,
            on_text=lambda chunk: print(chunk, end="", flush=True),
            dry_run=dry_run,
            on_notice=notice,
        )
    except Exception as exc:
        typer.secho(f"\nerror: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if dry_run:
        typer.secho(f"Dry run — wrote data packet to {result.path}", fg=typer.colors.GREEN)
        return
    print()

    drafts = f", {result.attempts} drafts" if result.attempts > 1 else ""
    typer.secho(
        f"\nSaved {result.path} ({result.input_tokens} in / {result.output_tokens} out, "
        f"{result.model}{drafts})",
        fg=typer.colors.GREEN,
    )

    if result.lint.ok:
        typer.secho(result.lint.render(), fg=typer.colors.GREEN)
        return
    typer.secho(f"\n{result.lint.render()}", fg=typer.colors.RED, bold=True, err=True)
    typer.secho(
        "The brief was saved with a LINT FAILED banner. Do not share it as a "
        "sourced brief — fix the flagged claims or re-run.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(1)


# --------------------------------------------------------------------- deposit

@app.command()
def deposit(
    amount: float = typer.Argument(..., help="Dollars in. Negative for a withdrawal."),
    paper: bool = PAPER_OPT,
    on_date: str = typer.Option("", "--date", help="Trade date, ISO (default: today)."),
    note: str = typer.Option("", "--note", help="Where the money came from."),
) -> None:
    """Record a deposit. The SPY benchmark buys SPY with the same dollars, same date."""
    book = journal.book_name(paper)
    _book_banner(book)
    conn = journal.connect()
    _review_notice(conn)
    try:
        entry = journal.add_deposit(
            conn, book, amount, journal.parse_date(on_date) if on_date else None, note
        )
    except ValueError as exc:
        _die(str(exc))
        return
    cash = journal.cash_balance(journal.deposits(conn, book), _all_trades(conn, book))
    verb = "Deposited" if entry.amount > 0 else "Withdrew"
    typer.secho(
        f"{verb} {track.money(abs(entry.amount))} to the {book} book on {entry.deposit_date}. "
        f"Cash now {track.money(cash)}.",
        fg=typer.colors.GREEN,
    )


def _all_trades(conn: sqlite3.Connection, book: str) -> list[journal.Trade]:
    return [t for h in journal.holdings(conn, book) for t in h.trades]


# --------------------------------------------------------------------- log buy

@log_app.command("buy")
def log_buy(
    ticker: str = typer.Argument(..., help="Ticker to open, e.g. AAPL"),
    paper: bool = PAPER_OPT,
    bucket: str = typer.Option("", "--bucket", help="core or active."),
    shares: float = typer.Option(0.0, "--shares", help="Share count (fractional allowed)."),
    price: float = typer.Option(0.0, "--price", help="Fill price (default: last close)."),
    thesis_text: str = typer.Option("", "--thesis", help="Why this makes money."),
    invalidation: str = typer.Option("", "--invalidation", help="What proves you wrong."),
    horizon: str = typer.Option("", "--horizon", help="How long you intend to hold."),
    exit_plan: str = typer.Option("", "--exit-plan", help="How you get out, win or lose."),
    stop: float = typer.Option(0.0, "--stop", help="Hard stop (required for Active)."),
    on_date: str = typer.Option("", "--date", help="Trade date, ISO (default: today)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Open a position. Refuses without a thesis, invalidation, horizon, and exit plan."""
    book = journal.book_name(paper)
    _book_banner(book)
    conn = journal.connect()
    ticker = ticker.strip().upper()
    as_of = journal.parse_date(on_date) if on_date else journal.today()
    _review_notice(conn, as_of)

    state = journal.load_state(conn, book)
    if not state.deposits:
        _die(
            f"the {book} book has no money in it. Run "
            f"`thesis deposit AMOUNT{' --paper' if paper else ''}` first."
        )

    # Rule 6 up front — don't make someone type a full thesis only to be refused.
    try:
        journal.check_review_gate(len(state.open_holdings), state.last_review, as_of)
    except RuleViolation as exc:
        _refused(exc)

    prices = _prices_for(state.open_holdings, ticker)
    last = prices.get(ticker)
    if last:
        typer.echo(f"{ticker} last close: {track.money(last)}")

    existing = journal.find_open(conn, book, ticker)
    if existing:
        typer.secho(
            f"Already holding {existing.shares:g} {ticker} @ {track.money(existing.avg_cost)} "
            f"({existing.bucket}). This add-on averages in and restates the plan.",
            fg=typer.colors.YELLOW,
        )

    if not bucket:
        bucket = typer.prompt(
            "Bucket [core/active]", default=existing.bucket if existing else journal.CORE
        )
    bucket = bucket.strip().lower()
    if bucket not in journal.BUCKETS:
        _die(f"bucket must be one of {journal.BUCKETS}")
    if not shares:
        shares = float(typer.prompt("Shares", type=float))
    if not price:
        price = float(_prompt_price("Fill price", last))

    typer.secho("\nThe plan — this is what the rules require:", bold=True)
    thesis_text = _prompt_min(
        "Thesis", journal.MIN_THESIS_CHARS, thesis_text or None,
        hint="What does this business do, and why does owning it make money?",
    )
    invalidation = _prompt_min(
        "Invalidation trigger", journal.MIN_PLAN_CHARS, invalidation or None,
        hint="Specific and checkable: what fact would prove this thesis wrong?",
    )
    horizon = _prompt_min(
        "Time horizon", journal.MIN_PLAN_CHARS, horizon or None,
        hint="Core: years. Active: days to weeks.",
    )
    exit_plan = _prompt_min(
        "Exit plan", journal.MIN_PLAN_CHARS, exit_plan or None,
        hint="How you get out — both when it works and when it doesn't.",
    )
    stop_price: float | None = stop or None
    if bucket == journal.ACTIVE and stop_price is None:
        stop_price = float(typer.prompt("Hard stop (required for Active)", type=float))

    request = journal.BuyRequest(
        book=book,
        ticker=ticker,
        bucket=bucket,
        shares=shares,
        price=price,
        thesis=thesis_text,
        invalidation=invalidation,
        horizon=horizon,
        exit_plan=exit_plan,
        stop_price=stop_price,
        trade_date=as_of,
    )

    try:
        check = journal.validate_buy(request, state, prices, as_of)
    except RuleViolation as exc:
        _refused(exc)
        return

    typer.echo("")
    typer.secho(
        f"{ticker} — {bucket} — {shares:g} sh @ {track.money(price)} "
        f"= {track.money(check.cost)}",
        bold=True,
    )
    typer.echo(
        f"  {check.position_pct:.1%} of {track.money(check.account_equity)} equity "
        f"(cap {journal.cap_for(bucket):.0%}) · cash after "
        f"{track.money(check.cash - check.cost)}"
    )
    if stop_price:
        risk = shares * (price - stop_price)
        typer.echo(f"  stop {track.money(stop_price)} — risking {track.money(risk)} if hit")
    for warning in check.warnings:
        typer.secho(f"  ! {warning}", fg=typer.colors.YELLOW)

    if not yes and not typer.confirm("\nLog it?", default=True):
        typer.secho("Not logged.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)

    try:
        holding = journal.log_buy(conn, check)
    except RuleViolation as exc:
        _refused(exc)
        return
    typer.secho(
        f"Logged: {holding.ticker} {holding.shares:g} sh @ "
        f"{track.money(holding.avg_cost)} ({holding.bucket}) in the {book} book, "
        f"thesis timestamped {holding.position.opened_at}.",
        fg=typer.colors.GREEN,
    )


# -------------------------------------------------------------------- log sell

@log_app.command("sell")
def log_sell(
    ticker: str = typer.Argument(..., help="Ticker to close, e.g. AAPL"),
    paper: bool = PAPER_OPT,
    shares: float = typer.Option(0.0, "--shares", help="Shares to sell (default: all)."),
    price: float = typer.Option(0.0, "--price", help="Fill price (default: last close)."),
    outcome: str = typer.Option("", "--outcome", help="What happened vs. the thesis."),
    on_date: str = typer.Option("", "--date", help="Trade date, ISO (default: today)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Close a position and record what actually happened vs. the thesis."""
    book = journal.book_name(paper)
    _book_banner(book)
    conn = journal.connect()
    ticker = ticker.strip().upper()
    as_of = journal.parse_date(on_date) if on_date else journal.today()
    _review_notice(conn, as_of)

    holding = journal.find_open(conn, book, ticker)
    if holding is None:
        open_names = ", ".join(h.ticker for h in journal.open_holdings(conn, book)) or "none"
        _die(f"no open {ticker} position in the {book} book. Open: {open_names}")
        return

    prices = _prices_for([holding])
    last = prices.get(ticker)
    typer.echo(
        f"Holding {holding.shares:g} {ticker} @ {track.money(holding.avg_cost)} "
        f"({holding.bucket}, opened {holding.entry_date})"
    )
    if last:
        typer.echo(
            f"Last close {track.money(last)} — unrealized "
            f"{track.signed_money(holding.unrealized_pnl(last))}"
        )
    typer.echo(f"Thesis was: {holding.position.thesis}")
    typer.echo(f"Invalidation was: {holding.position.invalidation}")

    if not shares:
        shares = float(typer.prompt("Shares to sell", type=float, default=holding.shares))
    if not price:
        price = _prompt_price("Fill price", last)
    outcome = _prompt_min(
        "What actually happened vs. the thesis", journal.MIN_PLAN_CHARS, outcome or None,
        hint="One honest line. This is the part that makes the track record worth anything.",
    )

    try:
        journal.validate_sell(holding, shares, price, outcome)
    except RuleViolation as exc:
        _refused(exc)
        return

    realized = shares * (price - holding.avg_cost)
    typer.echo("")
    typer.secho(
        f"Selling {shares:g} {ticker} @ {track.money(price)} "
        f"= {track.money(shares * price)} · realized {track.signed_money(realized)}",
        bold=True,
    )
    typer.echo(
        f"  proceeds settle {journal.settlement_date(as_of)} (T+1) — "
        "unsettled cash can be spent but not re-sold before then"
    )
    if not yes and not typer.confirm("Log it?", default=True):
        typer.secho("Not logged.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)

    after = journal.log_sell(conn, holding, shares, price, outcome, as_of)
    state = "closed" if not after.is_open else f"open with {after.shares:g} sh left"
    typer.secho(
        f"Logged: {ticker} {state}, realized {track.signed_money(after.realized_pnl)} "
        f"in the {book} book. Run `thesis track{' --paper' if paper else ''}` "
        "to see it against SPY.",
        fg=typer.colors.GREEN,
    )


# ---------------------------------------------------------------------- review

@app.command()
def review(
    paper: bool = PAPER_OPT,
    peek: bool = typer.Option(
        False, "--peek", help="Look without recording a review (does not satisfy rule 6)."
    ),
    note: str = typer.Option("", "--note", help="Note to file with the review."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Record the review without the per-position prompts."
    ),
) -> None:
    """Weekly review: every open position against its thesis, P&L vs SPY."""
    book = journal.book_name(paper)
    _book_banner(book)
    conn = journal.connect()
    _review_notice(conn)

    report = _build_report(conn, book)
    if report is None:
        typer.secho(f"Nothing logged in the {book} book yet.", fg=typer.colors.YELLOW)
    else:
        acct = report.account
        typer.secho(f"\nReview — {book} book — {report.as_of}", bold=True)
        typer.echo(
            f"Equity {track.money(acct.equity)} vs SPY benchmark "
            f"{track.money(acct.spy_equity)} · {track.pct(acct.pnl_pct)} vs "
            f"{track.pct(acct.spy_pct)} ({track.points(acct.edge_pp)})"
        )
        typer.echo(
            f"Cash {track.money(acct.cash)} (settled {track.money(acct.settled)})"
        )

        if not report.open_lines:
            typer.echo("\nNo open positions.")
        for line in report.open_lines:
            pos = line.position
            typer.secho(
                f"\n{line.ticker} — {line.bucket.title()} — {line.held_days}d held",
                bold=True,
            )
            typer.echo(
                f"  {line.shares:g} sh @ {track.money(line.avg_cost)} → "
                f"{track.money(line.last_price)} · {track.signed_money(line.pnl)} "
                f"({track.pct(line.pnl_pct)})"
            )
            typer.echo(
                f"  same dollars in SPY: {track.signed_money(line.spy_pnl)} "
                f"({track.pct(line.spy_pct)}) · edge {track.points(line.edge_pp)}"
            )
            typer.echo(f"  Thesis: {pos.thesis}")
            typer.echo(f"  Horizon: {pos.horizon} · Exit plan: {pos.exit_plan}")
            if line.stop_breached:
                typer.secho(
                    f"  STOP BREACHED — stop was {track.money(line.stop_price)}, "
                    f"last {track.money(line.last_price)}. Your own plan says act.",
                    fg=typer.colors.RED,
                    bold=True,
                )
            elif line.stop_price:
                room = (line.last_price / line.stop_price - 1.0) if line.stop_price else None
                typer.echo(
                    f"  Stop {track.money(line.stop_price)} — {track.pct(room)} above it"
                )
            # The invalidation is stated last, immediately above the question, so
            # the answer is given against the trigger as written rather than
            # against whatever you remember writing.
            typer.secho(
                f"  Invalidation trigger, as written at entry:", fg=typer.colors.CYAN
            )
            typer.secho(f"    {pos.invalidation}", fg=typer.colors.CYAN, bold=True)
            if not yes and not peek:
                fired = typer.confirm("  Has exactly that happened?", default=False)
                if fired:
                    typer.secho(
                        "  Flagged. Your exit plan is above — follow it, or write down "
                        "why the thesis changed.",
                        fg=typer.colors.RED,
                    )
                    note = f"{note}\n{line.ticker}: invalidation fired.".strip()

    if peek:
        typer.secho(
            "\nPeek only — no review recorded. Rule 6 still stands.", fg=typer.colors.YELLOW
        )
        return

    entry = journal.record_review(conn, book, note)
    typer.secho(
        f"\nReview recorded {entry.review_date} ({book} book). "
        f"Next one due within {journal.REVIEW_MAX_AGE_DAYS} days.",
        fg=typer.colors.GREEN,
    )


# ----------------------------------------------------------------------- track

def track_cmd(
    paper: bool = PAPER_OPT,
    export: bool = typer.Option(
        False, "--export", help="Write the report to reports/ as markdown and PDF."
    ),
) -> None:
    """The results engine: P&L vs SPY, per bucket, every trade with its thesis."""
    book = journal.book_name(paper)
    conn = journal.connect()
    _review_notice(conn)
    report = _build_report(conn, book)
    if report is None:
        typer.secho(
            f"Nothing logged in the {book} book yet — "
            f"`thesis deposit AMOUNT{' --paper' if paper else ''}` to start.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(0)

    markdown = track.render_markdown(report)
    typer.echo(markdown)

    journal.snapshot_equity(
        conn, book, report.account.equity, report.account.spy_equity, report.as_of
    )

    if export:
        config.REPORTS_DIR.mkdir(exist_ok=True)
        stem = f"track_{book}_{report.as_of}"
        markdown_path = config.REPORTS_DIR / f"{stem}.md"
        markdown_path.write_text(markdown, encoding="utf-8")
        typer.secho(f"Wrote {markdown_path}", fg=typer.colors.GREEN)

        label = "PAPER — simulated money" if report.is_paper else "REAL money"
        try:
            pdf_path = pdf.render_pdf(
                markdown,
                config.REPORTS_DIR / f"{stem}.pdf",
                paper=report.is_paper,
                title=f"Track Record — {label} — {report.as_of}",
                generated=report.as_of,
            )
        except Exception as exc:
            _die(f"markdown written, but the PDF failed to render ({exc})")
            return
        typer.secho(f"Wrote {pdf_path}", fg=typer.colors.GREEN)
        if report.is_paper:
            typer.secho(
                "Every page of that PDF is stamped PAPER — simulated money.",
                fg=typer.colors.MAGENTA,
            )


# Registered under the name `track` while the module keeps its own name.
app.command("track")(track_cmd)


def screen_cmd(
    top: int = typer.Option(20, "--top", help="How many names to show."),
    explain: str = typer.Option(
        "", "--explain", metavar="TICKER", help="Show the arithmetic behind one name's rank."
    ),
    min_dollar_volume: float = typer.Option(
        screen.DEFAULT_MIN_DOLLAR_VOLUME,
        "--min-dollar-volume",
        help="Liquidity floor: median 63-day dollar volume.",
    ),
    min_price: float = typer.Option(
        screen.DEFAULT_MIN_PRICE, "--min-price", help="Liquidity floor: last close."
    ),
    universe_file: str = typer.Option(
        "", "--universe", help="One ticker per line, instead of the S&P 500 snapshot."
    ),
    refresh: bool = typer.Option(
        False, "--refresh", help="Ignore the price cache and re-download the full window."
    ),
) -> None:
    """Momentum + quality screen over a liquid US universe → candidates for `research`."""
    _notice_only()
    try:
        tickers = universe.load(universe_file or None)
    except (OSError, ValueError) as exc:
        _die(str(exc))
        return

    source = universe_file or f"S&P 500 snapshot ({universe.AS_OF})"
    typer.secho(
        f"Screening {len(tickers)} names from {source}"
        f"{' — full re-download' if refresh else ' — cache first'}...",
        fg=typer.colors.BRIGHT_BLACK,
        err=True,
    )
    params = screen.ScreenParams(
        min_dollar_volume=min_dollar_volume, min_price=min_price
    )
    snapshot = universe.describe(universe_file or None, tickers)
    try:
        result = screen.run(
            tickers,
            params=params,
            top=top,
            require=[explain] if explain else (),
            price_fetcher=lambda names: screen.fetch_prices(names, refresh=refresh),
            snapshot=snapshot,
        )
    except Exception as exc:
        _die(f"could not run the screen ({exc})")
        return

    typer.echo(screen.render_table(result, top=top))
    if explain:
        typer.echo(screen.explain(result, explain))

    stale = snapshot.warning()
    if stale:
        typer.secho(stale, fg=typer.colors.YELLOW, err=True)


app.command("screen")(screen_cmd)


# -------------------------------------------------------------------- discord bot

@app.command("bot")
def bot_cmd() -> None:
    """Run the Discord league bot. Needs DISCORD_TOKEN in .env."""
    try:
        config.discord_token()
    except RuntimeError as exc:
        _die(str(exc))
        return
    from thesis import bot as bot_module

    typer.secho(
        f"Starting the league bot — books in {config.league_db_path()}, "
        f"cycles on {config.LEAGUE_MODEL}.",
        fg=typer.colors.GREEN,
    )
    bot_module.run()


# -------------------------------------------------------------------- universe

@app.command("universe")
def universe_cmd(
    check: bool = typer.Option(
        False,
        "--check",
        help="Fetch every symbol and report the ones that return no data.",
    ),
    universe_file: str = typer.Option(
        "", "--universe", help="Audit a custom list instead of the S&P 500 snapshot."
    ),
) -> None:
    """Show the screening universe's provenance, and audit it for dead symbols."""
    try:
        tickers = universe.load(universe_file or None)
    except (OSError, ValueError) as exc:
        _die(str(exc))
        return

    snapshot = universe.describe(universe_file or None, tickers)
    typer.echo(snapshot.describe())
    if universe_file:
        typer.secho(
            "Custom lists carry no snapshot date, so staleness cannot be checked "
            "for you — `--check` still audits the symbols.",
            fg=typer.colors.BRIGHT_BLACK,
        )
    else:
        typer.echo(
            f"{len(universe.RENAMED)} ticker change(s) applied, "
            f"{len(universe.RETIRED)} symbol(s) retired."
        )
        for old, new in sorted(universe.RENAMED.items()):
            typer.echo(f"  renamed  {old:<6} -> {new}")
        for symbol, why in sorted(universe.RETIRED.items()):
            typer.echo(f"  retired  {symbol:<6} — {why}")

    stale = snapshot.warning()
    if stale:
        typer.secho(f"\n{stale}", fg=typer.colors.YELLOW)
    elif snapshot.as_of:
        remaining = universe.STALE_AFTER_DAYS - (snapshot.age_days or 0)
        typer.secho(f"\nSnapshot is current — refresh due in {remaining} days.", fg=typer.colors.GREEN)

    if not check:
        typer.secho(
            "\nRun `thesis universe --check` to fetch every symbol and find the dead ones.",
            fg=typer.colors.BRIGHT_BLACK,
        )
        return

    typer.secho(
        f"\nChecking {len(tickers)} symbols against the price provider "
        "(cache first — this costs nothing if you screened today)...",
        fg=typer.colors.BRIGHT_BLACK,
        err=True,
    )
    try:
        fetched = screen.fetch_prices(tickers)
    except Exception as exc:
        _die(f"could not check the universe ({exc})")
        return

    if not fetched.missing:
        typer.secho(
            f"\nAll {len(tickers)} symbols returned data. Nothing to fix.",
            fg=typer.colors.GREEN,
        )
        return

    typer.secho(
        f"\n{len(fetched.missing)} symbol(s) returned no data after retries:",
        fg=typer.colors.RED,
        bold=True,
    )
    for symbol in sorted(fetched.missing):
        typer.echo(f"  {symbol}")
    typer.secho(
        "\nEach one is either renamed or delisted — retrying will not fix it.\n"
        "For each symbol: search for the company's current ticker.\n"
        "  · still listed under a new symbol -> add it to universe.RENAMED\n"
        "  · acquired or taken private       -> add it to universe.RETIRED with today's date\n"
        "Then bump universe.AS_OF and re-run `thesis universe --check` until this is empty.",
        fg=typer.colors.YELLOW,
    )
    raise typer.Exit(1)


# ----------------------------------------------------------------------- arena

arena_app = typer.Typer(
    help="Simulated LLM portfolio experiment — agent books, fully isolated.",
    no_args_is_help=True,
)
app.add_typer(arena_app, name="arena")


def _arena_prices(conn: sqlite3.Connection, extra: Sequence[str] = ()) -> dict[str, float]:
    held = {h.ticker for a in arena.agents(conn) for h in journal.open_holdings(conn, a)}
    # Pending orders need a mark too — they are about to become holdings, and a
    # reservation priced at nothing reserves nothing.
    held.update(o.decision.ticker for o in arena.pending_orders(conn))
    held.update(t.upper() for t in extra)
    if not held:
        return {}
    try:
        return market.get_last_closes(sorted(held))
    except Exception as exc:
        _die(f"could not fetch prices for the arena ({exc})")
        raise


@arena_app.command("init")
def arena_init(
    cash: float = typer.Option(
        arena.STARTING_CASH, "--cash", help="Simulated starting capital per agent."
    ),
) -> None:
    """Create the three agent books and fund each with simulated capital."""
    conn = arena.connect()
    created = arena.init(conn, cash=cash)
    typer.secho(
        f"Arena database: {config.arena_db_path()} "
        "(separate file from your journal — the books cannot mix)",
        fg=typer.colors.BRIGHT_BLACK,
    )
    if not created:
        typer.secho("All agents already exist. Nothing to do.", fg=typer.colors.YELLOW)
    for name in created:
        typer.secho(
            f"  {name:<9} {track.money(cash)} — {arena.PERSONAS[name].summary}",
            fg=typer.colors.GREEN,
        )
    typer.echo("\nNext: `thesis arena run --dry-run` to see the packets and the cost.")


@arena_app.command("run")
def arena_run(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Build the packets and price the cycle without calling the API."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the cost confirmation."),
    top: int = typer.Option(15, "--top", help="How many screen names to include."),
    on_date: str = typer.Option("", "--date", help="Cycle date, ISO (default: today)."),
) -> None:
    """Run one weekly decision cycle: packet → JSON decisions → the same 7 rules."""
    import anthropic

    conn = arena.connect()
    names = arena.agents(conn)
    if not names:
        _die("no agents — run `thesis arena init` first")
    as_of = journal.parse_date(on_date) if on_date else journal.today()
    model = config.model()

    # -- Settle last cycle's orders BEFORE anything else. Each fills at the open
    # -- after its own decision date, which by now is history.
    settled = arena.fill_pending(conn, _arena_prices(conn))
    for order, outcome in settled:
        if outcome.filled:
            typer.secho(
                f"  filled {order.decision.action} {order.decision.shares:g} "
                f"{order.decision.ticker} (decided {order.decided_on}) @ "
                f"{track.money(outcome.fill_price)} on {outcome.fill_date}",
                fg=typer.colors.GREEN,
            )
        elif outcome.status == "rejected":
            rule = f"rule {outcome.rule}: " if outcome.rule else ""
            typer.secho(
                f"  rejected {order.decision.action} {order.decision.ticker} "
                f"(decided {order.decided_on}) — {rule}{outcome.reason}. Forfeited.",
                fg=typer.colors.RED,
            )

    # -- the shared half of the packet: one screen, one brief corpus, for everyone
    typer.secho("Running the screen for this cycle...", fg=typer.colors.BRIGHT_BLACK, err=True)
    try:
        tickers = universe.load()
        result = screen.run(
            tickers, top=top, snapshot=universe.describe(None, tickers),
            price_fetcher=lambda n: screen.fetch_prices(n),
        )
        screen_table = screen.render_table(result, top=top)
    except Exception as exc:
        _die(f"the arena needs a screen and it failed ({exc})")
        return

    held = {h.ticker for a in names for h in journal.open_holdings(conn, a)}
    candidates = [row.ticker for row in result.selected[:top]]
    briefs = arena.briefs_for(sorted(held) + candidates)
    prices = _arena_prices(conn, candidates)

    packets: dict[str, str] = {}
    for name in names:
        packets[name] = arena.build_packet(
            agent=name,
            holdings=journal.holdings(conn, name),
            deposits=journal.deposits(conn, name),
            prices=prices,
            screen_table=screen_table,
            briefs=briefs,
            as_of=as_of,
            last_trade=arena.last_trade_date(conn, name),
            pending=arena.pending_orders(conn, name),
        )

    client = anthropic.Anthropic()
    counts = [
        arena.count_tokens(client, model, arena.PERSONAS[n].system_prompt, packets[n])
        for n in names
    ]
    estimate = arena.estimate_cost(counts, model)
    typer.secho(estimate.render(), fg=typer.colors.YELLOW)

    if dry_run:
        config.REPORTS_DIR.mkdir(exist_ok=True)
        for name, packet in packets.items():
            path = config.REPORTS_DIR / f"arena_packet_{name}_{as_of}.txt"
            path.write_text(packet, encoding="utf-8")
            typer.secho(f"Wrote {path} ({len(packet):,} chars)", fg=typer.colors.GREEN)
        typer.secho("Dry run — no API call made, no decisions logged.", fg=typer.colors.GREEN)
        return

    if not yes and not typer.confirm("Spend that and run the cycle?", default=True):
        typer.secho("Cancelled.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)

    cycle_id = arena.start_cycle(conn, as_of, model)
    total_in = total_out = 0
    for name in names:
        typer.secho(f"\n=== {name} ===", bold=True)
        try:
            answer = arena.ask_agent(client, arena.PERSONAS[name], packets[name], model)
        except Exception as exc:
            typer.secho(f"  {name} failed to answer ({exc}) — skipped this cycle", fg=typer.colors.RED)
            continue
        total_in += answer.input_tokens
        total_out += answer.output_tokens
        arena.record_reasoning(conn, cycle_id, name, answer.reasoning)
        # The packet put every position, thesis and stop in front of the agent —
        # that is the weekly review, so rule 6's clock is honestly satisfied.
        arena.record_cycle_review(conn, name, as_of)
        typer.echo(f"  {answer.reasoning[:300]}")

        if not answer.decisions:
            typer.secho("  No trades this week.", fg=typer.colors.BRIGHT_BLACK)
            continue

        for decision in answer.decisions:
            arena.record_decision(
                conn, cycle_id,
                arena.Outcome(
                    decision, "pending", f"awaiting the first session open after {as_of}"
                ),
            )
            typer.secho(
                f"  ORDER {decision.action} {decision.shares:g} {decision.ticker} "
                f"— fills at the first open after {as_of}",
                fg=typer.colors.CYAN,
            )

    # One fill pass for everything outstanding, each order at its own decision
    # date. Today's orders usually stay pending until the next session opens.
    typer.secho("\nAttempting fills...", fg=typer.colors.BRIGHT_BLACK)
    for order, outcome in arena.fill_pending(conn, prices):
        label = f"{order.decision.action} {order.decision.shares:g} {order.decision.ticker}"
        if outcome.filled:
            typer.secho(
                f"  FILLED {label} @ {track.money(outcome.fill_price)} "
                f"({outcome.fill_date} open, decided {order.decided_on})",
                fg=typer.colors.GREEN,
            )
        elif outcome.status == "pending":
            typer.secho(f"  PENDING {label} — {outcome.reason}", fg=typer.colors.YELLOW)
        else:
            rule = f"rule {outcome.rule}: " if outcome.rule else ""
            typer.secho(
                f"  REJECTED {label} — {rule}{outcome.reason}. Forfeited.",
                fg=typer.colors.RED,
            )

    arena.finish_cycle(conn, cycle_id, total_in, total_out)
    typer.secho(
        f"\nCycle {cycle_id} complete — {total_in:,} in / {total_out:,} out tokens. "
        "Run `thesis arena report`.",
        fg=typer.colors.GREEN,
    )


@arena_app.command("report")
def arena_report(
    export: bool = typer.Option(False, "--export", help="Write the scoreboard to reports/."),
    decisions: bool = typer.Option(
        True, "--decisions/--no-decisions", help="Include the full decision log."
    ),
) -> None:
    """Scoreboard: each agent vs SPY vs your paper book, with every decision."""
    conn = arena.connect()
    names = arena.agents(conn)
    if not names:
        _die("no agents — run `thesis arena init` first")

    prices = _arena_prices(conn)
    cards: list[arena.Scorecard] = []
    for name in names:
        deposits = journal.deposits(conn, name)
        holdings = journal.holdings(conn, name)
        spy = _spy_for(deposits, holdings)
        if spy is None:
            continue
        report = track.build_report(name, deposits, holdings, prices, spy)
        counts = conn.execute(
            "SELECT COUNT(*) total, SUM(status = 'rejected') rejected "
            "FROM arena_decisions WHERE agent = ?",
            (name,),
        ).fetchone()
        cards.append(
            arena.build_scorecard(
                name, arena.PERSONAS[name].summary, report,
                decisions=counts["total"] or 0, rejected=counts["rejected"] or 0,
            )
        )

    # The human's paper book lives in the other database entirely.
    human = journal.connect()
    human_report = _build_report(human, journal.PAPER)
    if human_report is not None:
        cards.append(
            arena.build_scorecard(
                "you (paper)", "your own paper book, same rules, same benchmark",
                human_report,
            )
        )
    human.close()

    if not cards:
        typer.secho("Nothing to score yet — no funded books.", fg=typer.colors.YELLOW)
        raise typer.Exit(0)

    text = arena.render_scoreboard(cards, journal.today())
    if decisions:
        text += "\n" + arena.render_decisions(arena.decision_log(conn))
    typer.echo(text)

    if export:
        config.REPORTS_DIR.mkdir(exist_ok=True)
        path = config.REPORTS_DIR / f"arena_{journal.today()}.md"
        path.write_text(text, encoding="utf-8")
        typer.secho(f"Wrote {path}", fg=typer.colors.GREEN)


if __name__ == "__main__":
    sys.exit(app())
