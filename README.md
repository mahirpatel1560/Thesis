# thesis

A CLI research copilot and trading journal. Three jobs:

1. **`research`** — build an AI research brief on a company from its 10-K, financials, and news, with a source tag on every claim and no recommendation language.
2. **`log` / `review`** — enforce a disciplined journal. No position exists without a written thesis, an invalidation trigger, a horizon, and an exit plan.
3. **`track`** — produce a timestamped, SPY-benchmarked record of what actually happened.

`screen` sits in front of all of it, turning a 494-name universe into a short candidate list worth researching.

Design rule throughout: **no black boxes.** Every factor is a plain formula, written out below. Every rule refusal names the rule. Every performance number appears next to its benchmark.

---

## Setup

```bash
uv sync                            # Python 3.12, managed by uv
cp .env.example .env               # then fill in the two secrets
uv run thesis --help
```

`.env` needs:

```
ANTHROPIC_API_KEY=sk-ant-...
SEC_EDGAR_USER_AGENT=thesis-cli your.email@example.com   # SEC requires a contact
```

Run the tests with `uv run pytest`. They are fully offline — no network, no API key.

---

## Coming back after a break

If it has been weeks or months, run these four in order. They are designed to tell you what has rotted while you were away, in the order it matters.

```bash
uv run thesis track                 # 1. where do I actually stand, vs SPY?
uv run thesis review                # 2. clear the review gate (it is blocking buys)
uv run thesis universe --check      # 3. has the S&P snapshot gone stale?
uv run thesis screen --top 20       # 4. what is worth researching now
```

**What you should expect to see, and what it means:**

1. **`track`** prints the record and, before it, a red `REVIEW OVERDUE` banner. Any command prints that banner — you cannot do anything in this tool without being told the review clock has run out. Add `--export` to get `reports/track_real_<date>.md` and a matching PDF.
2. **`review`** walks every open position against its own thesis and invalidation trigger, flags breached stops, and records the review. Until this runs, **`log buy` is refused** (rule 6). This is deliberate: coming back after two months and immediately buying something is exactly the behaviour the rule exists to stop.
3. **`universe --check`** fetches every symbol in the snapshot and lists any that return no data. After two months there will usually be some — see [Keeping the universe current](#keeping-the-universe-current). A stale snapshot does not corrupt anything; it just quietly stops screening names that have been renamed.
4. **`screen`** is cache-first, so the first run after a break re-downloads and takes ~45 seconds. Every run after that on the same day is instant.

Nothing here mutates a position. `track`, `universe --check` and `screen` are read-only; `review` only records that you looked.

If you want to try something without touching your real record, add `--paper` to any journal command, or point `THESIS_DB` at a scratch file.

---

## Command reference

Every journal command (`deposit`, `log buy`, `log sell`, `review`, `track`) takes **`--paper` / `-p`** to route it to the practice book.

### `thesis screen`

Rank the universe by momentum + quality.

| Flag | Default | Effect |
|---|---|---|
| `--top N` | 20 | How many qualifying names to show |
| `--explain TICKER` | — | Show the arithmetic behind one name's rank, or which gate excluded it |
| `--min-dollar-volume N` | 20000000 | Liquidity floor: median 63-day dollar volume |
| `--min-price N` | 5 | Liquidity floor: last close |
| `--universe PATH` | — | Screen a custom list (one symbol per line, `#` comments) |
| `--refresh` | off | Ignore the price cache and re-download the full window |

### `thesis universe`

Show the screening universe's provenance; audit it for dead symbols.

| Flag | Effect |
|---|---|
| `--check` | Fetch every symbol and list the ones returning no data. Exits non-zero if any do |
| `--universe PATH` | Audit a custom list instead of the snapshot |

### `thesis research TICKER`

Build a brief from the 10-K, financials and news; lint it; save to `briefs/`.

| Flag | Effect |
|---|---|
| `--peers A,B,C` | Add peer multiples to the valuation section |
| `--dry-run` | Assemble and write the data packet without calling the API (free; useful for checking what the model will actually see) |

Exits non-zero if the brief fails the lint gate after its one retry.

### `thesis deposit AMOUNT`

Record cash in; negative for a withdrawal. Position caps and the SPY benchmark are both measured against deposits, so nothing can be logged before one exists.

| Flag | Effect |
|---|---|
| `--date ISO` | Trade date (default today) |
| `--note TEXT` | Where the money came from |

### `thesis log buy TICKER`

Open or add to a position. Prompts for anything not passed as a flag, and re-prompts when an answer is too short to be a real answer.

| Flag | Effect |
|---|---|
| `--bucket core\|active` | Which book half |
| `--shares N` · `--price N` | Size and fill (price defaults to the last close) |
| `--thesis` · `--invalidation` · `--horizon` · `--exit-plan` | The written plan rule 1 requires |
| `--stop N` | Hard stop — required for Active, must be below entry |
| `--date ISO` | Trade date (default today) |
| `--yes` / `-y` | Skip the confirmation prompt |

### `thesis log sell TICKER`

Close or partially close a position.

| Flag | Effect |
|---|---|
| `--shares N` | Default: the whole position |
| `--price N` | Fill (defaults to the last close) |
| `--outcome TEXT` | What actually happened vs. the thesis — required |
| `--date ISO` · `--yes` | Trade date; skip confirmation |

### `thesis review`

Weekly review: every open position against its thesis, P&L vs SPY, breached stops flagged.

| Flag | Effect |
|---|---|
| `--peek` | Look without recording a review — does **not** satisfy rule 6 |
| `--yes` / `-y` | Record without the per-position invalidation prompts |
| `--note TEXT` | File a note with the review |

### `thesis track`

The results engine, and the deliverable.

| Flag | Effect |
|---|---|
| `--export` | Write `reports/track_<book>_<date>.md` **and** `.pdf` |

### `thesis bot`

Run the Discord league. Needs `DISCORD_TOKEN` in `.env`. Long-running.

| Flag | Effect |
|---|---|
| `--clear-global` | Maintenance, **not** startup: remove the leftover global command registrations an earlier global sync left behind, then exit without starting the bot. Run once if a command appears twice in the picker — see [Clearing the leftover global registrations](#clearing-the-leftover-global-registrations) |

---

## `thesis track --export` — the PDF

`--export` writes the report twice: markdown for reading and diffing, and a PDF for handing to someone.

The PDF is rendered from that same markdown, so the two can never disagree. It is landscape (the open-positions table is twelve columns wide), tables repeat their header row across page breaks, money columns are right-aligned, and each trade-log entry is kept on one page so a thesis is never split in half.

It contains exactly what the markdown does:

- total and per-bucket P&L, each beside its SPY mirror, with the edge in percentage points
- every open and closed position with its full numbers
- the trade log: every position's **original thesis, invalidation trigger, horizon, exit plan and outcome, with the UTC timestamp it was written at**

**A paper report is stamped on every page.** A magenta band across the top and a repeated footer both read `PAPER — SIMULATED MONEY — NOT A REAL TRACK RECORD`, so a page that gets separated from the document is still obviously simulated. Real reports carry no such banner and the word "PAPER" appears nowhere in them.

Rendering uses `reportlab` — pure Python, no system libraries, so it works the same on Windows as anywhere else.

---

## Keeping the universe current

The screen's universe is a **dated snapshot**, not a live index feed. Index changes and ticker renames accumulate silently: a renamed symbol simply stops returning data, and without a process that reads as "12 names possibly delisted" rather than "your list is four months old."

So staleness is caught by the calendar, not by 404s:

- **Every screen is stamped** with the snapshot date and its age: `Universe: S&P 500 snapshot — 486 names, snapshot 2026-07-01 (27 days old)`.
- **Past 90 days** (`universe.STALE_AFTER_DAYS`) the screen prints a warning in its output *and* on stderr, telling you to run the check.
- **`thesis universe`** shows provenance, every applied rename, and every retired symbol.
- **`thesis universe --check`** fetches all of them and exits non-zero listing any that return nothing.

### The refresh procedure

1. Run `uv run thesis universe --check`.
2. If it lists nothing, bump `AS_OF` in `src/thesis/data/universe.py` and you're done.
3. For each symbol it lists, find the company's current ticker, then edit `universe.py`:
   - **Still listed under a new symbol** → add `"OLD": "NEW"` to `RENAMED`. `load()` applies it to custom watchlists too, so old lists keep working.
   - **Acquired or taken private** → add it to `RETIRED` with the date you checked and what you found.
   - **Genuinely new index member** → add it to the `SP500` tuple.
4. Bump `AS_OF` to today.
5. Re-run `thesis universe --check` until it is clean.

Retrying never fixes these. When this was last done, all twelve failures were permanent: four renames (`BK`→`BNY`, `MMC`→`MRSH`, `FI`→`FISV`, `PARA`→`PSKY`) and eight completed acquisitions or take-privates. Individual retries with backoff recovered **zero** of them.

---

## `thesis screen` — the factors

The universe is a dated S&P 500 snapshot in `src/thesis/data/universe.py` (486 symbols as of 2026-07-01). It is a hand-maintained approximation of the index, not a live constituent feed — good enough to generate candidates, not good enough to backtest. Override it with `--universe path/to/tickers.txt` (one symbol per line, `#` comments allowed). See [Keeping the universe current](#keeping-the-universe-current) for how staleness is caught.

Prices are split- and dividend-adjusted daily closes from yfinance, so all returns are total returns. Gates run in this order, and a name that fails one is never scored by the next.

### 1. Data gate

A name needs **253 daily closes** (`LOOKBACK_12M + 1`). Recent IPOs, delistings, and renamed symbols land here. They are reported in the exclusion summary rather than silently dropped, and a symbol that returned nothing even on individual retries is reported separately from one with merely thin history — the first is a universe problem, the second is a data problem.

### 2. Liquidity gate

```
median_dollar_volume = median( close × volume )  over the last 63 trading days
```

Pass requires **both**:

- `last close ≥ $5` (`--min-price`)
- `median_dollar_volume ≥ $20,000,000` (`--min-dollar-volume`)

Median rather than mean, so one earnings-day volume spike cannot make an illiquid name look tradeable. 63 trading days is about one quarter.

### 3. Momentum score

Two returns, using the exact bar `n` trading days back:

```
ret_6m  = close[-1] / close[-127] - 1      # 126 trading days ≈ 6 months
ret_12m = close[-1] / close[-253] - 1      # 252 trading days ≈ 12 months
```

Each is converted to a **cross-sectional percentile rank** across every liquid name (not just the top slice), with ties averaged:

```
pctl(x) = rank(x) / n × 100
```

so the strongest return scores 100 and the weakest scores `100/n`. Then:

```
score = 0.5 × pctl(ret_6m) + 0.5 × pctl(ret_12m)
```

Two deliberate choices, both arguable:

- **Ranks, not raw returns.** One name up 700% would otherwise dominate a raw-return average and compress everything else into noise.
- **Equal weights.** Neither horizon is more trustworthy than the other, and inventing weights without evidence would be a black box with extra steps.
- **No skip-month.** Academic momentum usually skips the most recent month to avoid short-term reversal. This does not, because it is a candidate generator for human research, not a systematic strategy. If you use it as one, add the skip.

Ties in the composite break on ticker, so a screen is reproducible run to run.

### 4. Quality gate

A binary filter, not a score — it removes names, it does not rank them:

- `trailing net income > 0` (yfinance `netIncomeToCommon`)
- `trailing free cash flow > 0` (yfinance `freeCashflow`)

A name with either figure missing fails the gate as `no fundamentals available`, rather than being assumed healthy.

Fundamentals cost one network round trip per name, so they are fetched **lazily walking down the momentum ranking** until `--top` names pass (capped by `max_quality_checks`, default 80). The consequence, stated plainly: momentum ranks are universe-wide and exact, while quality is only known for the names the walk reached. `--explain TICKER` forces a fundamentals fetch for that one name so it can always be explained.

### Reading the output

The table shows both raw returns, both percentile ranks, the composite score, both quality figures, and the liquidity measure — everything that produced the rank. `--explain TICKER` shows the same arithmetic for one name and, critically, says which gate removed it if it isn't in the table.

A rank is a candidate, not a thesis. It says a name has gone up and earns money. It says nothing about why, or whether that continues.

### The price cache

Bars that have already closed never change, so they are stored once in `.cache/prices.db` (gitignored; `THESIS_PRICE_CACHE` overrides the path).

- A ticker **already fetched today** is served from disk with **no network call**. A same-day re-run, including every `--explain`, downloads nothing.
- A ticker fetched **earlier** is **topped up from its last stored bar**, not re-downloaded. Measured on the full universe: a cold run downloads 486 names in ~44s; an 8-day-stale run tops all 486 up in ~33s and adds only the 24 genuinely new bars to a 243,000-bar table.
- `--refresh` ignores the cache and re-downloads the full window.

Freshness is "was this fetched today", not "is the last bar recent". Holidays, half-days and delistings all make bar-date arithmetic wrong in ways that either re-download everything or serve stale data without saying so.

The last stored bar is deliberately re-requested on a top-up, so a restated close overwrites the cached one rather than being stranded.

### When the provider drops a name

A bulk download quietly omits names, and the omission looks identical whether the provider hiccuped or the symbol is dead. The screen distinguishes them:

1. Anything missing from the batch is retried **individually**, up to `retries` times, with exponential backoff (1s, 2s, 4s).
2. A name that succeeds on retry is reported as a **transient failure recovered** — it stays in the screen.
3. A name that fails every attempt is excluded as `symbol returned no data after retries`, reported separately, and `--explain` says outright that this is not flakiness and the universe list is what needs fixing.

**Retrying is not a cure for a stale universe.** On this universe it recovers nothing, because the 12 names that were failing were not flaky — four had been renamed and eight had been acquired or taken private. Those are handled in `universe.py`:

- `RENAMED` maps old symbols to current ones (`BK`→`BNY`, `MMC`→`MRSH`, `FI`→`FISV`, `PARA`→`PSKY`), applied by `load()` so an old watchlist keeps working.
- `RETIRED` records symbols dropped from the snapshot, each with the date they were checked, so the removal is auditable rather than silent.

Both were verified by fetching each symbol: the old ones return a 404 "Quote not found", the replacements return current bars.

---

## `thesis research` — the brief linter

The prompt asks for a source tag on every factual claim and bans recommendation language. Asking is not enforcing, so every generated brief is linted. If the first draft violates anything, the violations are fed back to the model and it gets **exactly one** retry; the second draft is linted again, and whatever survives is reported. A brief that still fails is saved with a `LINT FAILED` banner listing the violations, and the command exits non-zero.

One retry, not a loop: if the model cannot fix its own citations on a second pass, that is a prompt problem, not something to throw money at.

### Source rule

A unit of text needs a source tag if it **states a figure** (contains a digit) or **asserts a fact** (contains one of the words in `lint.FACT_WORDS` — revenue, margin, debt, grew, guidance, acquired, and so on).

Valid tags are exactly the ones the prompt defines:

`[10-K 1]` · `[10-K 1A]` · `[10-K 7]` · `[yf prices]` · `[yf financials]` · `[yf valuation]` · `[yf peers]` · `[news: <headline>]`

A bracketed token that isn't one of those is reported as an **unknown tag** rather than quietly accepted as a citation.

Exemptions, all narrow and deliberate:

- **Headings** — the template writes them, so `## 3. Financial snapshot — revenue trend, margins, debt, FCF` is not an unsourced claim.
- **Tables** — a whole table is one unit, and its tag may sit anywhere inside it or on its caption line (the nearest non-blank line above). Requiring a tag per row would be theatre.
- **`not in provided data`** — the prompt's own escape hatch for something the packet doesn't cover.
- **Fenced code blocks** are skipped entirely.

### Recommendation rule

The hard part is catching advice without catching description. "What they sell" and "the acquisition will add $2.5B" are facts about a company; "sell the shares" and "investors should add here" are advice. So the check is a list of constructions, not a bare word list:

| Flagged | Example |
|---|---|
| explicit recommendation | "we recommend", "recommendation" |
| price target | "our price target is $260" |
| position weighting | "overweight", "underweight" |
| advice aimed at the reader | "investors should", "you could" |
| advisory `should` | "margins should improve" |
| action on the position | "buy the shares", "trim the position", "exit it" |
| imperative advice | a line starting "Buy", "Sell", "Hold" |
| entry-timing language | "attractive entry", "compelling risk-reward" |
| verdict phrased as a call | "a clear buy" |
| bare `buy` / `sell` | "sell before the print" |

Bare `buy`/`sell` are **exempt when the preceding word makes them descriptive** — `they`, `customers`, `users`, `clients`, `consumers`, `firms`, and similar. That is what lets `what they sell` and `customers buy replacement thrusters` through while still catching `sell the stock`.

`should` is flagged even about the business, not just the reader. A brief describes what is true; "margins should improve" is a forecast wearing a fact's clothes. Write "management guided to margin improvement [10-K 7]" instead.

Nested matches are reported once — "you should" does not also report as "should".

---

## The journal

### The seven account rules

Each is enforced in code, and each has a test proving it. A refusal names its rule number.

1. **No `log buy` without a written plan** — thesis (≥40 chars), invalidation trigger, time horizon, exit plan. Minimum lengths exist so "good company" cannot pass as a thesis.
2. **Active trades log a stop at entry** — and the stop must be *below* the entry price, or it would fire immediately.
3. **Position caps** — ≤20% of account equity per Core name, ≤10% per Active name, counting shares already held in that name and measured at market.
4. **Shares only** — no options (symbols with digits are refused), no shorting (share counts must be positive; you cannot sell more than you hold), no margin (a buy cannot exceed the book's cash).
5. **T+1 settlement warning** — a buy that reaches into unsettled sale proceeds warns and names the pending sale, but does not block. Market holidays are not modelled, so verify with your broker.
6. **Weekly review** — once you hold anything, `log buy` is blocked if the last `review` is more than 9 days old. The first-ever buy is not trapped: with nothing held, there is nothing to review. **From two days out, every command prints a banner** — `Review due in 2 days`, then `REVIEW OVERDUE` past the limit — so the deadline arrives as a warning rather than as a refusal in the middle of placing a trade. The banner and the block are computed by the same function, so a warning can never disagree with the rule it is warning about; a test asserts that across the whole range of ages. The clock is per book, and the banner names which book owes a review.
7. **Every performance number appears next to SPY.** A structural test asserts that every table in a `track` report carries a benchmark column.

### The SPY benchmark

The benchmark is a **mirror**, not an approximation. Every dollar the journal puts to work is simultaneously put into SPY on the same date, and taken back out in the same proportion on the same date:

- The **account** mirror runs on deposits — what the same deposits, on the same dates, would have made in SPY.
- A **position** mirror runs on that position's own trades.
- A **bucket's** benchmark is the sum of its positions' mirrors.

So "this trade vs. SPY over the same window" needs no hand-waving: it is the same money over the same days. Idle cash is included in account equity, because sitting in cash is a choice the benchmark should charge you for.

### Repeat buys average in

Buying a name you already hold does **not** open a second lot. Instead:

- **Shares add**, and the cost basis becomes the **weighted average** of all buys.
- The **add-on is appended to the thesis**, stamped with its own date and size: `[added 2026-07-22 — 10 sh @ $120.00] <the new argument>`. The original argument is never overwritten, so the record shows what you believed and when.
- The **invalidation trigger, horizon, exit plan, and stop are replaced** by the ones you state at the add-on. The current plan is the one that governs, and re-stating it is the point of the exercise.
- The **entry date stays the first buy**, which is what the position's age is measured from.
- A name **cannot be open in two buckets at once**. A Core holding can't quietly acquire an Active tranche; close it first.

This keeps one thesis per name and one number per position. It does not distort the benchmark, because the SPY mirror runs on individual trades — each buy's dollars enter SPY on that buy's own date, not on the position's first date.

Realized P&L uses **average-cost accounting**, walked chronologically, so a partial sell followed by a later add-on lands on the right basis. Partial sells keep the position open and append a dated note to the outcome; the position is stamped closed when the last share goes.

### Two books

`--paper` routes any journal command to a practice book funded with fake money. It shares one implementation with the real book, so all seven rules bind, caps are not relaxed, T+1 still applies, and P&L is benchmarked by the identical mirror. Practice is only worth anything if it is the same exercise.

The books never mix — separate cash, positions, and review clocks — and a paper report is labelled `PAPER — simulated money` so it can never be passed off as a track record.

---

## Data sources and their limits

| Source | Used for | Limitation worth knowing |
|---|---|---|
| yfinance | prices, volume, fundamentals, news | Unofficial API. `.info` fields are occasionally missing or stale; missing quality data fails the gate rather than passing it. Bulk downloads silently drop names — see above. |
| SEC EDGAR | 10-K Items 1, 1A, 7 | Public domain. Requires a contact `User-Agent`. Section parsing is regex-based over filing HTML — see below. |
| Claude API | brief generation | Non-deterministic. This is why the linter exists. |
| `universe.py` | the screening universe | A dated snapshot, not a live index feed. |

`revenueGrowth` is a **quarterly** figure despite its name, and it does not always reconcile with the provider's own statements — see [Provider metrics do not always mean what they are named](#provider-metrics-do-not-always-mean-what-they-are-named).

### 10-K section extraction

Item headings are located at the start of a line once the filing HTML is flattened, and the longest candidate per item wins (a table-of-contents entry is followed almost immediately by the next TOC line). Two filer conventions break the naive version of that, both now handled:

- **Separators other than whitespace.** Costco writes `Item 1—Business` with the em dash glued to the number. The parser delimits the item number by "not a letter or digit" instead of assuming a space, which also keeps `Item 10` from ever reading as `Item 1`.
- **Incorporation by reference.** JPMorgan answers Item 7 with a 388-character pointer — "appears on pages 46-160" — and puts the MD&A later in the same document under a running page header. When a section comes back too short, the parser locates the section by that repeated header: occurrences are grouped by proximity, isolated ones (the TOC entry) are dropped, and the section spans the first to the last of what remains.

Every extracted section must exceed `MIN_SECTION_CHARS` (2,000). If any does not, `get_10k` raises `SectionExtractionError` **at fetch time**, naming the filing and what came back, rather than passing placeholder text into a brief. A brief built on placeholders looks completely normal and is completely ungrounded — this is the one failure mode that must be loud.

Validated on AAPL, COST, MU, JPM, KO and CAT: all six extract substantive Items 1, 1A and 7.

Storage is a single SQLite file, `journal.db`, gitignored. `THESIS_DB` overrides its path — tests use it, and so should any manual experiment you don't want in your real record.

---

## `thesis arena` — the LLM portfolio experiment

Three agent personas each run a simulated $100,000 book. Every week each one gets the same packet — its own positions, the latest screen, and the briefs for names it holds or wants — and returns decisions as structured JSON.

| Persona | Mandate |
|---|---|
| `value` | Long-term quality + valuation. Core bucket, horizons in years. Most weeks it should do nothing. |
| `momentum` | Swing trades on confirmed strength. Active bucket, a stop on every entry, days-to-weeks horizons. |
| `monk` | At most **one trade per month**. Cash is a legitimate position, and an empty decision list is the expected answer. |

```bash
uv run thesis arena init                # three books, $100k each
uv run thesis arena run --dry-run       # packets + cost estimate, no API spend
uv run thesis arena run                 # one weekly cycle
uv run thesis arena report              # scoreboard + every decision
```

### Agent trades are not special

This is the whole point of the experiment. Every decision is turned into a `journal.BuyRequest` and pushed through `journal.validate_buy` — **the same function the human's trades go through**. All seven rules bind identically: written thesis, invalidation, horizon and exit plan; a stop below entry for Active names; position caps; shares only; no margin; no shorting.

A decision that fails is **rejected, logged with the rule number that refused it, and forfeited**. It is never repaired, never re-asked, never quietly downgraded into something legal. Re-prompting until the model produces a legal trade would launder a bad decision into a good-looking one, so the arena does not offer the single retry that `research` does.

Rejections are as much a result as fills — the scoreboard counts them, and the decision log shows each one with the rule that killed it and the agent's own stated reasoning.

### Isolation

Arena books live in **their own database file** (`.cache/arena.db`; `THESIS_ARENA_DB` overrides), not in a different column of your journal. An arena bug cannot reach `journal.db` because it never opens it. Cash, positions, review clocks and equity are separate per agent, and separate again from your real and paper books — all of which is asserted by tests.

**Agents can never reach a brokerage.** There is no broker integration anywhere in this codebase, and a test parses `arena.py`'s imports against an allowlist (`json`, `sqlite3`, `pandas`, `yfinance`, `thesis`, stdlib) so one cannot be added without the test failing. The only outbound calls the module can make are to the market-data provider and the Claude API. Fills are simulated.

### Fills, cost, and the benchmark

- **Fills simulate at the open of the first session after the order's own decision date** — strictly after, since decisions are made on the close. An order decided on the 1st fills at the 3rd's open even if it is settled on the 11th; by then that open is history, not a guess. It is never filled at the newest available open, which would hand the agent a price it could not have seen.
- **Every run settles outstanding orders first**, oldest decision first, so an earlier fill is a real position by the time a later order is judged. That ordering is what makes the caps bind across cycles.
- **Open and pending exposure are counted together.** An order that has been placed and not executed is committed capital, so it counts against the caps and the cash. Two filters keep that honest: orders that will each take their own turn in the same pass are not reserved against each other (that would double-count), and an order decided *later* never reserves against an earlier fill (a fill on the 3rd cannot be charged for capital committed on the 10th — the later order is instead judged against the position the earlier one created).
- **Packets list the agent's own pending orders**, with the capital they commit and an explicit warning not to re-order the same name. Without it an agent sees "entirely in cash" and buys the same names again; that happened, and it is what the duplicate rejections in the log are.
- **Cost is printed before every run** — input tokens counted exactly via the API's free `count_tokens` endpoint, output estimated, priced from `arena.MODEL_PRICING`. A measured cycle runs **~21,000 input tokens and ~$0.20** on `claude-opus-4-8`. `--dry-run` writes the packets to `reports/` and spends nothing.
- **Benchmarking is the same SPY mirror** as your own books — same dollars, same dates, proportional exits — so agent, human and SPY are genuinely comparable.

Decisions come back through `output_config.format` with a JSON schema, so the response is schema-valid by construction. That guarantees it *parses*; it guarantees nothing about whether it is *legal*, which is what the journal rules are for.

---

## `thesis bot` — the Discord league

A hosted Arena. Members of a Discord server `/join` to get a simulated $100,000 book traded by an LLM agent under a mandate they pick; once a week the bot runs a cycle and posts what every agent did and why.

```bash
uv run thesis bot                  # needs DISCORD_TOKEN in .env
uv run thesis bot --clear-global   # one-off maintenance, then exits — see below
```

| Command | What it does | Daily cap |
|---|---|---:|
| `/join [mandate]` | Creates and funds your book. Mandate is one of `value`, `momentum`, `monk` (default `value`). Idempotent — joining twice does not fund you twice | — |
| `/buy TICKER [bucket] [price] [stop]` | Opens a modal for the written plan, then logs the buy | 8 accepted |
| `/sell TICKER [price]` | Opens a modal for size and outcome, then closes or trims | 8 accepted |
| `/review [note]` | Every open position against its own stated trigger. Clears the 9-day lockout | 6 |
| `/research TICKER` | This week's brief for a company, generated on a cache miss | 3 |
| `/standings` | Every member's book ranked by return, with the SPY mirror as the bottom row | — |
| `/cycle` | Runs this week's cycle immediately. Server managers only | — |

`/join` and `/standings` are deliberately unmetered: the first is idempotent, so a
repeat writes nothing, and the second is a pure read that spends no API budget.
`league.DAILY_LIMITS` carries a number for both anyway — it is the policy table, and
the bot decides which commands to draw against it.

### `/buy` and `/sell` — the modal is the written plan

Discord allows **five** text inputs per modal, and the plan rule 1 requires is exactly five: shares, thesis, invalidation, horizon, exit plan. So the modal *is* the plan, and everything numeric that isn't the size — ticker, bucket, price, stop — lives on the slash command. `/sell` asks for size (blank means the whole position) and the one honest line about what happened versus the thesis.

**A refusal uses the CLI's exact words.** Both surfaces render through `journal.refusal_text`, so the same violation produces the same sentence:

```
REFUSED — rule 3: core names cap at 20% of the account; this buy would make it 30.0% ...
```

A parametrised test reproduces six violations through *both* the bot path and the CLI's own `journal.validate_buy`, and asserts the strings are byte-identical — not merely similar.

### `/review` and the lockout

`/review` shows each open position with the invalidation trigger *as the member wrote it*, flags breached stops, and records the review. Past 9 days, `/buy` refuses with rule 6 exactly as `thesis log buy` does.

One subtlety worth knowing: rule 6 is a property of a *book*, and the weekly agent cycle also records a review. Left alone, a running agent would keep the lockout permanently cleared and `/review` would be decorative. So the member's gate counts **human reviews only** — `league.last_human_review` ignores the cycle's own marker, and `league.human_state` feeds that into the unmodified `journal.validate_buy`. Same rule, correct input.

### `/research` — weekly cache, Sonnet, lint-gated

Keyed by **company and ISO week**: a hit is served from disk, a miss generates. Last week's brief is never reused. Generation runs on `claude-sonnet-5` and writes to `briefs/league/`, kept separate from your own `briefs/` so a cheaper league brief can never be mistaken later for your own research.

**The lint gate is a hard gate.** A brief whose citations don't survive `lint.lint_brief` after its one retry is **not served** — the reply says it failed its citation check and why, and the unsourced text never reaches the channel. Posting an unsourced brief to a room of people is worse than posting nothing.

### Rate limits

Per user, per command, per UTC day. Two jobs: keep one member from burning the API budget on `/research` misses, and keep the journal's discipline from being brute-forced — someone who needs sixteen buys a day is not writing sixteen theses.

**Refusals are free on `/buy` and `/sell`.** Only an accepted trade draws down the
allowance. A refusal there is pure validation against the seven rules — it calls no
API and writes no row — and for someone learning the discipline the refusal *is* the
lesson. Charging for it would ration the teaching and push a member to guess more
loosely rather than more carefully, which is backwards.

`/research` and `/review` count the **attempt**, because a `/research` miss generates
a brief and costs real money whether the member likes what it says or not. The two
styles share one cap per command; they differ only in which event is billable. Both
are implemented over the same counter — `league.rate_status` reads it without
charging, `league.consume_rate` charges once — and a test pins the pair to the same
cap boundary so the styles can't drift apart. Successful replies show the remaining
allowance.

The weekly post fires **Monday 22:00 UTC** — after the US close, so the week's sessions are settled and the screen is current. It carries each agent's reasoning verbatim, then the fills, the refusals with their rule numbers, and anything still waiting on the next open.

### It is a wrapper, and the tests hold it to that

`league.py` is the domain layer and `bot.py` is Discord plumbing. Neither implements a trading rule. A league order becomes a `journal.BuyRequest` and goes through `journal.validate_buy` — the same function `thesis log buy` calls — so all seven account rules bind with no league-specific variant.

Three tests enforce this rather than trusting it:

- **`test_the_bot_cannot_construct_a_trade_the_cli_would_refuse`** runs ten illegal orders (one per rule failure) through *both* the league path and the CLI's own journal path, and requires both to refuse each one **with the same rule number**. Its mirror asserts a legal order is accepted by both and produces identical shares and cost basis.
- **`test_the_bot_module_holds_no_trade_logic`** parses `bot.py`'s AST: no call to `log_buy` / `log_sell` / `add_deposit` / `validate_buy`, no reference to `BuyRequest`, and no handle on `journal.connect` or `arena.connect` — only `league.connect`.
- **`test_the_league_module_defines_no_rule_of_its_own`** greps `league.py` for restated thresholds (`0.20`, `0.10`, `BUCKET_CAPS`, `MIN_THESIS_CHARS`, …) so a cap cannot be quietly duplicated.

An order also cannot be placed on someone else's book: whatever book name the model puts in its JSON is overwritten with the caller's own before validation, and there's a test for it.

### Isolation, scoping, and cost

- **League books live in `.cache/league.db`** — a third database, separate from `journal.db` *and* `arena.db`. A test fills a league order and then asserts both other databases still have zero trades.
- **One league per Discord server.** Books are keyed by `(guild_id, member_id)`, so two servers run independent leagues; the same person in two servers gets two books. Book names are short slugs (`m1`, `m2`, …) because `journal.BOOK_RE` caps a name at 32 characters and a Discord snowflake pair is far longer.
- **Cycles run on `claude-sonnet-5`**, not the Opus the human's own research uses — a league fans out one packet per member, so the cheaper model is the right trade. Set by `config.LEAGUE_MODEL`.
- **`DISCORD_TOKEN`** is read from `.env` exactly once, at startup. A test asserts it is read in one place and never appears on a line that sends, logs or prints.

Fills work exactly as the Arena's do: an order fills at the open of the first session **after its own decision date**, settled at the start of the next cycle, with open and pending exposure counted together against the caps.

### The three-second contract

Discord throws away an interaction that has not been **acknowledged within three
seconds** and shows the member *"The application did not respond."* Nothing here is
reliably that fast — a screen touches ~490 tickers, a brief calls a model, and even
`/join` writes to SQLite, which waits on the default five-second busy timeout if a
cycle holds the write lock. So every handler acknowledges first and answers later.

Five commands go through `bot.run_command`, whose **first await** is
`interaction.response.defer` — before any model call, network request, database read,
rate-limit check or validation. It then runs the synchronous league seam in a worker
thread and replies with `followup.send`, split to Discord's 2,000-character limit.

### Off the loop is not enough — the GIL is

Live testing found `/research` and `/standings` both failing with **10062 Unknown
interaction** *at the `defer` call*, while another `/research` was running. Every
handler already used `asyncio.to_thread`, so nothing was blocking the loop directly.
The cause was the GIL: a regex over a multi-megabyte filing is a single C call that
never yields it, and the event loop is just another thread waiting its turn. CPython
starves a waiting I/O thread badly once several CPU-bound threads compete.

Measured on this machine with `/research`-shaped work — worst event-loop stall by
number of concurrent commands:

| concurrent | 1 | 2 | 4 | 8 | 12 |
|---|---|---|---|---|---|
| worst loop stall | 0.12s | 0.27s | **4.01s** | 8.41s | 13.02s |

Four is enough to blow Discord's three-second window for everyone else. Twelve
members at once, driven through the real `run_command`: **7 of 12 got 10062** with a
worst stall of 11.49s.

So `MAX_CONCURRENT_WORK` bounds how much blocking work runs at once. At a cap of 2
the same twelve members produce a worst stall of 0.83s and **zero** failed
deferrals — and total wall time is unchanged (14.1s → 13.4s), because GIL-bound
threads were never buying parallelism in the first place. The cap costs nothing and
buys liveness.

The queue sits strictly **after** the deferral. That ordering is the point: once
deferred, Discord allows fifteen minutes, so a queued member sees "thinking…" and
gets an answer, whereas queueing before the deferral would burn the three-second
window and kill the interaction — reintroducing the exact bug. A test fills every
slot and asserts the next command is still acknowledged immediately, and another
asserts the ordering against `run_command`'s AST.

`/buy` and `/sell` are the deliberate exception. Opening a modal **is** the
acknowledgement, and deferring first makes `send_modal` illegal — so their deferral
lives in the modal's `on_submit`, which is where the slow work is. Nothing is exempt;
the acknowledgement just moves.

The only thing allowed before an acknowledgement is a plain attribute read —
`interaction.guild_id`, a permission bit — never a call that can block.

`test_league.py` enforces this against the AST of every registered handler: it walks
each one in **evaluation order** (an argument evaluates before the call it sits in, so
`run_command(interaction, seam, fetch_marks(...))` really does fetch first) and fails
on any blocking call reached on a path that has not acknowledged. Three deliberately
broken handlers prove the check has teeth, and the command list is pinned, so an
eighth command fails the suite until it is covered.

### Nothing fails silently

An exception raised *before* the acknowledgement is indistinguishable from a hang
from the outside — which is how the first live `/cycle` failure presented. So
`LeagueTree.on_error` and each modal's `on_error` catch everything, log the traceback,
and tell the member what broke. The message names the exception type and is worded so
it can never be mistaken for a rule refusal: a refusal is the system working, and
always names its rule.

`report_failure` is the last thing in that chain, so an exception escaping it turns a
diagnosable failure back into a silent timeout. It cannot raise. `is_done()` picks
which reply route to try *first*, not which to use — it can disagree with Discord,
since a `defer` that died with 10062 leaves it `False`, and a race can leave it
`False` when Discord has already acknowledged (40060) and only a followup will work.
Both routes are tried, and Discord's numeric code is logged either way, because a
bare 404 says nothing while `10062` says the window closed. When both routes fail the
interaction is genuinely dead and there is nowhere left to reach the member — that is
physics, not a bug — so it logs an `ERROR` carrying the *original* failure, which is
the thing worth keeping.

### Registration is per guild

The first live test found `/research` missing from the slash-command picker — typing
it posted as plain text. It was on the command tree the whole time. The problem was
scope: `tree.sync()` with no guild registers **globally**, and a global registration
can take up to an hour to reach a member's client. `/join` and `/cycle`, registered by
an earlier run, had propagated; the four newer commands had not.

So commands sync **per guild**, which Discord applies immediately. Leagues are
per-server anyway, so guild scope is also the honest scope.

The sync happens in `on_ready`, not `setup_hook` — `setup_hook` runs inside `login`,
before the gateway connects, so `self.guilds` is empty there and a per-guild loop
would register nothing at all. A test asserts the sync is *not* called from
`setup_hook`, because that mistake logs one error and otherwise looks fine.
`on_ready` can replay on a reconnect, so the sync is guarded to run once, and
`on_guild_join` syncs a server added later rather than making it wait for a restart.

A live start prints exactly what Discord accepted:

```
2026-08-12 23:03:57 INFO     thesis.bot  connected as ThesisLeague#4242 (id 1399…)
2026-08-12 23:03:57 INFO     thesis.bot  in 1 guild(s): UIUC Investing (1122…)
2026-08-12 23:03:57 INFO     thesis.bot  commands Discord accepted for UIUC Investing (1122…) — 7: buy, cycle, join, research, review, sell, standings
```

A command defined here but not accepted logs `ERROR` and says it will not appear in
the picker; one accepted but unknown here logs `WARNING`; a failed sync logs the
traceback and says Discord still holds the previous set; no guilds at all is an
`ERROR`, since guild-scoped commands with no guilds can appear nowhere.

### Clearing the leftover global registrations

Switching to per-guild syncing does not retract what an earlier global sync
registered, so both copies exist and every command shows up **twice** in the picker.
Normal startup only reports the leftovers — a global write applies to every server
the application is in, and a routine restart should not make that decision for you.

Removing them is an explicit one-off:

```bash
uv run thesis bot --clear-global
```

It logs in over HTTP, lists what is registered globally, calls
`tree.clear_commands(guild=None)` then `await tree.sync()` — an empty global payload,
which is how Discord is told to drop them all — re-fetches to confirm they are gone,
and exits. It never starts the gateway and never serves an interaction:

```
INFO  thesis.bot  removing 7 global command registration(s): buy, cycle, join, research, review, sell, standings
INFO  thesis.bot  removed 7 global registration(s): buy, cycle, join, research, review, sell, standings. Per-guild registrations are untouched — restart with `thesis bot` and each command appears once.
```

Then start the bot normally. The per-guild registrations are untouched by the clear,
so nothing needs re-syncing — though `on_ready` re-syncs anyway.

Two details worth knowing. `login` calls `setup_hook`, so a maintenance client is
constructed with `serve=False` and schedules no weekly loop in a process that is
about to exit. And the removal is *verified* rather than assumed: if Discord still
reports global commands afterwards, that logs `ERROR` instead of claiming success.

The separation is asserted structurally, not just tested behaviourally:
`clear_commands` may appear in exactly one function in `bot.py`, and it is not any
startup path — `setup_hook`, `on_ready`, `on_guild_join`, `sync_commands`,
`sync_one_guild`, `report_global_leftovers` and `run` are each checked for it.

Two tests keep the registered set honest, and both read **PRODUCT.md's v1 scope**
rather than restating it: one fails if a command in that scope is not registered, the
other if something registered is not in the scope. A third checks every name,
description, parameter and choice against Discord's length limits, because one
oversized description makes Discord reject the whole sync payload — which presents as
every new command missing, the same symptom from a cause a registration test alone
would not find.

---

## DEPLOY — always-on hosting

The bot has to outlive a closed laptop. It runs as a container on a small VPS:
one process, one mounted volume, restarted automatically.

**Sizing.** 1 vCPU and 1 GB of RAM is enough; 2 GB is comfortable. It is not CPU
work that limits this but the GIL — see [the concurrency
cap](#off-the-loop-is-not-enough--the-gil-is) — so a bigger box does not raise
`MAX_CONCURRENT_WORK`.

### Provision (once, on the server)

```bash
curl -fsSL https://get.docker.com | sh
```

```bash
sudo usermod -aG docker $USER && newgrp docker
```

```bash
git clone <your-repo-url> thesis && cd thesis
```

Then write the secrets. They live only on the server, only in this file, and are
passed to the container as environment variables — never built into the image:

```bash
cp .env.example .env && nano .env
```

`DISCORD_TOKEN`, `ANTHROPIC_API_KEY` and `SEC_EDGAR_USER_AGENT` are all required.
Compose refuses to start without them by name rather than booting a bot that
cannot log in, or one that fails every `/research` an hour later.

### Ship

```bash
docker compose up -d --build
```

Confirm it actually registered its commands — this is the line that matters, and
the one whose absence caused the `/research` outage:

```bash
docker compose logs bot | grep "commands Discord accepted"
```

### Update

```bash
git pull && docker compose up -d --build
```

The books are on a named volume, so this replaces the code and keeps every
member's positions, history and cached briefs. Nothing needs re-syncing —
`on_ready` re-registers the commands on each start.

### Logs

```bash
docker compose logs -f --tail=100 bot
```

Rotation is configured in `docker-compose.yml` (10 MB × 3), because an always-on
process on a small disk will otherwise fill it.

### Other operations

```bash
docker compose restart bot
```

```bash
docker compose down          # stop; the volume and its data survive
```

The one-off global-command cleanup, in the container:

```bash
docker compose run --rm bot thesis bot --clear-global
```

Back up the books — do this before anything irreversible:

```bash
docker run --rm -v thesis_thesis-data:/data -v "$PWD:/backup" busybox tar czf /backup/thesis-books-$(date +%F).tar.gz -C /data .
```

`docker compose down -v` would delete that volume and every member's book with
it. There is no undo, so take the backup first.

### A cycle missed while the process was down

**It is reported and skipped, never replayed on start.**

The container runs `restart: unless-stopped`, so a crash restarts it. A
replay-on-start would then run one cycle per restart — each spending Sonnet budget
and placing orders on every member's book, unattended. Skipping costs one quiet
week that a server manager fixes with `/cycle`; replaying on a restart loop costs
money and moves every book, repeatedly, with nobody watching. The failure modes
are not comparable, so the safe default wins.

An idempotent catch-up ("run only if none has run this week") would bound the
damage, and it was the tempting option. It was rejected because it makes the
safety of a money-spending, order-placing action depend on one query being
right — whereas skipping is safe by construction, and `/cycle` already exists
precisely so a human can start one deliberately.

On startup the console says which it is:

```
WARNING  thesis.bot  MISSED CYCLE — one was due 2026-08-10 22:00 UTC, 4 member(s) enrolled, the last ran 2026-08-03. It will NOT be replayed automatically: this process restarts on failure, and replaying on start would run a cycle per restart, spending API budget and placing orders on every book each time. A server manager can run /cycle to catch up now; otherwise the next scheduled cycle is 2026-08-17 22:00 UTC.
```

Nothing is said when a cycle is not overdue, or when no member has joined yet —
a server with no books has missed nothing.

### What is deliberately not in the image

Secrets and state. `.dockerignore` excludes `.env`, `journal.db`, `*.db`,
`.cache/`, `briefs/` and `reports/`; the Dockerfile copies named paths rather than
`COPY . .`, so a new file cannot slip in by default; and no `ENV` or `ARG` carries
a credential. An image layer travels wherever the image does and a rebuild cannot
unpublish it, so this is asserted by tests in `tests/test_deploy.py` rather than
left to review.

State paths are environment-driven — `THESIS_CACHE_DIR`, `THESIS_BRIEFS_DIR`,
`THESIS_REPORTS_DIR` — and compose points all three under `/data`. A test asserts
every one of them resolves onto the volume, because a path outside it is a path
that disappears on the next deploy.

---

## The landing page — `site/`

One screen, one file: `site/index.html` carries its own CSS inline, loads nothing from
another origin, and ships no JavaScript beyond a single `onerror` on the screenshot.
A `Content-Security-Policy` meta tag (`default-src 'none'`) enforces that at runtime
rather than leaving it as a claim, and it travels with the file instead of depending on
host headers.

**The copy is not written here.** The positioning line and the four contract lines are
quoted verbatim from `PRODUCT.md`, and `tests/test_site.py` pins them in both
directions — reword either file without the other and the suite fails. The same tests
run the page's own prose through `lint._recommendation_hits`, so the page is held to the
bar it advertises. The one exemption is the contract line *"Never recommends what to
buy"*, which trips a lexicon that cannot tell a promise from an instruction; a companion
test proves the exemption is that narrow and not a hole.

Preview it locally:

```bash
uv run python -m http.server 4173 --directory site
```

### Deploying it

Vercel, zero build step. `site/` has no `package.json`, so there is nothing to detect
and nothing to build — a test asserts no manifest ever appears there.

One setting is not in any file: **Root Directory must be set to `site`** in the Vercel
project settings. `vercel.json` cannot set its own root, and it is only read once
`site/` *is* the root — deploy from the repo root instead and `site/vercel.json` is
silently ignored. The config itself only adds response headers and a one-hour cache
policy for the weekly screenshot, which is replaced under the same filename.

Two blanks are left deliberately unfilled rather than guessed:

- `DISCORD_CLIENT_ID` in the invite `href` — paste the application's client ID.
- the repo link, which currently points at `github.com`.

Both are visibly unfinished instead of being plausible links that go nowhere. The
screenshot slot expects `site/standings.png` at roughly 4:3; until it exists the slot
renders a labelled placeholder, and on a phone an empty slot yields the top of the
screen to the product name rather than to a "drop a file here" box.

---

## Known limits

Things this tool cannot do, or does in a way you should know about before trusting a number. None of these are bugs; they are consequences of choices made where the alternative was worse.

### The screen is structurally blind to banks

The quality gate requires a positive `freeCashflow`, and the provider does not report that field for most financials. The effect is not subtle: **BNY ranks 39th of 486 on momentum and is then dropped** as `no fundamentals available`. Insurers and asset managers hit the same wall.

This is the gate working exactly as specified — a name whose quality cannot be verified fails rather than being assumed healthy — but the honest description is that **whole sectors never appear in your candidate list**, and not because they scored badly. If you want financials, research them directly; the screen will not surface them. `--explain TICKER` states the reason outright, which is how this was found.

Free cash flow is also the wrong quality test for a bank in the first place, so loosening the gate would trade a visible blind spot for an invisible wrong answer.

### The universe is a snapshot, and snapshots rot

There is no free, stable feed of index membership, so the universe is a dated list checked into the repo. Two consequences:

- **Names added to the index after `AS_OF` are never screened.** They are not excluded — they are simply not there, and nothing in the output will mention them.
- **Renamed tickers stop returning data** and look identical to delistings until someone checks.

The policy is that this is caught on a **calendar**, not by failures: every screen is stamped with the snapshot date and its age, and past **90 days** it warns and points at `thesis universe --check`. That check is the audit; the [refresh procedure](#keeping-the-universe-current) is the fix. Retrying is not — when this last happened, individual retries with backoff recovered zero of twelve failures, because all twelve were permanent.

A screen run against a stale snapshot is still valid for the names it *did* screen. It is simply narrower than you think it is.

### Provider metrics do not always mean what they are named

`revenueGrowth` from yfinance is **quarterly** — the most recent quarter against the same quarter a year earlier — not the annual rate its name suggests. Verified against the statements, it reproduces quarterly growth to two decimals for AAPL (16.60%), KO (12.07%), MU (345.72%) and CAT (22.22%), while annual growth for those same names is 6.4%, 1.9%, 48.9% and 4.3%.

Labelling it `revenue growth (yoy)` in the data packet was enough to make a brief report Costco growing **21.5%** when its annual figures implied **8.2%**. The packet now names it as a quarterly rate, carries an annual figure computed from the income statement beside it, and states that the two are not comparable.

Worse, it is not always reconcilable with the provider's *own* statements: for COST the field reads 21.5% where its quarterly revenue implies 11.6%. It is therefore carried as a **provider-reported metric**, labelled as such, rather than presented as ground truth.

The general lesson, which applies to every `.info` field: a plausible number with an ambiguous name is more dangerous than a missing one. Where a figure matters, the packet computes it from the statements.

### Paper and real: what is actually guaranteed

`--paper` is a full second book, not a display mode. What is guaranteed:

- **All seven rules bind identically.** Caps are not relaxed, T+1 settlement still applies, the stop requirement still applies, and the review clock still blocks buying.
- **The same code computes both.** There is no paper-specific arithmetic anywhere; a test asserts that identical trade histories in the two books produce byte-identical report bodies.
- **The books never mix.** Separate cash, separate positions, separate review clocks, separate equity snapshots. A paper review does not unlock a real buy, and paper cash cannot fund a real position — both are tested.
- **Simulated output is labelled at every exit.** The markdown report is headed `PAPER — simulated money`, and every page of the PDF carries a banner in the header *and* the footer. The word "PAPER" appears nowhere in a real report.

What is **not** guaranteed, and cannot be: paper fills are whatever price you type. There is no spread, no slippage, no partial fill, and no emotional cost to being wrong. Paper results are evidence that you followed the process, not evidence that you can trade.

The same caveat applies to the arena, plus one more: agent fills use the next session's open with no spread or slippage, and an agent that "beats SPY" over a handful of weekly cycles has demonstrated nothing except that it followed the rules. The experiment is worth running for what the *decision log* shows — how each mandate reasons, and which rules it keeps tripping over — not for the P&L column.

---

## Money expectations

Four weeks of P&L on a sub-$1k account is noise in either direction. The compounding assets are the tool, the skill, and the track record. Twenty-plus logged theses with honest outcomes against SPY is what makes a record mean anything; a handful of closed trades cannot separate skill from luck, and `track` says so on the report itself.
