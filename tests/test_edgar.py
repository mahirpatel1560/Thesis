"""Offline tests for the EDGAR parsing layer (no network)."""

from pathlib import Path

import pytest

from thesis.data import edgar

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def mini_10k_text() -> str:
    html = (FIXTURES / "mini_10k.html").read_text(encoding="utf-8")
    return edgar.html_to_text(html)


# ---------------------------------------------------------------- html_to_text

def test_html_to_text_strips_scripts_and_styles(mini_10k_text: str) -> None:
    assert "should never appear" not in mini_10k_text
    assert "margin: 0" not in mini_10k_text


def test_html_to_text_flattens_entities(mini_10k_text: str) -> None:
    assert "ACME CORP — FORM 10-K" in mini_10k_text
    assert "\xa0" not in mini_10k_text


# --------------------------------------------------------------- extract_items

def test_extracts_all_three_items(mini_10k_text: str) -> None:
    items = edgar.extract_items(mini_10k_text)
    assert set(items) == {"1", "1A", "7"}


def test_item_1_is_body_not_toc(mini_10k_text: str) -> None:
    item1 = edgar.extract_items(mini_10k_text)["1"]
    assert "rocket-powered roller skates" in item1
    # TOC candidate for Item 1 is just "Business / 3" — the real body must win
    assert len(item1) > 200


def test_midline_cross_reference_does_not_split_section(mini_10k_text: str) -> None:
    item1 = edgar.extract_items(mini_10k_text)["1"]
    # "see Item 1A of this Annual Report" sits mid-paragraph inside Item 1;
    # the sentence after it must still belong to Item 1
    assert "one reportable segment" in item1


def test_item_1a_contains_risks_only(mini_10k_text: str) -> None:
    item1a = edgar.extract_items(mini_10k_text)["1A"]
    assert "single customer" in item1a
    assert "anvil iron could increase our cost" in item1a
    assert "rocket-powered roller skates" not in item1a  # that's Item 1
    assert "None." not in item1a  # that's Item 1B


def test_item_7_stops_at_item_7a(mini_10k_text: str) -> None:
    item7 = edgar.extract_items(mini_10k_text)["7"]
    assert "Net revenue increased 14%" in item7
    assert "hypothetical 10%" not in item7  # that's Item 7A


def test_page_number_noise_lines_removed(mini_10k_text: str) -> None:
    items = edgar.extract_items(mini_10k_text)
    for body in items.values():
        for line in body.splitlines():
            assert line.strip() not in {"4", "12", "Table of Contents"}


def test_extract_items_empty_when_nothing_matches() -> None:
    assert edgar.extract_items("just some prose with no headings") == {}


# ------------------------------------------------------------- ticker -> CIK

TICKER_MAPPING = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
}


def test_cik_for_ticker_case_insensitive() -> None:
    assert edgar.cik_for_ticker(TICKER_MAPPING, "aapl") == 320193


def test_cik_for_ticker_unknown_raises() -> None:
    with pytest.raises(ValueError, match="ZZZZ"):
        edgar.cik_for_ticker(TICKER_MAPPING, "ZZZZ")


# -------------------------------------------------------------- latest_filing

SUBMISSIONS = {
    "name": "Apple Inc.",
    "filings": {
        "recent": {
            "form": ["8-K", "10-Q", "10-K", "10-K"],
            "accessionNumber": ["0-8k", "0-10q", "0001-10k-new", "0002-10k-old"],
            "primaryDocument": ["a.htm", "b.htm", "aapl-2025.htm", "aapl-2024.htm"],
            "filingDate": ["2025-06-01", "2025-05-01", "2024-11-01", "2023-11-02"],
            "reportDate": ["2025-05-30", "2025-03-29", "2024-09-28", "2023-09-30"],
        }
    },
}


def test_latest_filing_picks_first_10k() -> None:
    filing = edgar.latest_filing(SUBMISSIONS, "10-K")
    assert filing.accession_number == "0001-10k-new"
    assert filing.primary_document == "aapl-2025.htm"
    assert filing.filing_date == "2024-11-01"


def test_latest_filing_missing_form_raises() -> None:
    with pytest.raises(ValueError, match="10-K/A"):
        edgar.latest_filing(SUBMISSIONS, "10-K/A")


# ================================================== filer-format robustness

def prose(sentence: str, paragraphs: int) -> str:
    """A section's worth of body text, one paragraph per block element.

    Separate `<p>` elements, not one long one: real sections run to thousands of
    lines, and the distance between the table of contents and the body is what
    the running-header clustering depends on.
    """
    return "\n".join(f"<p>{sentence}</p>" for _ in range(paragraphs))


BODY = prose("The Company operates warehouses and sells memberships.", 60)
RISKS = prose("Our results depend on a small number of suppliers.", 60)
MDNA = prose("Net sales increased on higher comparable sales.", 60)


def build_emdash_10k() -> str:
    """A Costco-style filing: the item number is glued to an em dash.

    The table of contents lists "Item 1." on its own line with the title on the
    next, so the only TOC candidate for Item 1 is the bare word "Business" —
    which is exactly what the old parser handed to the brief.
    """
    return f"""<html><body>
<div>Table of Contents</div>
<div>Item 1.</div><div>Business</div><div>3</div>
<div>Item 1A.</div><div>Risk Factors</div><div>12</div>
<div>Item 7.</div><div>Management's Discussion and Analysis</div><div>25</div>
<div>Item 1—Business</div>
{BODY}
<div>Item 1A—Risk Factors</div>
{RISKS}
<div>Item 1B—Unresolved Staff Comments</div>
<p>None.</p>
<div>Item 7—Management's Discussion and Analysis</div>
{MDNA}
<div>Item 7A—Quantitative and Qualitative Disclosures</div>
<p>Not applicable.</p>
</body></html>"""


def test_em_dash_headings_yield_real_sections_not_toc_stubs() -> None:
    """Regression: COST returned Item 1 = 'Business' (8 chars) and lost all grounding."""
    items = edgar.extract_items(edgar.html_to_text(build_emdash_10k()))

    assert items["1"].startswith("Business")
    assert "warehouses and sells memberships" in items["1"]
    assert len(items["1"]) > edgar.MIN_SECTION_CHARS
    assert "small number of suppliers" in items["1A"]
    assert len(items["1A"]) > edgar.MIN_SECTION_CHARS
    assert "comparable sales" in items["7"]
    assert len(items["7"]) > edgar.MIN_SECTION_CHARS
    assert edgar.section_problems(items) == {}


def test_em_dash_sections_still_stop_at_the_next_item() -> None:
    items = edgar.extract_items(edgar.html_to_text(build_emdash_10k()))
    assert "small number of suppliers" not in items["1"]  # that is Item 1A
    assert "None." not in items["1A"]  # that is Item 1B
    assert "Not applicable" not in items["7"]  # that is Item 7A


@pytest.mark.parametrize(
    "heading,expected",
    [
        ("Item 1. Business", "1"),
        ("Item 1—Business", "1"),
        ("Item 1-Business", "1"),
        ("ITEM 1: BUSINESS", "1"),
        ("Item 1A. Risk Factors", "1A"),
        ("Item 7—MD&A", "7"),
    ],
)
def test_heading_separators_all_parse(heading: str, expected: str) -> None:
    match = edgar._ITEM_RE.search(f"{heading}\n")
    assert match is not None, heading
    assert match.group(1).upper() == expected


def test_item_10_is_never_read_as_item_1() -> None:
    """The number is delimited by 'not a letter or digit', so 10 stays 10."""
    text = "Item 10. Directors\nSome governance prose here.\nItem 11. Compensation\n"
    assert edgar.extract_items(text, ("1",)) == {}
    assert edgar._ITEM_RE.search("Item 10.\n").group(1) == "10"


# -------------------------------------------- incorporation by reference

def build_by_reference_10k(filler_lines: int = 1_200) -> str:
    """A JPMorgan-style filing: Item 7 is a pointer, the content is elsewhere.

    The real MD&A carries a running page header, and `filler_lines` of tables in
    the middle leave a long stretch with no header at all — which is what split
    the section into two clusters in the live filing.
    """
    header = "<div>Management's discussion and analysis</div>"
    filler = "\n".join(f"<p>Table row {i} with figures.</p>" for i in range(filler_lines))
    early = "\n".join(f"{header}\n{prose('Consolidated results improved.', 20)}" for _ in range(2))
    late = "\n".join(f"{header}\n{prose('Segment results are described here.', 20)}" for _ in range(6))
    # Items 1 and 1A are long enough to strand the TOC entry on its own, as they
    # are in a real filing (JPM's is ~3,900 lines from the body).
    return f"""<html><body>
<div>Table of Contents</div>
<div>Management's Discussion and Analysis of Financial Condition and Results of Operations.</div>
<div>Item 7.</div><div>46</div>
<div>Item 1. Business.</div>
{prose("The Firm operates through four business segments.", 700)}
<div>Item 1A. Risk Factors.</div>
{prose("Our results depend on a small number of suppliers.", 700)}
<div>Item 7. Management's Discussion and Analysis of Financial Condition and Results of Operations.</div>
<p>Management's discussion and analysis appears on pages 46-160. Such information
should be read together with the Consolidated Financial Statements.</p>
<div>Item 7A. Quantitative and Qualitative Disclosures About Market Risk.</div>
<p>Refer to the Market Risk Management section on pages 133-142.</p>
<div>Introduction</div>
{early}
{filler}
{late}
</body></html>"""


def test_a_cross_referenced_section_is_recovered_from_its_running_header() -> None:
    """Regression: JPM's Item 7 was a 388-char pointer, losing the entire MD&A."""
    text = edgar.html_to_text(build_by_reference_10k())
    items = edgar.extract_items(text)

    assert len(items["7"]) > edgar.MIN_SECTION_CHARS
    assert "Consolidated results improved" in items["7"]
    assert "Segment results are described here" in items["7"]
    assert edgar.section_problems(items) == {}


def test_recovery_spans_a_gap_instead_of_taking_the_biggest_cluster() -> None:
    """A long run of tables must not lop off the first half of the section."""
    text = edgar.html_to_text(build_by_reference_10k(filler_lines=1_200))
    recovered = edgar.resolve_by_running_header(text, "7")

    assert recovered is not None
    assert "Consolidated results improved" in recovered  # before the gap
    assert "Segment results are described here" in recovered  # after the gap
    assert "Table row 600" in recovered  # the gap itself is inside the section


def test_recovery_ignores_the_lone_table_of_contents_entry() -> None:
    """The TOC title sits far above the body and must not start the section."""
    text = edgar.html_to_text(build_by_reference_10k())
    recovered = edgar.resolve_by_running_header(text, "7")
    assert "Risk Factors" not in recovered
    assert "small number of suppliers" not in recovered  # Item 1A, above the TOC entry


def test_recovery_refuses_to_invent_a_section_from_one_heading() -> None:
    text = "Business\nSome prose about the business.\nSomething else entirely.\n"
    assert edgar.resolve_by_running_header(text, "1") is None
    assert edgar.resolve_by_running_header(text, "7") is None


def test_recovery_declines_an_item_it_has_no_title_for() -> None:
    assert edgar.resolve_by_running_header("Item 9B\nprose\n", "9B") is None


# ------------------------------------------------------- substance checks

def test_section_problems_flags_a_placeholder_section() -> None:
    problems = edgar.section_problems({"1": "Business", "1A": "x" * 5_000, "7": "y" * 5_000})
    assert set(problems) == {"1"}
    assert "only 8 chars" in problems["1"]
    assert "'Business'" in problems["1"]


def test_section_problems_flags_a_missing_section() -> None:
    problems = edgar.section_problems({"1": "x" * 5_000, "1A": "y" * 5_000})
    assert "no heading for it was found" in problems["7"]


def test_section_problems_is_silent_when_every_section_is_substantive() -> None:
    good = {item: "x" * 5_000 for item in edgar.DEFAULT_ITEMS}
    assert edgar.section_problems(good) == {}


# ------------------------------------------- fetch-time failure (fake client)

class _FakeResponse:
    def __init__(self, payload: object = None, text: str = "") -> None:
        self._payload = payload
        self.text = text

    def raise_for_status(self) -> "_FakeResponse":
        return self

    def json(self) -> object:
        return self._payload


class _FakeClient:
    """Serves the three GETs `get_10k` makes, in order."""

    def __init__(self, html: str) -> None:
        self.html = html

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get(self, url: str) -> _FakeResponse:
        if url == edgar.TICKERS_URL:
            return _FakeResponse({"0": {"cik_str": 909832, "ticker": "COST"}})
        if url.startswith("https://data.sec.gov/submissions"):
            return _FakeResponse(
                {
                    "name": "Costco Wholesale Corp",
                    "filings": {
                        "recent": {
                            "form": ["10-K"],
                            "accessionNumber": ["0000909832-25-000101"],
                            "primaryDocument": ["cost-20250831.htm"],
                            "filingDate": ["2025-10-08"],
                            "reportDate": ["2025-08-31"],
                        }
                    },
                }
            )
        return _FakeResponse(text=self.html)


def test_get_10k_fails_loudly_rather_than_returning_placeholders(monkeypatch) -> None:
    """The whole point: a thin section must never reach the brief packet."""
    placeholder_only = """<html><body>
<div>Item 1.</div><div>Business</div>
<div>Item 1A.</div><div>Risk Factors</div>
<div>Item 7.</div><div>MD&A</div>
</body></html>"""
    monkeypatch.setattr(edgar, "_client", lambda: _FakeClient(placeholder_only))

    with pytest.raises(edgar.SectionExtractionError) as exc:
        edgar.get_10k("COST")

    message = str(exc.value)
    assert "COST" in message
    assert "0000909832-25-000101" in message
    assert "Item 1" in message and "Item 1A" in message and "Item 7" in message
    assert "Refusing to build a brief on placeholder text" in message


def test_get_10k_returns_the_filing_when_every_section_is_substantive(monkeypatch) -> None:
    monkeypatch.setattr(edgar, "_client", lambda: _FakeClient(build_emdash_10k()))

    tenk = edgar.get_10k("COST")

    assert tenk.ticker == "COST"
    assert tenk.company == "Costco Wholesale Corp"
    assert tenk.filing.accession_number == "0000909832-25-000101"
    for item in edgar.DEFAULT_ITEMS:
        assert len(tenk.items[item]) > edgar.MIN_SECTION_CHARS
