"""AoPS Wiki parser tests.

The fixtures are hand-written to the documented MediaWiki API format, not
captured from the live site (which was behind an anti-bot challenge). See
fixtures/README.md.
"""

from __future__ import annotations

import json

import pytest

from mathbot.http import HttpStatusError, SourceBlockedError
from mathbot.models import KnownEdition
from mathbot.sources.aops_wiki import (
    API_URL,
    AopsWikiAdapter,
    WikiApiError,
    WikiParseError,
    edition_years,
    extract_statement,
    page_title,
    parse_answer_key,
    parse_payload,
    payload_html,
    payload_links,
    problem_numbers,
)
from tests.conftest import FixtureClient, read_fixture


def load(name: str) -> dict:
    return json.loads(read_fixture("aops_wiki", name))


def test_page_titles():
    assert page_title(2024, "AMC_10A") == "2024_AMC_10A_Problems"
    assert page_title(2021, "Fall_AMC_10A", 7) == "2021_Fall_AMC_10A_Problems/Problem_7"


def test_statement_stops_at_next_heading_not_at_the_word_solution():
    html = payload_html(parse_payload(load("problem_parse_v2.json"), API_URL))
    statement = extract_statement(html)
    assert "How many integer solutions" in statement
    assert "mentions a solution on purpose" in statement  # the old scraper truncated here
    assert 'alt="[asy] draw((0,0)--(1,1)); [/asy]"' in statement
    assert "\\textbf{(E) }49" in statement  # answer choices are part of the statement
    assert "Both" not in statement and "See Also" not in statement  # solution sections excluded
    assert "Contents" not in statement and "redirect to this page" not in statement


def test_statement_with_mediawiki_143_heading_wrapper():
    statement = extract_statement(payload_html(parse_payload(load("problem_parse_mw143.json"), API_URL)))
    assert "Find the remainder when" in statement and 'alt="$3^{10}$"' in statement
    assert "Compute directly" not in statement


def test_formatversion_1_payload_and_page_without_problem_heading():
    statement = extract_statement(payload_html(parse_payload(load("problem_parse_v1_no_heading.json"), API_URL)))
    assert "Prove that for every positive integer" in statement
    assert "consecutive integers" not in statement


def test_page_with_no_statement_is_a_parse_error():
    with pytest.raises(WikiParseError):
        extract_statement(payload_html(parse_payload(load("problem_parse_empty.json"), API_URL)))
    with pytest.raises(WikiParseError):
        payload_html({"text": "   "})


def test_api_error_object_raises():
    with pytest.raises(WikiApiError, match="missingtitle"):
        parse_payload(load("error_missingtitle.json"), API_URL)
    with pytest.raises(WikiApiError):
        parse_payload({"unexpected": True}, API_URL)
    with pytest.raises(WikiApiError):
        parse_payload([], API_URL)


def test_discovery_from_hub_links_ignores_missing_pages_and_other_series():
    links = payload_links(parse_payload(load("hub_links.json"), API_URL))
    assert edition_years(links, "AMC_10A") == [2002, 2021, 2098, 2099]  # 2100 is a red link
    assert edition_years(links, "AMC_10") == [2000, 2001]
    assert edition_years(links, "Fall_AMC_10A") == [2021]
    assert edition_years(links, "AMC_10B") == [2002]


def test_problem_numbers_from_edition_links():
    links = payload_links(parse_payload(load("edition_links.json"), API_URL))
    assert problem_numbers(links, 2099, "AMC_10A") == [1, 2, 3]  # 4 does not exist; 2098 is another edition


def test_answer_key():
    key = parse_answer_key(payload_html(parse_payload(load("answer_key.json"), API_URL)))
    assert key == {1: "C", 2: "A", 3: "E"}
    assert parse_answer_key("<ol><li>See the solutions</li></ol>") == {}


# --------------------------------------------------------------------------- adapter (offline)


def wiki_route(overrides=None):
    overrides = overrides or {}

    def route(url, params):
        assert url == API_URL and params["format"] == "json"
        page = params.get("page")
        if (params["action"], page) in overrides:
            return overrides[(params["action"], page)]
        if params["action"] == "query":
            return read_fixture("aops_wiki", "revisions_query.json")
        if page == "AMC_10_Problems_and_Solutions":
            return read_fixture("aops_wiki", "hub_links.json")
        if page == "2099_AMC_10A_Problems":
            return read_fixture("aops_wiki", "edition_links.json")
        if page == "2099_AMC_10A_Answer_Key":
            return read_fixture("aops_wiki", "answer_key.json")
        if page == "2099_AMC_10A_Problems/Problem_1":
            return read_fixture("aops_wiki", "problem_parse_v2.json")
        if page == "2099_AMC_10A_Problems/Problem_2":
            return read_fixture("aops_wiki", "problem_parse_mw143.json")
        if page == "2099_AMC_10A_Problems/Problem_3":
            return read_fixture("aops_wiki", "problem_parse_empty.json")
        return read_fixture("aops_wiki", "error_missingtitle.json")

    return route


def edition_2099(adapter, registry):
    series = registry.get("AMC10A")
    return series, next(e for e in adapter.discover(series) if e.year == 2099)


def test_discover_uses_the_hub_page(registry):
    adapter = AopsWikiAdapter(FixtureClient(wiki_route()))
    refs = adapter.discover(registry.get("AMC10A"))
    assert [r.year for r in refs] == [2002, 2021, 2098, 2099]
    assert all(r.origin == "discovered" for r in refs)
    assert refs[-1].source_identifier == "2099_AMC_10A_Problems"
    assert refs[-1].source_url == "https://artofproblemsolving.com/wiki/index.php/2099_AMC_10A_Problems"
    # AMC 10B shares the hub page: it is fetched once
    adapter.discover(registry.get("AMC10B"))
    assert sum("AMC_10_Problems_and_Solutions" in call for call in adapter.client.calls) == 1


def test_discover_falls_back_to_registry_years_when_hub_is_missing(registry):
    route = wiki_route(
        {("parse", "AMC_10_Problems_and_Solutions"): read_fixture("aops_wiki", "error_missingtitle.json")}
    )
    refs = AopsWikiAdapter(FixtureClient(route)).discover(registry.get("AMC10"))
    assert [r.year for r in refs] == [2000, 2001] and all(r.origin == "fallback" for r in refs)


def test_blocked_source_is_never_papered_over_with_fallback_data(registry):
    blocked = SourceBlockedError("challenge", url=API_URL, status=403)
    adapter = AopsWikiAdapter(FixtureClient(lambda url, params: blocked))
    with pytest.raises(SourceBlockedError):
        adapter.discover(registry.get("AMC10A"))


def test_fetch_edition_reports_good_and_bad_problems_separately(registry):
    adapter = AopsWikiAdapter(FixtureClient(wiki_route()))
    series, edition = edition_2099(adapter, registry)
    result = adapter.fetch_edition(series, edition)

    assert [p.number for p in result.problems] == [1, 2]
    first = result.problems[0]
    assert first.source_identifier == "2099_AMC_10A_Problems/Problem_1"
    assert first.source_revision == "123456"
    assert first.source_url.endswith("/2099_AMC_10A_Problems/Problem_1")
    assert first.answer == "C" and first.answer_source.endswith("2099_AMC_10A_Answer_Key")
    assert "mw-parser-output" in first.raw_payload  # the full source HTML is kept for provenance

    assert [(f.number, f.stage, f.invalid) for f in result.failures] == [(3, "parse", True)]


def test_fetch_edition_only_selected_numbers(registry):
    adapter = AopsWikiAdapter(FixtureClient(wiki_route()))
    series, edition = edition_2099(adapter, registry)
    result = adapter.fetch_edition(series, edition, only_numbers=frozenset({2}))
    assert [p.number for p in result.problems] == [2] and result.failures == []


def test_incremental_mode_skips_pages_whose_revision_is_unchanged(registry):
    adapter = AopsWikiAdapter(FixtureClient(wiki_route()))
    series, edition = edition_2099(adapter, registry)
    # Problem 3 redirects: its revision is reported under the target page's title.
    known = KnownEdition(revisions={1: "1001", 2: "999", 3: "1003"})
    result = adapter.fetch_edition(series, edition, known=known)
    assert result.skipped_numbers == [1, 3]
    assert [p.number for p in result.problems] == [2]
    assert not any(
        "Problem_1&" in call or call.endswith("Problem_1") for call in adapter.client.calls if "prop=text" in call
    )


def test_missing_problem_page_is_a_failure_not_an_empty_problem(registry):
    route = wiki_route(
        {("parse", "2099_AMC_10A_Problems/Problem_2"): read_fixture("aops_wiki", "error_missingtitle.json")}
    )
    adapter = AopsWikiAdapter(FixtureClient(route))
    series, edition = edition_2099(adapter, registry)
    result = adapter.fetch_edition(series, edition)
    assert [p.number for p in result.problems] == [1]
    assert {(f.number, f.stage) for f in result.failures} == {(2, "fetch"), (3, "parse")}


def test_http_error_on_one_problem_does_not_abort_the_edition(registry):
    error = HttpStatusError("HTTP 500", url=API_URL, status=500)
    adapter = AopsWikiAdapter(FixtureClient(wiki_route({("parse", "2099_AMC_10A_Problems/Problem_1"): error})))
    series, edition = edition_2099(adapter, registry)
    result = adapter.fetch_edition(series, edition)
    assert [p.number for p in result.problems] == [2]
    assert (1, "fetch") in {(f.number, f.stage) for f in result.failures}


def test_expected_count_is_used_when_the_edition_page_lists_nothing(registry):
    route = wiki_route({("parse", "2099_AMC_10A_Problems"): json.dumps({"parse": {"title": "x", "links": []}})})
    adapter = AopsWikiAdapter(FixtureClient(route))
    series, edition = edition_2099(adapter, registry)
    result = adapter.fetch_edition(series, edition)
    assert len(result.problems) + len(result.failures) == 25  # every expected number is accounted for
