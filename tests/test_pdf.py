"""Markdown -> PDF rendering for the track report."""

from __future__ import annotations

from datetime import date

import pytest
from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table

from thesis import journal, pdf, track

from conftest import d, deposit, holding, spy_series, trade

SPY = spy_series({"2026-07-01": 500.0, "2026-07-10": 550.0, "2026-07-20": 600.0})


def build_markdown(book: str = journal.REAL) -> str:
    """A real track report: one open position, one closed, both with theses."""
    closed = holding(
        [
            trade("buy", 5, 100.0, "2026-07-10", id=1),
            trade("sell", 5, 130.0, "2026-07-20", id=2),
        ],
        id=1,
        book=book,
        status="closed",
        closed_at="2026-07-20T20:00:00+00:00",
        outcome="Re-rated on the services print — thesis played out early.",
    )
    open_line = holding(
        [trade("buy", 2, 50.0, "2026-07-10", ticker="WILE", position_id=2, id=9)],
        id=2,
        book=book,
        ticker="WILE",
        bucket=journal.ACTIVE,
        stop_price=45.0,
    )
    report = track.build_report(
        book,
        [deposit(2000.0, "2026-07-01", book=book)],
        [closed, open_line],
        {"ACME": 130.0, "WILE": 60.0},
        SPY,
        as_of=d("2026-07-20"),
    )
    return track.render_markdown(report)


# ------------------------------------------------------------- block parsing

def test_parse_markdown_recognises_every_construct_the_report_emits() -> None:
    blocks = pdf.parse_markdown(build_markdown())
    kinds = {b.kind for b in blocks}
    assert kinds == {"heading", "para", "table", "quote", "bullet"}

    titles = [b for b in blocks if b.kind == "heading" and b.level == 1]
    assert len(titles) == 1
    assert titles[0].text.startswith("Track Record")


def test_tables_drop_the_divider_row_and_keep_the_header() -> None:
    markdown = "| A | B |\n|---|---|\n| 1 | 2 |\n"
    (table,) = pdf.parse_markdown(markdown)
    assert table.kind == "table"
    assert table.rows == (("A", "B"), ("1", "2"))


def test_a_wrapped_paragraph_becomes_one_block() -> None:
    blocks = pdf.parse_markdown("one line\ncontinues here\n\nsecond para\n")
    assert [b.text for b in blocks] == ["one line continues here", "second para"]


def test_bullets_and_quotes_are_their_own_blocks() -> None:
    blocks = pdf.parse_markdown("> a warning\n\n- **Thesis:** it compounds\n")
    assert blocks[0].kind == "quote"
    assert blocks[0].text == "a warning"
    assert blocks[1].kind == "bullet"
    assert blocks[1].text == "**Thesis:** it compounds"


def test_heading_levels_are_captured() -> None:
    blocks = pdf.parse_markdown("# One\n\n## Two\n\n### Three\n")
    assert [(b.level, b.text) for b in blocks] == [(1, "One"), (2, "Two"), (3, "Three")]


# --------------------------------------------------------- inline formatting

def test_ampersands_are_escaped_before_markup_is_added() -> None:
    """'P&L' is in every table header — unescaped it breaks the PDF parser."""
    assert pdf.inline("P&L") == "P&amp;L"
    assert pdf.inline("**P&L**") == "<b>P&amp;L</b>"


def test_angle_brackets_are_escaped() -> None:
    assert pdf.inline("a < b > c") == "a &lt; b &gt; c"


def test_bold_italic_and_code_become_reportlab_markup() -> None:
    assert pdf.inline("**bold**") == "<b>bold</b>"
    assert pdf.inline("*quiet*") == "<i>quiet</i>"
    assert pdf.inline("`paper`") == '<font face="Courier">paper</font>'


def test_bold_wins_over_italic_so_double_stars_are_not_mangled() -> None:
    assert pdf.inline("**Edge vs SPY**") == "<b>Edge vs SPY</b>"


def test_plain_strips_markup_for_width_measurement() -> None:
    assert pdf.plain("**+$43.21**") == "+$43.21"
    assert pdf.plain("*No open positions.*") == "No open positions."


# ------------------------------------------------------------- character safety

def test_typographic_characters_survive() -> None:
    for char in "—·’“”•…":
        assert pdf.sanitize(char) == char


def test_characters_the_pdf_fonts_cannot_encode_are_spelled_out() -> None:
    assert pdf.sanitize("100 → 120") == "100 -> 120"
    assert pdf.sanitize("≥ 20%") == ">= 20%"


def test_anything_else_degrades_instead_of_raising() -> None:
    """A thesis with an emoji in it must not kill the export."""
    assert pdf.sanitize("rocket 🚀 skates") == "rocket ? skates"
    assert pdf.inline("🚀") == "?"


# ------------------------------------------------------------------- tables

def test_column_widths_fill_the_page_exactly() -> None:
    rows = [("Ticker", "P&L"), ("ACME", "+$300.00")]
    widths = pdf.column_widths(rows, available=500.0)
    assert len(widths) == 2
    assert sum(widths) == pytest.approx(500.0)
    assert all(w > 0 for w in widths)


def test_column_widths_reflect_content_length() -> None:
    rows = [("A", "a much longer heading here"), ("1", "2")]
    narrow, wide = pdf.column_widths(rows, available=500.0)
    assert wide > narrow


def test_money_columns_are_detected_for_right_alignment() -> None:
    rows = [
        ("Ticker", "Bucket", "P&L", "Return"),
        ("ACME", "Core", "+$300.00", "+30.00%"),
        ("WILE", "Active", "-$25.00", "-5.00%"),
    ]
    assert pdf.numeric_columns(rows) == {2, 3}


def test_a_header_only_table_has_no_numeric_columns() -> None:
    assert pdf.numeric_columns([("A", "B")]) == set()


# --------------------------------------------------------------- the story

def texts(story) -> list[str]:
    """Every string in a flowable tree, for asserting on document content."""
    found: list[str] = []
    for item in story:
        if isinstance(item, Paragraph):
            found.append(item.text)
        elif isinstance(item, KeepTogether):
            found.extend(texts(item._content))
        elif isinstance(item, Table):
            for row in item._cellvalues:
                found.extend(texts(row))
    return found


def test_a_paper_report_is_stamped_paper_in_the_body() -> None:
    story = pdf.build_story(build_markdown(journal.PAPER), paper=True)
    assert pdf.PAPER_BANNER in texts(story)[0]


def test_a_real_report_carries_no_paper_stamp_anywhere() -> None:
    story = pdf.build_story(build_markdown(journal.REAL), paper=False)
    body = " ".join(texts(story))
    assert "PAPER" not in body
    assert "SIMULATED" not in body


def test_the_story_carries_the_numbers_and_the_benchmark() -> None:
    story = pdf.build_story(build_markdown(), paper=False)
    body = " ".join(texts(story))

    assert "Track Record" in body
    assert "SPY" in body
    assert "Account" in body
    assert "By bucket" in body
    assert "Open positions" in body
    assert "Closed trades" in body


def test_the_story_carries_every_timestamped_thesis() -> None:
    """The theses are the point of the document — they must survive the render."""
    story = pdf.build_story(build_markdown(), paper=False)
    body = " ".join(texts(story))

    assert "2026-07-10T14:00:00+00:00" in body  # opened_at, as recorded
    assert "Invalidation:" in body
    assert "Exit plan:" in body
    assert "Re-rated on the services print" in body  # the closed trade's outcome
    assert "ACME" in body and "WILE" in body  # closed and open


def test_each_trade_log_entry_is_kept_on_one_page() -> None:
    story = pdf.build_story(build_markdown(), paper=False)
    groups = [item for item in story if isinstance(item, KeepTogether)]
    assert groups, "trade-log entries should be grouped so they do not split"
    grouped = " ".join(texts(groups))
    assert "Thesis:" in grouped


def test_tables_become_reportlab_tables() -> None:
    story = pdf.build_story(build_markdown(), paper=False)
    tables = [item for item in story if isinstance(item, Table)]
    assert len(tables) >= 4  # account, buckets, open, closed, stats


# ---------------------------------------------------------------- the file

def test_render_pdf_writes_a_valid_pdf(tmp_path) -> None:
    path = pdf.render_pdf(
        build_markdown(),
        tmp_path / "track.pdf",
        paper=False,
        title="Track Record — REAL money",
        generated=date(2026, 7, 20),
    )
    data = path.read_bytes()

    assert path.exists()
    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")
    assert len(data) > 3_000


def test_render_pdf_creates_the_directory_if_needed(tmp_path) -> None:
    path = pdf.render_pdf(
        build_markdown(), tmp_path / "nested" / "deep" / "t.pdf", paper=True
    )
    assert path.exists()


def test_a_paper_pdf_renders_too(tmp_path) -> None:
    path = pdf.render_pdf(build_markdown(journal.PAPER), tmp_path / "p.pdf", paper=True)
    assert path.read_bytes().startswith(b"%PDF-")


def test_an_empty_book_still_renders(tmp_path) -> None:
    report = track.build_report(
        journal.REAL, [deposit(1000.0, "2026-07-01")], [], {}, SPY, as_of=d("2026-07-20")
    )
    path = pdf.render_pdf(
        track.render_markdown(report), tmp_path / "empty.pdf", paper=False
    )
    assert path.read_bytes().startswith(b"%PDF-")
