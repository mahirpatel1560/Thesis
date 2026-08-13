"""SEC EDGAR adapter: fetch the latest 10-K for a ticker and extract
Items 1 (Business), 1A (Risk Factors), and 7 (MD&A) as clean text.

Network calls live in the fetch_*/get_* functions; parsing is pure so it can be
tested offline against fixtures.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import warnings

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# Modern 10-K primary documents are XHTML; the HTML parser handles them fine.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

from thesis import config

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{doc}"

DEFAULT_ITEMS = ("1", "1A", "7")

#: A section shorter than this is a heading or a pointer, not a section. Every
#: Item 1/1A/7 in a real 10-K runs to tens of thousands of characters; the
#: failures this guards against came back at 8 and 12 characters.
MIN_SECTION_CHARS = 2_000


class SectionExtractionError(ValueError):
    """A 10-K was fetched but a required item could not be recovered from it.

    Raised at fetch time. A brief built on placeholder text looks perfectly
    normal and is completely ungrounded, so this fails loudly instead.
    """


@dataclass
class Filing:
    form: str
    accession_number: str  # with dashes, as EDGAR reports it
    primary_document: str
    filing_date: str
    report_date: str


@dataclass
class TenK:
    ticker: str
    company: str
    cik: int
    filing: Filing
    items: dict[str, str] = field(default_factory=dict)


# ------------------------------------------------------------- pure parsing

def cik_for_ticker(mapping: dict[str, Any], ticker: str) -> int:
    """Resolve a ticker to its CIK from the company_tickers.json payload. Pure."""
    want = ticker.upper()
    for entry in mapping.values():
        if entry.get("ticker", "").upper() == want:
            return int(entry["cik_str"])
    raise ValueError(f"Ticker {ticker!r} not found in SEC company list")


def latest_filing(submissions: dict[str, Any], form: str = "10-K") -> Filing:
    """Most recent filing of the given form type from a submissions payload. Pure."""
    recent = submissions["filings"]["recent"]
    for i, form_type in enumerate(recent["form"]):
        if form_type == form:
            return Filing(
                form=form_type,
                accession_number=recent["accessionNumber"][i],
                primary_document=recent["primaryDocument"][i],
                filing_date=recent["filingDate"][i],
                report_date=recent["reportDate"][i],
            )
    raise ValueError(f"No {form} found in recent filings")


_BLOCK_TAGS = [
    "p", "div", "table", "tr", "li", "br", "hr", "section", "article",
    "h1", "h2", "h3", "h4", "h5", "h6",
]
# Sentinel for block boundaries — must survive whitespace collapsing, because
# newlines inside source text (line wraps, inline <a>/<b> splits) are NOT
# structure and would otherwise put cross-references like "see Item 1A" at a
# line start, where they'd be mistaken for section headings.
_BLOCK_BREAK = ""


def html_to_text(html: str) -> str:
    """Filing HTML -> plain text, one block-level element per line. Pure.

    Inline markup and source line-wraps are joined with spaces; only block
    elements (p, div, tr, headings, ...) produce line breaks.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before(_BLOCK_BREAK)
        tag.insert_after(_BLOCK_BREAK)
    text = soup.get_text(separator=" ")
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[^\S]+", " ", text)  # collapse all whitespace, keep sentinels
    lines = [segment.strip() for segment in text.split(_BLOCK_BREAK)]
    return "\n".join(lines)


# Item headings sit at the start of a line once HTML structure is flattened.
# Cross-references ("see Item 1A of this report") stay mid-line and are ignored.
#
# The separator between the number and the title varies by filer and must not be
# assumed to be whitespace: Costco writes "Item 1—Business" with the em dash glued
# to both sides, which an earlier `item[ \t]+(\d+)[.:–—-]?(?=[ \t]|$)` could not
# match at all — it found only the table-of-contents stubs and extracted the word
# "Business" as the entire section. The number is instead delimited by "not a
# letter or digit", which keeps "Item 10" from ever reading as "Item 1".
_ITEM_RE = re.compile(r"(?im)^[ \t]*item[ \t]*(\d{1,2}[a-c]?)(?![0-9a-z])[ \t]*[.:–—-]?[ \t]*")

# Filings that answer an item with a pointer rather than prose.
_CROSS_REFERENCE_RE = re.compile(
    r"(?i)(appears? on page|set forth on page|presented on page|incorporated (?:herein )?by"
    r" reference|refer to|see page|included (?:in|on) page)"
)

# The canonical title of each item, as it appears in a running page header.
_ITEM_TITLES: dict[str, re.Pattern[str]] = {
    "1": re.compile(r"(?i)^business[.:]?$"),
    "1A": re.compile(r"(?i)^risk factors[.:]?$"),
    "7": re.compile(
        r"(?i)^management.{0,3}s discussion and analysis"
        r"(?: of financial condition and results of operations)?[.:]?$"
    ),
}

#: Max line gap between two running-header occurrences for them to count as the
#: same section. Observed page spacing in a large filing is ~300 lines; this is
#: comfortably above that and well below the distance to a stray TOC entry.
_MAX_HEADER_GAP = 1_000

# Lines that are page furniture, not prose.
_NOISE_RE = re.compile(r"(?i)^(?:\d{1,3}|f-\d+|table of contents|\| ?)$")


def _clean_section(body: str) -> str:
    lines = [line for line in body.splitlines() if not _NOISE_RE.match(line.strip())]
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _header_clusters(hits: list[int]) -> list[list[int]]:
    """Group line numbers into runs separated by no more than `_MAX_HEADER_GAP`."""
    clusters: list[list[int]] = []
    for hit in hits:
        if clusters and hit - clusters[-1][-1] <= _MAX_HEADER_GAP:
            clusters[-1].append(hit)
        else:
            clusters.append([hit])
    return clusters


def resolve_by_running_header(text: str, item: str) -> str | None:
    """Recover a section the filing answered with a cross-reference. Pure.

    Large filers — banks especially — answer an item with a pointer ("appears on
    pages 46-160") and put the real content later in the same document, where
    every page carries the section's title as a running header. Those repeated
    headers bracket the section: it runs from the first to the last of them.

    The title also appears once in the table of contents, far from the body. So
    occurrences are grouped by proximity, isolated ones are dropped as headings
    rather than page furniture, and the section spans the first to the last of
    what remains. Spanning rather than taking the largest group matters: a long
    run of tables can leave thousands of lines with no header, and picking the
    biggest group alone would silently start the section halfway down.

    Requiring three surviving occurrences is the guard against inventing a
    section out of one stray heading — that is how you capture the financial
    statements too and never notice.
    """
    pattern = _ITEM_TITLES.get(item.upper())
    if pattern is None:
        return None

    lines = text.splitlines()
    hits = [i for i, line in enumerate(lines) if pattern.match(line.strip())]
    clusters = [c for c in _header_clusters(hits) if len(c) >= 2]
    if sum(len(c) for c in clusters) < 3:
        return None

    first, last = clusters[0][0], clusters[-1][-1]
    body = "\n".join(
        line
        for line in lines[first + 1 : last]
        if not pattern.match(line.strip())  # drop the repeated header itself
    )
    return _clean_section(body)


def extract_items(text: str, wanted: tuple[str, ...] = DEFAULT_ITEMS) -> dict[str, str]:
    """Extract item sections from flattened 10-K text. Pure.

    A 10-K mentions each item heading at least twice (table of contents + body).
    Every occurrence starts a candidate section that runs to the next item
    heading; the longest candidate per item is the real body — TOC entries are
    followed almost immediately by the next TOC line.

    An item whose best candidate is too short to be a section falls back to
    `resolve_by_running_header`, which handles filings that incorporate the
    content by reference. The fallback is only accepted if it produces more text
    than the anchored attempt did.
    """
    wanted_set = {w.upper() for w in wanted}
    matches = [(m.start(), m.end(), m.group(1).upper()) for m in _ITEM_RE.finditer(text)]
    best: dict[str, str] = {}
    for i, (_, end, label) in enumerate(matches):
        if label not in wanted_set:
            continue
        next_start = matches[i + 1][0] if i + 1 < len(matches) else len(text)
        body = _clean_section(text[end:next_start])
        if len(body) > len(best.get(label, "")):
            best[label] = body

    for label in wanted_set:
        if len(best.get(label, "")) >= MIN_SECTION_CHARS:
            continue
        recovered = resolve_by_running_header(text, label)
        if recovered and len(recovered) > len(best.get(label, "")):
            best[label] = recovered
    return best


def section_problems(
    items: dict[str, str],
    wanted: tuple[str, ...] = DEFAULT_ITEMS,
    min_chars: int = MIN_SECTION_CHARS,
) -> dict[str, str]:
    """Which wanted items are missing or too thin to be real sections. Pure."""
    problems: dict[str, str] = {}
    for item in wanted:
        body = items.get(item.upper())
        if body is None:
            problems[item] = "no heading for it was found in the filing"
        elif len(body) < min_chars:
            excerpt = " ".join(body.split())[:100] or "(empty)"
            problems[item] = (
                f"only {len(body)} chars, need {min_chars:,} — got {excerpt!r}"
            )
    return problems


# ------------------------------------------------------------ network layer

def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": config.sec_user_agent()},
        timeout=30.0,
        follow_redirects=True,
    )


def get_10k(ticker: str, items: tuple[str, ...] = DEFAULT_ITEMS) -> TenK:
    """Fetch the latest 10-K for `ticker` and extract the requested items."""
    with _client() as client:
        tickers = client.get(TICKERS_URL).raise_for_status().json()
        cik = cik_for_ticker(tickers, ticker)

        submissions = client.get(SUBMISSIONS_URL.format(cik=cik)).raise_for_status().json()
        filing = latest_filing(submissions, "10-K")

        doc_url = ARCHIVES_URL.format(
            cik=cik,
            accession=filing.accession_number.replace("-", ""),
            doc=filing.primary_document,
        )
        html = client.get(doc_url).raise_for_status().text

    text = html_to_text(html)
    extracted = extract_items(text, items)

    problems = section_problems(extracted, items)
    if problems:
        detail = "\n".join(f"  Item {item}: {why}" for item, why in problems.items())
        raise SectionExtractionError(
            f"{ticker.upper()} 10-K ({filing.accession_number}, "
            f"{filing.primary_document}) parsed, but these sections did not come "
            f"out as usable text:\n{detail}\n"
            "Refusing to build a brief on placeholder text — the filing's HTML "
            "layout is not one this parser handles yet."
        )
    return TenK(
        ticker=ticker.upper(),
        company=submissions.get("name", ticker.upper()),
        cik=cik,
        filing=filing,
        items=extracted,
    )
