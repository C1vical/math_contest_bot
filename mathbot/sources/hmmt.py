"""HMMT adapter: the Harvard-MIT Mathematics Tournament (February and November) and HMIC.

Verified against the live site on 2026-11-26 (plain HTTP, honest User-Agent; robots.txt
allows every agent except named AI-training crawlers, and its content signal permits this
kind of reference use):

Listing  https://www.hmmt.org/www/archive/problems
  * A grid with columns November / February / Invitational; each cell links an edition page
    ``/www/archive/<id>`` labelled with its year.
Edition pages
  * February / November: ``table.display-tournament`` rows, one per round: the round's name
    (Team, Algebra and Number Theory, Guts, General part 1, ...) and links to
    ``problems.pdf`` and ``solutions.pdf`` on hmmt-archive.s3.amazonaws.com. Rounds changed
    over the years (Algebra and Calculus until 2010, Algebra and Number Theory from 2017, ...);
    each (tournament, round) is its own contest series, all in the family ``HMMT``.
    Integration Bee, Oral and Power rounds, 2000's one General round and 2011's paired
    subject tests are not stored.
  * HMIC: links to one ``problems.pdf`` and ``solutions.pdf``.

Papers (typeset PDFs; see ``mathbot/pdf.py`` for how a problem becomes a picture)
  * Problems are numbered "1.", "2.", ... (with the points in brackets from about 2003).
    Guts rounds repeat a header with a "Team ID#" box at the top of every page.
  * A paper is stored only when its problems PDF and its solutions PDF agree on the number of
    problems (each read independently, labels accepted only in sequence). The answer key is
    taken from the solutions' "Answer:" lines when the answer is a plain integer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from mathbot.contests import HMMT, ContestSeries
from mathbot.http import PoliteClient
from mathbot.models import EditionRef, EditionResult, KnownEdition, ParsedProblem, ProblemFailure
from mathbot.pdf import (
    Layout,
    PdfLayoutError,
    Problem,
    check_problems,
    misaligned_lines,
    pdf_digest,
    read_document,
    segment,
    statement_html,
)

log = logging.getLogger(__name__)

BASE_URL = "https://www.hmmt.org"
ARCHIVE_URL = f"{BASE_URL}/www/archive/problems"

# Part of every edition fingerprint: bump when parsing output can change for an unchanged PDF.
# 2: answers only when printed as a plain number level with "Answer:"
# 3: a guts page's mid-page cut line and header end the problem above them
# 4: answer sheets end the last problem; restarted numbering and runaway problems reject a paper
# 5: the closing "Time limit" line is not part of the last problem
PARSER_VERSION = 5

MAX_MISALIGNED = 0.05  # see pdf.misaligned_lines

_C = re.compile
LAYOUT = Layout(
    start=_C(r"(?P<number>\d{1,2})\s?\.(?:\s|$)"),
    furniture=(
        _C(r"^(?:\.\s*){8,}$"),  # the cut line of a guts round page
        _C(r"(?i)\bGUTS ROUND\b"),
        _C(r"^Organization\b|\bTeam ID ?#"),
        _C(r"^©\s*\d{4}\s+HMMT"),
    ),
    # Answer sheets appended to some papers ("PUT LABEL HERE", Name, Team, Score) and the closing
    # "Time limit: 4 hours" end the last problem.
    stop=(
        _C(r"(?i)^(?:put label here|name|school|score\s*:?|team\s*:?|answer sheet|answers?)\s*$"),
        _C(r"(?i)^time limit\b"),
    ),
    repeated_heads=True,
)
_ANSWER = _C(r"^Answer\s*:\s*(.+?)\s*$")
_ANSWER_LABEL = _C(r"^Answer\s*:")
_INTEGER = _C(r"^-?\d{1,15}$")

# (tournament column, round name on the edition page) -> series key
ROUNDS: dict[tuple[str, str], str] = {
    ("feb", "Team"): "HMMTFTEAM",
    ("feb", "Team A"): "HMMTFTEAMA",
    ("feb", "Team B"): "HMMTFTEAMB",
    ("feb", "Algebra and Number Theory"): "HMMTFALGNT",
    ("feb", "Algebra"): "HMMTFALG",
    ("feb", "Combinatorics"): "HMMTFCOMB",
    ("feb", "Geometry"): "HMMTFGEO",
    ("feb", "Calculus"): "HMMTFCALC",
    ("feb", "Advanced Topics"): "HMMTFADV",
    ("feb", "General part 1"): "HMMTFGEN1",
    ("feb", "General part 2"): "HMMTFGEN2",
    ("feb", "Guts"): "HMMTFGUTS",
    ("nov", "General"): "HMMTNGEN",
    ("nov", "General part 1"): "HMMTNGEN1",
    ("nov", "General part 2"): "HMMTNGEN2",
    ("nov", "Theme"): "HMMTNTHEME",
    ("nov", "Team"): "HMMTNTEAM",
    ("nov", "Guts"): "HMMTNGUTS",
    ("hmic", ""): "HMIC",
}
_COLUMNS = {"november": "nov", "february": "feb", "invitational": "hmic"}


@dataclass(frozen=True)
class Round:
    series_key: str
    year: int
    name: str
    edition_url: str
    problems_url: str | None
    solutions_url: str | None


def parse_archive(html: str) -> list[tuple[str, int, str]]:
    """``(tournament, year, edition page URL)`` for every edition in the archive grid."""
    soup = BeautifulSoup(html, "html5lib")
    columns: list[str | None] = []
    out = []
    for row in soup.select("#content .row"):
        cells = row.find_all("div", class_="col-2")
        labels = [cell.get_text(" ", strip=True).lower() for cell in cells]
        if any(label in _COLUMNS for label in labels):
            columns = [_COLUMNS.get(label) for label in labels]
            continue
        for column, cell in zip(columns, cells, strict=False):
            anchor = cell.find("a", href=True)
            if column is None or anchor is None:
                continue
            year = anchor.get_text(strip=True)
            if year.isdecimal() and len(year) == 4:
                out.append((column, int(year), urljoin(BASE_URL, anchor["href"])))
    return out


def parse_edition(html: str, tournament: str, year: int, url: str) -> list[Round]:
    """The rounds of one edition page whose problems are stored (see ``ROUNDS``)."""
    soup = BeautifulSoup(html, "html5lib")
    found: list[tuple[str, list[str]]] = []
    for tr in soup.select("table.display-tournament tr"):
        cells = tr.find_all("td")
        if cells:
            found.append((cells[0].get_text(" ", strip=True), [a["href"] for a in tr.find_all("a", href=True)]))
    if not found:  # HMIC: links straight under the heading
        found.append(("", [a["href"] for a in soup.select("#content a[href]")]))
    rounds = []
    for name, links in found:
        key = ROUNDS.get((tournament, name))
        if key is None:
            continue
        absolute = [urljoin(url, link) for link in links]
        rounds.append(
            Round(
                key,
                year,
                name,
                url,
                next((link for link in absolute if link.endswith("/problems.pdf")), None),
                next((link for link in absolute if link.endswith("/solutions.pdf")), None),
            )
        )
    return rounds


class HmmtAdapter:
    provider = HMMT

    def __init__(self, client: PoliteClient, *, refresh: bool = False) -> None:
        self.client = client
        self.refresh = refresh
        self._rounds: list[Round] | None = None

    def _all_rounds(self) -> list[Round]:
        if self._rounds is None:
            rounds = []
            for tournament, year, url in parse_archive(self.client.get(ARCHIVE_URL, refresh=self.refresh).text):
                page = self.client.get(url, refresh=self.refresh).text
                rounds.extend(parse_edition(page, tournament, year, url))
            self._rounds = rounds
        return self._rounds

    def discover(self, series: ContestSeries) -> list[EditionRef]:
        editions: dict[int, EditionRef] = {}
        for round_ in self._all_rounds():
            if round_.series_key != series.key or round_.problems_url is None:
                continue
            editions[round_.year] = EditionRef(
                series_key=series.key,
                year=round_.year,
                label=f"{series.name} {round_.year}",
                source_url=round_.problems_url,
                source_identifier=round_.problems_url.split("tournaments/", 1)[-1],
                expected_problems=None,  # checked against the solutions instead
                extra={"solutions_url": round_.solutions_url, "edition_url": round_.edition_url},
            )
        return [editions[year] for year in sorted(editions)]

    def fetch_edition(
        self,
        series: ContestSeries,
        edition: EditionRef,
        *,
        known: KnownEdition | None = None,
        only_numbers: frozenset[int] | None = None,
    ) -> EditionResult:
        solutions_url = str(edition.extra.get("solutions_url") or "") or None
        problems_pdf = self.client.get(edition.source_url, refresh=self.refresh).content
        solutions_pdf = self.client.get(solutions_url, refresh=self.refresh).content if solutions_url else None
        fingerprint = pdf_digest(problems_pdf) + (f"+{pdf_digest(solutions_pdf)}" if solutions_pdf else "")
        result = EditionResult(edition=edition, source_hash=f"{fingerprint}+parser{PARSER_VERSION}")
        if known is not None and known.source_hash == result.source_hash:
            result.unchanged = True
            return result
        try:
            if solutions_pdf is None:
                raise PdfLayoutError("no solutions PDF to check the problems against")
            problems = _read(problems_pdf, "problems")
            solutions = _read(solutions_pdf, "solutions")
            check_problems(problems, [(None, n) for n in range(1, len(problems) + 1)])
            if len(solutions) != len(problems):
                raise PdfLayoutError(
                    f"the problems PDF has {len(problems)} problems but the solutions PDF {len(solutions)}"
                )
        except PdfLayoutError as exc:
            result.failures.append(ProblemFailure(None, edition.source_url, "parse", str(exc), invalid=True))
            return result

        digest = pdf_digest(problems_pdf)
        for number, (problem, solution) in enumerate(zip(problems, solutions, strict=True), start=1):
            if only_numbers is not None and number not in only_numbers:
                continue
            html = statement_html(edition.source_url, digest, problem)
            answer = _answer(solution)
            result.problems.append(
                ParsedProblem(
                    number=number,
                    statement_html=html,
                    raw_payload=html,
                    source_url=edition.source_url,
                    source_identifier=f"{edition.source_identifier}#{number}",
                    source_revision=digest,
                    answer=answer,
                    answer_source=solutions_url if answer else None,
                    solution_html=_SOLUTION_HTML,
                    solution_source=solutions_url,
                )
            )
        return result


# The official solutions are a PDF: stored as a pointer so the bot can link it.
_SOLUTION_HTML = "<p>Official solutions (PDF)</p>"


def _read(data: bytes, what: str) -> list[Problem]:
    document = read_document(data)
    blank = misaligned_lines(document)
    if blank > MAX_MISALIGNED:
        raise PdfLayoutError(f"{blank:.0%} of the {what} PDF's text lines are not where its text is printed")
    try:
        problems = segment(document, LAYOUT)
    except PdfLayoutError as exc:
        raise PdfLayoutError(f"{what} PDF: {exc}") from exc
    # A label out of sequence set exactly where this problem's own label is means the paper
    # numbers in a way the reader does not follow (2008 November General Part 2 restarts at 1
    # for each themed set): its problems and solutions would agree and both be wrong.
    for number, problem in enumerate(problems, start=1):
        pages = sorted({piece.page for piece in problem.pieces})
        if what == "problems" and len(pages) > 2:
            # No HMMT problem fills more than a page break: this one ran on into what follows the
            # paper (answer sheets, scratch pages).
            raise PdfLayoutError(f"{what} PDF: problem {number} runs over {len(pages)} pages")
        own = next((line for line in problem.lines if LAYOUT.start.match(line.text)), None)
        for line in problem.lines:
            if line is not own and own is not None and abs(line.x0 - own.x0) < 2 and LAYOUT.start.match(line.text):
                raise PdfLayoutError(
                    f"{what} PDF: problem {number} contains another problem label ({line.text[:30]!r}); "
                    "its numbering can't be followed"
                )
    return problems


def _answer(solution: Problem) -> str | None:
    """The answer given after "Answer:" in a solution, when it is a plain integer.

    The value is usually set apart from the label, as separate text on the same line. It is
    taken only from text exactly level with the label; if anything else overlaps that line
    nearby (the denominator of a fraction, an exponent, a root), the answer is not a plain
    number and none is stored - a misread answer would be worse than none.
    """
    labels = [line for line in solution.lines if _ANSWER_LABEL.match(line.text)]
    if len(labels) != 1:
        return None
    label = labels[0]
    inline = _ANSWER.match(label.text)
    value = inline.group(1) if inline else ""
    near = [
        line
        for line in solution.lines
        if line is not label
        and line.page == label.page
        and label.x1 - 1 <= line.x0 <= label.x1 + 150
        and line.top < label.bottom + 4
        and line.bottom > label.top - 4
    ]
    level = [line for line in near if abs(line.top - label.top) < 1.5 and abs(line.bottom - label.bottom) < 1.5]
    if len(level) != len(near) or (value and level) or len(level) > 1:
        return None
    if level:
        value = level[0].text
    answer = value.replace(",", "").replace("−", "-").replace(" ", "").rstrip(".")
    return answer if _INTEGER.match(answer) else None


__all__ = ["HmmtAdapter", "parse_archive", "parse_edition"]
