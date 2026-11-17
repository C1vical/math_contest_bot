"""The contract every source adapter implements."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from mathbot.contests import ContestSeries
from mathbot.models import EditionRef, EditionResult, KnownEdition

DATA_DIR = Path(__file__).resolve().parent / "data"


class SourceAdapter(Protocol):
    """Discovers editions of a series and extracts their problems.

    Adapters never write to the database and never swallow errors: a failed
    fetch raises (edition-level) or is reported as a ``ProblemFailure``
    (problem-level). ``known`` carries what is already stored so the adapter
    can skip unchanged content in incremental mode; ``None`` means full refresh.
    """

    provider: str

    def discover(self, series: ContestSeries) -> list[EditionRef]: ...

    def fetch_edition(
        self,
        series: ContestSeries,
        edition: EditionRef,
        *,
        known: KnownEdition | None = None,
        only_numbers: frozenset[int] | None = None,
    ) -> EditionResult: ...


def load_manifest(name: str) -> dict:
    """Load a seed manifest shipped with the package (data carried over from pre-2.0)."""
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))
