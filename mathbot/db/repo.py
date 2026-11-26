"""Repository: the queries behind ingestion, rendering and the bot.

The read-only reports in ``validate.py`` and ``coverage.py`` run their own SELECTs
against ``Repository.conn``; every write goes through this class.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from mathbot.contests import PART_LABEL_SERIES, ContestRegistry, edition_id, problem_id
from mathbot.db.connection import iso_utc
from mathbot.htmlnorm import NormalizedContent, sha256_text
from mathbot.models import EditionRef, KnownEdition, ParsedProblem

INSERTED, UPDATED, UNCHANGED = "inserted", "updated", "unchanged"


@dataclass(frozen=True)
class ProblemRow:
    id: str
    series_key: str
    series_name: str
    family: str
    provider: str
    year: int
    number: int
    section: str | None
    label: str
    statement_html: str
    statement_text: str
    content_hash: str
    version: int
    answer: str | None
    source_url: str
    solution_url: str | None = None
    part_number: int | None = None  # position within its part (section), e.g. 1 for B1

    @property
    def display_number(self) -> str:
        """``B1`` for a contest numbered by part (CSMC, COMC...), else the number (``7``)."""
        section = self.section or ""
        if self.series_key in PART_LABEL_SERIES and len(section) == 1 and section.isalpha() and self.part_number:
            return f"{section}{self.part_number}"
        return str(self.number)

    @property
    def title(self) -> str:
        return f"{self.year} {self.series_name} Problem {self.display_number}"


@dataclass(frozen=True)
class SeriesStats:
    key: str
    name: str
    family: str
    provider: str
    editions: int
    problems: int
    first_year: int | None
    last_year: int | None


_PROBLEM_SELECT = """
    SELECT p.id, s.key AS series_key, s.name AS series_name, s.family, s.provider,
           e.year, p.number, p.section, e.label, p.statement_html, p.statement_text,
           p.content_hash, p.version, p.answer, p.source_url,
           (SELECT sol.source_url FROM solutions sol WHERE sol.problem_id = p.id
            ORDER BY sol.id LIMIT 1) AS solution_url,
           (SELECT COUNT(*) FROM problems q WHERE q.edition_id = p.edition_id AND q.section = p.section
            AND q.number <= p.number) AS part_number
    FROM problems p
    JOIN contest_editions e ON e.id = p.edition_id
    JOIN contest_series s ON s.id = e.series_id
"""


DIFFICULTIES = ("easy", "medium", "hard")
# Families sat over two days of three problems each, ordered easy -> hard within each day.
_TWO_DAY_FAMILIES = ("USAMO", "USAJMO", "IMO")
# The problem's third of its paper (0 easy, 1 medium, 2 hard), a proxy for difficulty: contests
# order their problems by difficulty. For a six-problem two-day olympiad, its place in the day.
_DIFFICULTY_JOIN = """
    JOIN (SELECT edition_id, MAX(number) AS total FROM problems GROUP BY edition_id) papers
      ON papers.edition_id = p.edition_id
"""
_DIFFICULTY_BUCKET = f"""
    (CASE WHEN s.family IN ({", ".join(f"'{f}'" for f in _TWO_DAY_FAMILIES)}) AND papers.total = 6
          THEN (p.number - 1) % 3
          ELSE ((p.number - 1) * 3) / papers.total END)
"""
_HAS_IMAGE = """
    EXISTS (SELECT 1 FROM renders r
            WHERE r.problem_id = p.id AND r.content_hash = p.content_hash AND r.status = 'ok')
"""


def _placeholders(values: Sequence[object]) -> str:
    return ",".join("?" * len(values))


class Repository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.conn.in_transaction:  # nested use joins the outer transaction
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ registry

    def sync_registry(self, registry: ContestRegistry) -> None:
        """Mirror the code registry into the DB so SQL can join on series and aliases."""
        now = iso_utc()
        with self.transaction():
            for s in registry.all(include_disabled=True):
                self.conn.execute(
                    """
                    INSERT INTO contest_series (id, key, name, family, provider, source_name, enabled, notes,
                                                created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        key = excluded.key, name = excluded.name, family = excluded.family,
                        provider = excluded.provider, source_name = excluded.source_name,
                        enabled = excluded.enabled, notes = excluded.notes, updated_at = excluded.updated_at
                    """,
                    (s.id, s.key, s.name, s.family, s.provider, s.source_name, int(s.enabled), s.notes, now, now),
                )
            self.conn.execute("DELETE FROM contest_aliases")
            self.conn.executemany(
                "INSERT INTO contest_aliases (alias, series_id) VALUES (?, ?)",
                [(alias, key.lower()) for alias, key in registry.aliases().items()],
            )

    # ------------------------------------------------------------------ editions

    def upsert_edition(self, ref: EditionRef) -> str:
        eid = edition_id(ref.series_key, ref.year)
        now = iso_utc()
        self.conn.execute(
            """
            INSERT INTO contest_editions (id, series_id, year, label, source_url, source_identifier,
                                          expected_problems, origin, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                label = excluded.label, source_url = excluded.source_url,
                source_identifier = excluded.source_identifier,
                expected_problems = excluded.expected_problems, origin = excluded.origin,
                updated_at = excluded.updated_at
            """,
            (
                eid,
                ref.series_key.lower(),
                ref.year,
                ref.label,
                ref.source_url,
                ref.source_identifier,
                ref.expected_problems,
                ref.origin,
                now,
                now,
            ),
        )
        return eid

    def edition_origin(self, series_key: str, year: int) -> str | None:
        """How a stored edition was found (``discovered``, ``fallback``, ``legacy_import``), or None."""
        row = self.conn.execute(
            "SELECT origin FROM contest_editions WHERE id = ?", (edition_id(series_key, year),)
        ).fetchone()
        return row["origin"] if row else None

    def set_edition_source_hash(self, eid: str, source_hash: str | None) -> None:
        self.conn.execute(
            "UPDATE contest_editions SET source_hash = ?, updated_at = ? WHERE id = ?", (source_hash, iso_utc(), eid)
        )

    def known_edition(self, series_key: str, year: int) -> KnownEdition:
        eid = edition_id(series_key, year)
        row = self.conn.execute("SELECT source_hash FROM contest_editions WHERE id = ?", (eid,)).fetchone()
        revisions = {
            r["number"]: r["source_revision"]
            for r in self.conn.execute(
                """
                SELECT p.number, sr.source_revision
                FROM problems p
                JOIN source_records sr ON sr.problem_id = p.id
                WHERE p.edition_id = ? AND sr.source_revision IS NOT NULL
                  AND sr.id = (SELECT MAX(id) FROM source_records WHERE problem_id = p.id)
                """,
                (eid,),
            )
        }
        return KnownEdition(source_hash=row["source_hash"] if row else None, revisions=revisions)

    # ------------------------------------------------------------------ problems

    def classify_problem(self, series_key: str, year: int, number: int, content_hash: str) -> str:
        """What :meth:`upsert_problem` would do, without writing (dry-run)."""
        row = self.conn.execute(
            "SELECT content_hash FROM problems WHERE id = ?", (problem_id(series_key, year, number),)
        ).fetchone()
        if row is None:
            return INSERTED
        return UNCHANGED if row["content_hash"] == content_hash else UPDATED

    def upsert_problem(
        self,
        ref: EditionRef,
        parsed: ParsedProblem,
        content: NormalizedContent,
        *,
        provider: str,
        run_id: int | None = None,
        solution: NormalizedContent | None = None,
    ) -> str:
        """Insert a new problem or replace a changed one, keeping history.

        Unlike the old ``INSERT OR IGNORE``, corrected source content replaces
        the stored statement and bumps ``version``; identical content is a no-op
        apart from refreshing provenance timestamps. Returns
        ``inserted`` / ``updated`` / ``unchanged``.
        """
        pid = problem_id(ref.series_key, ref.year, parsed.number)
        eid = edition_id(ref.series_key, ref.year)
        now = iso_utc()

        with self.transaction():
            row = self.conn.execute(
                "SELECT content_hash, version, answer FROM problems WHERE id = ?", (pid,)
            ).fetchone()
            if row is None:
                outcome, version = INSERTED, 1
                self.conn.execute(
                    """
                    INSERT INTO problems (id, edition_id, number, section, statement_html, statement_text,
                                          content_hash, version, answer, answer_source, source_url,
                                          created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        pid,
                        eid,
                        parsed.number,
                        parsed.section,
                        content.html,
                        content.text,
                        content.content_hash,
                        parsed.answer,
                        parsed.answer_source if parsed.answer else None,
                        parsed.source_url,
                        now,
                        now,
                    ),
                )
            elif row["content_hash"] != content.content_hash:
                outcome, version = UPDATED, row["version"] + 1
                self.conn.execute(
                    """
                    UPDATE problems SET section = ?, statement_html = ?, statement_text = ?, content_hash = ?,
                                        version = ?, source_url = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        parsed.section,
                        content.html,
                        content.text,
                        content.content_hash,
                        version,
                        parsed.source_url,
                        now,
                        pid,
                    ),
                )
            else:
                outcome, version = UNCHANGED, row["version"]

            if outcome != UNCHANGED:
                self.conn.execute(
                    """
                    INSERT INTO problem_versions (problem_id, version, content_hash, statement_html, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (pid, version, content.content_hash, content.html, now),
                )

            # Answers are only ever set from the source; a missing answer never erases one.
            if row is not None and parsed.answer and parsed.answer != row["answer"]:
                self.conn.execute(
                    "UPDATE problems SET answer = ?, answer_source = ?, updated_at = ? WHERE id = ?",
                    (parsed.answer, parsed.answer_source, now, pid),
                )
                if outcome == UNCHANGED:
                    outcome = UPDATED

            if (
                solution is not None
                and parsed.solution_source
                and self._upsert_solution(pid, provider, parsed.solution_source, solution, now)
                and outcome == UNCHANGED
            ):
                outcome = UPDATED

            self.conn.execute(
                """
                INSERT INTO source_records (problem_id, provider, source_url, source_identifier, source_revision,
                                            raw_payload, raw_hash, first_seen_at, last_seen_at, run_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (problem_id, provider, raw_hash) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at, source_url = excluded.source_url,
                    source_revision = excluded.source_revision, run_id = excluded.run_id
                """,
                (
                    pid,
                    provider,
                    parsed.source_url,
                    parsed.source_identifier,
                    parsed.source_revision,
                    parsed.raw_payload,
                    sha256_text(parsed.raw_payload),
                    now,
                    now,
                    run_id,
                ),
            )
        return outcome

    def _upsert_solution(self, pid: str, provider: str, source_url: str, content: NormalizedContent, now: str) -> bool:
        """Insert or update the official solution. Returns True when anything changed."""
        row = self.conn.execute(
            "SELECT content_hash, version FROM solutions WHERE problem_id = ? AND provider = ?", (pid, provider)
        ).fetchone()
        if row is not None and row["content_hash"] == content.content_hash:
            return False
        self.conn.execute(
            """
            INSERT INTO solutions (problem_id, provider, source_url, solution_html, solution_text, content_hash,
                                   version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT (problem_id, provider) DO UPDATE SET
                source_url = excluded.source_url, solution_html = excluded.solution_html,
                solution_text = excluded.solution_text, content_hash = excluded.content_hash,
                version = solutions.version + 1, updated_at = excluded.updated_at
            """,
            (pid, provider, source_url, content.html, content.text, content.content_hash, now, now),
        )
        return True

    def has_source_record_not_from(self, pid: str, identifier_prefix: str) -> bool:
        """Whether any source record of ``pid`` has an identifier not starting with ``identifier_prefix``."""
        row = self.conn.execute(
            """
            SELECT 1 FROM source_records
            WHERE problem_id = ? AND (source_identifier IS NULL OR source_identifier NOT LIKE ?)
            LIMIT 1
            """,
            (pid, identifier_prefix + "%"),
        ).fetchone()
        return row is not None

    def get_solution(self, pid: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM solutions WHERE problem_id = ? ORDER BY id LIMIT 1", (pid,)).fetchone()

    def _problem(self, row: sqlite3.Row | None) -> ProblemRow | None:
        return ProblemRow(**dict(row)) if row is not None else None

    def get_problem(self, series_key: str, year: int, number: int) -> ProblemRow | None:
        return self.get_problem_by_id(problem_id(series_key, year, number))

    def get_problem_by_part(self, series_key: str, year: int, part: str, position: int) -> ProblemRow | None:
        """Problem ``B1`` of an edition: the ``position``-th problem whose section is ``part``."""
        if position < 1:
            return None
        row = self.conn.execute(
            "SELECT id FROM problems WHERE edition_id = ? AND section = ? ORDER BY number LIMIT 1 OFFSET ?",
            (edition_id(series_key, year), part.upper(), position - 1),
        ).fetchone()
        return self.get_problem_by_id(row["id"]) if row else None

    def parts_for(self, series_key: str, year: int) -> dict[str, int]:
        """``{"A": 6, "B": 3}``: how many problems each part of an edition has, in paper order."""
        rows = self.conn.execute(
            """
            SELECT section, COUNT(*) AS problems FROM problems
            WHERE edition_id = ? AND section IS NOT NULL
            GROUP BY section ORDER BY MIN(number)
            """,
            (edition_id(series_key, year),),
        )
        return {r["section"]: r["problems"] for r in rows}

    def get_problem_by_id(self, pid: str) -> ProblemRow | None:
        return self._problem(self.conn.execute(_PROBLEM_SELECT + " WHERE p.id = ?", (pid,)).fetchone())

    def random_problem(
        self,
        *,
        series_keys: Sequence[str] | None = None,
        year: int | None = None,
        year_from: int | None = None,
        year_to: int | None = None,
        number: int | None = None,
        provider: str | None = None,
        difficulty: str | None = None,
        exclude_ids: Sequence[str] = (),
        prefer_rendered: bool = True,
    ) -> ProblemRow | None:
        """A random problem of an enabled series matching every filter given.

        ``difficulty`` is ``easy`` / ``medium`` / ``hard``: the problem's position in its
        paper (see ``_DIFFICULTY_BUCKET``). With ``prefer_rendered`` a problem that has an
        image is chosen whenever one matches, so the text fallback is the exception.
        """
        where, params, joins = ["s.enabled = 1"], [], ""
        if series_keys is not None:
            where.append(f"s.key IN ({_placeholders(series_keys)})")
            params.extend(series_keys)
        for clause, value in (
            ("e.year = ?", year),
            ("e.year >= ?", year_from),
            ("e.year <= ?", year_to),
            ("p.number = ?", number),
            ("s.provider = ?", provider),
        ):
            if value is not None:
                where.append(clause)
                params.append(value)
        if difficulty is not None:
            if difficulty not in DIFFICULTIES:
                raise ValueError(f"difficulty must be one of {DIFFICULTIES}, got {difficulty!r}")
            joins = _DIFFICULTY_JOIN
            where.append(f"{_DIFFICULTY_BUCKET} = ?")
            params.append(DIFFICULTIES.index(difficulty))
        if exclude_ids:
            where.append(f"p.id NOT IN ({_placeholders(exclude_ids)})")
            params.extend(exclude_ids)
        order = f"{_HAS_IMAGE} DESC, RANDOM()" if prefer_rendered else "RANDOM()"
        # Only the id is sorted: sorting whole rows (statements are large) took ~200 ms.
        sql = f"""
            SELECT p.id FROM problems p
            JOIN contest_editions e ON e.id = p.edition_id
            JOIN contest_series s ON s.id = e.series_id
            {joins}
            WHERE {" AND ".join(where)} ORDER BY {order} LIMIT 1
        """
        row = self.conn.execute(sql, params).fetchone()
        return self.get_problem_by_id(row["id"]) if row else None

    def neighbour(self, pid: str, step: int) -> ProblemRow | None:
        """The next (``step`` > 0) or previous problem of the same sitting, skipping gaps."""
        comparison, order = (">", "ASC") if step > 0 else ("<", "DESC")
        sql = f"""
            {_PROBLEM_SELECT}
            WHERE p.edition_id = (SELECT edition_id FROM problems WHERE id = ?)
              AND p.number {comparison} (SELECT number FROM problems WHERE id = ?)
            ORDER BY p.number {order} LIMIT 1
        """
        return self._problem(self.conn.execute(sql, (pid, pid)).fetchone())

    def iter_problems(self, series_keys: Sequence[str] | None = None, year: int | None = None) -> list[ProblemRow]:
        where, params = ["1 = 1"], []
        if series_keys is not None:
            where.append(f"s.key IN ({_placeholders(series_keys)})")
            params.extend(series_keys)
        if year is not None:
            where.append("e.year = ?")
            params.append(year)
        sql = f"{_PROBLEM_SELECT} WHERE {' AND '.join(where)} ORDER BY s.key, e.year, p.number"
        return [ProblemRow(**dict(r)) for r in self.conn.execute(sql, params)]

    def years_for(self, series_key: str) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT e.year FROM contest_editions e
            JOIN problems p ON p.edition_id = e.id
            WHERE e.series_id = ? ORDER BY e.year
            """,
            (series_key.lower(),),
        )
        return [r["year"] for r in rows]

    def numbers_for(self, series_key: str, year: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT number FROM problems WHERE edition_id = ? ORDER BY number", (edition_id(series_key, year),)
        )
        return [r["number"] for r in rows]

    def series_stats(self, *, include_empty: bool = True) -> list[SeriesStats]:
        rows = self.conn.execute(
            """
            SELECT s.key, s.name, s.family, s.provider,
                   COUNT(DISTINCT e.id) AS editions, COUNT(p.id) AS problems,
                   MIN(e.year) AS first_year, MAX(e.year) AS last_year
            FROM contest_series s
            LEFT JOIN contest_editions e ON e.series_id = s.id
                 AND EXISTS (SELECT 1 FROM problems WHERE edition_id = e.id)
            LEFT JOIN problems p ON p.edition_id = e.id
            WHERE s.enabled = 1
            GROUP BY s.id
            ORDER BY s.provider, s.family, s.key
            """
        )
        stats = [SeriesStats(**dict(r)) for r in rows]
        return stats if include_empty else [s for s in stats if s.problems]

    def totals(self) -> dict[str, int]:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "series": one("SELECT COUNT(*) FROM contest_series WHERE enabled = 1"),
            "editions": one("SELECT COUNT(DISTINCT edition_id) FROM problems"),
            "problems": one("SELECT COUNT(*) FROM problems"),
            "with_answers": one("SELECT COUNT(*) FROM problems WHERE answer IS NOT NULL"),
            "with_solutions": one("SELECT COUNT(DISTINCT problem_id) FROM solutions"),
            "renders_ok": one("SELECT COUNT(*) FROM renders WHERE status = 'ok'"),
            "renders_uploaded": one("SELECT COUNT(*) FROM renders WHERE status = 'ok' AND uploaded_at IS NOT NULL"),
        }

    # ------------------------------------------------------------------ ingestion runs

    def start_run(self, mode: str, selection: Iterable[str]) -> int:
        """Open a run. Runs still marked ``running`` were interrupted (the process died
        before :meth:`finish_run`), so they are closed as ``failed`` first. Concurrent
        ingests against one database are not supported."""
        self.conn.execute(
            """
            UPDATE ingestion_runs SET status = 'failed', finished_at = ?,
                   report = json_object('note', 'interrupted: the process ended before the run finished')
            WHERE status = 'running'
            """,
            (iso_utc(),),
        )
        cur = self.conn.execute(
            "INSERT INTO ingestion_runs (started_at, mode, selection, status) VALUES (?, ?, ?, 'running')",
            (iso_utc(), mode, json.dumps(sorted(selection))),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, counters: dict[str, int], report: dict) -> None:
        self.conn.execute(
            """
            UPDATE ingestion_runs SET finished_at = ?, status = ?, discovered = ?, inserted = ?, updated = ?,
                                      skipped = ?, failed = ?, invalid = ?, report = ?
            WHERE id = ?
            """,
            (
                iso_utc(),
                status,
                counters.get("discovered", 0),
                counters.get("inserted", 0),
                counters.get("updated", 0),
                counters.get("skipped", 0),
                counters.get("failed", 0),
                counters.get("invalid", 0),
                json.dumps(report),
                run_id,
            ),
        )

    def add_error(
        self,
        run_id: int,
        *,
        provider: str,
        series_key: str,
        year: int | None,
        number: int | None,
        url: str | None,
        stage: str,
        error: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO ingestion_errors (run_id, provider, series_key, year, problem_number, url, stage, error,
                                          created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, provider, series_key, year, number, url, stage, error, iso_utc()),
        )

    def checkpoint(self, eid: str, run_id: int) -> None:
        self.conn.execute(
            """
            INSERT INTO ingestion_checkpoints (edition_id, run_id, completed_at) VALUES (?, ?, ?)
            ON CONFLICT (edition_id) DO UPDATE SET run_id = excluded.run_id, completed_at = excluded.completed_at
            """,
            (eid, run_id, iso_utc()),
        )

    def resumable_editions(self, current_run_id: int) -> set[str]:
        """Editions finished by the most recent earlier run, if that run did not complete cleanly."""
        row = self.conn.execute(
            "SELECT id, status FROM ingestion_runs WHERE id < ? ORDER BY id DESC LIMIT 1", (current_run_id,)
        ).fetchone()
        if row is None or row["status"] == "completed":
            return set()
        rows = self.conn.execute("SELECT edition_id FROM ingestion_checkpoints WHERE run_id = ?", (row["id"],))
        return {r["edition_id"] for r in rows}

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM ingestion_runs ORDER BY id DESC LIMIT 1").fetchone()

    # ------------------------------------------------------------------ renders

    def problems_needing_render(
        self, renderer_version: str, *, series_keys: Sequence[str] | None = None, retry_failed: bool = True
    ) -> list[ProblemRow]:
        """Problems whose *current* content has no successful render by this renderer."""
        statuses = "('ok')" if retry_failed else "('ok', 'failed')"
        where, params = ["1 = 1"], [renderer_version]
        if series_keys is not None:
            where.append(f"s.key IN ({_placeholders(series_keys)})")
            params.extend(series_keys)
        sql = f"""
            {_PROBLEM_SELECT}
            LEFT JOIN renders r ON r.problem_id = p.id AND r.content_hash = p.content_hash
                 AND r.renderer_version = ? AND r.status IN {statuses}
            WHERE r.id IS NULL AND {" AND ".join(where)}
            ORDER BY s.key, e.year, p.number
        """
        return [ProblemRow(**dict(r)) for r in self.conn.execute(sql, params)]

    def record_render(
        self,
        *,
        problem_id: str,
        content_hash: str,
        renderer_version: str,
        status: str,
        local_path: str | None = None,
        object_key: str | None = None,
        width: int | None = None,
        height: int | None = None,
        size_bytes: int | None = None,
        attempts: int = 1,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO renders (problem_id, content_hash, renderer_version, status, local_path, object_key,
                                 width, height, size_bytes, attempts, error, rendered_at, uploaded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT (problem_id, content_hash, renderer_version) DO UPDATE SET
                status = excluded.status, local_path = excluded.local_path, object_key = excluded.object_key,
                width = excluded.width, height = excluded.height, size_bytes = excluded.size_bytes,
                attempts = renders.attempts + excluded.attempts, error = excluded.error,
                rendered_at = excluded.rendered_at, uploaded_at = NULL
            """,
            (
                problem_id,
                content_hash,
                renderer_version,
                status,
                local_path,
                object_key,
                width,
                height,
                size_bytes,
                attempts,
                error,
                iso_utc(),
            ),
        )

    def current_render(self, pid: str) -> sqlite3.Row | None:
        """Newest successful render matching the problem's current content hash."""
        return self.conn.execute(
            """
            SELECT r.* FROM renders r JOIN problems p ON p.id = r.problem_id
            WHERE r.problem_id = ? AND r.content_hash = p.content_hash AND r.status = 'ok'
            ORDER BY r.rendered_at DESC, r.id DESC LIMIT 1
            """,
            (pid,),
        ).fetchone()

    def current_renders(self, renderer_version: str, *, pending_only: bool = False) -> list[sqlite3.Row]:
        """Successful renders of each problem's current content by this renderer version."""
        pending = "AND r.uploaded_at IS NULL" if pending_only else ""
        return self.conn.execute(
            f"""
            SELECT r.* FROM renders r JOIN problems p ON p.id = r.problem_id
            WHERE r.status = 'ok' AND r.content_hash = p.content_hash AND r.renderer_version = ? {pending}
            ORDER BY r.id
            """,
            (renderer_version,),
        ).fetchall()

    def render_status(self, renderer_version: str) -> list[sqlite3.Row]:
        """One row per problem: its current render (if any) and its latest failure (if any)."""
        return self.conn.execute(
            """
            SELECT p.id AS problem_id, ok.local_path, ok.uploaded_at, ok.id AS render_id,
                   (SELECT f.error FROM renders f
                    WHERE f.problem_id = p.id AND f.content_hash = p.content_hash AND f.status = 'failed'
                    ORDER BY f.id DESC LIMIT 1) AS failed_error
            FROM problems p
            LEFT JOIN renders ok ON ok.problem_id = p.id AND ok.content_hash = p.content_hash
                 AND ok.renderer_version = ? AND ok.status = 'ok'
            ORDER BY p.id
            """,
            (renderer_version,),
        ).fetchall()

    def mark_uploaded(self, render_id: int) -> None:
        self.conn.execute("UPDATE renders SET uploaded_at = ? WHERE id = ?", (iso_utc(), render_id))

    def stale_renders(self, renderer_version: str) -> list[sqlite3.Row]:
        """Renders cleanup may delete: of superseded content, or by another renderer version.

        A successful render of the current content by another version is only stale once
        ``renderer_version`` has rendered that content too; until then it is what the bot shows.
        """
        return self.conn.execute(
            """
            SELECT r.* FROM renders r JOIN problems p ON p.id = r.problem_id
            WHERE r.content_hash != p.content_hash
               OR (r.renderer_version != ?
                   AND (r.status != 'ok' OR EXISTS (
                        SELECT 1 FROM renders c
                        WHERE c.problem_id = r.problem_id AND c.content_hash = p.content_hash
                          AND c.renderer_version = ? AND c.status = 'ok')))
            ORDER BY r.id
            """,
            (renderer_version, renderer_version),
        ).fetchall()

    def delete_render(self, render_id: int) -> None:
        self.conn.execute("DELETE FROM renders WHERE id = ?", (render_id,))

    def all_renders(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM renders ORDER BY id").fetchall()

    # ------------------------------------------------------------------ assets

    def get_asset(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM assets WHERE source_url = ?", (url,)).fetchone()

    def upsert_asset(
        self,
        url: str,
        *,
        status: str,
        sha256: str | None = None,
        content_type: str | None = None,
        size_bytes: int | None = None,
        local_path: str | None = None,
        expires_at: str | None = None,
        error: str | None = None,
    ) -> int:
        self.conn.execute(
            """
            INSERT INTO assets (source_url, status, sha256, content_type, size_bytes, local_path, fetched_at,
                                expires_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_url) DO UPDATE SET
                status = excluded.status, sha256 = excluded.sha256, content_type = excluded.content_type,
                size_bytes = excluded.size_bytes, local_path = excluded.local_path,
                fetched_at = excluded.fetched_at, expires_at = excluded.expires_at, error = excluded.error
            """,
            (url, status, sha256, content_type, size_bytes, local_path, iso_utc(), expires_at, error),
        )
        return int(self.conn.execute("SELECT id FROM assets WHERE source_url = ?", (url,)).fetchone()["id"])

    def link_asset(self, pid: str, asset_id: int) -> None:
        self.conn.execute(
            "INSERT INTO problem_assets (problem_id, asset_id) VALUES (?, ?) ON CONFLICT DO NOTHING", (pid, asset_id)
        )
