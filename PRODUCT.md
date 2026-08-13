# Thesis — PRODUCT.md

**Last updated:** 2026-08-12
**Repo:** `C:\Thesis`
**Read this before any product-level build task.** Architecture and code conventions live in `CLAUDE.md`; this file governs product scope, priorities, and what must never ship.

---

## One-line positioning

*The paper-trading league where the AI opponents show their work — and every trade needs a thesis.*

## The anti-slop contract (four lines, non-negotiable)

1. Never recommends what to buy.
2. Never states a fact without a source.
3. Never hides the benchmark.
4. Never touches real money.

These are the product. Any feature that violates one does not ship, regardless of how much users ask for it.

---

## Target user

**Wedge:** first-time investors — people who want to learn how markets and companies actually work and have never placed a trade.

Not: active traders (served by TradeZella/TraderSync), quants (QuantConnect), or people hunting signals (we are structurally the wrong product for them, on purpose).

Distribution reflects the wedge: UIUC campus channels first (NOVA's existing Discord audience), then r/UIUC, then beginner-investing communities where anti-hype positioning is an asset.

---

## Current status (2026-08-12)

| Component | State |
|---|---|
| CLI: data pipeline + SEC EDGAR parser | Shipped |
| CLI: rules-enforced journal, 7 rules, `--paper` mode, SPY mirror | Shipped |
| CLI: screener + brief lint gate | Shipped |
| CLI: track report + PDF export | Shipped |
| Arena: 3 AI personas (Value, Momentum, Monk) vs SPY | Shipped |
| Arena: order-fill engine (time-aware fetcher, pending → filled at historical open) | Shipped |
| Discord bot: `/join`, `/standings`, `/cycle`, Monday auto-post | Shipped |
| Discord bot: `/buy`, `/sell`, `/review`, `/research` | **Next** |
| Landing page (`/site`) | **Next** |
| Test suite | 468 passing, 0 failing |

**Nothing ships on red.** Full suite green is the definition of done for every task.

---

## v1 form factor

**Discord bot + one-page landing site.** Web app is deferred to spring 2027 and is not in scope.

Rationale: the target user already lives in Discord, the league is inherently social, and a bot removes the auth/hosting/UI surface that would eat the entire hour budget.

---

## The hook: Arena-as-league

Arena is not a side experiment — it is the retention loop.

- Each member gets a simulated **$100,000** book run by an AI manager under **the same seven journal rules** the CLI enforces.
- Three AI mandates: **Value, Momentum, Monk.** Members pick one at `/join`.
- A weekly cycle runs Mondays (22:00 UTC), auto-posting standings plus **each agent's reasoning** to the channel.
- Everything is benchmarked against a **SPY mirror on the same dollars and dates**. The benchmark is always shown, including when the league is losing to it. Especially then.

**Locked design decisions:**

| Decision | Ruling |
|---|---|
| What `/join` creates | An agent-run book per member |
| Standings scope | One combined table: members + AI agents + SPY |
| League scoping | One league per Discord server (books keyed `guild_id, member_id`) |
| Mandates | **Limited to the three existing personas.** Custom member-written mandates are NOT in v1 — they add a prompt-injection surface for marginal gain at n=10 |
| Storage | Third DB, `.cache/league.db`, isolated from `journal.db` and `arena.db` |
| Model | `claude-sonnet-5` via `config.LEAGUE_MODEL` (Opus reserved for one-off deep briefs) |

**The wrapper guarantee:** the bot layer holds no trade logic of its own. All trades route through the existing journal modules, enforced by AST-level tests asserting `bot.py` contains no trade-construction calls and that the bot cannot construct a trade the CLI would refuse. This is tested, not trusted, and must stay tested as commands are added.

---

## v1 scope

**In:**
- `/join [mandate]`, `/standings`, `/cycle` (server managers only)
- `/buy` and `/sell` — modal collecting shares, thesis, invalidation, horizon, exit; same rules and same refusal wording as the CLI
- `/review` with the 9-day lockout
- `/research TICKER` — per-company weekly cached brief, generated on miss, Sonnet, lint-gated
- Per-user daily rate limits
- Weekly auto-post of the cycle with each agent's reasoning
- One-screen static landing page

**Explicitly deferred:** web app, mobile app, portfolio sync, real-money anything, brokerage APIs, custom mandates, leaderboards across servers, paid signals (never).

---

## Compliance rails (non-negotiable, and also the moat)

1. **Paper money only.** No brokerage APIs, no real-money features, no exceptions in v1.
2. **No recommendation language anywhere.** The lint gate applies to every user-facing surface, not just briefs.
3. **Standing disclaimer** on briefs and standings: educational tool, not investment advice.
4. **No paid "signals," ever.** If it looks like telling people what to buy, it doesn't ship.
5. **Lawyer consult before charging money** (one hour, before the paid tier opens).

Legal framing: impersonal, general-circulation, educational content is protected publishing. Personalized buy/sell recommendations, managing money, or performance promises would make this a regulated investment adviser. Stay on the correct side by design, not by disclaimer.

---

## Economics (v1 reality)

- **Briefs:** cached per company per week — one generation serves every user who asks. Sonnet, not Opus. 25 users reading 5 cached briefs/week ≈ a few dollars/month.
- **Arena/league cycle:** pennies per week on Sonnet.
- **Hosting:** bot process + SQLite on a $5–10/mo VPS or free tier. Landing page free (Vercel/GitHub Pages).
- **Data:** `yfinance` is unofficial — acceptable for a free beta, **not for a paid product**. The first paid dollars are earmarked for licensed data (Polygon starter or similar; delayed/EOD is fine, this product is not real-time).
- **Total burn before revenue:** ~$10–20/month. The constraint is founder hours, not money.

---

## Metrics that matter (in order)

1. **Weekly active league players** ← the Dec 15 number
2. Week-2 return rate (did they come back after the first review?)
3. Theses logged per player (the discipline metric — the product working)
4. Briefs read per week (utility pull-through)
5. Revenue (gated, see below)

---

## Roadmap

| Window | Milestone |
|---|---|
| Aug 12–24 | Ship `/buy` `/sell` `/review` `/research`; ship landing page; deploy |
| Aug 25–27 | Pre-launch: seed 2 cycles, pin welcome, self-test as a stranger |
| Aug 28–Sep 7 | Launch sequence: NOVA channels → personal DMs → X build-in-public → r/UIUC → beginner-investing threads |
| September | Weekly rhythm only. Ship only what real users request |
| Sep 30 | Read: 10+ weekly actives = on track; under 5 = one honest pivot conversation |
| Revenue gate | **Whenever 10+ weekly actives is hit:** lawyer consult → licensed data → launch $5/mo Supporter tier (Opus deep briefs, custom Arena persona, priority tickers). **The league itself stays free forever.** |
| **Dec 15 checkpoint** | 10+ weekly actives → web app in spring, renegotiate hours. Under 10 → maintenance mode, activate Plan B |

**Plan B (parked, zero hours until Dec 15):** an AI research-brief-to-publish tool for finance newsletter/FinTwit creators at ~$29/mo, reusing this repo's brief engine. Pre-researched, not started.

---

## Remaining Dispatch prompts

**Trade + brief commands:**

> Work in C:\Thesis. Read PRODUCT.md. Add `/buy` and `/sell` (modal collecting shares, thesis, invalidation, horizon, exit — same rules, same refusal wording as the CLI), `/review` with the 9-day lockout, and `/research TICKER` serving the per-company weekly cached brief (generate on miss, Sonnet, lint-gated). Per-user daily rate limits. Keep mandates limited to the three existing personas — do NOT add custom member-written mandates. Extend the existing wrapper-guarantee tests to cover the new commands: the bot must not construct any trade the CLI would refuse. All 468 tests stay green plus new ones. Report before/after counts and a demo transcript of a refused trade and an accepted one.

**Landing page:**

> Work in C:\Thesis. Read PRODUCT.md. Build a one-screen static landing page in /site: name, one-line positioning, the four-line anti-slop contract, a Discord invite button, and a screenshot slot for the weekly standings post. No framework bloat — single HTML/CSS file, dark, fast.

---

## Founder's weekly rhythm (the 5-hour container)

- **Sunday (45 min):** run the cycle + `review` + read the AI reasoning → that becomes the week's build-in-public post.
- **Daily (10 min):** one brief read; answer anything in the community channel within 24 hours.
- **One build block (≤3 hrs):** dispatch and review the next milestone, or community time once live. Fix only what real users hit — no features invented in a vacuum.
- **Everything else:** NOVA and Grainger coursework. Protected.

**Priority order when hours are short:** NOVA > coursework > Thesis. Thesis is containered at ~5 hrs/week and does not get to expand without a renegotiation triggered by the Dec 15 checkpoint.

---

## Operating rules for agents working in this repo

1. One dispatch at a time. Never run two agents against this repo concurrently.
2. Write failing regression tests **first** for any bug fix, then fix the code.
3. Never weaken, delete, or skip an existing assertion. If a test is provably wrong, say so with justification before touching it.
4. Full suite green before reporting done. Report before/after test counts and exactly what changed.
5. Never start long-running processes (e.g. `thesis bot`) during a build task — it blocks and may connect with a live token.
6. Secrets live in `.env` (`DISCORD_TOKEN`, `ANTHROPIC_API_KEY`) and must never be logged, printed, or committed.
