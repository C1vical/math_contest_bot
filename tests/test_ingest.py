"""Integration tests: adapters + normalization + database, entirely from local fixtures."""

from __future__ import annotations

import time

import pytest

from mathbot.contests import AOPS_COMMUNITY, AOPS_WIKI, CEMC
from mathbot.http import HttpStatusError, SourceBlockedError
from mathbot.ingest import Ingestor
from mathbot.models import EditionRef, EditionResult, ParsedProblem, ProblemFailure
from mathbot.sources.aops_community import AopsCommunityAdapter
from mathbot.sources.aops_wiki import API_URL, AopsWikiAdapter
from mathbot.sources.cemc import CemcAdapter
from tests.conftest import FixtureClient
from tests.test_aops_community import community_route
from tests.test_aops_wiki import wiki_route
from tests.test_cemc import cemc_route


def fixture_adapters():
    return {
        CEMC: CemcAdapter(FixtureClient(cemc_route)),
        AOPS_WIKI: AopsWikiAdapter(FixtureClient(wiki_route())),
        AOPS_COMMUNITY: AopsCommunityAdapter(FixtureClient(community_route)),
    }


class StubAdapter:
    """Scriptable adapter for pipeline behaviour that does not depend on a parser."""

    provider = AOPS_WIKI

    def __init__(self, statements: dict[int, dict[int, str]], fail_years=(), discover_error=None):
        self.statements = statements  # year -> number -> html
        self.fail_years = set(fail_years)
        self.discover_error = discover_error
        self.fetched: list[int] = []

    def discover(self, series):
        if self.discover_error:
            raise self.discover_error
        return [
            EditionRef(series.key, year, f"{year} {series.name}", f"https://example.org/{year}", str(year), None)
            for year in sorted(self.statements)
        ]

    def fetch_edition(self, series, edition, *, known=None, only_numbers=None):
        self.fetched.append(edition.year)
        if edition.year in self.fail_years:
            raise HttpStatusError("HTTP 500", url=edition.source_url, status=500)
        result = EditionResult(edition=edition, source_hash=f"hash-{edition.year}")
        for number, html in self.statements[edition.year].items():
            if only_numbers is not None and number not in only_numbers:
                continue
            if html is None:
                result.failures.append(ProblemFailure(number, edition.source_url, "fetch", "HTTP 404"))
                continue
            result.problems.append(
                ParsedProblem(number, html, f"{edition.source_url}/{number}", raw_payload=html, source_revision="1")
            )
        return result


def test_cemc_ingest_end_to_end_from_fixtures(repo, registry):
    ingestor = Ingestor(repo, fixture_adapters())
    report = ingestor.run([registry.get("EUCLID")], years=frozenset({2016}))

    assert report.ok and report.status == "completed"
    assert report.counters() == {
        "discovered": 10,
        "inserted": 10,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "invalid": 0,
    }
    problem = repo.get_problem("EUCLID", 2016, 8)
    assert problem.id == "euclid-2016-p08" and "Determine all real numbers" in problem.statement_html
    assert problem.source_url.endswith("/2016/2016EuclidContest.html")
    assert problem.label == "2016 Euclid Contest"
    run = repo.last_run()
    assert (run["status"], run["inserted"], run["mode"]) == ("completed", 10, "incremental")


def test_incremental_rerun_skips_unchanged_and_full_refresh_does_not_duplicate(repo, registry):
    ingestor = Ingestor(repo, fixture_adapters())
    series = [registry.get("EUCLID")]
    ingestor.run(series, years=frozenset({2016}))

    again = ingestor.run(series, years=frozenset({2016}))
    assert again.editions["unchanged"] == 1
    assert (again.totals["inserted"], again.totals["updated"], again.totals["skipped"]) == (0, 0, 10)

    full = ingestor.run(series, years=frozenset({2016}), mode="full")
    assert full.editions["fetched"] == 1 and full.totals["skipped"] == 10 and full.totals["updated"] == 0
    assert repo.totals()["problems"] == 10
    assert repo.conn.execute("SELECT MAX(version) FROM problems").fetchone()[0] == 1


def test_answers_and_sections_are_stored_for_multiple_choice(repo, registry):
    report = Ingestor(repo, fixture_adapters()).run([registry.get("PASCAL")], years=frozenset({2019}))
    assert report.ok and report.totals["inserted"] == 25
    problem = repo.get_problem("PASCAL", 2019, 12)
    assert (problem.answer, problem.section) == ("C", "B")
    assert repo.totals()["with_answers"] == 25


def test_official_solutions_are_stored_versioned_and_linked(repo, registry):
    Ingestor(repo, fixture_adapters()).run([registry.get("EUCLID")], years=frozenset({2016}))
    problem = repo.get_problem("EUCLID", 2016, 3)
    assert problem.solution_url.endswith("/2016/2016EuclidSolution.html")
    solution = repo.get_solution(problem.id)
    assert solution["version"] == 1 and solution["provider"] == "cemc" and len(solution["solution_text"]) > 100
    assert repo.totals()["with_solutions"] == 10
    # an unchanged rerun (full mode re-parses) does not create a new solution version
    Ingestor(repo, fixture_adapters()).run([registry.get("EUCLID")], years=frozenset({2016}), mode="full")
    assert repo.get_solution(problem.id)["version"] == 1


def test_dry_run_reports_but_writes_nothing(repo, registry):
    report = Ingestor(repo, fixture_adapters()).run([registry.get("EUCLID")], years=frozenset({2016}), dry_run=True)
    assert report.totals["inserted"] == 10 and report.run_id is None
    assert repo.totals()["problems"] == 0
    assert repo.conn.execute("SELECT COUNT(*) FROM contest_editions").fetchone()[0] == 0
    assert repo.last_run() is None
    assert "would be inserted" in report.format()


def test_wiki_ingest_records_invalid_and_failed_problems_with_context(repo, registry):
    report = Ingestor(repo, fixture_adapters()).run([registry.get("AMC10A")], years=frozenset({2099}))
    assert not report.ok and report.status == "completed_with_errors"
    assert (report.totals["inserted"], report.totals["invalid"]) == (2, 1)
    assert repo.numbers_for("AMC10A", 2099) == [1, 2]

    error = repo.conn.execute("SELECT * FROM ingestion_errors").fetchone()
    assert (error["provider"], error["series_key"], error["year"], error["problem_number"]) == (
        "aops_wiki",
        "AMC10A",
        2099,
        3,
    )
    assert error["stage"] == "parse" and error["url"].endswith("Problem_3")
    # a partly failed edition is not checkpointed, so the next run retries it
    assert repo.conn.execute("SELECT COUNT(*) FROM ingestion_checkpoints").fetchone()[0] == 0
    statement = repo.get_problem("AMC10A", 2099, 1)
    assert statement.answer == "C" and "$x^2 = 49$" in statement.statement_text


def test_community_ingest_from_fixtures(repo, registry):
    report = Ingestor(repo, fixture_adapters()).run([registry.get("CMO")], years=frozenset({2099}))
    assert report.totals["inserted"] == 3 and report.totals["invalid"] == 3
    problem = repo.get_problem("CMO", 2099, 2)
    assert problem.section == "Day 1" and "sum of its angles" in problem.statement_text
    assert 'src="https://latex.artofproblemsolving.com/' in problem.statement_html


def test_corrected_source_content_updates_the_stored_problem(repo, registry):
    series = registry.get("IMO")
    Ingestor(repo, {AOPS_WIKI: StubAdapter({2020: {1: "<p>Find all x with x = 1.</p>"}})}).run([series])
    report = Ingestor(repo, {AOPS_WIKI: StubAdapter({2020: {1: "<p>Find all x with x = 2.</p>"}})}).run([series])
    assert report.totals["updated"] == 1
    problem = repo.get_problem("IMO", 2020, 1)
    assert problem.version == 2 and "x = 2" in problem.statement_html


def test_empty_statements_are_counted_invalid_and_never_inserted(repo, registry):
    adapter = StubAdapter({2020: {1: "<p>A real statement here.</p>", 2: "<p>  </p>", 3: ""}})
    report = Ingestor(repo, {AOPS_WIKI: adapter}).run([registry.get("IMO")])
    assert (report.totals["inserted"], report.totals["invalid"]) == (1, 2)
    assert repo.numbers_for("IMO", 2020) == [1]
    assert {e.stage for e in report.errors} == {"validate"}


def test_failed_edition_does_not_stop_the_others(repo, registry):
    adapter = StubAdapter(
        {2019: {1: "<p>Problem of 2019.</p>"}, 2020: {1: "<p>Problem of 2020.</p>"}}, fail_years=[2019]
    )
    report = Ingestor(repo, {AOPS_WIKI: adapter}, concurrency=1).run([registry.get("IMO")])
    assert report.editions["failed"] == 1 and report.totals["inserted"] == 1
    assert repo.years_for("IMO") == [2020]
    assert [(e.year, e.stage) for e in report.errors] == [(2019, "fetch")]


def test_blocked_provider_is_reported_once_and_remaining_series_skipped(repo, registry):
    blocked = SourceBlockedError("anti-bot challenge (HTTP 403); not bypassing it", url=API_URL, status=403)
    adapter = StubAdapter({}, discover_error=blocked)
    report = Ingestor(repo, {AOPS_WIKI: adapter}).run([registry.get("IMO"), registry.get("USAMO")])
    assert list(report.blocked_providers) == [AOPS_WIKI]
    assert not report.ok and len(report.errors) == 2
    assert "skipped" in report.errors[1].error
    assert "BLOCKED aops_wiki" in report.format()
    assert repo.totals()["problems"] == 0


def test_resume_skips_editions_finished_by_an_interrupted_run(repo, registry):
    series = [registry.get("IMO")]
    statements = {2019: {1: "<p>Problem of 2019.</p>"}, 2020: {1: "<p>Problem of 2020.</p>"}}
    first = StubAdapter(statements, fail_years=[2020])
    Ingestor(repo, {AOPS_WIKI: first}, concurrency=1).run(series)  # 2019 ok, 2020 failed

    second = StubAdapter(statements)
    report = Ingestor(repo, {AOPS_WIKI: second}, concurrency=1).run(series, resume=True)
    assert second.fetched == [2020] and report.editions["resumed"] == 1
    assert repo.years_for("IMO") == [2019, 2020]


def test_single_problem_ingest_does_not_mark_the_edition_complete(repo, registry):
    adapter = StubAdapter({2020: {1: "<p>First problem.</p>", 2: "<p>Second problem.</p>"}})
    ingestor = Ingestor(repo, {AOPS_WIKI: adapter})
    report = ingestor.run([registry.get("IMO")], years=frozenset({2020}), numbers=frozenset({2}))
    assert report.totals["inserted"] == 1 and repo.numbers_for("IMO", 2020) == [2]
    assert repo.known_edition("IMO", 2020).source_hash is None


def test_year_filter_with_no_match_is_an_error_not_a_silent_no_op(repo, registry):
    adapter = StubAdapter({2020: {1: "<p>First problem.</p>"}})
    report = Ingestor(repo, {AOPS_WIKI: adapter}).run([registry.get("IMO")], years=frozenset({1901}))
    assert not report.ok and "no editions found" in report.errors[0].error


def test_discover_only_lists_editions_without_writing(repo, registry):
    found, report = Ingestor(repo, fixture_adapters()).discover([registry.get("EUCLID"), registry.get("AMC10A")])
    assert report.ok
    assert [r.year for r in found["EUCLID"]] == list(range(2016, 2027))
    assert [r.year for r in found["AMC10A"]] == [2002, 2021, 2098, 2099]
    assert repo.conn.execute("SELECT COUNT(*) FROM contest_editions").fetchone()[0] == 0


def test_unknown_mode_is_rejected(repo, registry):
    with pytest.raises(ValueError):
        Ingestor(repo, fixture_adapters()).run([registry.get("EUCLID")], mode="yolo")


def test_ctrl_c_stops_fetching_the_editions_still_queued(repo, registry):
    """Leaving the thread pool normally waits for every queued edition; an interrupt must not."""
    years = list(range(2016, 2021))
    statements = {year: {1: f"<p>Problem of {year}.</p>"} for year in years}

    class SlowAdapter(StubAdapter):
        def fetch_edition(self, series, edition, **options):
            if edition.year != years[0]:
                time.sleep(0.2)  # still busy when the first result interrupts the run
            return super().fetch_edition(series, edition, **options)

    def progress(message):
        if "stored" in message:
            raise KeyboardInterrupt

    adapter = SlowAdapter(statements)
    ingestor = Ingestor(repo, {AOPS_WIKI: adapter}, concurrency=1, progress=progress)
    with pytest.raises(KeyboardInterrupt):
        ingestor.run([registry.get("IMO")])
    assert len(adapter.fetched) <= 2  # the edition stored, plus at most the one already in flight
    assert repo.last_run()["status"] == "failed"


def test_build_adapters_constructs_every_provider():
    from mathbot.sources import build_adapters

    adapters = build_adapters(FixtureClient(lambda url, params: None), refresh=True)
    assert set(adapters) == {CEMC, AOPS_WIKI, AOPS_COMMUNITY}
    assert all(adapter.provider == provider for provider, adapter in adapters.items())


class CrashingAdapter(StubAdapter):
    """An adapter whose parser hits markup it does not expect for one edition."""

    def fetch_edition(self, series, edition, *, known=None, only_numbers=None):
        if edition.year == 2019:
            raise KeyError("to")
        return super().fetch_edition(series, edition, known=known, only_numbers=only_numbers)


def test_unexpected_parser_exception_fails_one_edition_not_the_run(repo, registry):
    adapter = CrashingAdapter({2019: {1: "<p>Problem of 2019.</p>"}, 2020: {1: "<p>Problem of 2020.</p>"}})
    report = Ingestor(repo, {AOPS_WIKI: adapter}, concurrency=1).run([registry.get("IMO")])
    assert report.status == "completed_with_errors"
    assert repo.years_for("IMO") == [2020]
    assert [(e.year, e.stage) for e in report.errors] == [(2019, "parse")]
    assert "KeyError" in report.errors[0].error
