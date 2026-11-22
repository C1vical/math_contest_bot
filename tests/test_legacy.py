"""Import of the pre-2.0 ``math_problems`` table (synthetic rows shaped like the old scrapers wrote them)."""

from __future__ import annotations

import sqlite3

import pytest

from mathbot.legacy import LEGACY_MARKER, import_legacy, resolve_legacy_contest, strip_legacy_title
from mathbot.models import EditionRef, ParsedProblem
from tests.conftest import store

ROWS = [
    # AoPS Wiki rows: the old scraper prepended "<h2>{year} {wiki title} Problem {n}</h2>"
    ("2024_AMC10A_5", 2024, "AMC10A", 5, "<h2>2024 AMC 10A Problem 5</h2><p>What is $2+2$ exactly?</p>", "0"),
    ("2010_AIME1_3", 2010, "AIME1", 3, "<h2>2010 AIME I Problem 3</h2><p>Find the number of pairs.</p>", "0"),
    ("1999_USAMO_1", 1999, "USAMO", 1, "<h2>1999 USAMO Problem 1</h2><p>Prove the inequality holds.</p>", "0"),
    # failed fetch: the old loader stored only the title
    ("2023_AMC10B_7", 2023, "AMC10B", 7, "<h2>2023 AMC 10B Problem 7</h2>", "0"),
    # CEMC rows: "<h2>Problem {n}</h2>", contest names as the old URL manifest spelled them
    ("2019_Euclid_2", 2019, "Euclid", 2, "<h2>Problem 2</h2><p>Determine all values of \\(x\\).</p>", "0"),
    ("2019_Gauss7_1", 2019, "Gauss7", 1, "<h2>Problem 1</h2><p>Which number is largest here?</p>", "0"),
    # community rows stored year and number as text
    ("2025_CMO_2", "2025", "CMO", "2", "<h2>2025 CMO Problem 2</h2><p>Let ABC be a triangle with...</p>", "0"),
    ("2020_Putnam_1", 2020, "Putnam", 1, "<h2>2020 Putnam Problem 1</h2><p>Out of scope.</p>", "0"),
]


def legacy_db(path, rows=ROWS):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE math_problems (id TEXT PRIMARY KEY, year INTEGER NOT NULL, contest TEXT NOT NULL,"
            " question_number INTEGER NOT NULL, question_statement TEXT NOT NULL, answer TEXT NOT NULL)"
        )
        conn.executemany("INSERT INTO math_problems VALUES (?, ?, ?, ?, ?, ?)", rows)
    return path


def test_strip_legacy_title():
    assert strip_legacy_title("<h2>2024 AMC 10A Problem 5</h2><p>x</p>") == "<p>x</p>"
    assert strip_legacy_title("  <h2>Problem 12</h2>\n<p>x</p>") == "<p>x</p>"
    assert strip_legacy_title("<h2>Part A</h2><p>x</p>") == "<h2>Part A</h2><p>x</p>"  # not a title: kept


def test_old_contest_names_map_to_series(registry):
    expected = {"AMC10A": "AMC10A", "AIME1": "AIME1", "Euclid": "EUCLID", "Gauss7": "GAUSS7", "CMO": "CMO"}
    for old, key in expected.items():
        assert resolve_legacy_contest(registry, old).key == key
    assert resolve_legacy_contest(registry, "AMC10FALLA").key == "AMC10FALLA"
    assert resolve_legacy_contest(registry, "Putnam") is None


def test_import_maps_cleans_and_reports(repo, registry, tmp_path):
    report = import_legacy(repo, registry, legacy_db(tmp_path / "old.db"))

    assert report.read == 8 and report.outcomes["inserted"] == 5
    assert report.unmapped == {"Putnam": 1}
    assert report.skipped_disabled == {"GAUSS7": 1}
    assert [old for old, _ in report.invalid] == ["2023_AMC10B_7"]  # title only: never stored
    assert report.answers_ignored == 8

    amc = repo.get_problem("AMC10A", 2024, 5)
    assert amc.statement_html == "<p>What is $2+2$ exactly?</p>"  # injected title removed
    assert amc.answer is None  # the old "0" was never a real answer
    assert amc.source_url == "https://artofproblemsolving.com/wiki/index.php/2024_AMC_10A_Problems/Problem_5"
    euclid = repo.get_problem("EUCLID", 2019, 2)
    assert euclid.source_url.endswith("/2019EuclidContest.html")  # from the URL manifest
    assert repo.get_problem("CMO", 2025, 2).source_url.endswith("/c4254835")  # category id from ids manifest

    record = repo.conn.execute("SELECT * FROM source_records WHERE problem_id = ?", (amc.id,)).fetchone()
    assert record["source_identifier"] == f"{LEGACY_MARKER}2024_AMC10A_5"
    assert record["raw_payload"].startswith("<h2>2024 AMC 10A Problem 5</h2>")  # original kept as provenance
    origin = repo.conn.execute("SELECT origin FROM contest_editions WHERE id = 'amc10a-2024'").fetchone()[0]
    assert origin == "legacy_import"
    assert "UNMAPPED" in report.format()


def test_import_is_idempotent_and_dry_run_writes_nothing(repo, registry, tmp_path):
    path = legacy_db(tmp_path / "old.db")
    preview = import_legacy(repo, registry, path, dry_run=True)
    assert preview.outcomes["inserted"] == 5 and repo.totals()["problems"] == 0

    import_legacy(repo, registry, path)
    again = import_legacy(repo, registry, path)
    assert again.outcomes["unchanged"] == 5 and again.outcomes["inserted"] == 0
    assert repo.totals()["problems"] == 5


def test_disabled_contests_only_with_flag(repo, registry, tmp_path):
    report = import_legacy(repo, registry, legacy_db(tmp_path / "old.db"), include_disabled=True)
    assert report.skipped_disabled == {} and repo.get_problem("GAUSS7", 2019, 1) is not None


def test_live_problems_are_never_overwritten_by_legacy_rows(repo, registry, tmp_path):
    live_ref = EditionRef("AMC10A", 2024, "2024 AMC 10A Problems", "https://example.org/e", "2024_AMC_10A_Problems", 25)
    store(repo, live_ref, ParsedProblem(5, "<p>The live statement of problem five.</p>", "https://x", raw_payload="r"))

    report = import_legacy(repo, registry, legacy_db(tmp_path / "old.db"))
    assert report.skipped_live == 1
    assert "live statement" in repo.get_problem("AMC10A", 2024, 5).statement_html
    origin = repo.conn.execute("SELECT origin FROM contest_editions WHERE id = 'amc10a-2024'").fetchone()[0]
    assert origin == "discovered"  # the live edition was not downgraded


def test_live_ingest_later_replaces_a_legacy_statement(repo, registry, tmp_path):
    import_legacy(repo, registry, legacy_db(tmp_path / "old.db"))
    live_ref = EditionRef("AIME1", 2010, "2010 AIME I Problems", "https://example.org/e", "2010_AIME_I_Problems", 15)
    outcome = store(
        repo,
        live_ref,
        ParsedProblem(3, "<p>Find the number of ordered pairs, fully stated.</p>", "https://x", raw_payload="r"),
    )
    assert outcome == "updated"
    problem = repo.get_problem("AIME1", 2010, 3)
    assert problem.version == 2 and "ordered pairs" in problem.statement_html
    # a re-run of the legacy import no longer touches it
    assert import_legacy(repo, registry, legacy_db(tmp_path / "old2.db")).skipped_live == 1


def test_bad_inputs(repo, registry, tmp_path):
    with pytest.raises(FileNotFoundError):
        import_legacy(repo, registry, tmp_path / "nope.db")
    sqlite3.connect(tmp_path / "other.db").execute("CREATE TABLE t (x)").connection.close()
    with pytest.raises(LookupError, match="math_problems"):
        import_legacy(repo, registry, tmp_path / "other.db")
    report = import_legacy(
        repo, registry, legacy_db(tmp_path / "weird.db", [("x", "MMXX", "IMO", 1, "<p>Statement.</p>", "")])
    )
    assert report.invalid == [("x", "year or problem number is not an integer")]


def test_legacy_aops_statements_lose_leaked_solutions(repo, registry, tmp_path):
    rows = [
        (
            "1976_IMO_4",
            1976,
            "IMO",
            4,
            "<h2>1976 IMO Problem 4</h2><p>Determine the greatest product of positive integers summing to 1976.</p>"
            '<h3><span class="mw-headline" id="Solution_1">Solution 1</span></h3><p>Use as many 3s as possible.</p>',
            "0",
        ),
        (
            "1956_AHSME_47",
            1956,
            "AHSME",
            47,
            "<p>How many days would it take with one machine?</p><p>Solution\n</p>",
            "0",
        ),
    ]
    import_legacy(repo, registry, legacy_db(tmp_path / "old.db", rows))
    imo = repo.get_problem("IMO", 1976, 4)
    assert "greatest product" in imo.statement_html and "3s" not in imo.statement_html
    assert "Solution" not in repo.get_problem("AHSME", 1956, 47).statement_text
