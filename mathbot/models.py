"""Plain data carriers shared by source adapters, the ingestion pipeline and the DB."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EditionRef:
    """One contest sitting (series + year) as discovered at a source."""

    series_key: str
    year: int
    label: str  # the source's own title, e.g. "2021 Fall AMC 10A Problems"
    source_url: str
    source_identifier: str | None = None  # wiki page title / community category id / file name
    expected_problems: int | None = None
    origin: str = "discovered"  # "discovered" (live source) or "fallback" (seed manifest / era table)
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedProblem:
    """A problem exactly as extracted from the source, before normalization."""

    number: int
    statement_html: str  # raw source fragment for this problem
    source_url: str
    raw_payload: str  # source HTML / JSON this problem was cut from (provenance)
    section: str | None = None
    source_identifier: str | None = None
    source_revision: str | None = None
    answer: str | None = None
    answer_source: str | None = None
    solution_html: str | None = None  # official solution fragment, when the source publishes one
    solution_source: str | None = None


@dataclass(frozen=True)
class ProblemFailure:
    """A problem (or a whole edition when ``number`` is None) that could not be ingested."""

    number: int | None
    url: str | None
    stage: str  # "fetch", "parse", "validate", "discover"
    error: str
    invalid: bool = False  # True when the source answered but the content is unusable


@dataclass
class EditionResult:
    edition: EditionRef
    problems: list[ParsedProblem] = field(default_factory=list)
    failures: list[ProblemFailure] = field(default_factory=list)
    skipped_numbers: list[int] = field(default_factory=list)  # unchanged at source, not re-fetched
    source_hash: str | None = None  # hash of the edition-level payload when there is one
    unchanged: bool = False  # the whole edition is unchanged since last ingest


@dataclass(frozen=True)
class KnownEdition:
    """What the database already holds for an edition (drives incremental mode)."""

    source_hash: str | None = None
    revisions: dict[int, str] = field(default_factory=dict)  # problem number -> source revision
