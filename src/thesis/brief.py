"""Research brief generation (Claude API).

Assembles a data packet (prices, fundamentals, news, 10-K Items 1/1A/7,
optional peer multiples), sends it to Claude with a structured prompt that
requires a source tag on every factual claim and bans recommendation language,
and renders the 8-section markdown brief to briefs/.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

import anthropic

from thesis import config, lint
from thesis.data import edgar, market
from thesis.data.market import fmt_compact, fmt_pct

# Per-section character caps keep the packet (and cost) bounded. Item 7 (MD&A)
# gets the most room; the tail of each section is the least information-dense.
SECTION_CHAR_CAPS = {"1": 25_000, "1A": 30_000, "7": 30_000}
MAX_NEWS = 10

SYSTEM_PROMPT = """\
You are an equity research analyst writing an internal research brief. You write \
for a disciplined individual investor who makes their own decisions.

## Data discipline
- Base every factual claim ONLY on the data packet provided in the user message. \
Do not use outside knowledge for facts or figures.
- Tag every factual claim with its source, immediately after the claim:
  [10-K 1], [10-K 1A], [10-K 7] — from the filing sections
  [yf prices], [yf financials], [yf valuation], [yf peers] — from market data
  [news: <headline>] — from a news headline
- If something the template asks for is not in the packet, write "not in provided \
data" rather than guessing or filling from memory.
- Use numbers, not adjectives, wherever the packet gives you numbers.

## Absolute ban on recommendation language
This brief informs; the human decides. Never use: buy, sell, hold, accumulate, \
trim, add, exit, overweight, underweight, price target, "I recommend", \
"you should", "attractive entry", or any phrasing that tells the reader what \
action to take. This applies everywhere, including the verdict line.

## Output format
Markdown, with exactly these eight numbered section headings (## level):
1. Business — what they sell, to whom, how they make money
2. Moat — the durable advantage, or its honest absence
3. Financial snapshot — revenue trend, margins, debt, FCF (numbers, not adjectives)
4. Risks — the top risks from 10-K Item 1A, translated into plain English
5. Valuation — current multiples vs. its own history where the packet allows, and \
vs. the peer data if provided; state plainly what is not comparable from the packet
6. Bull case / Bear case — steelman both sides
7. What would kill this thesis — specific, checkable triggers (metrics with \
thresholds, dated events, observable facts)
8. Verdict — ONE sentence characterizing the situation (e.g. what kind of setup \
this is and what the debate hinges on). Not advice, no action words.

Start directly with section 1 — no preamble, no title (the file already has one).\
"""


#: One regeneration. If the model cannot fix its own citations on the second
#: pass, a third is throwing money at a prompt problem.
MAX_ATTEMPTS = 2


@dataclass
class BriefResult:
    ticker: str
    path: Path
    model: str
    input_tokens: int
    output_tokens: int
    lint: lint.LintReport = lint.LintReport()
    attempts: int = 1


# ------------------------------------------------------------ packet assembly

def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[:cap].rsplit("\n", 1)[0] + "\n[... section truncated for length ...]"


def _ratio(value: float | None) -> str:
    return f"{value:.1f}" if value else "n/a"


def _valuation_lines(snap: dict[str, Any]) -> list[str]:
    return [
        f"market cap: {fmt_compact(snap.get('market_cap'))}",
        f"trailing P/E: {_ratio(snap.get('trailing_pe'))}",
        f"forward P/E: {_ratio(snap.get('forward_pe'))}",
        f"P/S (ttm): {_ratio(snap.get('price_to_sales'))}",
        f"EV/EBITDA: {_ratio(snap.get('ev_to_ebitda'))}",
        f"gross margin: {fmt_pct(snap.get('gross_margins'))}",
        "quarterly revenue growth (latest quarter vs. same quarter a year earlier): "
        f"{fmt_pct(snap.get('quarterly_revenue_growth_yoy'))}",
    ]


def build_packet(
    ticker: str,
    prices: dict[str, Any],
    fundamentals: dict[str, Any],
    news: list[dict[str, str]],
    tenk: edgar.TenK,
    peers: list[dict[str, Any]],
) -> str:
    """Render all collected data into the single text packet Claude receives. Pure."""
    snap = fundamentals["snapshot"]
    fin = fundamentals["financials"]
    parts: list[str] = []

    parts.append(f"# DATA PACKET: {ticker} ({snap.get('name')})")
    parts.append(f"Sector: {snap.get('sector')} | Industry: {snap.get('industry')}")

    parts.append("\n## PRICES [yf prices]")
    parts.append(f"Last close: ${prices['last_close']:.2f} (as of {prices['as_of']})")
    parts.append(f"52-week range: ${prices['low_52w']:.2f} – ${prices['high_52w']:.2f}")
    parts.append(
        f"Returns: 3m {fmt_pct(prices['ret_3m'])}, 1y {fmt_pct(prices['ret_1y'])}, "
        f"since {prices['period_start']} {fmt_pct(prices['ret_period'])}"
    )

    parts.append("\n## FINANCIALS [yf financials]")
    for row in fin["revenue_by_year"]:
        parts.append(f"FY{row['fy']} revenue: {fmt_compact(row['revenue'])}")
    for row in fin["net_income_by_year"]:
        parts.append(f"FY{row['fy']} net income: {fmt_compact(row['net_income'])}")
    parts.append(
        "Annual revenue growth (latest full fiscal year vs. the prior one): "
        f"{fmt_pct(fin.get('annual_revenue_growth'))}"
    )
    parts.append(f"Operating margin (latest FY): {fmt_pct(fin['operating_margin'])}")
    parts.append(f"Net margin (latest FY): {fmt_pct(fin['net_margin'])}")
    parts.append(f"Free cash flow (latest FY): {fmt_compact(fin['free_cash_flow'])}")
    parts.append(f"Total debt: {fmt_compact(fin['total_debt'])}")
    parts.append(f"Cash & equivalents: {fmt_compact(fin['cash'])}")

    parts.append("\n## VALUATION [yf valuation]")
    parts.append(
        "NOTE: the revenue-growth figure below is a QUARTERLY rate reported by the "
        "market-data provider (latest quarter vs. the same quarter a year earlier). "
        "It is not an annual rate, it is not comparable to the annual revenue "
        "growth in the FINANCIALS section above, and the two can differ by a wide "
        "margin for the same company. Label whichever you cite."
    )
    parts.extend(_valuation_lines(snap))

    if peers:
        parts.append("\n## PEERS [yf peers]")
        for peer in peers:
            parts.append(f"{peer['ticker']}: " + "; ".join(_valuation_lines(peer)))

    parts.append("\n## NEWS (recent headlines)")
    if news:
        for item in news[:MAX_NEWS]:
            parts.append(f"- [{item['published']}] {item['title']} ({item['publisher']})")
    else:
        parts.append("- no recent headlines returned")

    filing = tenk.filing
    parts.append(
        f"\n## 10-K (filed {filing.filing_date}, period {filing.report_date}, "
        f"accession {filing.accession_number})"
    )
    for label, name in (("1", "Business"), ("1A", "Risk Factors"), ("7", "MD&A")):
        body = _truncate(tenk.items[label], SECTION_CHAR_CAPS[label])
        parts.append(f"\n### ITEM {label} — {name} [10-K {label}]\n{body}")

    return "\n".join(parts)


# ----------------------------------------------------------------- generation

def _stream_body(
    client: Any,
    model: str,
    messages: list[dict[str, Any]],
    on_text: Callable[[str], None] | None,
) -> tuple[str, Any]:
    """One call to Claude. Returns the text body and the final message."""
    with client.messages.stream(
        model=model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=SYSTEM_PROMPT,
        messages=messages,
    ) as stream:
        for chunk in stream.text_stream:
            if on_text:
                on_text(chunk)
        message = stream.get_final_message()
    body = "".join(block.text for block in message.content if block.type == "text")
    return body, message


def _lint_banner(report: lint.LintReport) -> str:
    """Stamped on a brief that failed lint, so the file cannot be mistaken for clean."""
    lines = [
        "> **LINT FAILED — do not treat this brief as sourced.**",
        ">",
        f"> {len(report.violations)} violation(s) survived a regeneration pass:",
        ">",
    ]
    for violation in report.violations:
        lines.append(f"> - line {violation.line}: {violation.detail}")
    lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def generate(
    ticker: str,
    peers: list[str] | None = None,
    on_text: Callable[[str], None] | None = None,
    dry_run: bool = False,
    on_notice: Callable[[str], None] | None = None,
    client: Any | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    model: str | None = None,
    briefs_dir: Path | None = None,
) -> BriefResult:
    """Run the full pipeline: gather data -> Claude -> lint -> briefs/TICKER_DATE.md.

    `on_text` receives streamed output chunks (for live display in the CLI).
    `on_notice` receives status lines (e.g. that a regeneration is happening).
    `dry_run` gathers data and writes the assembled packet without calling Claude.

    The brief is linted for untagged claims and recommendation language. If the
    first draft violates either, the violations are fed back to the model and it
    gets exactly one chance to fix them; whatever comes back is linted again and
    reported honestly in the result.

    `model` overrides the configured model — the Discord league generates on
    Sonnet rather than the Opus the human's own research uses. `briefs_dir`
    overrides where the brief is written, so league-generated briefs stay out of
    the human's own store and their provenance is never ambiguous.
    """
    ticker = ticker.upper()
    today = date.today().isoformat()
    output_dir = briefs_dir or config.briefs_dir()

    prices = market.summarize_prices(market.get_prices(ticker, period="2y"))
    fundamentals = market.get_fundamentals(ticker)
    news = market.get_news(ticker, limit=MAX_NEWS)
    tenk = edgar.get_10k(ticker)
    peer_snaps = [market.get_valuation_snapshot(p) for p in (peers or [])]

    packet = build_packet(ticker, prices, fundamentals, news, tenk, peer_snaps)

    output_dir.mkdir(parents=True, exist_ok=True)
    if dry_run:
        path = output_dir / f"{ticker}_{today}.packet.txt"
        path.write_text(packet, encoding="utf-8")
        return BriefResult(ticker=ticker, path=path, model="dry-run", input_tokens=0, output_tokens=0)

    client = client or anthropic.Anthropic()  # ANTHROPIC_API_KEY from .env
    model = model or config.model()
    messages: list[dict[str, Any]] = [{"role": "user", "content": packet}]

    body = ""
    report = lint.LintReport()
    input_tokens = output_tokens = 0
    attempt = 0

    while attempt < max_attempts:
        attempt += 1
        body, message = _stream_body(client, model, messages, on_text)
        input_tokens += message.usage.input_tokens
        output_tokens += message.usage.output_tokens

        report = lint.lint_brief(body)
        if report.ok or attempt >= max_attempts:
            break

        if on_notice:
            on_notice(
                f"lint found {len(report.violations)} violation(s) in draft "
                f"{attempt} — regenerating once:\n{report.render()}"
            )
        messages = messages + [
            {"role": "assistant", "content": body},
            {"role": "user", "content": lint.correction_prompt(report)},
        ]

    header = (
        f"# {ticker} — Research Brief\n\n"
        f"*Generated {today} · model {model} · "
        f"10-K filed {tenk.filing.filing_date} (accession {tenk.filing.accession_number}) · "
        f"prices as of {prices['as_of']}*\n\n"
        f"*This brief informs; it does not recommend. Sources: SEC EDGAR, yfinance.*\n\n"
        "---\n\n"
    )
    if not report.ok:
        header += _lint_banner(report)

    path = output_dir / f"{ticker}_{today}.md"
    path.write_text(header + body, encoding="utf-8")

    return BriefResult(
        ticker=ticker,
        path=path,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        lint=report,
        attempts=attempt,
    )
