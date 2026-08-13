"""Brief lint: untagged claims and recommendation language.

The linter has to be strict enough to catch real drift and loose enough that a
correct brief passes — a checker that cries wolf on "what they sell" would just
get switched off.
"""

from __future__ import annotations

import pytest

from thesis import lint

# A short but realistic brief that obeys every rule. If the linter flags this,
# the linter is wrong.
CLEAN_BRIEF = """\
## 1. Business — what they sell, to whom, how they make money

Acme designs and sells rocket skates to desert-dwelling predators [10-K 1]. \
Revenue is concentrated in a single customer channel [10-K 1A]. Hardware is sold \
outright, and customers buy replacement thrusters on a subscription [10-K 1].

## 2. Moat

Switching costs are low and the patents expire in 2031 [10-K 1A]. There is no \
durable cost advantage described in the filing [10-K 1].

## 3. Financial snapshot — revenue trend, margins, debt, FCF

- FY2025 revenue: $4.20B [yf financials]
- Operating margin: 30.0% [yf financials]
- Free cash flow: $1.10B against total debt of $300.00M [yf financials]

## 4. Risks

A single distributor accounts for most volume [10-K 1A]. Product recalls have \
followed thruster failures [10-K 1A].

## 5. Valuation

The trailing P/E is 32.0 and the forward P/E is 28.5 [yf valuation]. Peer \
multiples are not in provided data.

## 6. Bull case / Bear case

Bull: subscription attachment keeps expanding and margins hold near 30% \
[yf financials]. Bear: the distributor concentration converts one lost contract \
into a revenue cliff [10-K 1A].

## 7. What would kill this thesis

- Subscription revenue growth below 5% for two consecutive quarters [yf financials]
- The distributor relationship ending [10-K 1A]

## 8. Verdict

A single-customer hardware business trading at a software multiple, where the \
debate hinges on whether subscription attachment is durable [yf valuation].
"""


def test_a_correct_brief_lints_clean() -> None:
    report = lint.lint_brief(CLEAN_BRIEF)
    assert report.ok, report.render()


# ------------------------------------------------------- the planted claim

def test_a_planted_untagged_claim_is_caught() -> None:
    """The core requirement: an untagged factual sentence must not slip through."""
    brief = CLEAN_BRIEF.replace(
        "Switching costs are low and the patents expire in 2031 [10-K 1A].",
        "Switching costs are low and the patents expire in 2031.",
    )
    report = lint.lint_brief(brief)

    assert not report.ok
    assert len(report.missing_sources) == 1
    violation = report.missing_sources[0]
    assert "patents expire in 2031" in violation.text
    assert violation.kind == "missing-source"
    assert "no source tag" in violation.detail


def test_an_untagged_numeric_claim_is_caught() -> None:
    report = lint.lint_brief("Revenue grew 14% in fiscal 2025.")
    assert not report.ok
    assert report.missing_sources[0].detail.endswith("states a figure")


def test_an_untagged_qualitative_fact_is_caught() -> None:
    """No digits, but it still asserts something that needs a source."""
    report = lint.lint_brief("Operating margins expanded across every segment.")
    assert len(report.missing_sources) == 1
    assert "asserts a fact" in report.missing_sources[0].detail


def test_only_the_untagged_sentence_in_a_paragraph_is_flagged() -> None:
    body = (
        "Revenue reached $4.20B in FY2025 [yf financials]. "
        "Margins expanded on the back of the subscription mix."
    )
    report = lint.lint_brief(body)
    assert len(report.missing_sources) == 1
    assert "Margins expanded" in report.missing_sources[0].text


def test_the_escape_hatch_needs_no_tag() -> None:
    report = lint.lint_brief("Peer EV/EBITDA multiples are not in provided data.")
    assert report.ok, report.render()


def test_headings_are_not_required_to_carry_tags() -> None:
    report = lint.lint_brief(
        "## 3. Financial snapshot — revenue trend, margins, debt, FCF\n"
    )
    assert report.ok, report.render()


def test_an_unknown_tag_is_reported_as_such() -> None:
    report = lint.lint_brief("Revenue grew 14% in fiscal 2025 [source: my notes].")
    assert not report.ok
    unknown = report.of_kind("unknown-tag")
    assert len(unknown) == 1
    assert "[source: my notes]" in unknown[0].detail
    assert not report.missing_sources  # reported once, not twice


def test_every_allowed_tag_is_accepted() -> None:
    for tag in (
        "[10-K 1]", "[10-K 1A]", "[10-K 7]", "[yf prices]", "[yf financials]",
        "[yf valuation]", "[yf peers]", "[news: Acme beats estimates]",
    ):
        report = lint.lint_brief(f"Revenue grew 14% in fiscal 2025 {tag}.")
        assert report.ok, f"{tag} rejected: {report.render()}"


# --------------------------------------------------------------- tables

def test_a_table_needs_one_tag_not_one_per_row() -> None:
    body = (
        "Financial snapshot [yf financials]:\n\n"
        "| Year | Revenue |\n"
        "|---|---|\n"
        "| FY2025 | $4.20B |\n"
        "| FY2024 | $3.80B |\n"
    )
    assert lint.lint_brief(body).ok


def test_a_table_with_no_tag_anywhere_is_flagged_once() -> None:
    body = "Financial snapshot:\n\n| Year | Revenue |\n|---|---|\n| FY2025 | $4.20B |\n"
    report = lint.lint_brief(body)
    assert len(report.missing_sources) == 1


# ----------------------------------------------- recommendation language

@pytest.mark.parametrize(
    "sentence",
    [
        "Investors should buy the shares here.",
        "We recommend the name at these levels.",
        "Our price target is $260.",
        "The setup argues for an overweight position.",
        "Sell the stock before the print.",
        "This is a clear buy.",
        "Buy at these levels.",
        "The risk-reward makes this an attractive entry.",
        "You should wait for a pullback.",
        "Margins should improve next year.",
    ],
)
def test_recommendation_language_is_caught(sentence: str) -> None:
    report = lint.lint_brief(f"{sentence} [10-K 7]")
    assert report.recommendations, f"missed: {sentence}"


@pytest.mark.parametrize(
    "sentence",
    [
        "Acme designs and sells rocket skates to enterprises [10-K 1].",
        "Customers buy replacement thrusters on a subscription [10-K 1].",
        "The acquisition will add $2.50B of revenue [yf financials].",
        "Management plans to exit the low-margin distribution segment [10-K 7].",
        "The company holds $1.10B of cash against $300.00M of debt [yf financials].",
        "Cost cuts trim operating expenses by 4% [10-K 7].",
        "The company sells directly and through resellers [10-K 1].",
        "Share buybacks reduced the share count by 3% [yf financials].",
    ],
)
def test_description_is_not_mistaken_for_advice(sentence: str) -> None:
    report = lint.lint_brief(sentence)
    assert report.ok, f"false positive on {sentence!r}: {report.render()}"


def test_a_nested_match_is_reported_once() -> None:
    """'You should' contains 'should' — one construction, one violation."""
    report = lint.lint_brief("You should wait for the print [10-K 7].")
    assert len(report.recommendations) == 1
    assert "advice aimed at the reader" in report.recommendations[0].detail


def test_two_distinct_constructions_are_reported_separately() -> None:
    report = lint.lint_brief("You should own the stock [10-K 7].")
    details = " ".join(v.detail for v in report.recommendations)
    assert len(report.recommendations) == 2
    assert "advice aimed at the reader" in details
    assert "action on the position" in details


def test_recommendation_language_in_a_heading_is_caught() -> None:
    report = lint.lint_brief("## 8. Verdict — Buy\n")
    assert report.recommendations


def test_the_template_heading_about_selling_is_not_a_recommendation() -> None:
    report = lint.lint_brief(
        "## 1. Business — what they sell, to whom, how they make money\n"
    )
    assert report.ok, report.render()


# ------------------------------------------------------ sentence splitting

def test_sentences_do_not_split_on_abbreviations_or_decimals() -> None:
    text = "Revenue was $4.20B in the U.S. market. Margins were 30.0% overall."
    assert lint.split_sentences(text) == [
        "Revenue was $4.20B in the U.S. market.",
        "Margins were 30.0% overall.",
    ]


def test_a_news_headline_containing_a_full_stop_does_not_split_its_tag() -> None:
    """Regression: a headline with its own '. ' used to orphan the tag it lived in."""
    headline = "[news: CXMT Surged 466% After IPO. Here Is What To Know]"
    text = f"A subsidized Chinese rival raised capital {headline} and adds supply."

    assert lint.split_sentences(text) == [text]
    assert lint.lint_brief(text).ok, lint.lint_brief(text).render()


def test_two_real_sentences_still_split_when_a_tag_is_present() -> None:
    text = "Revenue grew 14% [yf financials]. Margins held at 30% [yf financials]."
    assert len(lint.split_sentences(text)) == 2
    assert lint.lint_brief(text).ok


def test_bullets_are_checked_individually() -> None:
    body = (
        "- FY2025 revenue: $4.20B [yf financials]\n"
        "- FY2024 revenue: $3.80B\n"
    )
    report = lint.lint_brief(body)
    assert len(report.missing_sources) == 1
    assert "3.80B" in report.missing_sources[0].text


def test_code_blocks_are_skipped() -> None:
    body = "```\nrevenue = 4.2e9  # no citation needed in code\n```\n"
    assert lint.lint_brief(body).ok


# ------------------------------------------------------- the second pass

def test_the_correction_prompt_names_every_violation() -> None:
    brief = "Revenue grew 14% in fiscal 2025. Investors should buy the shares."
    report = lint.lint_brief(brief)
    prompt = lint.correction_prompt(report)

    assert "Claims without a valid source tag" in prompt
    assert "Revenue grew 14%" in prompt
    assert "Recommendation language" in prompt
    assert "[yf financials]" in prompt  # tells the model which tags are legal
    assert lint.ESCAPE_HATCH in prompt
    assert "Return the corrected brief in full" in prompt


def test_the_report_renders_line_numbers() -> None:
    body = "All tagged [10-K 1].\n\nRevenue grew 14% in fiscal 2025.\n"
    report = lint.lint_brief(body)
    assert report.violations[0].line == 3
    assert "line 3" in report.render()


def test_a_clean_report_renders_cleanly() -> None:
    assert "clean" in lint.LintReport().render()
    assert lint.LintReport().ok
