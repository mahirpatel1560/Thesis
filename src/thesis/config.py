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


def _from_env(name: str, fallback: Path) -> Path:
    override = os.environ.get(name, "").strip()
    return Path(override) if override else fallback


def cache_dir() -> Path:
    """Where derived state lives: the databases and the cached briefs.

    `THESIS_CACHE_DIR` moves all of it at once, which is what a container needs —
    one mounted volume and one variable, rather than remembering to redirect each
    database individually. The per-file overrides below still win where they are
    set, so a test can point one database at a tmp file without moving the rest.
    """
    return _from_env("THESIS_CACHE_DIR", CACHE_DIR)


def briefs_dir() -> Path:
    """Generated briefs. `THESIS_BRIEFS_DIR` puts them on the volume too.

    Worth persisting rather than regenerating: a brief costs a model call, and the
    league serves one per company per week to everybody who asks.
    """
    return _from_env("THESIS_BRIEFS_DIR", BRIEFS_DIR)


def reports_dir() -> Path:
    """Exported track records. `THESIS_REPORTS_DIR` overrides."""
    return _from_env("THESIS_REPORTS_DIR", REPORTS_DIR)


def db_path() -> Path:
    """The journal database. THESIS_DB overrides it (tests point at a tmp file)."""
    return _from_env("THESIS_DB", DB_PATH)


def price_cache_path() -> Path:
    """The price cache. THESIS_PRICE_CACHE overrides it (tests point at a tmp file)."""
    return _from_env("THESIS_PRICE_CACHE", cache_dir() / "prices.db")


#: League cycles run many member books at once, so they run on Sonnet rather than
#: the Opus the human's own research uses. Same request shape, ~40% of the cost.
LEAGUE_MODEL = "claude-sonnet-5"


def league_db_path() -> Path:
    """The Discord league's own database — separate from the arena and the journal.

    Same isolation rule as `arena_db_path`: a different file, so a bug in the bot
    layer cannot reach the human's books or the arena's. THESIS_LEAGUE_DB overrides.
    """
    return _from_env("THESIS_LEAGUE_DB", cache_dir() / "league.db")


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
    return _from_env("THESIS_ARENA_DB", cache_dir() / "arena.db")


def sec_user_agent() -> str:
    """SEC requires a descriptive User-Agent with contact info on all EDGAR requests."""
    ua = os.environ.get("SEC_EDGAR_USER_AGENT", "").strip()
    if not ua:
        raise RuntimeError(
            "SEC_EDGAR_USER_AGENT is not set. Add it to .env, e.g.\n"
            "  SEC_EDGAR_USER_AGENT=thesis-cli your.email@example.com"
        )
    return ua
