from __future__ import annotations

import sqlite3

import pytest

from mathbot.db import Repository, connect, migrate, open_database
from mathbot.db.repo import INSERTED, UNCHANGED, UPDATED
from mathbot.db.schema import LATEST_VERSION
from mathbot.models import EditionRef, ParsedProblem
from tests.conftest import store

URL = "https://example.org/2024_AMC_10A_Problems/Problem_5"


def edition(key="AMC10A", year=2024) -> EditionRef:
    return EditionRef(key, year, f"{year} {key} Problems", "https://example.org/edition", f"{year}_{key}", 25)


def parsed(number=5, html="<p>What is 2 + 2?</p>", **kw) -> ParsedProblem:
    return ParsedProblem(number=number, statement_html=html, source_url=URL, raw_payload=f"<raw>{html}</raw>", **kw)


def test_open_database_creates_missing_directories_and_migrates(tmp_path):
    path = tmp_path / "does" / "not" / "exist" / "problems.db"
    conn = open_database(path)
    assert path.exists()
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == LATEST_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "contest_series",
        "contest_aliases",
        "contest_editions",
        "problems",
        "problem_versions",
        "source_records",
        "assets",
        "renders",
        "ingestion_runs",
        "ingestion_errors",
        "ingestion_checkpoints",
    } <= tables
    conn.close()


def test_migrate_is_idempotent_and_rejects_newer_databases(tmp_path):
    conn = connect(tmp_path / "p.db")
    assert migrate(conn) == LATEST_VERSION
    assert migrate(conn) == LATEST_VERSION
    conn.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (999, 'x')")
    with pytest.raises(RuntimeError, match="newer"):
        migrate(conn)
    conn.close()


def test_legacy_table_is_left_alone(tmp_path):
    path = tmp_path / "p.db"
    with sqlite3.connect(path) as legacy:
        legacy.execute("CREATE TABLE math_problems (id TEXT PRIMARY KEY, question_statement TEXT)")
        legacy.execute("INSERT INTO math_problems VALUES ('2024_AMC10A_5', '<p>old</p>')")
    conn = open_database(path)
    assert conn.execute("SELECT COUNT(*) FROM math_problems").fetchone()[0] == 1
    conn.close()


def test_registry_sync_populates_series_and_aliases(repo):
    row = repo.conn.execute("SELECT * FROM contest_series WHERE key = 'AMC10A'").fetchone()
    assert (row["id"], row["family"], row["provider"], row["source_name"]) == ("amc10a", "AMC", "aops_wiki", "AMC_10A")
    assert repo.conn.execute("SELECT series_id FROM contest_aliases WHERE alias = 'AIMEII'").fetchone()[0] == "aime2"
    assert repo.conn.execute("SELECT enabled FROM contest_series WHERE key = 'GAUSS7'").fetchone()[0] == 0


def test_insert_then_identical_upsert_is_unchanged(repo):
    ref = edition()
    assert store(repo, ref, parsed()) == INSERTED
    assert store(repo, ref, parsed()) == UNCHANGED
    problem = repo.get_problem("AMC10A", 2024, 5)
    assert (problem.id, problem.version, problem.statement_html) == ("amc10a-2024-p05", 1, "<p>What is 2 + 2?</p>")
    assert repo.conn.execute("SELECT COUNT(*) FROM problem_versions").fetchone()[0] == 1
    assert repo.conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 1


def test_corrected_content_replaces_stale_content_and_keeps_history(repo):
    """The old INSERT OR IGNORE kept stale statements forever; upserts must not."""
    ref = edition()
    store(repo, ref, parsed(html="<p>What is 2 + 2?</p>"))
    assert store(repo, ref, parsed(html="<p>What is 2 + 3?</p>")) == UPDATED

    problem = repo.get_problem("AMC10A", 2024, 5)
    assert problem.statement_html == "<p>What is 2 + 3?</p>" and problem.version == 2
    versions = repo.conn.execute("SELECT version, statement_html FROM problem_versions ORDER BY version").fetchall()
    assert [tuple(v) for v in versions] == [(1, "<p>What is 2 + 2?</p>"), (2, "<p>What is 2 + 3?</p>")]
    # raw payloads of both source versions are preserved separately from the normalized HTML
    raws = [r[0] for r in repo.conn.execute("SELECT raw_payload FROM source_records ORDER BY id")]
    assert raws == ["<raw><p>What is 2 + 2?</p></raw>", "<raw><p>What is 2 + 3?</p></raw>"]


def test_answers_come_only_from_the_source_and_are_never_erased(repo):
    ref = edition()
    store(repo, ref, parsed())
    assert repo.get_problem("AMC10A", 2024, 5).answer is None  # no fake default answer
    assert store(repo, ref, parsed(answer="C", answer_source="https://example.org/key")) == UPDATED
    assert repo.get_problem("AMC10A", 2024, 5).answer == "C"
    assert store(repo, ref, parsed()) == UNCHANGED  # a later fetch without the key keeps it
    assert repo.get_problem("AMC10A", 2024, 5).answer == "C"


def test_constraints_reject_bad_rows(repo):
    ref = edition()
    repo.upsert_edition(ref)
    with pytest.raises(sqlite3.IntegrityError):
        repo.conn.execute(
            "INSERT INTO problems (id, edition_id, number, statement_html, statement_text, content_hash, source_url,"
            " created_at, updated_at) VALUES ('x', 'amc10a-2024', 1, '   ', '', 'h', 'u', 't', 't')"
        )
    with pytest.raises(sqlite3.IntegrityError):  # foreign key
        repo.conn.execute(
            "INSERT INTO problems (id, edition_id, number, statement_html, statement_text, content_hash, source_url,"
            " created_at, updated_at) VALUES ('y', 'nope-1999', 1, '<p>x</p>', 'x', 'h', 'u', 't', 't')"
        )
    store(repo, ref, parsed(number=1))
    with pytest.raises(sqlite3.IntegrityError):  # (edition, number) is unique
        repo.conn.execute(
            "INSERT INTO problems (id, edition_id, number, statement_html, statement_text, content_hash, source_url,"
            " created_at, updated_at) VALUES ('dup', 'amc10a-2024', 1, '<p>x</p>', 'x', 'h', 'u', 't', 't')"
        )


def test_queries_and_random_filters(repo):
    for key, year, number in [("AMC10A", 2024, 1), ("AMC10A", 2024, 2), ("AMC10A", 2023, 1), ("EUCLID", 2024, 1)]:
        store(repo, edition(key, year), parsed(number=number, html=f"<p>{key} {year} #{number} statement</p>"))

    assert repo.get_problem("AMC10A", 1999, 1) is None
    assert repo.years_for("AMC10A") == [2023, 2024]
    assert repo.numbers_for("AMC10A", 2024) == [1, 2]
    assert repo.random_problem(series_keys=["EUCLID"]).series_key == "EUCLID"
    assert repo.random_problem(series_keys=["AMC10A"], year=2023).year == 2023
    assert repo.random_problem(provider="cemc").series_key == "EUCLID"
    assert repo.random_problem(number=2).number == 2
    assert repo.random_problem(series_keys=["IMO"]) is None
    assert repo.random_problem(year=1900) is None
    assert {repo.random_problem().id for _ in range(40)} == {
        "amc10a-2024-p01",
        "amc10a-2024-p02",
        "amc10a-2023-p01",
        "euclid-2024-p01",
    }

    stats = {s.key: s for s in repo.series_stats()}
    assert (stats["AMC10A"].problems, stats["AMC10A"].editions) == (3, 2)
    assert (stats["AMC10A"].first_year, stats["AMC10A"].last_year) == (2023, 2024)
    assert stats["IMO"].problems == 0
    assert repo.totals()["problems"] == 4


def test_known_edition_reports_hash_and_latest_revisions(repo):
    ref = edition()
    store(repo, ref, parsed(number=1, source_revision="100"))
    store(repo, ref, parsed(number=2, source_revision="200"))
    repo.set_edition_source_hash("amc10a-2024", "abc")
    known = repo.known_edition("AMC10A", 2024)
    assert known.source_hash == "abc" and known.revisions == {1: "100", 2: "200"}
    assert repo.known_edition("AMC10A", 1990).revisions == {}


def test_runs_errors_and_resume_checkpoints(repo):
    ref = edition()
    repo.upsert_edition(ref)
    first = repo.start_run("incremental", ["AMC10A"])
    repo.checkpoint("amc10a-2024", first)
    repo.add_error(
        first, provider="aops_wiki", series_key="AMC10A", year=2023, number=3, url=URL, stage="fetch", error="x"
    )
    repo.finish_run(first, "completed_with_errors", {"inserted": 4, "failed": 1}, {"note": "partial"})

    second = repo.start_run("incremental", ["AMC10A"])
    assert repo.resumable_editions(second) == {"amc10a-2024"}
    repo.finish_run(second, "completed", {}, {})

    third = repo.start_run("incremental", ["AMC10A"])
    assert repo.resumable_editions(third) == set()  # the previous run finished cleanly
    assert repo.last_run()["id"] == third
    assert repo.conn.execute("SELECT failed, inserted FROM ingestion_runs WHERE id = ?", (first,)).fetchone()[:] == (
        1,
        4,
    )


def test_interrupted_running_run_is_closed_as_failed(repo):
    crashed = repo.start_run("incremental", ["IMO"])  # process "dies" without finish_run
    later = repo.start_run("incremental", ["IMO"])
    row = repo.conn.execute(
        "SELECT status, finished_at, report FROM ingestion_runs WHERE id = ?", (crashed,)
    ).fetchone()
    assert row["status"] == "failed" and row["finished_at"] and "interrupted" in row["report"]
    assert repo.conn.execute("SELECT status FROM ingestion_runs WHERE id = ?", (later,)).fetchone()[0] == "running"


def test_known_edition_uses_newest_source_record_even_with_equal_timestamps(repo):
    ref = edition()
    store(repo, ref, parsed(number=1, source_revision="100"))
    store(repo, ref, parsed(number=1, html="<p>What is 2 + 5?</p>", source_revision="200"))
    repo.conn.execute("UPDATE source_records SET last_seen_at = '2026-01-01T00:00:00Z'")  # identical timestamps
    assert repo.known_edition("AMC10A", 2024).revisions == {1: "200"}


def test_transaction_rolls_back_on_error(repo):
    with pytest.raises(RuntimeError), repo.transaction():
        repo.upsert_edition(edition())
        raise RuntimeError("boom")
    assert repo.conn.execute("SELECT COUNT(*) FROM contest_editions").fetchone()[0] == 0


def test_repository_works_on_a_fresh_connection_per_thread(tmp_path, registry):
    path = tmp_path / "p.db"
    first = Repository(open_database(path))
    first.sync_registry(registry)
    store(first, edition(), parsed())
    second = Repository(connect(path))
    assert second.get_problem("AMC10A", 2024, 5) is not None
    first.conn.close()
    second.conn.close()


# Columns a reverted experiment added to source_records as migration 3 (applied on 2026-11-22
# to a real database). Code that did not know version 3 refused to open that database.
_EXPERIMENT_V3 = (
    "ALTER TABLE source_records ADD COLUMN parser_source TEXT NOT NULL DEFAULT 'existing'",
    "ALTER TABLE source_records ADD COLUMN fallback_used INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE source_records ADD COLUMN fetch_status INTEGER",
    "ALTER TABLE source_records ADD COLUMN fetch_timestamp TEXT",
    "ALTER TABLE source_records ADD COLUMN fallback_error TEXT",
    "ALTER TABLE source_records ADD COLUMN asset_status TEXT NOT NULL DEFAULT 'not_needed'",
    "ALTER TABLE source_records ADD COLUMN render_status TEXT NOT NULL DEFAULT 'pending'",
    "ALTER TABLE source_records ADD COLUMN ingestion_error TEXT",
)


def _columns(conn, table):
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def test_database_at_version_3_from_the_reverted_experiment_opens_and_matches_a_fresh_one(tmp_path):
    from mathbot.db.schema import MIGRATIONS

    old = connect(tmp_path / "old.db")
    old.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version, sql in MIGRATIONS[:2]:
        for statement in filter(None, (s.strip() for s in sql.split(";"))):
            old.execute(statement)
        old.execute("INSERT INTO schema_migrations VALUES (?, 'x')", (version,))
    for statement in _EXPERIMENT_V3:
        old.execute(statement)
    old.execute("INSERT INTO schema_migrations VALUES (3, 'x')")
    old.close()

    reopened = open_database(tmp_path / "old.db")  # raised "schema version 3 is newer" before
    fresh = open_database(tmp_path / "fresh.db")
    assert _columns(reopened, "source_records") == _columns(fresh, "source_records")
    reopened.close()
    fresh.close()


def test_concurrent_migration_does_not_apply_a_version_twice(tmp_path):
    first, second = connect(tmp_path / "p.db"), connect(tmp_path / "p.db")
    assert migrate(first) == LATEST_VERSION
    assert migrate(second) == LATEST_VERSION  # sees the work of the first, applies nothing
    assert second.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == LATEST_VERSION
    first.close()
    second.close()


def _store_numbers(repo, key, year, numbers):
    for number in numbers:
        store(repo, edition(key, year), parsed(number=number, html=f"<p>{key} {year} problem {number} text</p>"))


def test_random_year_range_and_excluded_problems(repo):
    for year in (2019, 2020, 2021, 2022):
        _store_numbers(repo, "AMC10A", year, [1])
    years = {repo.random_problem(year_from=2020, year_to=2021).year for _ in range(30)}
    assert years == {2020, 2021}
    assert repo.random_problem(year_from=2023) is None
    shown = ["amc10a-2019-p01", "amc10a-2020-p01", "amc10a-2021-p01"]
    assert repo.random_problem(exclude_ids=shown).id == "amc10a-2022-p01"


def test_random_difficulty_is_the_position_in_the_paper(repo):
    _store_numbers(repo, "AMC10A", 2024, range(1, 26))  # thirds: 1-9, 10-17, 18-25
    _store_numbers(repo, "IMO", 2020, range(1, 7))  # two days: 1/4 easy, 2/5 medium, 3/6 hard
    easy = {repo.random_problem(series_keys=["AMC10A"], difficulty="easy").number for _ in range(60)}
    hard = {repo.random_problem(series_keys=["AMC10A"], difficulty="hard").number for _ in range(60)}
    assert max(easy) <= 9 and min(hard) >= 18
    assert {repo.random_problem(series_keys=["IMO"], difficulty="easy").number for _ in range(30)} == {1, 4}
    assert {repo.random_problem(series_keys=["IMO"], difficulty="hard").number for _ in range(30)} == {3, 6}
    with pytest.raises(ValueError):
        repo.random_problem(difficulty="impossible")


def test_random_prefers_problems_that_have_an_image(repo):
    _store_numbers(repo, "AMC10A", 2024, [1, 2, 3])
    rendered = repo.get_problem("AMC10A", 2024, 2)
    repo.record_render(
        problem_id=rendered.id, content_hash=rendered.content_hash, renderer_version="v", status="ok", local_path="x"
    )
    assert {repo.random_problem().id for _ in range(20)} == {rendered.id}
    assert len({repo.random_problem(prefer_rendered=False).id for _ in range(40)}) == 3


def test_neighbouring_problems_in_the_same_sitting(repo):
    _store_numbers(repo, "AMC10A", 2024, [1, 2, 4])
    _store_numbers(repo, "AMC10A", 2023, [5])
    assert repo.neighbour("amc10a-2024-p02", +1).number == 4  # gaps are skipped
    assert repo.neighbour("amc10a-2024-p02", -1).number == 1
    assert repo.neighbour("amc10a-2024-p04", +1) is None  # never crosses into another year
    assert repo.neighbour("amc10a-2024-p01", -1) is None
    assert repo.neighbour("no-such-problem", +1) is None


def test_random_problem_uses_covering_indexes(repo):
    plan = " ".join(
        row[3]
        for row in repo.conn.execute(
            "EXPLAIN QUERY PLAN SELECT p.id FROM problems p WHERE EXISTS (SELECT 1 FROM renders r"
            " WHERE r.problem_id = p.id AND r.content_hash = p.content_hash AND r.status = 'ok')"
        )
    )
    assert "COVERING INDEX idx_problems_cover" in plan and "COVERING INDEX idx_renders_current" in plan
