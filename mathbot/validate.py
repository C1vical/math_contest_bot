"""Database and render-manifest integrity checks (``python -m mathbot validate``)."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from mathbot.contests import ContestRegistry
from mathbot.db.repo import Repository
from mathbot.htmlnorm import MIN_TEXT_CHARS

ERROR, WARNING = "error", "warning"
# Statements that could hold solution text: a heading, a wikitext marker or the word itself.
_MAY_LEAK = re.compile(r"<h[1-6]|mw-heading|==|solution", re.IGNORECASE)
# "(A)", "(\mathrm{A})", "\mathrm{(A)}", "\textbf{(A) }" ... as they appear in statement text.
_CHOICE_MARKER = re.compile(r"\(\s*(?:\\[a-zA-Z]+\s*\{\s*)?A\s*\}?\s*\)")


@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    message: str


@dataclass
class ValidationReport:
    findings: list[Finding] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def format(self, *, max_per_check: int = 15) -> str:
        lines = ["Validation report"]
        lines += [f"  {key}: {value}" for key, value in self.summary.items()]
        shown: dict[str, int] = {}
        for finding in self.findings:
            shown[finding.check] = shown.get(finding.check, 0) + 1
            if shown[finding.check] <= max_per_check:
                lines.append(f"  [{finding.severity}] {finding.check}: {finding.message}")
        for check, count in shown.items():
            if count > max_per_check:
                lines.append(f"  ... {count - max_per_check} more '{check}' findings")
        lines.append(
            f"Result: {'OK' if self.ok else 'FAILED'} ({len(self.errors)} error(s), {len(self.warnings)} warning(s))"
        )
        return "\n".join(lines)


def amc_problems_without_choices(conn: sqlite3.Connection) -> list[tuple[str, str, int, int]]:
    """``(problem id, contest, year, number)`` of AMC-family statements showing no "(A)" choice.

    Every AMC-family problem is multiple choice, so such a statement was most likely
    truncated at the source or by the pre-2.0 scraper (or its choices are an image).
    """
    rows = conn.execute(
        """
        SELECT p.id, upper(s.key) AS contest, e.year, p.number, p.statement_text FROM problems p
        JOIN contest_editions e ON e.id = p.edition_id JOIN contest_series s ON s.id = e.series_id
        WHERE s.family = 'AMC' ORDER BY s.key, e.year, p.number
        """
    )
    return [
        (r["id"], r["contest"], r["year"], r["number"]) for r in rows if not _CHOICE_MARKER.search(r["statement_text"])
    ]


def leaked_solutions(conn: sqlite3.Connection) -> list[str]:
    """AoPS Wiki statements that still carry solution text after the statement (see ``trim_statement``)."""
    from mathbot.sources.aops_wiki import trim_statement

    rows = conn.execute(
        """
        SELECT p.id, p.statement_html FROM problems p
        JOIN contest_editions e ON e.id = p.edition_id JOIN contest_series s ON s.id = e.series_id
        WHERE s.provider = 'aops_wiki' ORDER BY p.id
        """
    )
    # Parsing is only worth it when a cut point is possible at all.
    return [
        r["id"]
        for r in rows
        if _MAY_LEAK.search(r["statement_html"]) and trim_statement(r["statement_html"]) != r["statement_html"]
    ]


def _delimiters(html: str, delimiter: str) -> int:
    """Occurrences of a MathJax delimiter, not counting ``\\\\[2mm]`` (a TeX line break)."""
    return len(re.findall(r"(?<!\\)" + re.escape(delimiter), html))


def unbalanced_tex(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """``(problem id, detail)`` for statements whose ``\\(``/``\\)`` or ``\\[``/``\\]`` counts differ."""
    found = []
    for row in conn.execute(
        "SELECT id, statement_html FROM problems WHERE instr(statement_html, '\\') > 0 ORDER BY id"
    ):
        html = row["statement_html"]
        for opening, closing in (("\\(", "\\)"), ("\\[", "\\]")):
            opened, closed = _delimiters(html, opening), _delimiters(html, closing)
            if opened != closed:
                found.append((row["id"], f"{opened} {opening} but {closed} {closing}"))
    return found


def missing_answers(conn: sqlite3.Connection, registry: ContestRegistry) -> list[tuple[str, int, int]]:
    """``(edition id, without answer, problems)`` for multiple-choice editions missing answers."""
    keys = [s.id for s in registry.all(include_disabled=True) if s.source.get("multiple_choice")]
    if not keys:
        return []
    rows = conn.execute(
        f"""
        SELECT e.id, SUM(p.answer IS NULL) AS missing, COUNT(*) AS total
        FROM problems p JOIN contest_editions e ON e.id = p.edition_id
        WHERE e.series_id IN ({",".join("?" * len(keys))})
        GROUP BY e.id HAVING missing > 0 ORDER BY e.id
        """,
        keys,
    )
    return [(r["id"], r["missing"], r["total"]) for r in rows]


def validate(
    repo: Repository, renders_dir: Path, renderer_version: str, *, registry: ContestRegistry | None = None
) -> ValidationReport:
    """Run every integrity check; errors mean the data is wrong, warnings that work is pending.

    ``registry`` enables the checks that need contest metadata (missing multiple-choice answers).
    """
    conn = repo.conn
    report = ValidationReport()
    add = lambda severity, check, message: report.findings.append(Finding(severity, check, message))  # noqa: E731

    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        add(ERROR, "sqlite-integrity", integrity)
    for row in conn.execute("PRAGMA foreign_key_check"):
        add(ERROR, "foreign-key", f"table {row[0]} rowid {row[1]} references missing {row[2]}")

    # Image-only statements have little text; those are legitimate.
    for row in conn.execute(
        """
        SELECT id FROM problems
        WHERE (length(trim(statement_html)) = 0 OR length(trim(statement_text)) < ?)
          AND instr(statement_html, '<img') = 0
        """,
        (MIN_TEXT_CHARS,),
    ):
        add(ERROR, "empty-statement", row["id"])

    for row in conn.execute(
        """
        SELECT p.id FROM problems p
        WHERE NOT EXISTS (SELECT 1 FROM source_records sr WHERE sr.problem_id = p.id)
        """
    ):
        add(ERROR, "missing-provenance", f"{row['id']} has no source record")

    for row in conn.execute(
        """
        SELECT p.id FROM problems p
        WHERE NOT EXISTS (SELECT 1 FROM problem_versions v
                          WHERE v.problem_id = p.id AND v.version = p.version AND v.content_hash = p.content_hash)
        """
    ):
        add(ERROR, "version-history", f"{row['id']} current version is missing from problem_versions")

    for row in conn.execute(
        """
        SELECT e.id, e.expected_problems, COUNT(p.id) AS n, MAX(p.number) AS highest
        FROM contest_editions e LEFT JOIN problems p ON p.edition_id = e.id
        GROUP BY e.id
        """
    ):
        if row["n"] == 0:
            add(WARNING, "empty-edition", f"{row['id']} was discovered but has no problems")
        elif row["highest"] != row["n"]:
            add(WARNING, "numbering-gap", f"{row['id']} has {row['n']} problems but numbers go up to {row['highest']}")
        elif row["expected_problems"] and row["n"] != row["expected_problems"]:
            add(WARNING, "problem-count", f"{row['id']} has {row['n']} problems, expected {row['expected_problems']}")

    for problem_id, *_ in amc_problems_without_choices(conn):
        add(WARNING, "no-answer-choices", f"{problem_id} shows no (A)-(E) choices; the statement may be truncated")

    for problem_id in leaked_solutions(conn):
        add(WARNING, "leaked-solution", f"{problem_id} contains solution text after the statement")
    for problem_id, detail in unbalanced_tex(conn):
        add(WARNING, "tex-delimiters", f"{problem_id}: {detail}")
    if registry is not None:
        for edition, missing, total in missing_answers(conn, registry):
            add(WARNING, "missing-answer", f"{edition}: {missing} of {total} multiple-choice problems have no answer")

    # One set-based query for every problem's render state (no per-problem queries).
    missing_render = failed_render = missing_file = not_uploaded = 0
    for row in repo.render_status(renderer_version):
        if row["render_id"] is None:
            if row["failed_error"] is not None:
                failed_render += 1
                add(WARNING, "render-failed", f"{row['problem_id']}: {row['failed_error']}")
            else:
                missing_render += 1
                add(WARNING, "render-missing", row["problem_id"])
            continue
        if not row["local_path"] or not (renders_dir / row["local_path"]).is_file():
            missing_file += 1
            add(WARNING, "render-file-missing", f"{row['problem_id']}: {row['local_path']}")
        if row["uploaded_at"] is None:
            not_uploaded += 1

    last = repo.last_run()
    if last is not None and last["status"] != "completed":
        add(WARNING, "last-ingestion", f"run {last['id']} ended with status {last['status']} ({last['failed']} failed)")

    report.summary = {
        **repo.totals(),
        "renders_missing": missing_render,
        "renders_failed": failed_render,
        "render_files_missing": missing_file,
        "renders_not_uploaded": not_uploaded,
    }
    return report
