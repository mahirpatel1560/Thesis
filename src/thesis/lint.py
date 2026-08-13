"""Citation and recommendation discipline for generated briefs.

The prompt asks for a source tag on every factual claim and bans recommendation
language. Asking is not enforcing. This module reads a finished brief and finds
the places where the model drifted, so `thesis research` can push back once and
then fail loudly rather than filing an unsourced brief.

Two checks, both plain rules written out in the README:

**Sources.** A unit of text needs a source tag if it contains a figure, or if it
uses one of the fact words in `FACT_WORDS`. Valid tags are exactly the ones the
prompt defines — `[10-K 1]`, `[10-K 1A]`, `[10-K 7]`, `[yf prices]`,
`[yf financials]`, `[yf valuation]`, `[yf peers]`, `[news: ...]`. A bracketed
token that is not one of those is reported as an unknown tag rather than
silently accepted. Headings are exempt (the template writes them); a markdown
table is treated as one unit, and its tag may sit anywhere in the table or on
the line above it. `"not in provided data"` is the prompt's own escape hatch and
is always exempt.

**Recommendations.** A lexicon of advisory constructions, deliberately written to
catch advice without catching description. "What they sell" and "the acquisition
will add $2B" are facts about a company; "sell the shares" and "investors should
add here" are advice. The rules are listed in `RECOMMENDATION_RULES`, and bare
`buy`/`sell` are exempt when the preceding word makes them descriptive.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

# ------------------------------------------------------------------- source tags

VALID_TAGS = frozenset(
    {
        "10-k 1",
        "10-k 1a",
        "10-k 7",
        "yf prices",
        "yf financials",
        "yf valuation",
        "yf peers",
    }
)

#: A bracketed token that is not immediately followed by "(" (which would be a link).
TAG_RE = re.compile(r"\[([^\[\]]+)\](?!\()")

#: Words that make a sentence a factual assertion even when it carries no figure.
FACT_WORDS = (
    "revenue", "revenues", "margin", "margins", "debt", "cash flow", "fcf",
    "earnings", "profit", "profits", "income", "loss", "losses", "grew", "growth",
    "declined", "fell", "rose", "increased", "decreased", "customers", "segment",
    "segments", "guidance", "guided", "filed", "reported", "announced", "acquired",
    "acquisition", "launched", "contract", "contracts", "market share", "employees",
    "subscribers", "backlog", "capex", "buyback", "buybacks", "dividend", "dividends",
    "valuation", "multiple", "multiples", "competitor", "competitors", "peers",
    "shares outstanding", "operating expenses", "free cash flow",
)
FACT_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in FACT_WORDS) + r")\b", re.I)

DIGIT_RE = re.compile(r"\d")

#: The prompt's sanctioned way to say the packet did not cover something.
ESCAPE_HATCH = "not in provided data"


# --------------------------------------------------------------- recommendations

RECOMMENDATION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("explicit recommendation", re.compile(r"\brecommend(?:s|ed|ing|ation|ations)?\b", re.I)),
    ("price target", re.compile(r"\bprice\s+targets?\b", re.I)),
    ("position weighting", re.compile(r"\b(?:over|under)weight(?:ed|ing)?\b", re.I)),
    (
        "advice aimed at the reader",
        re.compile(r"\b(?:you|your|investors?|readers?|one)\s+(?:should|ought|must|could|might|may|would want)\b", re.I),
    ),
    ("advisory 'should'", re.compile(r"\bshould\b", re.I)),
    (
        "action on the position",
        re.compile(
            r"\b(?:buy|sell|hold|accumulate|trim|add|exit|own)\s+"
            r"(?:the\s+|this\s+|these\s+|its\s+|your\s+|more\s+|into\s+)?"
            r"(?:shares?|stock|position|positions|equity|name|it)\b",
            re.I,
        ),
    ),
    ("imperative advice", re.compile(r"^\s*(?:buy|sell|hold|accumulate|trim|exit)\b", re.I)),
    (
        "entry-timing language",
        re.compile(r"\b(?:attractive|compelling|good|great)\s+(?:entry|entry\s+point|risk[-/\s]reward|level|levels)\b", re.I),
    ),
    ("verdict phrased as a call", re.compile(r"\b(?:a|strong|clear|outright)\s+(?:buy|sell)\b", re.I)),
)

#: Bare buy/sell, flagged unless the preceding word makes it a description.
BARE_ACTION_RE = re.compile(r"\b(buy|sell)\b", re.I)
DESCRIPTIVE_SUBJECTS = frozenset(
    {
        "they", "customers", "users", "clients", "consumers", "businesses",
        "enterprises", "companies", "firms", "people", "who", "that", "retailers",
        "partners", "subscribers", "buyers", "shoppers", "advertisers", "we",
    }
)


# -------------------------------------------------------------------- data model

@dataclass(frozen=True)
class Violation:
    kind: str  # "missing-source" | "unknown-tag" | "recommendation"
    line: int
    text: str
    detail: str

    def render(self) -> str:
        return f"  line {self.line}: {self.detail}\n    > {self.text}"


@dataclass(frozen=True)
class LintReport:
    violations: tuple[Violation, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    def of_kind(self, kind: str) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.kind == kind)

    @property
    def missing_sources(self) -> tuple[Violation, ...]:
        return self.of_kind("missing-source")

    @property
    def recommendations(self) -> tuple[Violation, ...]:
        return self.of_kind("recommendation")

    def render(self) -> str:
        if self.ok:
            return "Brief lint: clean — every claim tagged, no recommendation language."
        counts: dict[str, int] = {}
        for violation in self.violations:
            counts[violation.kind] = counts.get(violation.kind, 0) + 1
        headline = ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))
        lines = [f"Brief lint: {len(self.violations)} violation(s) — {headline}"]
        lines.extend(v.render() for v in self.violations)
        return "\n".join(lines)


# ----------------------------------------------------------------- text handling

_ABBREVIATIONS = frozenset(
    {
        "u.s.", "u.k.", "inc.", "corp.", "co.", "ltd.", "vs.", "e.g.", "i.e.",
        "etc.", "approx.", "no.", "fig.", "est.", "ca.", "yoy.",
    }
)
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'\[(*])")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_INITIAL = re.compile(r"^[A-Z]\.$")


def _ends_with_abbreviation(text: str) -> bool:
    token = text.rstrip().split()[-1].lower() if text.strip() else ""
    return token in _ABBREVIATIONS or bool(_INITIAL.match(token.upper()))


def split_sentences(text: str) -> list[str]:
    """Split prose into sentences, without breaking on `U.S.`, `$1.2B`, or a tag.

    A `[news: ...]` tag can contain a headline with its own full stop, and
    splitting inside the brackets would orphan the tag from the claim it
    supports — flagging a correctly-sourced sentence as unsourced.
    """
    sentences: list[str] = []
    start = 0
    for boundary in _SENTENCE_BOUNDARY.finditer(text):
        prefix = text[:boundary.start()]
        if prefix.count("[") > prefix.count("]"):
            continue  # inside a source tag
        candidate = text[start : boundary.start()]
        if _ends_with_abbreviation(candidate):
            continue
        if candidate.strip():
            sentences.append(candidate.strip())
        start = boundary.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences


@dataclass(frozen=True)
class Unit:
    """A checkable chunk of the brief: a sentence, a heading, or a whole table."""

    line: int
    text: str
    kind: str  # "prose" | "heading" | "table"
    context: str = ""  # for tables: the line above, where a tag may live


def units(body: str) -> Iterator[Unit]:
    """Break a brief into checkable units, skipping fenced code."""
    lines = body.splitlines()
    index = 0
    in_code = False
    paragraph: list[str] = []
    paragraph_line = 0

    def flush() -> Iterator[Unit]:
        nonlocal paragraph
        if paragraph:
            joined = " ".join(paragraph)
            for sentence in split_sentences(joined):
                yield Unit(paragraph_line, sentence, "prose")
            paragraph = []

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()

        if stripped.startswith("```"):
            yield from flush()
            in_code = not in_code
            index += 1
            continue
        if in_code:
            index += 1
            continue
        if not stripped:
            yield from flush()
            index += 1
            continue
        if stripped.startswith("#"):
            yield from flush()
            yield Unit(index + 1, stripped, "heading")
            index += 1
            continue
        if stripped.startswith("|"):
            yield from flush()
            start = index
            block: list[str] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                block.append(lines[index].strip())
                index += 1
            # A table's tag may sit on its caption — the nearest non-blank line above.
            back = start - 1
            while back >= 0 and not lines[back].strip():
                back -= 1
            preceding = lines[back].strip() if back >= 0 else ""
            yield Unit(start + 1, "\n".join(block), "table", context=preceding)
            continue
        if stripped.startswith(">") or (set(stripped) <= {"-", "*", "_"} and len(stripped) >= 3):
            # Block quotes and horizontal rules carry no claims of their own.
            yield from flush()
            index += 1
            continue

        if _LIST_MARKER.match(raw) and paragraph:
            yield from flush()
        if not paragraph:
            paragraph_line = index + 1
        paragraph.append(_LIST_MARKER.sub("", stripped))
        index += 1

    yield from flush()


# --------------------------------------------------------------------- the checks

def tags_in(text: str) -> list[str]:
    return TAG_RE.findall(text)


def is_valid_tag(tag: str) -> bool:
    normalized = " ".join(tag.split()).lower()
    return normalized in VALID_TAGS or normalized.startswith("news:")


def strip_tags(text: str) -> str:
    return TAG_RE.sub(" ", text)


def source_requirement(text: str) -> str | None:
    """Why this text needs a source tag, or None if it is allowed to stand alone."""
    if ESCAPE_HATCH in text.lower():
        return None
    bare = strip_tags(text)
    if DIGIT_RE.search(bare):
        return "states a figure"
    match = FACT_RE.search(bare)
    if match:
        return f"asserts a fact about {match.group(1).lower()!r}"
    return None


def _recommendation_hits(text: str) -> list[tuple[int, int, str]]:
    """(start, end, rule name) for every advisory construction in the text."""
    hits: list[tuple[int, int, str]] = []
    for name, pattern in RECOMMENDATION_RULES:
        for match in pattern.finditer(text):
            hits.append((match.start(), match.end(), name))

    for match in BARE_ACTION_RE.finditer(text):
        preceding = text[: match.start()].split()
        previous = preceding[-1].lower().strip(",;:\"'([") if preceding else ""
        if previous in DESCRIPTIVE_SUBJECTS:
            continue
        hits.append((match.start(), match.end(), f"action word {match.group(1).lower()!r}"))

    # Drop hits fully contained in a longer hit: "you should" beats "should".
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    kept: list[tuple[int, int, str]] = []
    for hit in hits:
        if any(hit[0] >= k[0] and hit[1] <= k[1] for k in kept):
            continue
        kept.append(hit)
    return kept


def lint_brief(body: str) -> LintReport:
    """Check a generated brief for untagged claims and recommendation language."""
    violations: list[Violation] = []

    for unit in units(body):
        haystack = f"{unit.context}\n{unit.text}" if unit.kind == "table" else unit.text
        found = tags_in(haystack)

        if unit.kind != "heading":
            reason = source_requirement(unit.text)
            if reason:
                if not found:
                    violations.append(
                        Violation(
                            "missing-source",
                            unit.line,
                            _excerpt(unit.text),
                            f"no source tag on text that {reason}",
                        )
                    )
                elif not any(is_valid_tag(tag) for tag in found):
                    unknown = ", ".join(f"[{tag}]" for tag in found if not is_valid_tag(tag))
                    violations.append(
                        Violation(
                            "unknown-tag",
                            unit.line,
                            _excerpt(unit.text),
                            f"{unknown} is not one of the allowed source tags",
                        )
                    )

        for start, end, rule in _recommendation_hits(unit.text):
            violations.append(
                Violation(
                    "recommendation",
                    unit.line,
                    _excerpt(unit.text),
                    f"{rule}: {unit.text[start:end]!r}",
                )
            )

    violations.sort(key=lambda v: (v.line, v.kind))
    return LintReport(tuple(violations))


def _excerpt(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# -------------------------------------------------------------- the second pass

def correction_prompt(report: LintReport) -> str:
    """The follow-up message that asks the model to fix exactly these violations."""
    lines = [
        "Your draft broke the brief's rules. Below is every violation, with the "
        "line of text that triggered it.",
        "",
    ]
    if report.missing_sources or report.of_kind("unknown-tag"):
        lines.append("## Claims without a valid source tag")
        for violation in report.missing_sources + report.of_kind("unknown-tag"):
            lines.append(f"- {violation.detail}\n  > {violation.text}")
        lines.append("")
        lines.append(
            "For each one: append the tag that actually supports it "
            "([10-K 1], [10-K 1A], [10-K 7], [yf prices], [yf financials], "
            "[yf valuation], [yf peers], or [news: <headline>]), or if the data "
            'packet does not support the claim, delete it or replace it with '
            f'"{ESCAPE_HATCH}".'
        )
        lines.append("")
    if report.recommendations:
        lines.append("## Recommendation language")
        for violation in report.recommendations:
            lines.append(f"- {violation.detail}\n  > {violation.text}")
        lines.append("")
        lines.append(
            "Rewrite each of these to describe rather than advise. State what is "
            "true and let the reader decide; do not tell them what to do, and do "
            "not use 'should' even about the business."
        )
        lines.append("")
    lines.append(
        "Return the corrected brief in full, same eight sections, same structure. "
        "Change nothing except what is required to fix the violations above."
    )
    return "\n".join(lines)
