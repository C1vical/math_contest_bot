"""AoPS Community parser tests (hand-written fixtures; see fixtures/README.md)."""

from __future__ import annotations

import json

import pytest

from mathbot.config import HttpSettings
from mathbot.http import PoliteClient, RobotsDisallowedError, SourceBlockedError
from mathbot.models import KnownEdition
from mathbot.sources.aops_community import (
    CATEGORY_URL,
    AopsCommunityAdapter,
    CommunityParseError,
    extract_bootstrap,
    find_category,
    parse_category_page,
    parse_edition_items,
    parse_problem_items,
)
from tests.conftest import FixtureClient, read_fixture
from tests.test_http import AOPS_ROBOTS, FakeSession

URL = CATEGORY_URL.format(category_id=9000002)


def test_bootstrap_json_is_extracted_from_the_page_script():
    data = extract_bootstrap(read_fixture("aops_community", "category_cmo.html"))
    assert data["init_time"] == 1700000000
    with pytest.raises(CommunityParseError):
        extract_bootstrap("<html><body>Just a moment...</body></html>")
    with pytest.raises(CommunityParseError):
        extract_bootstrap("<script>AoPS.bootstrap_data = {broken json;</script>")


def test_category_is_found_by_id_wherever_it_is_nested():
    category = parse_category_page(read_fixture("aops_community", "category_cmo.html"), 3277)
    assert category["category_name"] == "Canada National Olympiad" and len(category["items"]) == 4
    with pytest.raises(CommunityParseError):
        find_category({"a": [{"b": 1}]}, 3277)


def test_edition_folders_take_year_from_label_then_score_and_skip_bad_ids():
    category = parse_category_page(read_fixture("aops_community", "category_cmo.html"), 3277)
    # 2099 from the label, 2098 from item_score; the non-numeric id and the score-0 post are dropped
    assert parse_edition_items(category) == [(2099, 9000002), (2098, 9000001)]


def test_problem_items_handle_labels_sections_missing_fields_and_duplicates():
    category = parse_category_page(read_fixture("aops_community", "category_cmo_2099.html"), 9000002)
    problems, failures = parse_problem_items(category, URL)

    assert [p.number for p in problems] == [1, 2, 5]
    assert all(p.section == "Day 1" for p in problems)
    assert "Find all positive integers" in problems[0].statement_html
    assert problems[0].source_url == "https://artofproblemsolving.com/community/h401p501"
    assert problems[0].source_identifier == "501"
    assert json.loads(problems[0].raw_payload)["item_text"] == "1"  # the raw item is kept as provenance
    assert len(problems[0].source_revision) == 64

    assert sorted((f.number, f.error) for f in failures) == [
        (2, "duplicate or invalid problem number"),
        (3, "item has no post_data.post_rendered"),
        (4, "item has no post_data.post_rendered"),
    ]
    assert all(f.invalid for f in failures)


def test_truncated_category_is_reported_rather_than_silently_partial():
    envelope = json.loads(read_fixture("aops_community", "ajax_envelope.json"))
    category = find_category(envelope, 9000002)  # the old ajax envelope shape is understood too
    problems, failures = parse_problem_items(category, URL)
    assert [p.number for p in problems] == [1]
    assert len(failures) == 1 and "reports 12 items but the page delivered 1" in failures[0].error


# --------------------------------------------------------------------------- adapter (offline)


def community_route(url, params):
    if url == CATEGORY_URL.format(category_id=3277):
        return read_fixture("aops_community", "category_cmo.html")
    if url == URL:
        return read_fixture("aops_community", "category_cmo_2099.html")
    return None


def test_discover_merges_live_folders_with_seed_manifest(registry):
    adapter = AopsCommunityAdapter(FixtureClient(community_route))
    refs = {r.year: r for r in adapter.discover(registry.get("CMO"))}
    assert refs[2099].origin == "discovered" and refs[2099].source_identifier == "9000002"
    assert refs[2025].origin == "fallback" and refs[2025].source_identifier == "4254835"  # from the old ids.json
    assert refs[1969].source_identifier == "5014"
    assert min(refs) == 1969


def test_discover_uses_seed_manifest_when_the_page_is_unparseable(registry):
    adapter = AopsCommunityAdapter(FixtureClient(lambda url, params: "<html>layout changed</html>"))
    refs = adapter.discover(registry.get("CMO"))
    assert refs and all(r.origin == "fallback" for r in refs)


def test_blocked_source_raises(registry):
    blocked = SourceBlockedError("challenge", url=URL, status=403)
    adapter = AopsCommunityAdapter(FixtureClient(lambda url, params: blocked))
    with pytest.raises(SourceBlockedError):
        adapter.discover(registry.get("CMO"))


def test_fetch_edition_and_incremental_skip(registry):
    adapter = AopsCommunityAdapter(FixtureClient(community_route))
    series = registry.get("CMO")
    edition = next(r for r in adapter.discover(series) if r.year == 2099)
    result = adapter.fetch_edition(series, edition)
    assert [p.number for p in result.problems] == [1, 2, 5] and len(result.failures) == 3
    assert adapter.fetch_edition(series, edition, only_numbers=frozenset({5})).problems[0].number == 5

    again = adapter.fetch_edition(series, edition, known=KnownEdition(source_hash=result.source_hash))
    assert again.unchanged and again.problems == []


def test_unparseable_edition_page_is_an_invalid_edition_not_zero_problems(registry):
    adapter = AopsCommunityAdapter(FixtureClient(lambda url, params: "<html>nothing here</html>"))
    series = registry.get("CMO")
    edition = adapter.discover(series)[0]
    result = adapter.fetch_edition(series, edition)
    assert result.problems == [] and result.failures[0].invalid and result.failures[0].stage == "parse"


def test_the_old_ajax_endpoint_is_refused_by_robots_enforcement():
    """The pre-2.0 scraper's endpoint is disallowed by AoPS robots.txt; the shared client blocks it."""
    session = FakeSession([], robots=AOPS_ROBOTS)
    client = PoliteClient(HttpSettings(min_interval=0), session=session, sleep=lambda s: None)
    with pytest.raises(RobotsDisallowedError):
        client.get("https://artofproblemsolving.com/m/community/ajax.php")
    assert session.requests == []
