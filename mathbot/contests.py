"""Contest registry: the single place that says which contests exist.

Each :class:`ContestSeries` has a canonical ``key`` (``AMC10A``), a display
``name``, the ``source_name`` exactly as the provider spells it, and aliases.
Lookups are case- and punctuation-insensitive, so ``amc 10a``, ``AMC_10A`` and
``Amc10a`` all resolve to the same series while the original source spelling
is kept for provenance.

``eras`` are *fallback* knowledge only: adapters discover editions from the
live source first and fall back to these ranges when discovery is not
possible. They also give the expected problem count for validation.

Optional series are registered with ``enabled=False`` and must be switched on
deliberately via ``ENABLE_CONTESTS`` after validating the source (see README).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from types import MappingProxyType

AOPS_WIKI = "aops_wiki"
AOPS_COMMUNITY = "aops_community"
CEMC = "cemc"
PROVIDERS = (AOPS_WIKI, AOPS_COMMUNITY, CEMC)
AOPS_PROVIDERS = frozenset({AOPS_WIKI, AOPS_COMMUNITY})

PROVIDER_NAMES = {
    AOPS_WIKI: "AoPS Wiki",
    AOPS_COMMUNITY: "AoPS Community",
    CEMC: "CEMC (University of Waterloo)",
}


@dataclass(frozen=True)
class Era:
    """A span of years sharing one problem count. ``last_year=None`` means ongoing."""

    first_year: int
    last_year: int | None
    problems: int

    def contains(self, year: int) -> bool:
        return self.first_year <= year and (self.last_year is None or year <= self.last_year)


@dataclass(frozen=True)
class ContestSeries:
    key: str
    name: str
    family: str
    provider: str
    source_name: str
    eras: tuple[Era, ...] = ()
    aliases: tuple[str, ...] = ()
    enabled: bool = True
    legacy_name: str | None = None  # name in the pre-2.0 database and the CEMC seed manifest
    source: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    notes: str = ""

    @property
    def id(self) -> str:
        return self.key.lower()

    def expected_problems(self, year: int) -> int | None:
        for era in self.eras:
            if era.contains(year):
                return era.problems
        return None

    def fallback_years(self, today: date | None = None) -> list[int]:
        """Years implied by ``eras``; used only when live discovery is unavailable."""
        current = (today or date.today()).year
        years: set[int] = set()
        for era in self.eras:
            years.update(range(era.first_year, (era.last_year or current) + 1))
        return sorted(years)


def normalize_alias(text: str) -> str:
    """Canonical lookup form: upper-case with everything but letters/digits removed."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def edition_id(series_key: str, year: int) -> str:
    return f"{series_key.lower()}-{year}"


def problem_id(series_key: str, year: int, number: int) -> str:
    """Stable deterministic problem ID, e.g. ``amc10a-2024-p05``."""
    return f"{edition_id(series_key, year)}-p{number:02d}"


def compress_years(years: Iterable[int]) -> str:
    """``[2016, 2017, 2018, 2020]`` -> ``2016-2018, 2020``."""
    ranges: list[tuple[int, int]] = []
    for year in sorted(set(years)):
        if ranges and year == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], year)
        else:
            ranges.append((year, year))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in ranges) or "none"


def _src(**kwargs: object) -> Mapping[str, object]:
    return MappingProxyType(kwargs)


def _wiki(key: str, name: str, family: str, title: str, hub: str, eras: Iterable[Era], **kw) -> ContestSeries:
    return ContestSeries(
        key=key,
        name=name,
        family=family,
        provider=AOPS_WIKI,
        source_name=title,
        eras=tuple(eras),
        source=_src(hub_page=hub, answer_key=family in {"AMC", "AIME"}),
        **kw,
    )


def _cemc(key: str, name: str, family: str, category: int, problems: int, **kw) -> ContestSeries:
    kw.setdefault("source_name", name)  # the title in CEMC's past-contests listing
    kw.setdefault("legacy_name", key.capitalize())  # "Euclid", "Gauss7": names in the pre-2.0 database
    return ContestSeries(
        key=key,
        name=name,
        family=family,
        provider=CEMC,
        eras=(Era(2016, None, problems),),
        source=_src(category_id=category, multiple_choice=family in {"PCF", "GAUSS"}),
        **kw,
    )


def _community(key: str, name: str, category: int, eras: Iterable[Era], **kw) -> ContestSeries:
    return ContestSeries(
        key=key,
        name=name,
        family=key,
        provider=AOPS_COMMUNITY,
        source_name=name,
        eras=tuple(eras),
        source=_src(category_id=category),
        **kw,
    )


# fmt: off
_SERIES: tuple[ContestSeries, ...] = (
    # ---- AoPS Wiki: AMC family (incl. historical AJHSME / AHSME) ----
    _wiki("AJHSME", "AJHSME", "AMC", "AJHSME", "AJHSME_Problems_and_Solutions", [Era(1985, 1998, 25)],
          notes="Predecessor of AMC 8."),
    _wiki("AMC8", "AMC 8", "AMC", "AMC_8", "AMC_8_Problems_and_Solutions", [Era(1999, None, 25)]),
    _wiki("AMC10", "AMC 10", "AMC", "AMC_10", "AMC_10_Problems_and_Solutions", [Era(2000, 2001, 25)],
          notes="Single AMC 10 before the A/B split in 2002."),
    _wiki("AMC10A", "AMC 10A", "AMC", "AMC_10A", "AMC_10_Problems_and_Solutions", [Era(2002, None, 25)]),
    _wiki("AMC10B", "AMC 10B", "AMC", "AMC_10B", "AMC_10_Problems_and_Solutions", [Era(2002, None, 25)]),
    _wiki("AMC10FALLA", "Fall AMC 10A", "AMC", "Fall_AMC_10A", "AMC_10_Problems_and_Solutions",
          [Era(2021, 2021, 25)], aliases=("AMC 10A Fall", "Fall 10A"),
          notes="Second 2021 administration (November 2021)."),
    _wiki("AMC10FALLB", "Fall AMC 10B", "AMC", "Fall_AMC_10B", "AMC_10_Problems_and_Solutions",
          [Era(2021, 2021, 25)], aliases=("AMC 10B Fall", "Fall 10B")),
    _wiki("AHSME", "AHSME", "AMC", "AHSME", "AHSME_Problems_and_Solutions",
          [Era(1950, 1959, 50), Era(1960, 1967, 40), Era(1968, 1973, 35), Era(1974, 1999, 30)],
          notes="Predecessor of AMC 12; problem count changed over time."),
    _wiki("AMC12", "AMC 12", "AMC", "AMC_12", "AMC_12_Problems_and_Solutions", [Era(2000, 2001, 25)],
          notes="Single AMC 12 before the A/B split in 2002."),
    _wiki("AMC12A", "AMC 12A", "AMC", "AMC_12A", "AMC_12_Problems_and_Solutions", [Era(2002, None, 25)]),
    _wiki("AMC12B", "AMC 12B", "AMC", "AMC_12B", "AMC_12_Problems_and_Solutions", [Era(2002, None, 25)]),
    _wiki("AMC12FALLA", "Fall AMC 12A", "AMC", "Fall_AMC_12A", "AMC_12_Problems_and_Solutions",
          [Era(2021, 2021, 25)], aliases=("AMC 12A Fall", "Fall 12A")),
    _wiki("AMC12FALLB", "Fall AMC 12B", "AMC", "Fall_AMC_12B", "AMC_12_Problems_and_Solutions",
          [Era(2021, 2021, 25)], aliases=("AMC 12B Fall", "Fall 12B")),
    # ---- AoPS Wiki: AIME ----
    _wiki("AIME", "AIME", "AIME", "AIME", "AIME_Problems_and_Solutions", [Era(1983, 1999, 15)],
          notes="Single AIME before the I/II split in 2000."),
    _wiki("AIME1", "AIME I", "AIME", "AIME_I", "AIME_Problems_and_Solutions", [Era(2000, None, 15)],
          aliases=("AIME I", "AIME 1")),
    _wiki("AIME2", "AIME II", "AIME", "AIME_II", "AIME_Problems_and_Solutions", [Era(2000, None, 15)],
          aliases=("AIME II", "AIME 2")),
    # ---- AoPS Wiki: olympiads ----
    _wiki("USAJMO", "USAJMO", "USAJMO", "USAJMO", "USAJMO_Problems_and_Solutions", [Era(2010, None, 6)]),
    _wiki("USAMO", "USAMO", "USAMO", "USAMO", "USAMO_Problems_and_Solutions",
          [Era(1972, 1995, 5), Era(1996, None, 6)],
          notes="Five problems until 1995, six from 1996."),
    _wiki("IMO", "IMO", "IMO", "IMO", "IMO_Problems_and_Solutions", [Era(1959, None, 6)],
          notes="A few early years had seven problems; discovery uses the count the wiki lists."),
    # ---- AoPS Community ----
    # Early CMOs were longer; counts per year as in the AoPS Contest Collection.
    _community("CMO", "CMO", 3277,
               [Era(1969, 1972, 10), Era(1973, 1974, 7), Era(1975, 1976, 8), Era(1977, 1977, 7),
                Era(1978, 1978, 6), Era(1979, None, 5)],
               aliases=("Canadian Mathematical Olympiad", "Canada MO"),
               notes="Canadian Mathematical Olympiad (AoPS Contest Collections)."),
    _community("CJMO", "CJMO", 1231801, [Era(2020, None, 5)], enabled=False,
               aliases=("Canadian Junior Mathematical Olympiad",),
               notes="Optional. Category data not validated; enable with ENABLE_CONTESTS=CJMO."),
    _community("CMOQR", "CMOQR", 3280, [Era(2009, None, 8)], enabled=False,
               aliases=("CMO Qualifying Repechage", "Repechage"),
               notes="Optional. Category data not validated; enable with ENABLE_CONTESTS=CMOQR."),
    # ---- CEMC ----
    _cemc("EUCLID", "Euclid", "EUCLID", 24, 10),
    _cemc("FRYER", "Fryer", "FGH", 25, 4),
    _cemc("GALOIS", "Galois", "FGH", 25, 4),
    _cemc("HYPATIA", "Hypatia", "FGH", 25, 4),
    _cemc("PASCAL", "Pascal", "PCF", 14, 25),
    _cemc("CAYLEY", "Cayley", "PCF", 14, 25),
    _cemc("FERMAT", "Fermat", "PCF", 14, 25),
    _cemc("CIMC", "CIMC", "CSIMC", 23, 9, source_name="Canadian Intermediate Mathematics Contest",
          legacy_name="CIMC", aliases=("Canadian Intermediate",),
          notes="Canadian Intermediate Mathematics Contest (November). Part A: 6 short-answer, "
                "Part B: 3 full-solution questions."),
    _cemc("CSMC", "CSMC", "CSIMC", 23, 9, source_name="Canadian Senior Mathematics Contest",
          legacy_name="CSMC", aliases=("Canadian Senior",),
          notes="Canadian Senior Mathematics Contest (November). Part A: 6 short-answer, "
                "Part B: 3 full-solution questions."),
    _cemc("GAUSS7", "Gauss (Grade 7)", "GAUSS", 13, 25, enabled=False, aliases=("Gauss 7",),
          notes="Out of scope by default; enable with ENABLE_CONTESTS=GAUSS7."),
    _cemc("GAUSS8", "Gauss (Grade 8)", "GAUSS", 13, 25, enabled=False, aliases=("Gauss 8",),
          notes="Out of scope by default; enable with ENABLE_CONTESTS=GAUSS8."),
)
# fmt: on

FAMILY_NAMES = {
    "AMC": "AMC (8 / 10 / 12, AJHSME, AHSME)",
    "AIME": "AIME",
    "USAJMO": "USAJMO",
    "USAMO": "USAMO",
    "IMO": "IMO",
    "CMO": "Canadian Mathematical Olympiad",
    "CJMO": "Canadian Junior Mathematical Olympiad",
    "CMOQR": "CMO Qualifying Repechage",
    "EUCLID": "Euclid",
    "FGH": "Fryer / Galois / Hypatia",
    "PCF": "Pascal / Cayley / Fermat",
    "CSIMC": "Canadian Senior / Intermediate (CSMC / CIMC)",
    "GAUSS": "Gauss",
}


class UnknownContestError(LookupError):
    """Raised when a name matches no series, family or provider."""


class ContestRegistry:
    """Lookup over the series table with enable/disable overrides applied."""

    def __init__(
        self,
        series: Iterable[ContestSeries] = _SERIES,
        *,
        enable: Iterable[str] = (),
        disable: Iterable[str] = (),
    ) -> None:
        enable_n = {normalize_alias(k) for k in enable}
        disable_n = {normalize_alias(k) for k in disable}
        self._series: dict[str, ContestSeries] = {}
        self._aliases: dict[str, str] = {}
        for s in series:
            norm = normalize_alias(s.key)
            if norm in enable_n:
                s = replace(s, enabled=True)
            if norm in disable_n:
                s = replace(s, enabled=False)
            if s.key in self._series:
                raise ValueError(f"duplicate contest key {s.key}")
            self._series[s.key] = s
            for alias in (s.key, s.name, s.source_name, *s.aliases):
                alias_n = normalize_alias(alias)
                owner = self._aliases.setdefault(alias_n, s.key)
                if owner != s.key:
                    raise ValueError(f"alias {alias!r} is claimed by both {owner} and {s.key}")
        unknown = (enable_n | disable_n) - {normalize_alias(k) for k in self._series}
        if unknown:
            raise UnknownContestError(f"ENABLE_CONTESTS/DISABLE_CONTESTS name unknown contest(s): {sorted(unknown)}")

    def all(self, *, include_disabled: bool = False) -> list[ContestSeries]:
        return [s for s in self._series.values() if include_disabled or s.enabled]

    def get(self, key: str) -> ContestSeries:
        return self._series[key]

    def aliases(self) -> dict[str, str]:
        """Normalized alias -> series key."""
        return dict(self._aliases)

    def resolve(self, text: str, *, include_disabled: bool = False) -> ContestSeries | None:
        """Resolve one series from any alias, ignoring case, spaces and punctuation."""
        key = self._aliases.get(normalize_alias(text))
        if key is None:
            return None
        series = self._series[key]
        return series if (series.enabled or include_disabled) else None

    def select(self, text: str, *, include_disabled: bool = False) -> list[ContestSeries]:
        """Resolve a series alias, a family (``PCF``) or a provider (``cemc``)."""
        norm = normalize_alias(text)
        # A family that shares its name with a series (AIME, AMC...) means the whole family.
        family = [s for s in self.all(include_disabled=include_disabled) if normalize_alias(s.family) == norm]
        if family:
            return family
        series = self.resolve(text, include_disabled=include_disabled)
        if series is not None:
            return [series]
        provider = [s for s in self.all(include_disabled=include_disabled) if normalize_alias(s.provider) == norm]
        if provider:
            return provider
        raise UnknownContestError(f"unknown contest, family or source: {text!r}")

    def families(self) -> dict[str, list[ContestSeries]]:
        out: dict[str, list[ContestSeries]] = {}
        for s in self.all():
            out.setdefault(s.family, []).append(s)
        return out
