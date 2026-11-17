"""What the database is missing (``python -m mathbot coverage``).

Four kinds of gap, each listed per contest:

* problems the bot can only show as text, grouped by why their render failed;
* contest years the registry expects but the database doesn't have;
* editions with question numbers missing;
* AMC-family statements with no visible answer choices (likely truncated).

Read-only; ``--csv FILE`` also writes one row per gap.
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from mathbot.contests import ContestRegistry, compress_years
from mathbot.db.repo import Repository
from mathbot.validate import amc_problems_without_choices

# Sittings that never happened, so their absence is not a gap.
NOT_HELD: frozenset[tuple[str, int]] = frozenset(
    {
        ("AMC8", 2021),  # cancelled; the January 2022 AMC 8 followed the 2020 one
        ("IMO", 1980),  # not held
    }
)

# Month from which this year's edition is expected to be loadable: the sitting plus the
# time the source takes to publish it. A series not listed here is not expected until
# the following year.
AVAILABLE_FROM_MONTH: dict[str, int] = {
    "AMC8": 2,
    "AMC10A": 11,  # AMC 10/12 have been written in November since 2021
    "AMC10B": 11,
    "AMC12A": 11,
    "AMC12B": 11,
    "AIME1": 3,
    "AIME2": 3,
    "USAJMO": 4,
    "USAMO": 4,
    "IMO": 8,
    "CMO": 4,
    "PASCAL": 4,
    "CAYLEY": 4,
    "FERMAT": 4,
    "EUCLID": 5,
    "FRYER": 5,
    "GALOIS": 5,
    "HYPATIA": 5,
    # CIMC / CSMC are written in late November and posted afterwards: next year.
}

NOT_RENDERED = "not rendered yet"


def failure_cause(error: str | None) -> str:
    """A short, stable cause for a render failure message (as stored in ``renders.error``)."""
    if not error:
        return NOT_RENDERED
    if error.startswith("asset:"):
        if "anti-bot challenge" in error:
            return "image behind an anti-bot challenge"
        if "robots.txt" in error:
            return "image download refused by robots.txt"
        if "temporary:" in error:
            return "image download failed temporarily"
        return "image unavailable"
    if "TeX error" in error:
        return "LaTeX that MathJax can't typeset"
    return "render failed"


@dataclass(frozen=True)
class Gap:
    kind: str  # no-image | year-not-loaded | missing-numbers | no-answer-choices
    contest: str
    year: int
    number: int | None = None
    problem_id: str | None = None
    cause: str = ""
    detail: str = ""


@dataclass
class CoverageReport:
    renderer_version: str
    problems: int = 0
    with_image: int = 0  # the bot has a picture of the current statement
    current_renderer: int = 0  # ... made by the current renderer version
    gaps: list[Gap] = field(default_factory=list)

    def of(self, kind: str) -> list[Gap]:
        return [g for g in self.gaps if g.kind == kind]

    def format(self) -> str:
        lines = [f"Coverage report (renderer {self.renderer_version})"]
        share = f" ({self.with_image / self.problems:.1%})" if self.problems else ""
        lines.append(f"  problems: {self.problems:,}; with an image: {self.with_image:,}{share}")
        stale = self.with_image - self.current_renderer
        if stale:
            lines.append(f"  {stale:,} of those images are from an older renderer; `render` will redo them")

        no_image = self.of("no-image")
        lines.append(f"\nProblems without an image: {len(no_image):,}")
        by_cause: dict[str, Counter[str]] = defaultdict(Counter)
        for gap in no_image:
            by_cause[gap.cause][gap.contest] += 1
        for cause, contests in sorted(by_cause.items(), key=lambda kv: -sum(kv[1].values())):
            per_contest = ", ".join(f"{k} {n}" for k, n in contests.most_common())
            lines.append(f"  {sum(contests.values()):>6,}  {cause}: {per_contest}")

        years = self.of("year-not-loaded")
        lines.append(f"\nContest years expected but not loaded: {len(years)}")
        by_contest: dict[str, list[int]] = defaultdict(list)
        for gap in years:
            by_contest[gap.contest].append(gap.year)
        for contest, values in sorted(by_contest.items()):
            lines.append(f"  {contest}: {compress_years(values)}")

        numbers = self.of("missing-numbers")
        lines.append(f"\nEditions with missing problem numbers: {len(numbers)}")
        lines += [f"  {g.contest} {g.year}: {g.detail}" for g in numbers]

        choices = self.of("no-answer-choices")
        lines.append(f"\nAMC statements with no visible answer choices: {len(choices)}")
        lines += [f"  {g.problem_id}" for g in choices[:30]]
        if len(choices) > 30:
            lines.append(f"  ... {len(choices) - 30} more (see --csv)")
        return "\n".join(lines)

    def write_csv(self, path: Path) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["kind", "contest", "year", "number", "problem_id", "cause", "detail"])
            for g in self.gaps:
                writer.writerow([g.kind, g.contest, g.year, g.number or "", g.problem_id or "", g.cause, g.detail])


def expected_years(registry: ContestRegistry, today: date) -> dict[str, list[int]]:
    """Years each enabled series should have, from its eras, minus sittings never held."""
    out: dict[str, list[int]] = {}
    for series in registry.all():
        years = series.fallback_years(today)
        if today.month < AVAILABLE_FROM_MONTH.get(series.key, 13):
            years = [y for y in years if y < today.year]
        out[series.key] = [y for y in years if (series.key, y) not in NOT_HELD]
    return out


def coverage(
    repo: Repository, registry: ContestRegistry, renderer_version: str, *, today: date | None = None
) -> CoverageReport:
    conn = repo.conn
    report = CoverageReport(renderer_version)
    enabled = {s.key for s in registry.all()}

    rows = conn.execute(
        """
        SELECT p.id, upper(s.key) AS contest, e.year, p.number,
               EXISTS (SELECT 1 FROM renders r WHERE r.problem_id = p.id AND r.content_hash = p.content_hash
                       AND r.status = 'ok') AS has_image,
               EXISTS (SELECT 1 FROM renders r WHERE r.problem_id = p.id AND r.content_hash = p.content_hash
                       AND r.status = 'ok' AND r.renderer_version = ?) AS current,
               (SELECT r.error FROM renders r WHERE r.problem_id = p.id AND r.content_hash = p.content_hash
                AND r.status = 'failed' ORDER BY r.id DESC LIMIT 1) AS error
        FROM problems p JOIN contest_editions e ON e.id = p.edition_id JOIN contest_series s ON s.id = e.series_id
        ORDER BY s.key, e.year, p.number
        """,
        (renderer_version,),
    ).fetchall()
    for row in rows:
        report.problems += 1
        report.with_image += row["has_image"]
        report.current_renderer += row["current"]
        if not row["has_image"]:
            cause = failure_cause(row["error"])
            report.gaps.append(
                Gap("no-image", row["contest"], row["year"], row["number"], row["id"], cause, row["error"] or "")
            )

    loaded: dict[str, set[int]] = defaultdict(set)
    for row in conn.execute(
        """
        SELECT upper(s.key) AS contest, e.year FROM contest_editions e JOIN contest_series s ON s.id = e.series_id
        WHERE EXISTS (SELECT 1 FROM problems p WHERE p.edition_id = e.id)
        """
    ):
        loaded[row["contest"]].add(row["year"])
    for key, years in expected_years(registry, today or date.today()).items():
        report.gaps += [Gap("year-not-loaded", key, y) for y in years if y not in loaded[key]]

    for row in conn.execute(
        """
        SELECT upper(s.key) AS contest, e.year, e.expected_problems, group_concat(p.number) AS numbers
        FROM contest_editions e JOIN contest_series s ON s.id = e.series_id
        JOIN problems p ON p.edition_id = e.id
        GROUP BY e.id ORDER BY s.key, e.year
        """
    ):
        if row["contest"] not in enabled:
            continue
        present = {int(n) for n in row["numbers"].split(",")}
        expected = row["expected_problems"] or registry.get(row["contest"]).expected_problems(row["year"])
        missing = sorted(set(range(1, max(expected or 0, max(present)) + 1)) - present)
        if missing:
            detail = f"missing {compress_years(missing)} (has {len(present)} of {expected or max(present)})"
            report.gaps.append(Gap("missing-numbers", row["contest"], row["year"], detail=detail))

    for problem_id, contest, year, number in amc_problems_without_choices(conn):
        report.gaps.append(Gap("no-answer-choices", contest, year, number, problem_id))
    return report
