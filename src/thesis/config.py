"""Environment/config handling. Secrets live in .env (gitignored)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (walks up from cwd), once at import time.
load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRIEFS_DIR = PROJECT_ROOT / "briefs"
REPORTS_DIR = PROJECT_ROOT / "reports"
DB_PATH = PROJECT_ROOT / "journal.db"
CACHE_DIR = PROJECT_ROOT / ".cache"

DEFAULT_MODEL = "claude-opus-4-8"


def model() -> str:
    return os.environ.get("THESIS_MODEL") or DEFAULT_MODEL


def db_path() -> Path:
    """The journal database. THESIS_DB overrides it (tests point at a tmp file)."""
    override = os.environ.get("THESIS_DB", "").strip()
    return Path(override) if override else DB_PATH


def price_cache_path() -> Path:
    """The price cache. THESIS_PRICE_CACHE overrides it (tests point at a tmp file)."""
    override = os.environ.get("THESIS_PRICE_CACHE", "").strip()
    return Path(override) if override else CACHE_DIR / "prices.db"


#: League cycles run many member books at once, so they run on Sonnet rather than
#: the Opus the human's own research uses. Same request shape, ~40% of the cost.
LEAGUE_MODEL = "claude-sonnet-5"


def league_db_path() -> Path:
    """The Discord league's own database — separate from the arena and the journal.

    Same isolation rule as `arena_db_path`: a different file, so a bug in the bot
    layer cannot reach the human's books or the arena's. THESIS_LEAGUE_DB overrides.
    """
    override = os.environ.get("THESIS_LEAGUE_DB", "").strip()
    return Path(override) if override else CACHE_DIR / "league.db"


def discord_token() -> str:
    """The bot token, from DISCORD_TOKEN in .env. Never logged, never echoed."""
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN is not set. Add it to .env, e.g.\n"
            "  DISCORD_TOKEN=your-bot-token\n"
            "The bot cannot start without it."
        )
    return token


def arena_db_path() -> Path:
    """The arena's own database — a separate file from the human journal.

    Physical isolation, not just a different `book` column: an arena bug cannot
    reach `journal.db` because it never opens it. THESIS_ARENA_DB overrides.
    """
    override = os.environ.get("THESIS_ARENA_DB", "").strip()
    return Path(override) if override else CACHE_DIR / "arena.db"


def sec_user_agent() -> str:
    """SEC requires a descriptive User-Agent with contact info on all EDGAR requests."""
    ua = os.environ.get("SEC_EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "SEC_EDGAR_USER_AGENT is not set. Add it to .env, e.g.\n"
            "  SEC_EDGAR_USER_AGENT=thesis-cli your.email@example.com"
        )
    return ua
