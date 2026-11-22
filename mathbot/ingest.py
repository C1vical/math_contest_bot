"""Ingestion pipeline: discover -> fetch -> normalize -> validate -> upsert -> report.

Modes
  incremental  (default) adapters are told what is already stored and skip
               source content that has not changed; cached HTTP responses are
               reused within their TTL.
  full         every page is re-requested and re-parsed. Unchanged statements
               still do not get a new version.

``dry_run`` performs all fetching and parsing but writes nothing.
``resume`` skips editions that an earlier, interrupted run already finished.

Fetching runs on a bounded thread pool (one task per edition; the HTTP client
enforces the per-host rate limit). All database writes happen on the calling
thread.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from mathbot.contests import ContestSeries, edition_id
from mathbot.db.repo import INSERTED, UNCHANGED, UPDATED, Repository
from mathbot.htmlnorm import InvalidContentError, normalize_fragment
from mathbot.http import HttpError, RobotsDisallowedError, SourceBlockedError
from mathbot.models import EditionRef, EditionResult, KnownEdition, ProblemFailure
from mathbot.sources.base import SourceAdapter

log = logging.getLogger(__name__)

COUNTERS = ("discovered", "inserted", "updated", "skipped", "failed", "invalid")
MAX_REPORTED_ERRORS = 40  # failures listed in the terminal report; all are in ingestion_errors


@dataclass
class ErrorEntry:
    provider: str
    series_key: str
    year: int | None
    number: int | None
    url: str | None
    stage: str
    error: str


@dataclass
class IngestReport:
    mode: str
    dry_run: bool
    run_id: int | None = None
    status: str = "running"
    totals: Counter = field(default_factory=Counter)
    by_series: dict[str, Counter] = field(default_factory=dict)
    editions: Counter = field(default_factory=Counter)  # discovered / fetched / unchanged / resumed / failed
    errors: list[ErrorEntry] = field(default_factory=list)
    blocked_providers: dict[str, str] = field(default_factory=dict)

    def bump(self, series_key: str, counter: str, amount: int = 1) -> None:
        self.totals[counter] += amount
        self.by_series.setdefault(series_key, Counter())[counter] += amount

    def counters(self) -> dict[str, int]:
        return {name: self.totals.get(name, 0) for name in COUNTERS}

    @property
    def ok(self) -> bool:
        return not self.errors and not self.blocked_providers

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "dry_run": self.dry_run,
            "run_id": self.run_id,
            "status": self.status,
            "totals": self.counters(),
            "editions": dict(self.editions),
            "by_series": {
                key: {name: c.get(name, 0) for name in COUNTERS} for key, c in sorted(self.by_series.items())
            },
            "blocked_providers": self.blocked_providers,
            "errors": [vars(e) for e in self.errors],
        }

    def format(self) -> str:
        verb = "would be " if self.dry_run else ""
        lines = [
            f"Ingestion report  mode={self.mode}{' (dry run - nothing written)' if self.dry_run else ''}"
            f"  status={self.status}" + (f"  run={self.run_id}" if self.run_id else ""),
            f"  editions: {self.editions.get('discovered', 0)} discovered, {self.editions.get('fetched', 0)} fetched, "
            f"{self.editions.get('unchanged', 0)} unchanged, {self.editions.get('resumed', 0)} resumed, "
            f"{self.editions.get('failed', 0)} failed",
            f"  problems: {self.totals['discovered']} discovered, {self.totals['inserted']} {verb}inserted, "
            f"{self.totals['updated']} {verb}updated, {self.totals['skipped']} skipped (unchanged), "
            f"{self.totals['failed']} failed, {self.totals['invalid']} invalid",
        ]
        if self.by_series:
            lines.append(f"  {'contest':<12}" + "".join(f"{name:>11}" for name in COUNTERS))
            for key, counter in sorted(self.by_series.items()):
                lines.append(f"  {key:<12}" + "".join(f"{counter.get(name, 0):>11}" for name in COUNTERS))
        for provider, reason in self.blocked_providers.items():
            lines.append(f"  BLOCKED {provider}: {reason}")
        if self.errors:
            lines.append(f"  errors ({len(self.errors)}):")
            for e in self.errors[:MAX_REPORTED_ERRORS]:
                where = " ".join(
                    str(part)
                    for part in (e.series_key, e.year, f"#{e.number}" if e.number is not None else None)
                    if part is not None
                )
                lines.append(f"    [{e.stage}] {where}: {e.error}" + (f"  <{e.url}>" if e.url else ""))
            if len(self.errors) > MAX_REPORTED_ERRORS:
                lines.append(f"    ... {len(self.errors) - MAX_REPORTED_ERRORS} more (see ingestion_errors table)")
        return "\n".join(lines)


class Ingestor:
    def __init__(
        self,
        repo: Repository,
        adapters: Mapping[str, SourceAdapter],
        *,
        concurrency: int = 2,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.repo = repo
        self.adapters = adapters
        self.concurrency = max(1, concurrency)
        self.progress = progress or (lambda _message: None)

    def discover(self, selection: Iterable[ContestSeries]) -> tuple[dict[str, list[EditionRef]], IngestReport]:
        """Discover editions for each series without fetching problems or writing."""
        report = IngestReport(mode="discover", dry_run=True)
        found = self._discover(list(selection), None, report)
        report.status = "completed" if report.ok else "completed_with_errors"
        return found, report

    def run(
        self,
        selection: Iterable[ContestSeries],
        *,
        years: frozenset[int] | None = None,
        numbers: frozenset[int] | None = None,
        mode: str = "incremental",
        dry_run: bool = False,
        resume: bool = False,
    ) -> IngestReport:
        if mode not in {"incremental", "full"}:
            raise ValueError(f"unknown mode {mode!r}")
        selection = list(selection)
        report = IngestReport(mode=mode, dry_run=dry_run)
        if not dry_run:
            report.run_id = self.repo.start_run(mode, [s.key for s in selection])

        try:
            discovered = self._discover(selection, years, report)
            resumable = self.repo.resumable_editions(report.run_id) if (resume and report.run_id) else set()

            jobs: list[tuple[ContestSeries, EditionRef]] = []
            for series in selection:
                for ref in discovered.get(series.key, []):
                    eid = edition_id(ref.series_key, ref.year)
                    if eid in resumable:
                        report.editions["resumed"] += 1
                        if report.run_id:
                            self.repo.checkpoint(eid, report.run_id)  # keep it resumable if we crash again
                        continue
                    if not dry_run:
                        self.repo.upsert_edition(ref)
                    jobs.append((series, ref))

            self._fetch_and_store(jobs, numbers, mode, dry_run, report)
        except BaseException:
            report.status = "failed"
            if report.run_id:
                self.repo.finish_run(report.run_id, "failed", report.counters(), report.to_dict())
            raise

        report.status = "completed" if report.ok else "completed_with_errors"
        if report.run_id:
            self.repo.finish_run(report.run_id, report.status, report.counters(), report.to_dict())
        return report

    # ------------------------------------------------------------------ steps

    def _error(self, report: IngestReport, series: ContestSeries, year: int | None, failure: ProblemFailure) -> None:
        entry = ErrorEntry(series.provider, series.key, year, failure.number, failure.url, failure.stage, failure.error)
        report.errors.append(entry)
        report.bump(series.key, "invalid" if failure.invalid else "failed")
        log.error(
            "ingestion failure",
            extra={
                "provider": series.provider,
                "contest": series.key,
                "year": year,
                "problem": failure.number,
                "url": failure.url,
                "stage": failure.stage,
                "error": failure.error,
            },
        )
        if report.run_id:
            self.repo.add_error(
                report.run_id,
                provider=series.provider,
                series_key=series.key,
                year=year,
                number=failure.number,
                url=failure.url,
                stage=failure.stage,
                error=failure.error,
            )

    def _block(self, report: IngestReport, provider: str, exc: HttpError) -> None:
        if provider not in report.blocked_providers:
            report.blocked_providers[provider] = str(exc)
            log.error(
                "source blocked; skipping the rest of this provider", extra={"provider": provider, "url": exc.url}
            )

    def _discover(
        self, selection: list[ContestSeries], years: frozenset[int] | None, report: IngestReport
    ) -> dict[str, list[EditionRef]]:
        found: dict[str, list[EditionRef]] = {}
        for series in selection:
            if series.provider in report.blocked_providers:
                self._error(
                    report,
                    series,
                    None,
                    ProblemFailure(None, None, "discover", f"skipped: {series.provider} is blocked"),
                )
                continue
            adapter = self.adapters[series.provider]
            try:
                refs = adapter.discover(series)
            except (SourceBlockedError, RobotsDisallowedError) as exc:
                self._block(report, series.provider, exc)
                self._error(report, series, None, ProblemFailure(None, exc.url, "discover", str(exc)))
                continue
            except (HttpError, ValueError) as exc:
                self._error(report, series, None, ProblemFailure(None, getattr(exc, "url", None), "discover", str(exc)))
                continue
            if years is not None:
                refs = [r for r in refs if r.year in years]
            if not refs:
                wanted = f" for year(s) {sorted(years)}" if years else ""
                self._error(report, series, None, ProblemFailure(None, None, "discover", f"no editions found{wanted}"))
                continue
            found[series.key] = refs
            report.editions["discovered"] += len(refs)
            self.progress(f"{series.key}: {len(refs)} edition(s) ({refs[0].year}-{refs[-1].year})")
        return found

    def _fetch_and_store(
        self,
        jobs: list[tuple[ContestSeries, EditionRef]],
        numbers: frozenset[int] | None,
        mode: str,
        dry_run: bool,
        report: IngestReport,
    ) -> None:
        # The SQLite connection belongs to this thread, so what is already stored is read
        # here, up front, and handed to the workers. A partial ingest (--number) never
        # uses edition-level skipping and never marks the edition as fully seen.
        partial = numbers is not None
        known: dict[tuple[str, int], KnownEdition | None] = {
            (s.key, r.year): (
                self.repo.known_edition(s.key, r.year) if (mode == "incremental" and not partial) else None
            )
            for s, r in jobs
        }

        pool = ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="ingest")
        try:
            futures: dict[Future[EditionResult], tuple[ContestSeries, EditionRef]] = {}
            for series, ref in jobs:
                future = pool.submit(self._guarded_fetch, series, ref, numbers, known[(series.key, ref.year)], report)
                futures[future] = (series, ref)

            for future in as_completed(futures):
                series, ref = futures[future]
                try:
                    result = future.result()
                except (SourceBlockedError, RobotsDisallowedError) as exc:
                    self._block(report, series.provider, exc)
                    report.editions["failed"] += 1
                    self._error(report, series, ref.year, ProblemFailure(None, exc.url, "fetch", str(exc)))
                    continue
                except HttpError as exc:
                    report.editions["failed"] += 1
                    self._error(report, series, ref.year, ProblemFailure(None, exc.url, "fetch", str(exc)))
                    continue
                self._store(series, result, dry_run, partial, report)
        except BaseException:
            # Ctrl+C or an unexpected error: drop the editions not started yet. (Leaving the
            # pool through "with" would wait for every queued fetch to run first.) Fetches
            # already in flight finish in the background; they never write to the database.
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)

    def _guarded_fetch(
        self,
        series: ContestSeries,
        ref: EditionRef,
        numbers: frozenset[int] | None,
        known: KnownEdition | None,
        report: IngestReport,
    ) -> EditionResult:
        if series.provider in report.blocked_providers:
            raise SourceBlockedError(f"skipped: {series.provider} is blocked", url=ref.source_url)
        log.info("fetching edition", extra={"provider": series.provider, "contest": series.key, "year": ref.year})
        return self.adapters[series.provider].fetch_edition(series, ref, known=known, only_numbers=numbers)

    def _store(
        self, series: ContestSeries, result: EditionResult, dry_run: bool, partial: bool, report: IngestReport
    ) -> None:
        ref = result.edition
        eid = edition_id(ref.series_key, ref.year)

        if result.unchanged:
            known_count = len(self.repo.numbers_for(series.key, ref.year))
            report.editions["unchanged"] += 1
            report.bump(series.key, "discovered", known_count)
            report.bump(series.key, "skipped", known_count)
            if report.run_id:
                self.repo.checkpoint(eid, report.run_id)
            return

        report.editions["fetched"] += 1
        report.bump(series.key, "discovered", len(result.problems) + len(result.skipped_numbers))
        report.bump(series.key, "skipped", len(result.skipped_numbers))
        for failure in result.failures:
            if failure.number is not None:
                report.bump(series.key, "discovered")
            self._error(report, series, ref.year, failure)

        stored = 0
        for parsed in result.problems:
            try:
                content = normalize_fragment(parsed.statement_html, parsed.source_url)
            except InvalidContentError as exc:
                self._error(
                    report,
                    series,
                    ref.year,
                    ProblemFailure(parsed.number, parsed.source_url, "validate", str(exc), True),
                )
                continue
            solution = None
            if parsed.solution_html:
                try:
                    solution = normalize_fragment(parsed.solution_html, parsed.solution_source or parsed.source_url)
                except InvalidContentError:
                    solution = None  # an empty solution is simply not stored; the statement is still valid
            if dry_run:
                outcome = self.repo.classify_problem(series.key, ref.year, parsed.number, content.content_hash)
            else:
                outcome = self.repo.upsert_problem(
                    ref, parsed, content, provider=series.provider, run_id=report.run_id, solution=solution
                )
            stored += 1
            report.bump(series.key, {INSERTED: "inserted", UPDATED: "updated", UNCHANGED: "skipped"}[outcome])

        hard_failures = [f for f in result.failures if f.number is not None or f.invalid or f.stage != "fetch"]
        clean = not hard_failures and (stored or result.skipped_numbers)
        if not clean:
            report.editions["failed"] += 1
        if not dry_run and clean and not partial:
            # Only a fully ingested edition records its source hash / checkpoint, so a
            # failed one is retried by the next incremental or resumed run.
            self.repo.set_edition_source_hash(eid, result.source_hash)
            if report.run_id:
                self.repo.checkpoint(eid, report.run_id)
        self.progress(
            f"{series.key} {ref.year}: {stored} stored, {len(result.skipped_numbers)} unchanged, "
            f"{len(result.failures)} failed"
        )
