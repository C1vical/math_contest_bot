"""HMMT: the archive pages and the papers (fixtures captured from hmmt.org on 2026-11-26)."""

from __future__ import annotations

import pytest

from mathbot.contests import ContestRegistry
from mathbot.http import Response, cache_key
from mathbot.models import EditionRef, KnownEdition
from mathbot.sources import hmmt
from tests.conftest import FIXTURES, read_fixture

PROBLEMS = (FIXTURES / "hmmt" / "2026-feb-algnt-problems.pdf").read_bytes()
SOLUTIONS = (FIXTURES / "hmmt" / "2026-feb-algnt-solutions.pdf").read_bytes()
PROBLEMS_URL = "https://hmmt-archive.s3.amazonaws.com/tournaments/2026/feb/algnt/problems.pdf"
SOLUTIONS_URL = "https://hmmt-archive.s3.amazonaws.com/tournaments/2026/feb/algnt/solutions.pdf"


class BytesClient:
    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages

    def get(self, url, params=None, **_options):
        return Response(cache_key(url, params), 200, {"content-type": "application/pdf"}, self.pages[url])


def test_archive_grid_gives_tournament_year_and_page():
    editions = hmmt.parse_archive(read_fixture("hmmt", "archive_trimmed.html"))
    assert ("nov", 2026, "https://www.hmmt.org/www/archive/301") in editions
    assert ("feb", 2026, "https://www.hmmt.org/www/archive/292") in editions
    assert ("hmic", 2026, "https://www.hmmt.org/www/archive/294") in editions
    assert {tournament for tournament, _, _ in editions} == {"nov", "feb", "hmic"}


def test_edition_page_lists_the_stored_rounds_only():
    url = "https://www.hmmt.org/www/archive/292"
    rounds = hmmt.parse_edition(read_fixture("hmmt", "edition_feb_2026_trimmed.html"), "feb", 2026, url)
    keys = [r.series_key for r in rounds]
    assert keys == ["HMMTFTEAM", "HMMTFALGNT", "HMMTFCOMB", "HMMTFGEO", "HMMTFGUTS"]  # no Integration Bee
    algnt = rounds[1]
    assert algnt.problems_url == PROBLEMS_URL and algnt.solutions_url == SOLUTIONS_URL


def test_hmic_page_has_one_paper():
    url = "https://www.hmmt.org/www/archive/294"
    [hmic] = hmmt.parse_edition(read_fixture("hmmt", "edition_hmic_2026_trimmed.html"), "hmic", 2026, url)
    assert hmic.series_key == "HMIC" and hmic.problems_url.endswith("/2026/hmic/problems.pdf")
    assert hmic.solutions_url.endswith("/2026/hmic/solutions.pdf")


def test_every_round_maps_to_a_registered_hmmt_series():
    registry = ContestRegistry()
    for key in set(hmmt.ROUNDS.values()):
        series = registry.get(key)
        assert series.provider == "hmmt" and series.family == "HMMT"


def edition():
    series = ContestRegistry().get("HMMTFALGNT")
    ref = EditionRef(
        "HMMTFALGNT",
        2026,
        "HMMT February Algebra and Number Theory 2026",
        PROBLEMS_URL,
        "2026/feb/algnt/problems.pdf",
        None,
        extra={"solutions_url": SOLUTIONS_URL},
    )
    return series, ref


def test_a_paper_is_stored_when_problems_and_solutions_agree():
    series, ref = edition()
    adapter = hmmt.HmmtAdapter(BytesClient({PROBLEMS_URL: PROBLEMS, SOLUTIONS_URL: SOLUTIONS}))
    result = adapter.fetch_edition(series, ref)
    assert not result.failures
    assert [p.number for p in result.problems] == list(range(1, 11))
    first = result.problems[0]
    assert "pdf-crop" in first.statement_html and first.solution_source == SOLUTIONS_URL
    answers = [p.answer for p in result.problems]
    assert all(a is None or a.lstrip("-").isdecimal() for a in answers)  # integers only, never garbled maths
    # Only answers printed as a plain number level with "Answer:" (2: 420261, 3: 48, 8: 279).
    assert answers == [None, "420261", "48", None, None, None, None, "279", None, None]

    again = adapter.fetch_edition(series, ref, known=KnownEdition(source_hash=result.source_hash))
    assert again.unchanged


def test_disagreeing_counts_reject_the_paper():
    series, ref = edition()
    # The problems PDF given as its own solutions still agrees; a one-page cut does not.
    adapter = hmmt.HmmtAdapter(BytesClient({PROBLEMS_URL: PROBLEMS, SOLUTIONS_URL: PROBLEMS[:2000]}))
    result = adapter.fetch_edition(series, ref)
    assert not result.problems and result.failures and result.failures[0].invalid


@pytest.mark.parametrize(
    "line, expected",
    [("Answer: 2026", "2026"), ("Answer: −12", "-12"), ("Answer: 1,000", "1000"), ("Answer: 3 √ 2", None)],
)
def test_only_integer_answers_are_kept(line, expected):
    from mathbot.pdf import Line, Problem

    solution = Problem(None, 1, lines=[Line(0, 0, 0, 10, 10, "1. Statement"), Line(0, 0, 20, 10, 30, line)])
    assert hmmt._answer(solution) == expected


def test_an_answer_set_apart_on_its_line_is_read_but_a_stacked_fraction_is_not():
    from mathbot.pdf import Line, Problem

    label = Line(0, 97, 363.7, 133, 373.7, "Answer:")
    assert hmmt._answer(Problem(None, 2, lines=[label, Line(0, 154, 363.7, 190, 373.7, "420261")])) == "420261"
    # The 2026 paper's answer -1/21: "-1" level with the label, "21" below it.
    fraction = [label, Line(0, 154, 363.0, 165, 374.2, "− 1"), Line(0, 163, 370.3, 172, 376.5, "21")]
    assert hmmt._answer(Problem(None, 1, lines=fraction)) is None
