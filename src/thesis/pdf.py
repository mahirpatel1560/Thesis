"""Markdown -> PDF for the track record.

The track report is already markdown, and markdown is what gets read on screen.
The PDF is the same document rendered for someone who was handed a file — a
friend, or anyone who wants the record without installing anything.

Rather than a general markdown engine, this renders exactly the constructs
`track.render_markdown` emits: headings, paragraphs, tables, block quotes and
bullet lists, with `**bold**`, `*italic*` and `` `code` `` inline. Everything is
a plain function over text, so the parsing and escaping are testable without
producing a file.

Landscape, because the open-positions table is twelve columns wide and a track
record that needs a magnifying glass does not get read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import LETTER, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

PAGE_SIZE = landscape(LETTER)
MARGIN = 0.5 * inch
BODY_FONT = "Helvetica"
BOLD_FONT = "Helvetica-Bold"

#: Shown on every page of a simulated report. A paper track record that gets
#: separated from its first page must still be obviously paper.
PAPER_BANNER = "PAPER — SIMULATED MONEY — NOT A REAL TRACK RECORD"

# Characters the standard PDF fonts cannot encode, and what to use instead.
_CHAR_SUBSTITUTIONS = {
    "→": "->",
    "←": "<-",
    "↑": "up",
    "↓": "down",
    "≥": ">=",
    "≤": "<=",
    "≈": "~",
    "×": "x",
    "‑": "-",  # non-breaking hyphen
    "−": "-",  # minus sign
    " ": " ",
    "​": "",
}


def sanitize(text: str) -> str:
    """Make text safe for the standard PDF fonts (WinAnsi).

    Em dashes, curly quotes and bullets survive; arrows and maths symbols are
    spelled out; anything else outside the encoding becomes '?' rather than
    blowing up a report at render time.
    """
    for bad, good in _CHAR_SUBSTITUTIONS.items():
        text = text.replace(bad, good)
    return text.encode("cp1252", errors="replace").decode("cp1252")


# --------------------------------------------------------------- block parsing

@dataclass(frozen=True)
class Block:
    kind: str  # "heading" | "para" | "table" | "quote" | "bullet"
    text: str = ""
    level: int = 0
    rows: tuple[tuple[str, ...], ...] = ()


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_TABLE_DIVIDER_RE = re.compile(r"^\|[\s:|-]+\|?$")


def _split_row(line: str) -> tuple[str, ...]:
    cells = line.strip().strip("|").split("|")
    return tuple(cell.strip() for cell in cells)


def parse_markdown(markdown: str) -> list[Block]:
    """Break the report into blocks. Pure."""
    blocks: list[Block] = []
    lines = markdown.splitlines()
    index = 0
    paragraph: list[str] = []

    def flush() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(Block("para", " ".join(paragraph)))
            paragraph = []

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        if not stripped:
            flush()
            index += 1
            continue

        heading = _HEADING_RE.match(stripped)
        if heading:
            flush()
            blocks.append(Block("heading", heading.group(2), level=len(heading.group(1))))
            index += 1
            continue

        if stripped.startswith("|"):
            flush()
            rows: list[tuple[str, ...]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                candidate = lines[index].strip()
                if not _TABLE_DIVIDER_RE.match(candidate):
                    rows.append(_split_row(candidate))
                index += 1
            if rows:
                blocks.append(Block("table", rows=tuple(rows)))
            continue

        if stripped.startswith(">"):
            flush()
            blocks.append(Block("quote", stripped.lstrip("> ").strip()))
            index += 1
            continue

        bullet = _BULLET_RE.match(line)
        if bullet:
            flush()
            blocks.append(Block("bullet", bullet.group(1)))
            index += 1
            continue

        paragraph.append(stripped)
        index += 1

    flush()
    return blocks


# ------------------------------------------------------------ inline formatting

def escape(text: str) -> str:
    """XML-escape before any markup is added — 'P&L' must not break the parser."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+)`")


def inline(text: str) -> str:
    """Markdown inline formatting -> the mini-markup reportlab understands. Pure."""
    out = escape(sanitize(text))
    out = _BOLD_RE.sub(r"<b>\1</b>", out)
    out = _ITALIC_RE.sub(r"<i>\1</i>", out)
    out = _CODE_RE.sub(r'<font face="Courier">\1</font>', out)
    return out


def plain(text: str) -> str:
    """The same text with markup removed, for width measurement. Pure."""
    out = _BOLD_RE.sub(r"\1", sanitize(text))
    out = _ITALIC_RE.sub(r"\1", out)
    return _CODE_RE.sub(r"\1", out)


# ---------------------------------------------------------------------- styles

def _styles() -> dict[str, ParagraphStyle]:
    base = ParagraphStyle(
        "body", fontName=BODY_FONT, fontSize=9, leading=12, spaceAfter=4
    )
    return {
        "title": ParagraphStyle(
            "title", parent=base, fontName=BOLD_FONT, fontSize=18, leading=22, spaceAfter=8
        ),
        "h2": ParagraphStyle(
            "h2", parent=base, fontName=BOLD_FONT, fontSize=13, leading=16,
            spaceBefore=12, spaceAfter=6, textColor=colors.HexColor("#1a3d5c"),
        ),
        "h3": ParagraphStyle(
            "h3", parent=base, fontName=BOLD_FONT, fontSize=10.5, leading=13,
            spaceBefore=9, spaceAfter=3,
        ),
        "body": base,
        "quote": ParagraphStyle(
            "quote", parent=base, leftIndent=12, borderPadding=4,
            textColor=colors.HexColor("#5a3d00"), backColor=colors.HexColor("#fff8e1"),
        ),
        "bullet": ParagraphStyle("bullet", parent=base, leftIndent=14, bulletIndent=4, spaceAfter=2),
        "cell": ParagraphStyle("cell", parent=base, fontSize=7.5, leading=9.5, spaceAfter=0),
        "cellnum": ParagraphStyle(
            "cellnum", parent=base, fontSize=7.5, leading=9.5, spaceAfter=0, alignment=TA_RIGHT
        ),
        "cellhead": ParagraphStyle(
            "cellhead", parent=base, fontName=BOLD_FONT, fontSize=7.5, leading=9.5, spaceAfter=0
        ),
        "cellheadnum": ParagraphStyle(
            "cellheadnum", parent=base, fontName=BOLD_FONT, fontSize=7.5, leading=9.5,
            spaceAfter=0, alignment=TA_RIGHT,
        ),
        "paperbadge": ParagraphStyle(
            "paperbadge", parent=base, fontName=BOLD_FONT, fontSize=12, leading=15,
            alignment=1, textColor=colors.white, backColor=colors.HexColor("#a4176d"),
            borderPadding=6, spaceAfter=10,
        ),
    }


_NUMERIC_RE = re.compile(r"^[+\-]?[$(]?[\d,.]+[%)]?$|^[+\-]?\$[\d,.]+$|pp$|^n/a$|^—$")


def _is_numeric(cell: str) -> bool:
    stripped = plain(cell).strip()
    return bool(stripped) and bool(_NUMERIC_RE.search(stripped))


def numeric_columns(rows: Sequence[Sequence[str]]) -> set[int]:
    """Columns whose body cells are mostly figures — those get right-aligned. Pure.

    Money columns that do not line up on the decimal are the fastest way to make
    a report look untrustworthy.
    """
    if len(rows) < 2:
        return set()
    body = rows[1:]
    columns = max(len(row) for row in rows)
    numeric: set[int] = set()
    for index in range(columns):
        cells = [row[index] for row in body if index < len(row) and row[index].strip()]
        if cells and sum(_is_numeric(c) for c in cells) * 2 > len(cells):
            numeric.add(index)
    return numeric


def column_widths(
    rows: Sequence[Sequence[str]], available: float, font_size: float = 7.5
) -> list[float]:
    """Fit columns to the page: natural width where it fits, squeezed where it doesn't. Pure."""
    if not rows:
        return []
    count = max(len(row) for row in rows)
    padding = 10.0
    natural = [0.0] * count
    for row in rows:
        for i, cell in enumerate(row):
            width = stringWidth(plain(cell), BODY_FONT, font_size) + padding
            natural[i] = max(natural[i], min(width, available * 0.35))
    total = sum(natural) or 1.0
    scale = available / total
    return [width * scale for width in natural]


# ------------------------------------------------------------------- the story

def build_story(markdown: str, *, paper: bool, available: float | None = None) -> list[Any]:
    """Markdown -> reportlab flowables. Separated from file writing so it can be tested."""
    styles = _styles()
    width = available if available is not None else PAGE_SIZE[0] - 2 * MARGIN
    story: list[Any] = []

    if paper:
        story.append(Paragraph(inline(PAPER_BANNER), styles["paperbadge"]))

    blocks = parse_markdown(markdown)
    pending: list[Any] = []  # a trade-log entry, kept on one page

    def emit(flowable: Any, groupable: bool = False) -> None:
        if groupable and pending:
            pending.append(flowable)
        else:
            _close_group()
            story.append(flowable)

    def _close_group() -> None:
        nonlocal pending
        if pending:
            story.append(KeepTogether(pending))
            pending = []

    for block in blocks:
        if block.kind == "heading":
            if block.level == 1:
                _close_group()
                story.append(Paragraph(inline(block.text), styles["title"]))
            elif block.level == 2:
                _close_group()
                story.append(Paragraph(inline(block.text), styles["h2"]))
            else:
                # A trade-log entry: keep its heading with its bullets.
                _close_group()
                pending = [Paragraph(inline(block.text), styles["h3"])]
        elif block.kind == "para":
            emit(Paragraph(inline(block.text), styles["body"]), groupable=True)
        elif block.kind == "quote":
            emit(Paragraph(inline(block.text), styles["quote"]))
        elif block.kind == "bullet":
            emit(
                Paragraph(inline(block.text), styles["bullet"], bulletText="•"),
                groupable=True,
            )
        elif block.kind == "table":
            _close_group()
            story.append(_table(block.rows, width, styles))
            story.append(Spacer(1, 6))

    _close_group()
    return story


def _table(rows: tuple[tuple[str, ...], ...], available: float, styles: dict) -> Table:
    header, *body = rows
    columns = max(len(row) for row in rows)

    def pad(row: Sequence[str]) -> list[str]:
        return list(row) + [""] * (columns - len(row))

    right = numeric_columns([pad(r) for r in rows])
    data = [
        [
            Paragraph(inline(cell), styles["cellheadnum" if i in right else "cellhead"])
            for i, cell in enumerate(pad(header))
        ],
        *[
            [
                Paragraph(inline(cell), styles["cellnum" if i in right else "cell"])
                for i, cell in enumerate(pad(row))
            ]
            for row in body
        ],
    ]
    table = Table(
        data,
        colWidths=column_widths([pad(r) for r in rows], available),
        repeatRows=1,
        hAlign="LEFT",
    )
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8eef3")),
        ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#1a3d5c")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cfd8dd")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    for index in range(1, len(data), 2):
        style.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#f7f9fa")))
    table.setStyle(TableStyle(style))
    return table


# --------------------------------------------------------------- page furniture

@dataclass
class _PageFurniture:
    """Draws the running header and footer on every page."""

    title: str
    paper: bool
    generated: str

    def __call__(self, canvas: Any, doc: Any) -> None:
        canvas.saveState()
        width, height = PAGE_SIZE

        if self.paper:
            canvas.setFillColor(colors.HexColor("#a4176d"))
            canvas.rect(0, height - 0.32 * inch, width, 0.32 * inch, stroke=0, fill=1)
            canvas.setFillColor(colors.white)
            canvas.setFont(BOLD_FONT, 9)
            canvas.drawCentredString(width / 2, height - 0.22 * inch, sanitize(PAPER_BANNER))
        else:
            canvas.setFillColor(colors.HexColor("#8a959c"))
            canvas.setFont(BODY_FONT, 8)
            canvas.drawString(MARGIN, height - 0.28 * inch, sanitize(self.title))

        canvas.setFillColor(colors.HexColor("#8a959c"))
        canvas.setFont(BODY_FONT, 8)
        canvas.drawString(MARGIN, 0.32 * inch, f"Generated {self.generated}")
        footer = PAPER_BANNER if self.paper else "Benchmarked against SPY — same deposits, same dates"
        canvas.drawCentredString(width / 2, 0.32 * inch, sanitize(footer))
        canvas.drawRightString(width - MARGIN, 0.32 * inch, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()


def render_pdf(
    markdown: str,
    path: Path,
    *,
    paper: bool,
    title: str = "Track Record",
    generated: date | None = None,
) -> Path:
    """Write the track report to `path` as a PDF. Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (generated or date.today()).isoformat()
    top = MARGIN + (0.28 * inch if paper else 0.14 * inch)

    document = SimpleDocTemplate(
        str(path),
        pagesize=PAGE_SIZE,
        leftMargin=MARGIN,
        rightMargin=MARGIN,
        topMargin=top,
        bottomMargin=MARGIN + 0.14 * inch,
        title=sanitize(title),
        author="thesis",
        subject="Timestamped trading track record, benchmarked against SPY",
    )
    furniture = _PageFurniture(title=title, paper=paper, generated=stamp)
    document.build(
        build_story(markdown, paper=paper, available=document.width),
        onFirstPage=furniture,
        onLaterPages=furniture,
    )
    return path
