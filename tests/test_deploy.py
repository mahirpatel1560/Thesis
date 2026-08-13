"""The deployment surface: configurable state paths, no baked secrets, missed cycles.

The bot has to survive a laptop closing, so it runs in a container on a VPS. Three
things about that are worth asserting rather than eyeballing:

* every path the bot writes to can be moved by an environment variable, because a
  container keeps its state on a mounted volume;
* no credential can reach the image, because an image layer travels wherever the
  image does and a rebuild cannot unpublish it;
* a cycle missed while the process was down is reported and skipped, which is the
  documented behaviour and the safe one.
"""

from __future__ import annotations

import datetime as dt
from datetime import date
from pathlib import Path

import pytest

from thesis import config

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO / "Dockerfile").read_text(encoding="utf-8")
COMPOSE = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
DOCKERIGNORE = (REPO / ".dockerignore").read_text(encoding="utf-8")

#: Every secret the bot reads. None of these may have a value inside the image.
SECRETS = ("DISCORD_TOKEN", "ANTHROPIC_API_KEY")


def ignored() -> set[str]:
    return {
        line.strip()
        for line in DOCKERIGNORE.splitlines()
        if line.strip() and not line.startswith("#")
    }


# ------------------------------------------------- state paths follow the volume

def test_the_cache_directory_is_configurable(monkeypatch, tmp_path) -> None:
    """One variable has to move all of it — a volume is one mount, not four.

    The per-file overrides are cleared first because conftest sets
    `THESIS_PRICE_CACHE` for every test, and it legitimately outranks the
    directory — see the next test.
    """
    for name in ("THESIS_PRICE_CACHE", "THESIS_LEAGUE_DB", "THESIS_ARENA_DB"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("THESIS_CACHE_DIR", str(tmp_path / "state"))

    for path in (
        config.league_db_path(), config.arena_db_path(), config.price_cache_path()
    ):
        assert path.parent == tmp_path / "state", path


def test_each_database_can_still_be_moved_on_its_own(monkeypatch, tmp_path) -> None:
    """The per-file overrides outrank the directory, which is what tests rely on."""
    monkeypatch.setenv("THESIS_CACHE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("THESIS_LEAGUE_DB", str(tmp_path / "elsewhere" / "league.db"))

    assert config.league_db_path() == tmp_path / "elsewhere" / "league.db"
    assert config.arena_db_path().parent == tmp_path / "state", "the rest stay put"


def test_generated_briefs_can_live_on_the_volume(monkeypatch, tmp_path) -> None:
    """A brief costs a model call, so it must outlive a rebuild."""
    monkeypatch.setenv("THESIS_BRIEFS_DIR", str(tmp_path / "briefs"))
    assert config.briefs_dir() == tmp_path / "briefs"

    from thesis import league

    assert league.briefs_dir() == tmp_path / "briefs" / "league"


def test_reports_can_live_on_the_volume(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THESIS_REPORTS_DIR", str(tmp_path / "reports"))
    assert config.reports_dir() == tmp_path / "reports"


def test_the_defaults_are_unchanged_when_nothing_is_set(monkeypatch) -> None:
    """A laptop run must behave exactly as it did before any of this existed."""
    for name in (
        "THESIS_CACHE_DIR", "THESIS_BRIEFS_DIR", "THESIS_REPORTS_DIR",
        "THESIS_LEAGUE_DB", "THESIS_ARENA_DB", "THESIS_PRICE_CACHE",
    ):
        monkeypatch.delenv(name, raising=False)

    assert config.cache_dir() == config.CACHE_DIR
    assert config.briefs_dir() == config.BRIEFS_DIR
    assert config.reports_dir() == config.REPORTS_DIR
    assert config.league_db_path() == config.CACHE_DIR / "league.db"
    assert config.arena_db_path() == config.CACHE_DIR / "arena.db"


def test_every_state_path_the_container_uses_is_under_the_volume() -> None:
    """A path outside /data is a path that vanishes on the next deploy."""
    mount = "/data"
    assert f"- thesis-data:{mount}" in COMPOSE

    for name in ("THESIS_CACHE_DIR", "THESIS_BRIEFS_DIR", "THESIS_REPORTS_DIR"):
        line = next(l for l in COMPOSE.splitlines() if l.strip().startswith(f"{name}:"))
        value = line.split(":", 1)[1].strip()
        assert value.startswith(f"{mount}/"), f"{name}={value} is not on the volume"
        assert f"{name}=" in DOCKERFILE, f"{name} should also default inside the image"


# --------------------------------------------------- no credential in the image

def test_dot_env_is_excluded_from_the_build_context() -> None:
    """The one that matters: a COPY picking up .env bakes the token into a layer."""
    assert ".env" in ignored()


def test_the_dockerfile_never_copies_the_whole_directory() -> None:
    """`COPY . .` would defeat .dockerignore's purpose the moment a rule slips."""
    copies = [
        line.strip() for line in DOCKERFILE.splitlines()
        if line.strip().upper().startswith("COPY")
    ]
    assert copies, "the image has to copy something"
    for line in copies:
        assert not line.split()[1:] == [".", "./"], line
        assert " . " not in line, f"a whole-context copy would include .env: {line}"


@pytest.mark.parametrize("secret", SECRETS)
def test_no_secret_is_given_a_value_in_the_image(secret: str) -> None:
    """ARG or ENV with a value ends up in the image's history, readable forever."""
    for line in DOCKERFILE.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.upper().startswith(("ENV", "ARG")):
            assert secret not in stripped, f"the image would carry {secret}: {stripped}"


@pytest.mark.parametrize("secret", SECRETS)
def test_every_secret_comes_from_the_environment_at_run_time(secret: str) -> None:
    """And is required: a bot with no token should fail loudly, not run uselessly."""
    line = next(l for l in COMPOSE.splitlines() if l.strip().startswith(f"{secret}:"))
    assert f"${{{secret}:?" in line, (
        f"{secret} must be substituted from the environment and required: {line}"
    )


def test_the_compose_file_hardcodes_no_secret_values() -> None:
    """Every secret is a substitution, never a literal."""
    for secret in SECRETS:
        for line in COMPOSE.splitlines():
            if line.strip().startswith(f"{secret}:"):
                value = line.split(":", 1)[1].strip()
                assert value.startswith("${"), f"{secret} looks hardcoded: {value}"


def test_the_example_env_file_carries_names_but_no_values() -> None:
    """It is committed, so a filled-in value there would be a leak."""
    example = (REPO / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() in SECRETS:
            assert value.strip() == "", f"{name} has a value in .env.example"


def test_local_databases_are_kept_out_of_the_image() -> None:
    """Shipping a laptop's league.db would overwrite the server's books."""
    excluded = ignored()
    for pattern in (".cache/", "journal.db", "*.db"):
        assert pattern in excluded, pattern


# ------------------------------------------------------------ the container shape

def test_the_bot_does_not_run_as_root() -> None:
    users = [
        line.strip().split()[1]
        for line in DOCKERFILE.splitlines()
        if line.strip().upper().startswith("USER")
    ]
    assert users, "the Dockerfile never drops privileges"
    assert users[-1] != "root", "the bot would run as root"


def test_the_unprivileged_user_owns_the_volume_before_it_is_declared() -> None:
    """Docker seeds a named volume from the image, ownership included.

    Get the order wrong and the volume arrives root-owned, so the bot cannot write
    to its own database — which only shows up on a fresh server.
    """
    chown = DOCKERFILE.index("chown")
    volume = DOCKERFILE.index("VOLUME")
    user = DOCKERFILE.index("\nUSER ")
    assert chown < volume < user, "chown must precede VOLUME, which must precede USER"


def test_the_container_restarts_by_itself() -> None:
    """The whole point: it has to survive a crash and a VPS reboot unattended."""
    assert "restart: unless-stopped" in COMPOSE


def test_logs_reach_docker_unbuffered() -> None:
    """Buffered stdout means `docker logs` shows nothing when it matters most."""
    assert "PYTHONUNBUFFERED=1" in DOCKERFILE


def test_the_dependency_install_is_locked() -> None:
    """An unlocked install makes a redeploy a different program."""
    assert "uv.lock" in DOCKERFILE
    assert "--frozen" in DOCKERFILE
    assert (REPO / "uv.lock").exists()


def test_the_image_pins_the_tools_it_builds_with() -> None:
    """`latest` would make two builds of one commit produce different images."""
    assert "ghcr.io/astral-sh/uv:latest" not in DOCKERFILE
    assert "FROM python:3.12" in DOCKERFILE
    uv_line = next(l for l in DOCKERFILE.splitlines() if "astral-sh/uv" in l)
    assert ":0." in uv_line or ":1." in uv_line, f"uv is unpinned: {uv_line}"


# --------------------------------------------- a cycle missed while it was down

MONDAY_2200 = dt.datetime(2026, 8, 10, 22, 0, tzinfo=dt.timezone.utc)


def test_the_last_scheduled_cycle_is_the_monday_just_gone() -> None:
    from thesis import bot

    # Wednesday: the Monday two days back.
    assert bot.last_scheduled_cycle(
        dt.datetime(2026, 8, 12, 9, 0, tzinfo=dt.timezone.utc)
    ) == MONDAY_2200
    # Monday 22:00 exactly counts as due.
    assert bot.last_scheduled_cycle(MONDAY_2200) == MONDAY_2200
    # Monday 21:59, an hour before: the *previous* Monday.
    assert bot.last_scheduled_cycle(
        dt.datetime(2026, 8, 10, 21, 59, tzinfo=dt.timezone.utc)
    ) == MONDAY_2200 - dt.timedelta(days=7)


def test_nothing_is_said_when_the_cycle_already_ran() -> None:
    from thesis import bot

    assert bot.missed_cycle_notice(
        MONDAY_2200, date(2026, 8, 10), members=3, next_at=MONDAY_2200
    ) is None


def test_nothing_is_said_when_there_are_no_members() -> None:
    """A fresh server has no books to cycle, so it has missed nothing."""
    from thesis import bot

    assert bot.missed_cycle_notice(
        MONDAY_2200, None, members=0, next_at=MONDAY_2200
    ) is None


@pytest.mark.parametrize(
    "last", [None, date(2026, 8, 3)], ids=["never ran", "ran last week"]
)
def test_a_missed_cycle_is_reported_and_explicitly_not_replayed(last) -> None:
    """The documented behaviour, asserted: reported, skipped, with the reason."""
    from thesis import bot

    notice = bot.missed_cycle_notice(
        MONDAY_2200, last, members=4,
        next_at=MONDAY_2200 + dt.timedelta(days=7),
    )
    assert notice is not None
    assert "MISSED CYCLE" in notice
    assert "2026-08-10 22:00" in notice, "name the slot that was missed"
    assert "NOT be replayed" in notice
    assert "per restart" in notice, "say why, or the next reader will 'fix' it"
    assert "/cycle" in notice, "give the operator the way to catch up"
    assert "2026-08-17 22:00" in notice, "and when the next one is due"


def test_the_startup_check_never_runs_a_cycle() -> None:
    """Asserted against the source: replaying here is the failure mode, and it
    would show up as a surprise API bill rather than as a broken test."""
    import ast

    from thesis import bot

    tree = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
    checked = {"report_missed_cycle", "cycle_state", "missed_cycle_notice"}
    banned = {"run_cycle_text", "post_weekly_cycle", "run_cycle", "apply_decision"}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name not in checked:
            continue
        called = {
            call.func.attr if isinstance(call.func, ast.Attribute) else call.func.id
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
            and isinstance(call.func, (ast.Attribute, ast.Name))
        }
        assert not (called & banned), (
            f"{node.name} would run a cycle on startup: {called & banned}"
        )


def test_the_weekly_loop_reports_before_it_starts_waiting() -> None:
    """Otherwise the operator hears about a missed cycle a week late."""
    import ast

    from thesis import bot

    tree = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
    loop = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_weekly_loop"
    )
    report = [
        call.lineno for call in ast.walk(loop)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        and call.func.attr == "report_missed_cycle"
    ]
    sleeps = [
        call.lineno for call in ast.walk(loop)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        and call.func.attr == "sleep"
    ]
    assert report and sleeps and min(report) < min(sleeps)
