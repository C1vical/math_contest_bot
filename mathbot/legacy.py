"""Import problems from the pre-2.0 ``math_problems`` table.

The old bot stored ``(id, year, contest, question_number, question_statement,
answer)``. Those rows may be the only AoPS content available while AoPS blocks
automated clients, so they are worth keeping, but they are lower quality than a
live ingest and are treated accordingly:

* every old ``answer`` is ignored: the old code only ever wrote the default
  ``"0"``, so none is a real answer;
* the ``<h2>... Problem N</h2>`` title the old scrapers prepended is stripped
  (the renderer adds its own);
* AoPS Wiki statements are cut at the first heading or solution marker: the old
  scraper sometimes let the solutions through (``<h3>Solution 1</h3>...``,
  ``<p>Solution</p>``, ``==Video Solution``), which would be drawn into the image;
* TeX that AoPS's page showed as plain text is made typesettable, as in a live
  ingest (``tex.py``); the raw payload keeps the original;
* statements that are empty once the title is gone are rejected - the old
  loader stored a bare title whenever a fetch failed;
* a problem that already came from a live ingest is never overwritten;
* every imported row is marked: edition ``origin='legacy_import'`` and a
  source record whose identifier starts with ``legacy_import:``. A later live
  ingest replaces the statement through the normal content-hash upsert.

Caveat: the old AoPS Wiki scraper stopped at the first element containing the
word "solution", so some legacy AoPS statements may be truncated. Re-ingest
from the live source when it becomes available.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from mathbot.contests import (
    AOPS_COMMUNITY,
    AOPS_PROVIDERS,
    AOPS_WIKI,
    CEMC,
    ContestRegistry,
    ContestSeries,
    normalize_alias,
    problem_id,
)
from mathbot.db.repo import INSERTED, UNCHANGED, UPDATED, Repository
from mathbot.htmlnorm import InvalidContentError, normalize_fragment
from mathbot.models import EditionRef, ParsedProblem
from mathbot.sources.aops_community import CATEGORY_URL
from mathbot.sources.aops_wiki import PAGE_URL, drop_extra_text, page_title, trim_statement
from mathbot.sources.base import load_manifest

log = logging.getLogger(__name__)

LEGACY_MARKER = "legacy_import:"
_TITLE = re.compile(r"^\s*<h2>[^<]{0,120}?\bProblem\s+\d+\s*</h2>", re.IGNORECASE)


@dataclass
class LegacyReport:
    dry_run: bool
    read: int = 0
    outcomes: Counter = field(default_factory=Counter)  # inserted / updated / unchanged
    skipped_live: int = 0  # already ingested from the live source
    skipped_disabled: Counter = field(default_factory=Counter)  # series key -> rows
    unmapped: Counter = field(default_factory=Counter)  # old contest name -> rows
    invalid: list[tuple[str, str]] = field(default_factory=list)  # (old id, reason)
    answers_ignored: int = 0

    def format(self) -> str:
        verb = "would be " if self.dry_run else ""
        lines = [
            f"Legacy import{' (dry run - nothing written)' if self.dry_run else ''}: {self.read} row(s) read",
            f"  {self.outcomes[INSERTED]} {verb}inserted, {self.outcomes[UPDATED]} {verb}updated, "
            f"{self.outcomes[UNCHANGED]} unchanged",
            f"  {self.skipped_live} skipped (already ingested from the live source)",
            f"  {len(self.invalid)} invalid (empty or unusable statement)",
            f"  {self.answers_ignored} stored answer(s) ignored (the old bot never had real answers)",
        ]
        if self.skipped_disabled:
            detail = ", ".join(f"{k}: {v}" for k, v in sorted(self.skipped_disabled.items()))
            lines.append(f"  skipped, contest disabled (use --include-disabled): {detail}")
        if self.unmapped:
            detail = ", ".join(f"{k!r}: {v}" for k, v in sorted(self.unmapped.items()))
            lines.append(f"  UNMAPPED contest names (not imported): {detail}")
        for old_id, reason in self.invalid[:20]:
            lines.append(f"    invalid {old_id}: {reason}")
        if len(self.invalid) > 20:
            lines.append(f"    ... {len(self.invalid) - 20} more invalid rows")
        return "\n".join(lines)


def strip_legacy_title(html: str) -> str:
    """Remove the ``<h2>... Problem N</h2>`` title the old scrapers put in front of a statement."""
    return _TITLE.sub("", html, count=1).strip()


def resolve_legacy_contest(registry: ContestRegistry, name: str) -> ContestSeries | None:
    """Map an old contest name (``AMC10A``, ``AIME1``, ``Euclid``, ``Gauss7``...) to a series."""
    wanted = normalize_alias(name)
    for series in registry.all(include_disabled=True):
        if normalize_alias(series.legacy_name or series.key) == wanted:
            return series
    return registry.resolve(name, include_disabled=True)


class _Locator:
    """Canonical source URLs for legacy rows, from the same data the live adapters use."""

    def __init__(self) -> None:
        self.cemc = load_manifest("cemc_urls.json")
        self.community = load_manifest("aops_community_ids.json")

    def edition(self, series: ContestSeries, year: int) -> tuple[str, str | None]:
        if series.provider == AOPS_WIKI:
            title = page_title(year, series.source_name)
            return PAGE_URL.format(title=title), title
        if series.provider == CEMC:
            url = self.cemc.get(f"{year}_{series.legacy_name or series.key}")
            if url:
                return url, url.rsplit("/", 1)[-1]
            return "https://cemc.uwaterloo.ca/resources/past-contests", None
        if series.provider == AOPS_COMMUNITY:
            category = self.community.get(f"{year} {series.key}") or series.source["category_id"]
            return CATEGORY_URL.format(category_id=category), str(category)
        raise ValueError(f"unknown provider {series.provider}")

    def problem(self, series: ContestSeries, year: int, number: int, edition_url: str) -> str:
        if series.provider == AOPS_WIKI:
            return PAGE_URL.format(title=page_title(year, series.source_name, number))
        return edition_url


def _int(value: object) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def read_legacy_rows(path: Path) -> list[sqlite3.Row]:
    """Every row of the old ``math_problems`` table, read through a read-only connection."""
    if not path.is_file():
        raise FileNotFoundError(f"no database at {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'math_problems'").fetchone()
        if not exists:
            raise LookupError(f"{path} has no math_problems table (not a pre-2.0 database)")
        return conn.execute(
            "SELECT id, year, contest, question_number, question_statement, answer FROM math_problems ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def import_legacy(
    repo: Repository,
    registry: ContestRegistry,
    legacy_db: Path,
    *,
    dry_run: bool = False,
    include_disabled: bool = False,
) -> LegacyReport:
    """Import the rows of ``legacy_db`` that no live ingest has stored (see the module docstring)."""
    report = LegacyReport(dry_run=dry_run)
    locator = _Locator()
    editions_done: set[tuple[str, int]] = set()

    for row in read_legacy_rows(legacy_db):
        report.read += 1
        old_id = str(row["id"])
        answer = str(row["answer"] or "").strip()
        if answer:
            report.answers_ignored += 1

        series = resolve_legacy_contest(registry, str(row["contest"] or ""))
        if series is None:
            report.unmapped[str(row["contest"])] += 1
            continue
        if not series.enabled and not include_disabled:
            report.skipped_disabled[series.key] += 1
            continue
        year, number = _int(row["year"]), _int(row["question_number"])
        if year is None or number is None or number < 1:
            report.invalid.append((old_id, "year or problem number is not an integer"))
            continue

        pid = problem_id(series.key, year, number)
        if repo.has_source_record_not_from(pid, LEGACY_MARKER):  # already ingested from the live source
            report.skipped_live += 1
            continue

        raw = str(row["question_statement"] or "")
        statement = strip_legacy_title(raw)
        if series.provider == AOPS_WIKI:
            # solution text the old scraper let through, or that a wiki page shows as statement text
            statement = drop_extra_text(trim_statement(statement), pid)
        edition_url, edition_identifier = locator.edition(series, year)
        source_url = locator.problem(series, year, number, edition_url)
        try:
            content = normalize_fragment(statement, source_url, aops=series.provider in AOPS_PROVIDERS, problem_id=pid)
        except InvalidContentError as exc:
            report.invalid.append((old_id, str(exc)))
            continue

        if dry_run:
            report.outcomes[repo.classify_problem(series.key, year, number, content.content_hash)] += 1
            continue

        ref = EditionRef(
            series_key=series.key,
            year=year,
            label=f"{year} {series.name}",
            source_url=edition_url,
            source_identifier=edition_identifier,
            expected_problems=series.expected_problems(year),
            origin="legacy_import",
        )
        if (series.key, year) not in editions_done:
            # Never downgrade an edition that a live ingest already described.
            if repo.edition_origin(series.key, year) in {None, "legacy_import"}:
                repo.upsert_edition(ref)
            editions_done.add((series.key, year))

        parsed = ParsedProblem(
            number=number,
            statement_html=statement,
            source_url=source_url,
            raw_payload=raw,
            source_identifier=f"{LEGACY_MARKER}{old_id}",
        )
        report.outcomes[repo.upsert_problem(ref, parsed, content, provider=series.provider)] += 1

    log.info("legacy import finished", extra={"read": report.read, **dict(report.outcomes)})
    return report
