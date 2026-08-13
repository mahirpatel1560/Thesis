"""Offline tests for brief packet assembly and the lint-and-regenerate loop.

No network, no API — the Claude client is faked so the retry behaviour is
exercised deterministically.
"""

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from thesis import brief, config
from thesis.data import edgar, market
from thesis.data.edgar import Filing, TenK

PRICES = {
    "last_close": 210.5,
    "as_of": "2026-07-15",
    "high_52w": 260.1,
    "low_52w": 169.2,
    "ret_3m": 0.05,
    "ret_1y": -0.02,
    "ret_period": 0.11,
    "period_start": "2024-07-15",
}

FUNDAMENTALS = {
    "snapshot": {
        "name": "Acme Corp",
        "sector": "Industrials",
        "industry": "Cartoon Logistics",
        "market_cap": 3.1e12,
        "trailing_pe": 32.0,
        "forward_pe": 28.5,
        "price_to_sales": 8.1,
        "ev_to_ebitda": 24.2,
        "gross_margins": 0.46,
        "quarterly_revenue_growth_yoy": 0.215,
    },
    "financials": {
        "revenue_by_year": [{"fy": 2025, "revenue": 4.2e9}],
        "net_income_by_year": [{"fy": 2025, "net_income": 1.0e9}],
        "annual_revenue_growth": 0.082,
        "operating_margin": 0.30,
        "net_margin": 0.24,
        "free_cash_flow": 1.1e9,
        "total_debt": 3.0e8,
        "cash": 1.1e9,
    },
}

NEWS = [{"title": "Acme beats estimates", "publisher": "Reuters", "published": "2026-07-10"}]

TENK = TenK(
    ticker="ACME",
    company="Acme Corp",
    cik=123,
    filing=Filing(
        form="10-K",
        accession_number="0000123-25-000001",
        primary_document="acme.htm",
        filing_date="2025-11-01",
        report_date="2025-09-30",
    ),
    items={"1": "We sell rocket skates.", "1A": "Single customer risk.", "7": "Revenue grew 14%."},
)


def _packet(peers: list[dict] | None = None) -> str:
    return brief.build_packet("ACME", PRICES, FUNDAMENTALS, NEWS, TENK, peers or [])


def test_packet_contains_tagged_sections() -> None:
    packet = _packet()
    for marker in (
        "[yf prices]",
        "[yf financials]",
        "[yf valuation]",
        "ITEM 1 — Business [10-K 1]",
        "ITEM 1A — Risk Factors [10-K 1A]",
        "ITEM 7 — MD&A [10-K 7]",
        "Acme beats estimates",
        "accession 0000123-25-000001",
    ):
        assert marker in packet


def test_packet_formats_numbers() -> None:
    packet = _packet()
    assert "FY2025 revenue: $4.20B" in packet
    assert "Last close: $210.50 (as of 2026-07-15)" in packet
    assert "trailing P/E: 32.0" in packet


def test_packet_labels_quarterly_and_annual_growth_distinctly() -> None:
    """Regression: an ambiguous 'revenue growth (yoy)' label produced a brief
    claiming 21.5% growth when the annual figures implied 8.2%."""
    packet = _packet()

    assert (
        "quarterly revenue growth (latest quarter vs. same quarter a year earlier): 21.5%"
        in packet
    )
    assert (
        "Annual revenue growth (latest full fiscal year vs. the prior one): 8.2%" in packet
    )
    assert "revenue growth (yoy)" not in packet  # the ambiguous label is gone
    assert "It is not an annual rate" in packet  # and the packet says so outright


def test_packet_peers_section_only_when_given() -> None:
    assert "[yf peers]" not in _packet()
    peer = {"ticker": "WILE", "market_cap": 1e12, "trailing_pe": 20.0}
    assert "WILE:" in _packet([peer])


def test_truncate_caps_long_sections() -> None:
    long_text = "line\n" * 20_000
    out = brief._truncate(long_text, 1_000)
    assert len(out) < 1_100
    assert out.endswith("[... section truncated for length ...]")


# ==================================================== the lint-and-retry loop

CLEAN_DRAFT = """\
## 1. Business

Acme sells rocket skates to desert predators [10-K 1].

## 3. Financial snapshot

FY2025 revenue was $4.20B [yf financials].
"""

DIRTY_DRAFT = """\
## 1. Business

Acme sells rocket skates to desert predators [10-K 1].

## 3. Financial snapshot

FY2025 revenue was $4.20B and margins expanded sharply.

## 8. Verdict

Investors should buy the shares here.
"""


class _FakeStream:
    def __init__(self, body: str) -> None:
        self._body = body

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    @property
    def text_stream(self):
        return iter([self._body])

    def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._body)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=200),
        )


class _FakeMessages:
    def __init__(self, bodies: tuple[str, ...]) -> None:
        self.bodies = list(bodies)
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self.bodies:
            raise AssertionError("the model was called more times than expected")
        return _FakeStream(self.bodies.pop(0))


class FakeClient:
    """Returns the given drafts in order, one per call."""

    def __init__(self, *bodies: str) -> None:
        self.messages = _FakeMessages(bodies)


@pytest.fixture
def stub_data(monkeypatch, tmp_path):
    """Every network dependency of `generate`, replaced with the fixtures above."""
    monkeypatch.setattr(market, "get_prices", lambda t, period="2y": pd.DataFrame())
    monkeypatch.setattr(market, "summarize_prices", lambda df: PRICES)
    monkeypatch.setattr(market, "get_fundamentals", lambda t: FUNDAMENTALS)
    monkeypatch.setattr(market, "get_news", lambda t, limit=10: NEWS)
    monkeypatch.setattr(market, "get_valuation_snapshot", lambda t: {"ticker": t})
    monkeypatch.setattr(edgar, "get_10k", lambda t: TENK)
    monkeypatch.setattr(config, "BRIEFS_DIR", tmp_path / "briefs")
    return tmp_path / "briefs"


def _written(briefs_dir) -> str:
    path = briefs_dir / f"ACME_{date.today().isoformat()}.md"
    return path.read_text(encoding="utf-8")


def test_a_clean_first_draft_is_not_regenerated(stub_data) -> None:
    client = FakeClient(CLEAN_DRAFT)
    result = brief.generate("ACME", client=client)

    assert result.attempts == 1
    assert len(client.messages.calls) == 1
    assert result.lint.ok
    assert result.input_tokens == 100 and result.output_tokens == 200
    assert "LINT FAILED" not in _written(stub_data)


def test_a_dirty_draft_is_regenerated_once_and_the_fix_is_accepted(stub_data) -> None:
    client = FakeClient(DIRTY_DRAFT, CLEAN_DRAFT)
    notices: list[str] = []
    result = brief.generate("ACME", client=client, on_notice=notices.append)

    assert result.attempts == 2
    assert result.lint.ok
    assert len(notices) == 1
    assert "regenerating once" in notices[0]

    # The second call carries the first draft plus the correction request.
    second = client.messages.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert second[1]["content"] == DIRTY_DRAFT
    assert "Recommendation language" in second[2]["content"]
    assert "margins expanded" in second[2]["content"].lower()

    # Tokens are the running total across both drafts, not just the last one.
    assert result.input_tokens == 200 and result.output_tokens == 400
    assert "LINT FAILED" not in _written(stub_data)


def test_a_brief_that_stays_dirty_fails_loudly_and_is_stamped(stub_data) -> None:
    client = FakeClient(DIRTY_DRAFT, DIRTY_DRAFT)
    result = brief.generate("ACME", client=client)

    assert result.attempts == 2
    assert len(client.messages.calls) == 2, "it must regenerate once, not forever"
    assert not result.lint.ok
    assert result.lint.missing_sources
    assert result.lint.recommendations

    written = _written(stub_data)
    assert "LINT FAILED — do not treat this brief as sourced" in written
    assert "no source tag" in written
    assert DIRTY_DRAFT in written  # the draft is kept so it can be inspected


def test_the_dry_run_skips_the_model_and_the_lint(stub_data) -> None:
    result = brief.generate("ACME", dry_run=True, client=FakeClient())
    assert result.path.name.endswith(".packet.txt")
    assert result.lint.ok
    assert result.attempts == 1
