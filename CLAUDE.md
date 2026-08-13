# Thesis — AI Research Copilot + Trading Journal

> Drop this file in your repo root as `CLAUDE.md`. Claude Code reads it automatically and will build against it.

## What this is

A CLI tool that (1) generates AI research briefs on companies from filings + financials + news, (2) enforces a disciplined trading journal — no position exists without a written thesis and exit plan, and (3) produces a timestamped, SPY-benchmarked track record. The `track` report is the deliverable: it's what proves results to anyone, including Mahir's friend.

Two buckets, one account:
- **Core** (~70%): long-term positions in businesses you understand. Thesis = quality + valuation. Horizon: years.
- **Active** (~30%): swing trades. Thesis = specific catalyst + setup. Horizon: days–weeks. Hard stop logged at entry.

## Non-negotiable account rules (enforce these in code, not vibes)

1. No `log buy` without: written thesis, invalidation trigger, time horizon, and exit plan. The command refuses otherwise.
2. Active-bucket trades additionally require a stop level at entry.
3. Position caps: ≤20% of account per Core name, ≤10% per Active name. Sizing check runs before logging.
4. Shares only. No options, no margin, no shorting in v1. Hard-coded.
5. Cash account reality: warn when a buy would use unsettled funds (T+1 — prevents good-faith violations).
6. Weekly review required. If the last `review` is >9 days old, `log buy` is blocked until one runs.
7. Every performance number is shown next to its SPY benchmark (same deposits, same dates, into SPY). Beating cash is not the bar.

## Architecture

- Python 3.12, managed with `uv`
- **Typer** CLI — no web UI in v1
- **SQLite** (`journal.db`) for positions, theses, reviews, equity snapshots
- **yfinance** for prices, fundamentals, news headlines
- **SEC EDGAR** (public domain) for 10-K/10-Q section text (Items 1, 1A, 7)
- **Claude API** for brief generation (structured prompt, citations required)
- **pytest** — all money math (position sizing, P&L, benchmark calc) must have tests; a P&L bug invalidates the entire track record
- Secrets in `.env`, never committed. Type hints throughout.

```
thesis/
├── CLAUDE.md            # this file
├── README.md            # factor formulas, lint rules, journal behaviour
├── pyproject.toml
├── src/thesis/
│   ├── cli.py           # typer app
│   ├── data/            # yfinance + edgar adapters + universe.py (S&P 500 snapshot)
│   ├── brief.py         # research brief generation (Claude API)
│   ├── lint.py          # citation + recommendation linter for generated briefs
│   ├── journal.py       # positions, rules enforcement
│   ├── track.py         # performance + SPY benchmark + report export
│   ├── pdf.py           # markdown → PDF for the exported track record
│   └── screen.py        # momentum + quality screen
├── tests/
├── briefs/              # generated research briefs (md)
└── reports/             # track-record exports (md/pdf)
```

## Commands (the product)

| Command | What it does |
|---|---|
| `thesis research TICKER` | Pulls 2y prices, fundamentals, latest 10-K Items 1/1A/7, recent news → generates a research brief (spec below) → saves to `briefs/` |
| `thesis deposit AMOUNT` | Records cash in (negative for a withdrawal). Position caps and the SPY benchmark are both measured against deposits, so nothing can be logged before one exists |
| `thesis log buy TICKER` | Interactive: bucket, shares, price, thesis, invalidation, horizon, exit plan (+stop if Active). Enforces all rules, writes to journal |
| `thesis log sell TICKER` | Closes position, records outcome + a one-line "what actually happened vs. thesis" |
| `thesis review` | Weekly: shows each open position against its thesis, flags invalidation triggers that have fired, unrealized P&L vs SPY |
| `thesis track` | The results engine: all-time and per-bucket P&L vs SPY, win rate, every closed trade with its original timestamped thesis. `--export` renders md → PDF |
| `thesis screen` | Momentum + quality screen over a liquid US universe → candidate list for `research`. `--explain TICKER` shows the arithmetic behind one name's rank, or which gate excluded it. Every factor formula is documented in the README |
| `thesis universe` | Snapshot provenance, applied renames, retired symbols. `--check` fetches every symbol and lists the dead ones, so universe staleness is caught by process rather than by 404s |
| `thesis arena init/run/report` | The LLM portfolio experiment: three agent personas on simulated $100k books, decisions validated by the same 7 rules, scored against SPY and your paper book. `--dry-run` shows the packets and the cost without spending |

### Paper book (`--paper`)

Every journal command takes `--paper`, which routes it to a second book of
practice positions funded with fake money. Practice is only worth anything if it
is the same exercise, so the paper book shares one implementation with the real
one: all 7 rules bind, position caps are not relaxed, T+1 settlement still
applies, and P&L is benchmarked by the identical SPY mirror. The two books never
mix — separate cash, separate positions, separate review clocks — and a paper
report is labelled `PAPER — simulated money` so it can never be passed off as a
track record.

Benchmark mechanism (both books): every deposit buys SPY on the same date, and
every position is mirrored by the same dollars into SPY on the same dates, exited
in the same proportions. Comparing a trade to "SPY over the same window" is then
the same money over the same days, not an approximation.

### Research brief spec (`brief.py`)

Fixed sections, every factual claim tagged with its source (`[10-K 1A]`, `[yf financials]`, `[news: <headline>]`):
1. Business — what they sell, to whom, how they make money
2. Moat — or honest absence of one
3. Financial snapshot — revenue trend, margins, debt, FCF (numbers, not adjectives)
4. Risks — top items from 10-K Item 1A, in plain English
5. Valuation — vs. its own history and 2–3 peers
6. Bull case / Bear case — steelman both
7. **What would kill this thesis** — specific, checkable triggers
8. Verdict line: NOT "buy/sell" — the brief informs, the human decides. Recommendation language is banned in the prompt.

**Lint gate (`lint.py`).** The prompt asking is not the prompt being obeyed, so every
generated brief is checked: any sentence stating a figure or asserting a fact must
carry one of the valid source tags, and no recommendation construction may appear
anywhere. Violations are fed back to the model for exactly one retry; if the second
draft still fails, the brief is saved with a `LINT FAILED` banner and `thesis research`
exits non-zero. Rules and exemptions are documented in the README.

## 4-Week Build (Jul 15 → Aug 11, ship before UIUC)

| Wk | Build | Learn (while building it) | Done when |
|---|---|---|---|
| 1 (Jul 15–21) | Scaffold repo, data adapters (yfinance + EDGAR section parser), `research` v0 | How to actually read a 10-K: Items 1, 1A, 7 — you're parsing them anyway | A real brief on a company you already know, with citations |
| 2 (Jul 22–28) | `journal.py` with all 7 rules enforced, `log`/`review`, `track` v0 with SPY benchmark | Position sizing + expectancy math — you're coding it | First 1–2 SMALL real positions logged with full theses (1 Core, ≤1 Active) |
| 3 (Jul 29–Aug 4) | `screen` v0 (momentum + quality filters), news into briefs, citation-discipline pass on brief quality | What momentum and quality factors actually measure | Screen → research → decision pipeline runs end to end |
| 4 (Aug 5–11) | `track --export` (md→PDF), review reminders, polish, README | How to judge a track record: benchmark, sample size, luck vs. skill at small N | **Demo to your friend: live `research` run + your track report** |

## Friend protocol

- Share briefs and `track` reports freely. Argue about theses — that's the point of him.
- His money stays in his account, always. If the results convince him, he opens his own brokerage and runs the same playbook beside you.
- Never trade his login, never hold his cash, never take a cut. That's broker-TOS violation territory, investment-adviser gray zone, and friendship poison. The track record exists so he never has to hand you money to participate.

## Money expectations (read once, then just build)

Four weeks of P&L on a sub-$1k account is noise in either direction — don't judge the system or yourself on it. The compounding assets are the tool, the skill, and the track record. Twenty-plus logged theses with honest outcomes vs. SPY is what actually unlocks capital — your friend's first, and credibility generally. Any zero-commission broker with fractional shares works; journal entry is manual in v1 (broker-agnostic, keeps scope tight).

## Claude Code starter prompts

**Week 1, paste first:**
> Read CLAUDE.md. Scaffold the project: uv-managed Python 3.12, typer CLI skeleton, src layout per the tree, pytest wired, .env handling. Then build the data layer: a yfinance adapter (prices, fundamentals, news) and an EDGAR adapter that fetches the latest 10-K for a ticker and extracts Items 1, 1A, and 7 as clean text. Tests for the parsers.

**Week 1, then:**
> Implement `thesis research TICKER` per the brief spec in CLAUDE.md: assemble the data, call the Claude API with a structured prompt that requires source tags on every claim and bans recommendation language, render the 8-section markdown brief to briefs/. Run it on AAPL and show me the output.

**Week 2:**
> Implement journal.py and the log/review/track commands per CLAUDE.md. Every one of the 7 account rules must be enforced in code with a test proving it. track shows P&L vs same-dated deposits into SPY.

## Later — only if v1 earns it

Streamlit dashboard · earnings-transcript summaries · Alpaca paper automation · the full quant-validation stack (the 12-month roadmap doc) when a Strategy idea needs real proof · productizing, if the friend isn't the only one who wants the briefs.

## Arena (week 4+ stretch) — LLM portfolio experiment

- `thesis arena init`: N simulated $100k portfolios, one per agent persona — Value (long-term, quality + valuation), Momentum (swing, strict stops), Monk (max 1 trade/month; cash is a position)
- Weekly cadence: each agent receives the same packet (its positions, screen output, briefs for held names) and returns decisions as JSON
- Agent trades pass through the SAME journal rules as human trades: thesis + invalidation + horizon required, position caps, shares only, no leverage. Invalid decisions are rejected and logged
- Fills simulated at next session's open (yfinance); everything stored in SQLite
- `thesis arena report`: scoreboard — each agent vs SPY vs the human account, with full decision logs
- Hard rule: arena agents never touch a real brokerage. The human account stays human-confirmed, always

**Week 4 stretch prompt:**
> Implement the Arena per CLAUDE.md: agent personas as system prompts, a weekly decision loop that validates each agent's JSON output against the journal rules (reject and log invalid decisions), simulated next-open fills, and `thesis arena report` producing the scoreboard vs SPY and vs the human account.
