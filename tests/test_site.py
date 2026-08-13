"""The landing page repeats claims that live in PRODUCT.md, so it can drift.

A marketing page is the one surface nobody runs, which makes it the one surface
that quietly stops being true. These tests pin its copy to PRODUCT.md in both
directions: reword the positioning line or the contract in either file without
the other and the suite says so.

They also hold the page to two promises the page itself makes — that it loads
nothing from an external origin, and that it recommends nothing — plus the
deploy shape the site is supposed to have (static, no build step).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import pytest

from thesis import lint

REPO = Path(__file__).resolve().parents[1]
SITE = REPO / "site"
PAGE = SITE / "index.html"
PRODUCT = REPO / "PRODUCT.md"

#: HTML void elements — no end tag, so they must not be pushed onto the stack.
VOID = frozenset(
    "area base br col embed hr img input link meta param source track wbr".split()
)


def squash(text: str) -> str:
    """Collapse HTML's insignificant whitespace so copy compares as written."""
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class Node:
    tag: str
    classes: frozenset[str] = frozenset()
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""
    #: Every class on every ancestor, so "the list items inside .contract" is
    #: expressible without building a real DOM.
    within: frozenset[str] = frozenset()


class _Elements(HTMLParser):
    """Just enough parsing to ask what the visible text of an element is."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.nodes: list[Node] = []
        self._stack: list[tuple[str, dict[str, str], list[str]]] = []
        self._muted = 0  # inside <style>/<script>: not visible copy

    def _ancestor_classes(self) -> frozenset[str]:
        return frozenset(
            cls
            for _, attrs, _ in self._stack
            for cls in attrs.get("class", "").split()
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        got = {key: (value or "") for key, value in attrs}
        if tag in ("style", "script"):
            self._muted += 1
            return
        if tag in VOID:
            self.nodes.append(
                Node(tag, frozenset(got.get("class", "").split()), got,
                     "", self._ancestor_classes())
            )
            return
        self._stack.append((tag, got, []))

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script"):
            self._muted = max(0, self._muted - 1)
            return
        if not self._stack or self._stack[-1][0] != tag:
            return  # the page is hand-written and well-formed; nothing to repair
        name, got, buffer = self._stack.pop()
        text = squash("".join(buffer))
        self.nodes.append(
            Node(name, frozenset(got.get("class", "").split()), got,
                 text, self._ancestor_classes())
        )
        if self._stack:
            self._stack[-1][2].append(f" {text} ")

    def handle_data(self, data: str) -> None:
        if self._muted or not self._stack:
            return
        self._stack[-1][2].append(data)


@pytest.fixture(scope="module")
def page() -> str:
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def elements(page: str) -> list[Node]:
    parser = _Elements()
    parser.feed(page)
    return parser.nodes


@pytest.fixture(scope="module")
def prose(elements: list[Node]) -> str:
    """The page's visible copy, as a reader receives it."""
    body = [node for node in elements if node.tag == "body"]
    assert body, "the page has no <body>"
    return body[0].text


def one(elements: list[Node], cls: str) -> Node:
    hit = [node for node in elements if cls in node.classes]
    assert len(hit) == 1, f"expected exactly one .{cls}, found {len(hit)}"
    return hit[0]


# ── PRODUCT.md is the source of truth for the copy ──────────────────────────


@pytest.fixture(scope="module")
def product() -> str:
    return PRODUCT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def positioning(product: str) -> str:
    """The one-line positioning, lifted out of PRODUCT.md's own section."""
    section = product.split("## One-line positioning", 1)
    assert len(section) == 2, "PRODUCT.md no longer has a positioning section"
    for raw in section[1].splitlines():
        line = raw.strip().strip("*").strip()
        if line:
            return squash(line)
    pytest.fail("PRODUCT.md's positioning section is empty")


@pytest.fixture(scope="module")
def contract(product: str) -> list[str]:
    """The four contract lines, lifted out of PRODUCT.md's numbered list."""
    section = product.split("## The anti-slop contract", 1)
    assert len(section) == 2, "PRODUCT.md no longer has an anti-slop contract"
    lines: list[str] = []
    for raw in section[1].splitlines():
        if raw.startswith("---") or raw.startswith("## "):
            break
        if match := re.match(r"^\d+\.\s+(.*\S)", raw.strip()):
            lines.append(squash(match.group(1)))
    return lines


def test_the_positioning_line_is_quoted_from_product_md(elements, positioning):
    assert one(elements, "tagline").text == positioning


def test_every_contract_line_is_quoted_from_product_md(elements, contract):
    on_page = [
        node.text
        for node in elements
        if node.tag == "li" and "contract" in node.within
    ]
    assert on_page == contract


def test_the_contract_is_still_exactly_four_lines(contract):
    """'Four lines, non-negotiable' — a fifth would make the page a lie."""
    assert len(contract) == 4


def test_the_contract_is_labelled_as_such_on_the_page(elements):
    assert one(elements, "contract-label").text.lower() == "the anti-slop contract"


def test_the_page_carries_the_product_name(elements):
    assert one(elements, "wordmark").text.startswith("Thesis")


def test_no_copy_placeholder_survives_in_the_shipped_site():
    """Placeholders are honest while drafting and a bug once deployed."""
    for path in sorted(SITE.rglob("*")):
        if path.is_file() and path.suffix in (".html", ".css", ".json"):
            body = path.read_text(encoding="utf-8")
            assert "TODO(copy)" not in body, f"{path.name} still has TODO(copy)"


# ── the page keeps the promises it prints ───────────────────────────────────


def test_the_standing_disclaimer_is_present(prose):
    """PRODUCT.md compliance rail 3 requires it on user-facing surfaces."""
    lowered = prose.lower()
    assert "not investment advice" in lowered
    assert "simulated money" in lowered


def test_the_page_recommends_nothing_outside_the_quoted_contract(prose, contract):
    """Rail 2: no recommendation language on any user-facing surface.

    The one exemption is the contract itself — 'Never recommends what to buy.'
    trips a lexicon that cannot see it is a promise not to. Every other word on
    the page is held to the same bar as a generated brief.
    """
    rest = prose
    for line in contract:
        rest = rest.replace(line, " ")
    hits = [rule for _, _, rule in lint._recommendation_hits(rest)]
    assert hits == [], f"the landing page recommends something: {hits}"


def test_that_recommendation_check_would_actually_catch_one(prose, contract):
    """Proof the exemption above is narrow, not a hole big enough to hide in."""
    rest = prose
    for line in contract:
        rest = rest.replace(line, " ")
    assert lint._recommendation_hits(rest + " You should buy NVDA today.")


def test_the_page_loads_nothing_from_an_external_origin(page: str, elements):
    """'No framework bloat' is only true if nothing is fetched off-origin."""
    assert "<script" not in page.lower(), "the page pulled in a script"
    assert "@import" not in page, "the stylesheet imports another stylesheet"
    assert not re.search(r"url\(\s*['\"]?https?:", page), "CSS fetches a remote asset"

    stylesheets = [
        node for node in elements
        if node.tag == "link" and "stylesheet" in node.attrs.get("rel", "")
    ]
    assert stylesheets == [], "styles must stay inline in the single file"

    for node in elements:
        if node.tag == "img":
            src = node.attrs.get("src", "")
            assert not src.startswith(("http:", "https:", "//")), src


def test_the_stylesheet_and_markup_stay_in_one_file():
    """Single HTML/CSS file — no sidecar .css or .js to keep in sync."""
    strays = [p.name for p in SITE.rglob("*") if p.suffix in (".css", ".js")]
    assert strays == [], f"the page is no longer self-contained: {strays}"


def test_the_screenshot_slot_is_relative_and_degrades_when_empty(page: str, elements):
    """The slot ships empty, so an absent screenshot must not look broken."""
    shot = [node for node in elements if node.tag == "img"]
    assert len(shot) == 1
    assert shot[0].attrs["src"] == "standings.png"
    assert shot[0].attrs.get("alt"), "the standings screenshot needs alt text"
    assert "onerror" in shot[0].attrs, "a missing screenshot would render broken"
    assert "placeholder" in page


def test_the_invite_button_points_at_a_discord_authorize_url(elements):
    invite = one(elements, "invite")
    href = invite.attrs.get("href", "")
    assert href.startswith("https://discord.com/oauth2/authorize?")
    assert "scope=bot" in href


# ── deploy shape: static, root directory /site, no build step ───────────────


def test_the_entry_point_sits_at_the_site_root():
    """Vercel serves site/index.html at / when Root Directory is `site`."""
    assert PAGE.is_file()


def test_nothing_in_the_site_directory_triggers_a_build():
    """A build step is exactly what a package manifest would introduce."""
    manifests = [
        name for name in ("package.json", "package-lock.json", "requirements.txt",
                          "pnpm-lock.yaml", "yarn.lock", "Makefile")
        if (SITE / name).exists()
    ]
    assert manifests == [], f"these would make Vercel build the site: {manifests}"


def test_the_vercel_config_is_valid_and_declares_no_build():
    config = json.loads((SITE / "vercel.json").read_text(encoding="utf-8"))
    for key in ("builds", "buildCommand", "installCommand", "framework"):
        assert key not in config, f"{key} would turn the static deploy into a build"
    sources = [entry["source"] for entry in config.get("headers", [])]
    assert "/standings.png" in sources, "the weekly screenshot needs a cache policy"
