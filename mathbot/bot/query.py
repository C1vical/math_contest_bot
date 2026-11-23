"""What users type, turned into lookups; and the filters an "Another random" button carries.

Pure functions and dataclasses: no Discord objects, no database, easy to test.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, fields

from mathbot.db.repo import DIFFICULTIES

MIN_YEAR, MAX_YEAR = 1900, 2100
MAX_NUMBER = 60  # AHSME had 50 problems; nothing has more
CUSTOM_ID_LIMIT = 100  # Discord's limit for a component custom_id
RANDOM_PREFIX = "mathbot:rand:"

_SEPARATORS = re.compile(r"[\s,_/-]+")
_EXPLICIT_NUMBER = re.compile(r"^(?:#|p|q|no\.?)(\d{1,3})$", re.IGNORECASE)
_NUMBER_WORDS = {"problem", "prob", "question", "q", "p", "no", "number", "#"}


@dataclass(frozen=True)
class ProblemRef:
    contest: str
    year: int | None
    number: int | None


def _number(text: str) -> int | None:
    value = int(text)
    return value if 1 <= value <= MAX_NUMBER else None


def parse_problem_reference(text: str, resolves: Callable[[str], bool]) -> ProblemRef:
    """Split ``2019 AMC 10A #25``, ``amc10a 2019 25`` or ``amc10a-2019-p25`` into its parts.

    A four-digit token is the year; ``#25``, ``P25``, ``Q25`` or ``Problem 25`` is the
    number. A bare trailing number is the problem number only when the name without it
    is a contest and the name with it is not, so ``AMC 8 2019 5`` is AMC 8 problem 5 and
    ``AIME 1 2019`` stays AIME I. ``resolves(name)`` says whether a name is a contest.
    """
    # Digits are tested with isdecimal(): isdigit() also accepts "²", which int() refuses.
    year: int | None = None
    number: int | None = None
    name: list[str] = []
    tokens = [t for t in _SEPARATORS.split(text.strip()) if t]
    expect_number = False
    for token in tokens:
        if expect_number and token.isdecimal():
            number, expect_number = _number(token), False
            if number is None:
                name.append(token)
            continue
        expect_number = False
        explicit = _EXPLICIT_NUMBER.match(token)
        if explicit and number is None and _number(explicit.group(1)) is not None:
            number = _number(explicit.group(1))
        elif token.isdecimal() and len(token) == 4 and year is None and MIN_YEAR <= int(token) <= MAX_YEAR:
            year = int(token)
        elif token.lower() in _NUMBER_WORDS and number is None:
            expect_number = True
        else:
            name.append(token)

    if number is None and len(name) > 1 and name[-1].isdecimal() and _number(name[-1]) is not None:
        shorter = " ".join(name[:-1])
        if not resolves(" ".join(name)) and resolves(shorter):
            number = _number(name.pop())
    return ProblemRef(" ".join(name), year, number)


# --------------------------------------------------------------------------- random filters

_FIELD_CODES = {
    "contest": "c",
    "year": "y",
    "year_from": "f",
    "year_to": "t",
    "number": "n",
    "difficulty": "d",
    "source": "s",
    "owner": "u",
}
_CODE_FIELDS = {code: name for name, code in _FIELD_CODES.items()}
_MAX_USER_ID = 2**64 - 1
_INT_RANGES = {
    "year": (MIN_YEAR, MAX_YEAR),
    "year_from": (MIN_YEAR, MAX_YEAR),
    "year_to": (MIN_YEAR, MAX_YEAR),
    "number": (1, MAX_NUMBER),
    "owner": (0, _MAX_USER_ID),
}
_TOKEN = re.compile(r"^[A-Za-z0-9_]{1,40}$")


@dataclass(frozen=True)
class RandomFilters:
    """The options of a ``/random`` call, so "Another random" can repeat it after a restart."""

    contest: str | None = None  # a series, family or source name, as typed (normalized)
    year: int | None = None
    year_from: int | None = None
    year_to: int | None = None
    number: int | None = None
    difficulty: str | None = None
    source: str | None = None
    owner: int | None = None  # the user allowed to press the button (None: anyone, older buttons)

    def custom_id(self) -> str | None:
        """``mathbot:rand:c=AMC10A;f=2010``, or None when it would not fit Discord's limit."""
        parts = [
            f"{_FIELD_CODES[f.name]}={getattr(self, f.name)}" for f in fields(self) if getattr(self, f.name) is not None
        ]
        custom_id = RANDOM_PREFIX + ";".join(parts)
        return custom_id if len(custom_id) <= CUSTOM_ID_LIMIT else None

    @classmethod
    def from_custom_id(cls, custom_id: str) -> RandomFilters | None:
        """Decode :meth:`custom_id`; None for anything malformed (ids come from clients)."""
        if not custom_id.startswith(RANDOM_PREFIX):
            return None
        values: dict[str, object] = {}
        body = custom_id[len(RANDOM_PREFIX) :]
        for part in filter(None, body.split(";")):
            code, _, raw = part.partition("=")
            name = _CODE_FIELDS.get(code)
            if name is None or not _TOKEN.match(raw):
                return None
            if name in _INT_RANGES:
                low, high = _INT_RANGES[name]
                if not raw.isdecimal() or not low <= int(raw) <= high:
                    return None
                values[name] = int(raw)
            else:
                values[name] = raw
        if values.get("difficulty") not in (None, *DIFFICULTIES):
            return None
        return cls(**values)  # type: ignore[arg-type]

    def describe(self) -> str:
        """``contest `PCF`, years 2016-2020, problem 5, easy`` (empty when unfiltered)."""
        parts = []
        if self.contest:
            parts.append(f"contest `{self.contest}`")
        if self.year is not None:
            parts.append(f"year {self.year}")
        if self.year_from is not None and self.year_to is not None:
            parts.append(f"years {self.year_from}-{self.year_to}")
        elif self.year_from is not None:
            parts.append(f"years {self.year_from} onward")
        elif self.year_to is not None:
            parts.append(f"years up to {self.year_to}")
        if self.number is not None:
            parts.append(f"problem {self.number}")
        if self.difficulty:
            parts.append(self.difficulty)
        if self.source:
            parts.append(f"source {self.source}")
        return ", ".join(parts)
