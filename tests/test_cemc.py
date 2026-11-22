"""CEMC parser tests against pages captured from the live site (see fixtures/README.md)."""

from __future__ import annotations

import pytest

from mathbot.contests import ContestRegistry
from mathbot.http import HttpStatusError
from mathbot.models import KnownEdition
from mathbot.sources.cemc import (
    LISTING_URL,
    MERGE_INTO_PREVIOUS,
    CemcAdapter,
    CemcParseError,
    parse_answers,
    parse_contest_page,
    parse_listing,
)
from tests.conftest import FixtureClient, read_fixture

DOCS = "https://cemc.uwaterloo.ca/sites/default/files/documents"


def test_listing_rows_carry_title_year_and_real_links():
    rows, has_next = parse_listing(read_fixture("cemc", "listing_euclid_page0.html"))
    assert has_next is True
    assert [r.year for r in rows] == list(range(2026, 2016, -1))
    assert {r.title for r in rows} == {"Euclid"}
    assert rows[0].contest_url == f"{DOCS}/2026/2026EuclidContest.html"
    assert rows[0].solution_url == f"{DOCS}/2026/2026EuclidSolution.html"


def test_listing_last_page_and_empty_page():
    rows, has_next = parse_listing(read_fixture("cemc", "listing_euclid_page1.html"))
    assert [(r.title, r.year) for r in rows] == [("Euclid", 2016)] and has_next is False
    assert parse_listing("<html><body><p>No results</p></body></html>") == ([], False)


def test_listing_link_directory_is_not_derived_from_the_year():
    """2019 Pascal's HTML lives under /2026/; the URL must come from the page, not a pattern."""
    rows, _ = parse_listing(read_fixture("cemc", "listing_pcf_2019.html"))
    by_title = {r.title: r for r in rows}
    assert set(by_title) == {"Pascal", "Cayley", "Fermat"}
    assert by_title["Pascal"].contest_url == f"{DOCS}/2026/2019PascalContest.html"
    assert by_title["Fermat"].contest_url == f"{DOCS}/2019/2019FermatContest.html"


def test_multiple_choice_contest_parts_and_numbering():
    page = parse_contest_page(read_fixture("cemc", "2025CayleyContest.html"))
    assert page.title == "2025 Cayley Contest"
    assert [n for n, _, _ in page.problems] == list(range(1, 26))
    assert [s for _, s, _ in page.problems] == ["A"] * 10 + ["B"] * 10 + ["C"] * 5
    number, section, html = page.problems[0]
    assert "\\(\\dfrac{20+25}{25+20}\\)" in html
    assert html.count("<li>") == 5 and 'class="horizontal"' in html  # the five answer choices
    assert "Instructions" not in "".join(h for _, _, h in page.problems)


def test_broken_2016_euclid_markup_is_detected_and_repaired():
    html = read_fixture("cemc", "2016EuclidContest.html")
    naive = parse_contest_page(html)
    assert naive.declared_count == 10 and len(naive.problems) == 11  # 8(b) surfaces as a stray item

    fixed = parse_contest_page(html, merge_into_previous=MERGE_INTO_PREVIOUS[("EUCLID", 2016)])
    assert [n for n, _, _ in fixed.problems] == list(range(1, 11))
    question8 = fixed.problems[7][2]
    assert "Determine all real numbers" in question8  # part (b) rejoined its question
    assert '<ol start="2" type="a">' in question8
    assert "Determine all real numbers" not in fixed.problems[8][2]


def test_content_spilled_out_of_a_closed_li_stays_with_its_question():
    """2019 Pascal closes <li> early whenever a question has a diagram; the question
    sentence and answer choices must not be lost (the first 2.0 draft dropped them)."""
    page = parse_contest_page(read_fixture("cemc", "2019PascalContest.html"))
    assert [n for n, _, _ in page.problems] == list(range(1, 26))
    question3 = page.problems[2][2]
    assert "some of the hexagons are shaded" in question3
    assert "What fraction of all of the hexagons are shaded?" in question3
    assert 'class="horizontal"' in question3
    multiple_choice = [h for n, _, h in page.problems if n <= 20]
    assert all('class="horizontal"' in h for h in multiple_choice)  # every A/B question keeps its 5 choices
    assert "What fraction of all" not in page.problems[3][2]


def test_unclosed_part_list_with_later_parts_nested_inside():
    """2017 Pascal never closes the Part A list and opens it twice, so Parts B and C
    (headings included) sit inside it. Numbering and sections must still be right,
    and the closing "Further Information" text must not leak into question 25."""
    page = parse_contest_page(read_fixture("cemc", "2017PascalContest.html"))
    assert [n for n, _, _ in page.problems] == list(range(1, 26))
    assert "".join(s for _, s, _ in page.problems) == "A" * 10 + "B" * 10 + "C" * 5
    assert not any("Further Information" in h or "Thank you for writing" in h for _, _, h in page.problems)
    assert 'class="tabbing"' in page.problems[4][2]  # question 5 uses the older choice layout


def test_official_solutions_one_per_question():
    from mathbot.sources.cemc import parse_solutions

    euclid = parse_solutions(read_fixture("cemc", "2016EuclidSolution.html"))
    assert sorted(euclid) == list(range(1, 11))
    assert all(len(text) > 200 for text in euclid.values())
    pascal = parse_solutions(read_fixture("cemc", "2019PascalSolution.html"))
    assert sorted(pascal) == list(range(1, 26)) and "Answer: (D)" in pascal[1]


def test_solutions_are_dropped_when_the_page_does_not_match_the_questions(registry):
    series = registry.get("EUCLID")

    def route(url, params):
        if url.endswith("2016EuclidSolution.html"):
            return "<html><body><ol><li><p>Only one solution here.</p></li></ol></body></html>"
        return cemc_route(url, params)

    adapter = CemcAdapter(FixtureClient(route))
    edition = adapter.discover(series)[0]
    result = adapter.fetch_edition(series, edition)
    assert len(result.problems) == 10 and all(p.solution_html is None for p in result.problems)
    assert [f.error for f in result.failures] == [
        "solutions page has 1 entries for 10 questions; solutions and answers not stored"
    ]
    assert all(f.warning for f in result.failures)  # statements are still complete


def test_page_without_questions_is_an_error_not_an_empty_result():
    with pytest.raises(CemcParseError):
        parse_contest_page("<html><body><h1>Page not found</h1><ol><li>Home</li></ol></body></html>")
    with pytest.raises(CemcParseError):
        parse_contest_page("<html><body><h2>Questions</h2><p>Coming soon.</p></body></html>")


def test_legacy_layout_without_headings_falls_back_to_list_after_infobox():
    page = parse_contest_page(
        '<html><body><ol><li>instruction</li></ol><div class="infobox">Note</div>'
        "<ol><li><p>First question?</p></li><li><p>Second question?</p></li></ol></body></html>"
    )
    assert [(n, h) for n, _, h in page.problems] == [(1, "<p>First question?</p>"), (2, "<p>Second question?</p>")]


def test_answer_key_from_solutions_page():
    answers = parse_answers(read_fixture("cemc", "2019PascalSolution.html"))
    assert len(answers) == 25
    assert (answers[1], answers[2], answers[25]) == ("D", "E", "E")  # 25 has a trailing paragraph after its answer
    assert set(answers.values()) <= set("ABCDE")
    assert parse_answers("<html><body><ol><li><p>A full written solution.</p></li></ol></body></html>") == {}


# --------------------------------------------------------------------------- adapter (offline)


def cemc_route(url, params):
    if url == LISTING_URL:
        category, page = params["contest_category"], params["page"]
        if category == 24:
            return read_fixture("cemc", f"listing_euclid_page{page}.html") if page in (0, 1) else "<html></html>"
        if category == 14 and page == 0:
            return read_fixture("cemc", "listing_pcf_2019.html")
        if category == 25 and page == 0:
            return "<html><body><table><tbody></tbody></table></body></html>"
        return "<html></html>"
    name = url.rsplit("/", 1)[-1]
    if name in {
        "2016EuclidContest.html",
        "2016EuclidSolution.html",
        "2016GaloisContest.html",
        "2019PascalContest.html",
        "2019PascalSolution.html",
    }:
        return read_fixture("cemc", name)
    return None  # 404


@pytest.fixture
def adapter():
    return CemcAdapter(FixtureClient(cemc_route))


def test_discover_follows_pagination_and_filters_by_title(adapter, registry):
    euclid = adapter.discover(registry.get("EUCLID"))
    assert [e.year for e in euclid] == list(range(2016, 2027))
    assert all(e.origin == "discovered" for e in euclid)
    assert euclid[0].source_url == f"{DOCS}/2016/2016EuclidContest.html"
    assert euclid[0].expected_problems == 10
    listing_calls = [c for c in adapter.client.calls if c.startswith(LISTING_URL)]
    assert len(listing_calls) == 2  # two pages, fetched once

    pascal = {e.year: e for e in adapter.discover(registry.get("PASCAL"))}
    assert pascal[2019].origin == "discovered"
    assert pascal[2019].source_url == f"{DOCS}/2026/2019PascalContest.html"
    assert pascal[2019].extra["solution_url"] == f"{DOCS}/2019/2019PascalSolution.html"


def test_discover_fills_gaps_from_seed_manifest_and_marks_them(adapter, registry):
    """Years or names missing from the listing can no longer leave variables unbound."""
    fryer = adapter.discover(registry.get("FRYER"))  # listing returns no rows at all
    assert fryer and all(e.origin == "fallback" for e in fryer)
    assert fryer[0].source_url.endswith("FryerContest.html")


def test_gauss_is_out_of_scope_unless_enabled():
    assert ContestRegistry().resolve("GAUSS7") is None
    assert ContestRegistry(enable=["GAUSS7"]).resolve("GAUSS7").source["category_id"] == 13


def test_fetch_edition_parses_validates_and_attaches_answers(adapter, registry):
    series = registry.get("PASCAL")
    edition = {e.year: e for e in adapter.discover(series)}[2019]
    result = adapter.fetch_edition(series, edition)
    assert result.failures == [] and len(result.problems) == 25
    first = result.problems[0]
    assert (first.number, first.section, first.answer) == (1, "A", "D")
    assert first.answer_source == f"{DOCS}/2019/2019PascalSolution.html"
    assert first.source_url == edition.source_url and result.source_hash.startswith(first.source_revision)


def test_fetch_edition_applies_known_layout_fix(adapter, registry):
    series = registry.get("EUCLID")
    edition = adapter.discover(series)[0]
    result = adapter.fetch_edition(series, edition)
    assert [p.number for p in result.problems] == list(range(1, 11)) and not result.failures
    assert all(p.answer is None for p in result.problems)  # written-answer contest: no key ingested
    only = adapter.fetch_edition(series, edition, only_numbers=frozenset({3, 4}))
    assert [p.number for p in only.problems] == [3, 4]


def test_fetch_edition_skips_unchanged_page_in_incremental_mode(adapter, registry):
    series = registry.get("EUCLID")
    edition = adapter.discover(series)[0]
    first = adapter.fetch_edition(series, edition)
    again = adapter.fetch_edition(series, edition, known=KnownEdition(source_hash=first.source_hash))
    assert again.unchanged and again.problems == []
    changed = adapter.fetch_edition(series, edition, known=KnownEdition(source_hash="something else"))
    assert not changed.unchanged and len(changed.problems) == 10


def test_wrong_declared_count_on_page_is_tolerated_when_registry_agrees(registry):
    """2016 Galois says 'Number of Questions: 10' but has 4, which is what FGH contests have."""
    series = registry.get("GALOIS")
    adapter = CemcAdapter(FixtureClient(cemc_route))
    edition = next(e for e in adapter.discover(series) if e.year == 2016)
    result = adapter.fetch_edition(series, edition)
    assert [p.number for p in result.problems] == [1, 2, 3, 4] and not result.failures


def test_count_mismatch_rejects_the_whole_edition(registry):
    series = registry.get("EUCLID")  # expects 10; the Cayley page has 25
    adapter = CemcAdapter(FixtureClient(lambda url, params: read_fixture("cemc", "2025CayleyContest.html")))
    edition = adapter.discover(series)[0]
    result = adapter.fetch_edition(series, edition)
    assert result.problems == []
    assert len(result.failures) == 1 and result.failures[0].invalid
    assert "questions but expected [10]" in result.failures[0].error


def test_http_failure_propagates_instead_of_yielding_empty_content(adapter, registry):
    series = registry.get("EUCLID")
    edition = adapter.discover(series)[-1]  # 2026 page is not in the fixture route -> 404
    with pytest.raises(HttpStatusError):
        adapter.fetch_edition(series, edition)


def test_solutions_page_with_the_list_opened_twice():
    """2018 Pascal's solutions page is ``<ol><ol><li>...``; answers and solutions were both lost."""
    from mathbot.sources.cemc import parse_solutions

    html = read_fixture("cemc", "2018PascalSolution.html")
    assert sorted(parse_solutions(html)) == list(range(1, 26))
    answers = parse_answers(html)
    assert len(answers) == 25 and set(answers.values()) <= set("ABCDE")


# --------------------------------------------------------------------------- CIMC / CSMC


@pytest.mark.parametrize("name", ["2020CIMC.html", "2018CSMC.html"])
def test_cimc_csmc_part_b_numbering_restarts_within_the_part(name):
    """Part B is split into lists numbered within the part (<ol>, <ol start="2">, <ol start="3">,
    or two items then <ol start="3">); they are questions 7, 8 and 9, not 2 and 3 again."""
    page = parse_contest_page(read_fixture("cemc", name))
    assert [n for n, _, _ in page.problems] == list(range(1, 10))
    assert [s for _, s, _ in page.problems] == ["A"] * 6 + ["B"] * 3
    assert all('type="a"' in html for _, _, html in page.problems[6:])  # each Part B question has parts


def test_relative_start_never_changes_absolute_pcf_numbering():
    page = parse_contest_page(read_fixture("cemc", "2017PascalContest.html"))
    assert [n for n, _, _ in page.problems] == list(range(1, 26))


def test_cimc_and_csmc_are_registered_as_one_family():
    registry = ContestRegistry()
    assert [s.key for s in registry.select("CSIMC")] == ["CIMC", "CSMC"]
    cimc = registry.resolve("canadian intermediate mathematics contest")
    assert cimc.key == "CIMC" and cimc.source["category_id"] == 23 and cimc.expected_problems(2025) == 9
    assert registry.resolve("Canadian Senior").key == "CSMC"


def test_discover_cimc_and_csmc_from_the_shared_listing(registry):
    def route(url, params):
        if url == LISTING_URL and params["contest_category"] == 23 and params["page"] == 0:
            return read_fixture("cemc", "listing_csimc_page0.html")
        return "<html></html>" if url == LISTING_URL else None

    adapter = CemcAdapter(FixtureClient(route))
    cimc = {e.year: e for e in adapter.discover(registry.get("CIMC"))}
    csmc = {e.year: e for e in adapter.discover(registry.get("CSMC"))}
    assert set(cimc) == set(csmc) == {2021, 2022, 2023, 2024, 2025}
    assert cimc[2025].source_url == f"{DOCS}/2026/2025CIMC.html"
    assert cimc[2025].extra["solution_url"] == f"{DOCS}/2026/2025CIMCSolution.html"
    assert cimc[2025].label == "2025 Canadian Intermediate Mathematics Contest"  # no "Contest Contest"
    assert csmc[2023].expected_problems == 9


def test_answer_key_in_the_2024_smallcaps_format():
    """Since 2024 the label is ``<span class="smallcaps">Answer</span>: (B)``, whose text is
    "Answer : (B)". Pascal/Cayley/Fermat 2024-2026 had solutions but no answers."""
    answers = parse_answers(read_fixture("cemc", "2024PascalSolution_trimmed.html"))
    assert answers == {1: "B", 2: "D", 3: "C"}


def test_answers_are_dropped_with_the_solutions_when_the_page_does_not_match(registry):
    series = registry.get("PASCAL")

    def route(url, params):
        if url.endswith("2019PascalSolution.html"):  # three items for 25 questions
            return read_fixture("cemc", "2024PascalSolution_trimmed.html")
        return cemc_route(url, params)

    adapter = CemcAdapter(FixtureClient(route))
    edition = {e.year: e for e in adapter.discover(series)}[2019]
    result = adapter.fetch_edition(series, edition)
    assert len(result.problems) == 25
    assert all(p.answer is None and p.solution_html is None for p in result.problems)


def test_a_parser_change_reparses_an_unchanged_page(adapter, registry):
    """The edition fingerprint includes PARSER_VERSION, so a stored hash from an older
    parser does not make an incremental run skip the page."""
    from mathbot.sources import cemc

    series = registry.get("EUCLID")
    edition = adapter.discover(series)[0]
    current = adapter.fetch_edition(series, edition).source_hash
    page_hash = current.split("+", 1)[0]
    older = KnownEdition(source_hash=f"{page_hash}+parser{cemc.PARSER_VERSION - 1}")
    assert not adapter.fetch_edition(series, edition, known=older).unchanged
    assert not adapter.fetch_edition(series, edition, known=KnownEdition(source_hash=page_hash)).unchanged


def test_numeric_part_c_answers_written_as_tex():
    page = (
        "<html><body><ol>"
        r'<li><p>Work.</p><p><span class="smallcaps">Answer</span>: <span class="math inline">\(11\)</span></p></li>'
        r'<li><p>Work.</p><p><span class="smallcaps">Answer</span>: <span class="math inline">\(-86\)</span>.</p></li>'
        "<li><p>Work.</p><p>Answer: 2023</p></li>"
        "</ol></body></html>"
    )
    assert parse_answers(page) == {1: "11", 2: "-86", 3: "2023"}
