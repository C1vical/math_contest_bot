"""Canadian Mathematical Society adapter: COMC (Canadian Open Mathematics Challenge, 1996-) and
CLMC (Canada Lynx Mathematics Competition, 2023-).

Verified against the live site on 2026-11-26 (plain HTTP, honest User-Agent; robots.txt only
disallows the CMO problem-set pages):

Listing  https://cms.math.ca/competitions/ (plus /competitions/comc/ and /competitions/clmc/,
         which also link the newest paper before it reaches the archive)
  * Links labelled "Problem Set" and "Problems with Solutions" / "Solutions" point at PDFs
    under /wp-content/uploads/. File names are not regular (``comc2016-exam-en.pdf``,
    ``COMC2025-DOWNLOADABLE-en.pdf``, ``CLMC_2023_Official_Solution_EN.pdf``), so URLs are
    always taken from the listings, never built; the year and contest come from the name.
    French (``-fr``, ``DOCM``) and Chinese (``-zh``) versions are skipped.

Papers (typeset PDFs; see ``mathbot/pdf.py`` for how a problem becomes a picture)
  * COMC 1996: PART A questions 1-10, PART B 1-3. 1997-2010: PART A 1-8, PART B 1-4, with a
    "PART A" heading and "1." labels. 1997's exam has an unreadable text layer (its "6."
    label is garbage), so that year is rejected rather than guessed.
  * COMC 2011-2013: answer booklets with one page per question, headed "PART A: Question #1"
    (2011) or "Part A: Question 1" (2012-2013), each followed by "Your Solution:". The booklets
    also carry a marking grid ("A1 A2 A3 A4 ...") that must not be read as labels.
  * COMC 2014-2016: the exam booklets mix question pages with answer pages and are not used;
    the official solutions restate every question, "1." under "Section A", followed by
    "Solution".
  * COMC 2017-: booklet pages headed "Question A1 (4 points)" with "Your solution:" boxes and a
    diagonal "Final" watermark. A1-A4, B1-B4, C1-C4 from 2011 on.
  * CLMC booklets have no text layer at all (the questions are drawn as graphics). The official
    solutions restate each question (1-15 numbered straight through Parts A, B and C), then
    "Solution:" and "Answer: (D)"; statements are cut from there, and the answer key too.

Every paper is checked against the question layout of its year (``expected_labels``); a paper
that does not match exactly is rejected with the reason, never stored with doubtful numbering.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from mathbot.contests import CMS, ContestSeries
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

BASE_URL = "https://cms.math.ca"
LISTING_URLS = (
    f"{BASE_URL}/competitions/",
    f"{BASE_URL}/competitions/comc/",
    f"{BASE_URL}/competitions/clmc/",
)

# Part of every edition fingerprint: bump when parsing output can change for an unchanged PDF.
# 2: a problem ends at its last text or the figure continuing it, not at the answer space below
# 3: 2012-2013 read as answer booklets; case-insensitive "Your Solution"
# 4: COMC 2012-2013 fall back to their solutions
# 5: statements from solutions keep parts (b), (c)... stated after part (a)'s solution
# 6: a formula rising above a label's line stays with its problem; boxed statements kept whole
# 7: "(a) Solution:" and "Solution¹:" end a statement; each must be followed by its solution
# 8, 9: 2011 booklet questions end above the answer lines
# 10: a problem ends where the ink below its text stops (formulas drawn as graphics kept)
# 11: lines near the page edges are furniture only if they recur or are page numbers
# 12: bilingual papers: only the English column
# 13, 14: columns split at wide gaps; page edges never cut through a line
# 15: exponents at the foot of a page are not page numbers
# 16: a formula rising above the first label of a part stays with it; "over ..." notes dropped
# 17: italic (sheared) type is kept; only turned text is a watermark
# 19: lone page numbers further from the edge are furniture too
# 20: section banners and the sponsor page end a question; page footers taken whole
# 21, 22: drawn page heads and feet (logos, rules) are left out
# 23: fallback papers are fetched only when needed (fingerprint covers the papers read)
PARSER_VERSION = 23

_FILE = re.compile(r"(?i)\b(comc|clmc)[-_]?(\d{4})")
_TRANSLATION = re.compile(r"(?i)(-fr\b|-zh\b|docm|problemes)")

_C = re.compile
_FURNITURE = (
    _C(r"Uniquely-identified page|NO PHOTOCOPIES|Official Problem Set|Official Solutions"),
    _C(r"Canadian Mathematical Society|cms\.math\.ca|comc\.math\.ca|clmc\.math\.ca"),
    _C(r"^Final$"),
    _C(r"(?i)\bPage \d+ of \d+\b"),
    _C(r"(?i)^(?:over|please turn over|continued)\s*(?:\.{2,}|…)?$"),  # "turn the page" notes
)
# "Solution:", "Solution 1:", "Solution¹:" (a footnote), "(a) Solution:".
_SOLUTION = _C(r"^(?:\([a-h]\)\s*)?Solutions?(?![a-z])")
# The booklets' back cover (sponsors, "The ... Challenge is presented by"): never part of a question.
_BACK_COVER = _C(
    r"is presented by|In partnership with|The CMS thanks|promotes the advancement"
    r"|would like to thank|^Academic Partners"
)
# 2026: a banner opens each section ("Section B questions are worth a maximum of 6 points").
_SECTION_BANNER = _C(r"^Section [A-C] questions(?![a-z])")
_ANSWER_LINE = _C(r"^Answer\s*:")
_ANSWER_SPACE = _C(r"(?i)^Your (?:solution|final answer)")

LAYOUTS: dict[str, Layout] = {
    # 2017-: "Question A1 (4 points)" on its own line, then the statement, then "Your solution:".
    "question": Layout(
        start=_C(r"Question (?P<section>[A-C])(?P<number>\d{1,2}) \(\d+ (?:points?|marks?)\)"),
        skip_label_line=True,
        stop=(_ANSWER_SPACE, _BACK_COVER, _SECTION_BANNER),
        furniture=_FURNITURE,
        first_page=1,
    ),
    # 2011-2013 answer booklets: "PART A: Question #1 (4 marks)" / "Part A: Question 1 (4 marks)".
    "booklet": Layout(
        start=_C(r"(?i)PART (?P<section>[A-C])\s*:\s*Question\s*#?\s*(?P<number>\d{1,2})\b"),
        skip_label_line=True,
        # The answer lines at the foot of each page carry stray dash glyphs ("– –").
        stop=(_ANSWER_SPACE, _C(r"^(?:Solution|Answer)\s*:"), _BACK_COVER, _C(r"^[\s\-–—=_.]{1,9}$")),
        furniture=_FURNITURE,
        first_page=1,
    ),
    # "A1 Determine ...": a label followed by words (not the "A1 A2 A3 A4" of a marking grid).
    "lettered": Layout(
        start=_C(r"(?P<section>[A-C])(?P<number>\d{1,2})\s+(?=.*[a-z]{3})"),
        stop=(_ANSWER_SPACE, _BACK_COVER),
        furniture=_FURNITURE,
    ),
    # 1996-2010: "PART A" heading, "1." labels.
    "parts": Layout(
        start=_C(r"(?P<number>\d{1,2})\.(?:\s|$)"),
        heading=_C(r"^(?:PART|Part)\s+(?P<section>[A-C])\b"),
        stop=(_BACK_COVER,),
        furniture=_FURNITURE,
    ),
    # 2014-2016 official solutions: "Section A - 4 marks each", "1." labels, then "Solution".
    "solutions": Layout(
        start=_C(r"(?P<number>\d{1,2})\.(?:\s|$)"),
        heading=_C(r"^(?:PART|Part|SECTION|Section)\s+(?P<section>[A-C])\b"),
        stop=(_SOLUTION,),
        forbid=(_ANSWER_LINE,),
        require_stop=True,  # a statement not followed by its solution was not cut where it ends
        resume=True,  # "(a) ... Solution ... (b) ... Solution ...": every part is kept
        furniture=_FURNITURE,
        pad_x=14.0,  # statements sit in shaded boxes in some years: keep the whole box
        pad_top=10.0,
    ),
    # CLMC official solutions: "Part A: 4 marks each", 1-15 numbered straight through.
    "clmc": Layout(
        start=_C(r"(?P<number>\d{1,2})\.(?:\s|$)"),
        heading=_C(r"^Part (?P<section>[A-C])\b"),
        continuous=True,
        stop=(_SOLUTION,),
        forbid=(_ANSWER_LINE,),
        require_stop=True,  # a statement not followed by its solution was not cut where it ends
        furniture=_FURNITURE,
        pad_x=14.0,  # statements sit in shaded boxes: keep the whole box
        pad_top=10.0,
    ),
}
# COMC 2012-2013 official solutions: "A1 ..." labels, then "Solution".
LAYOUTS["solutions_lettered"] = replace(
    LAYOUTS["lettered"],
    stop=(LAYOUTS["solutions"].stop[0], _BACK_COVER),
    forbid=(_ANSWER_LINE,),
    resume=True,
    require_stop=True,
)
# CLMC 2026's solutions number each part from 1.
LAYOUTS["clmc_by_part"] = replace(LAYOUTS["clmc"], continuous=False)
_CLMC_ANSWER = _C(r"^Answer\s*:\s*\(([A-F])\)")


def expected_labels(series_key: str, year: int) -> list[tuple[str, int]]:
    """The question labels a paper of that year has, in order (CLMC: numbered straight through)."""
    if series_key == "CLMC":
        return [
            (section, n)
            for section, numbers in (("A", range(1, 6)), ("B", range(6, 11)), ("C", range(11, 16)))
            for n in numbers
        ]
    if year == 1996:
        parts = (("A", 10), ("B", 3))
    elif year <= 2010:
        parts = (("A", 8), ("B", 4))
    else:
        parts = (("A", 4), ("B", 4), ("C", 4))
    return [(section, n) for section, count in parts for n in range(1, count + 1)]


# A text layer this far from the printed text (see pdf.misaligned_lines) can't place problems.
MAX_MISALIGNED = 0.05

# Papers whose boxes pass every automatic check yet were seen to be wrong when the pictures
# were compared with the paper. They are reported, never stored.
UNRELIABLE: dict[tuple[str, int], str] = {
    ("COMC", 1996): "the PDF's text layer sits about a line away from the printed text, so questions "
    "would be cut in the wrong places (checked by eye on 2026-11-26)",
}


def _by_part(labels: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Numbered straight through (A1-A5, B6-B10) -> numbered within each part (A1-A5, B1-B5)."""
    first: dict[str, int] = {}
    for section, number in labels:
        first.setdefault(section, number)
    return [(section, number - first[section] + 1) for section, number in labels]


def candidates(series_key: str, year: int) -> list[tuple[str, str]]:
    """``(document, layout)`` pairs to try, most specific first; the first that matches wins."""
    if series_key == "CLMC":
        return [("solutions", "clmc"), ("solutions", "clmc_by_part")]
    if year <= 2010:
        # The solutions restate the questions: the fallback when the exam's text layer is damaged.
        return [("problems", "parts"), ("solutions", "solutions")]
    if year <= 2013:
        # 2012's booklet fonts decode as garbage and 2013's lose some headings: their solutions
        # restate the questions ("A1 ...", then "Solution").
        return [("problems", "booklet"), ("solutions", "solutions_lettered")]
    if year <= 2016:
        return [("solutions", "solutions")]
    return [("problems", "question")]


@dataclass(frozen=True)
class Paper:
    series_key: str
    year: int
    problems_url: str | None
    solutions_url: str | None


def parse_listing(html: str, base_url: str = BASE_URL) -> list[Paper]:
    """Every English COMC / CLMC paper linked from a listing page."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[tuple[str, int], dict[str, str]] = {}
    for anchor in soup.find_all("a", href=True):
        url = urljoin(base_url, anchor["href"])
        name = url.split("?")[0].rsplit("/", 1)[-1]
        if not name.lower().endswith(".pdf") or _TRANSLATION.search(name):
            continue
        match = _FILE.search(name)
        if match is None:
            continue
        label = anchor.get_text(" ", strip=True).lower()
        kind = "solutions" if ("solution" in label or "solution" in name.lower()) else "problems"
        if kind == "problems" and "problem" not in label and "exam" not in name.lower():
            continue  # some other document (results, a poster)
        slot = found.setdefault((match.group(1).upper(), int(match.group(2))), {})
        slot.setdefault(kind, url)
    return [
        Paper(key, year, urls.get("problems"), urls.get("solutions")) for (key, year), urls in sorted(found.items())
    ]


class CmsAdapter:
    provider = CMS

    def __init__(self, client: PoliteClient, *, refresh: bool = False) -> None:
        self.client = client
        self.refresh = refresh
        self._papers: dict[tuple[str, int], Paper] | None = None

    def _listing(self) -> dict[tuple[str, int], Paper]:
        if self._papers is None:
            papers: dict[tuple[str, int], Paper] = {}
            for url in LISTING_URLS:
                for paper in parse_listing(self.client.get(url, refresh=self.refresh).text, url):
                    known = papers.get((paper.series_key, paper.year))
                    papers[(paper.series_key, paper.year)] = Paper(
                        paper.series_key,
                        paper.year,
                        (known.problems_url if known else None) or paper.problems_url,
                        (known.solutions_url if known else None) or paper.solutions_url,
                    )
            self._papers = papers
        return self._papers

    def discover(self, series: ContestSeries) -> list[EditionRef]:
        editions = []
        for (key, year), paper in sorted(self._listing().items()):
            if key != series.key:
                continue
            needed = {document for document, _ in candidates(key, year)}
            url = paper.problems_url if "problems" in needed else paper.solutions_url
            if url is None:
                log.info("paper not published yet", extra={"contest": key, "year": year})
                continue
            editions.append(
                EditionRef(
                    series_key=series.key,
                    year=year,
                    label=f"{year} {series.name}",
                    source_url=paper.problems_url or url,
                    source_identifier=url.rsplit("/", 1)[-1],
                    expected_problems=len(expected_labels(key, year)),
                    extra={"problems_url": paper.problems_url, "solutions_url": paper.solutions_url},
                )
            )
        return editions

    def fetch_edition(
        self,
        series: ContestSeries,
        edition: EditionRef,
        *,
        known: KnownEdition | None = None,
        only_numbers: frozenset[int] | None = None,
    ) -> EditionResult:
        urls = {"problems": edition.extra.get("problems_url"), "solutions": edition.extra.get("solutions_url")}
        if (series.key, edition.year) in UNRELIABLE:
            result = EditionResult(edition=edition)
            reason = UNRELIABLE[(series.key, edition.year)]
            result.failures.append(ProblemFailure(None, edition.source_url, "parse", reason, invalid=True))
            return result
        # The first paper that matches is used; a fallback (the solutions) is fetched only if needed.
        # The fingerprint covers the papers read, so an unchanged paper is not parsed again.
        documents: dict[str, bytes] = {}
        result = EditionResult(edition=edition)
        errors = []
        for document, layout_name in candidates(series.key, edition.year):
            if not urls[document]:
                errors.append(f"{document} PDF not published")
                continue
            if document not in documents:
                documents[document] = self.client.get(str(urls[document]), refresh=self.refresh).content
                fingerprint = "+".join(pdf_digest(documents[d]) for d in sorted(documents))
                result.source_hash = f"{fingerprint}+parser{PARSER_VERSION}"
                if known is not None and known.source_hash == result.source_hash:
                    result.unchanged = True
                    return result
            layout = LAYOUTS[layout_name]
            expected = expected_labels(series.key, edition.year)
            if not layout.continuous:
                expected = _by_part(expected)
            try:
                parsed = read_document(documents[document])
                blank = misaligned_lines(parsed)
                if blank > MAX_MISALIGNED:
                    raise PdfLayoutError(f"{blank:.0%} of its text lines are not where the text is printed")
                problems = segment(parsed, layout)
                check_problems(problems, expected)
            except PdfLayoutError as exc:
                errors.append(f"{document} PDF ({layout_name} layout): {exc}")
                continue
            self._add_problems(result, edition, str(urls[document]), documents[document], problems, only_numbers)
            return result
        result.failures.append(
            ProblemFailure(None, edition.source_url, "parse", "; ".join(errors) or "no PDF to read", invalid=True)
        )
        return result

    def _add_problems(
        self,
        result: EditionResult,
        edition: EditionRef,
        pdf_url: str,
        data: bytes,
        problems: list[Problem],
        only_numbers: frozenset[int] | None,
    ) -> None:
        digest = pdf_digest(data)
        solutions_url = str(edition.extra.get("solutions_url") or "") or None
        problems_url = str(edition.extra.get("problems_url") or "") or None
        for number, problem in enumerate(problems, start=1):
            if only_numbers is not None and number not in only_numbers:
                continue
            html = statement_html(pdf_url, digest, problem)
            answer = _clmc_answer(problem) if edition.series_key == "CLMC" else None
            result.problems.append(
                ParsedProblem(
                    number=number,
                    section=problem.section,
                    statement_html=html,
                    raw_payload=html,
                    # Link the problem set, not the solutions a statement may have been cut from.
                    source_url=problems_url or pdf_url,
                    source_identifier=f"{pdf_url.rsplit('/', 1)[-1]}#{problem.section or ''}{problem.position}",
                    source_revision=digest,
                    answer=answer,
                    answer_source=solutions_url if answer else None,
                    solution_html=_SOLUTION_HTML if solutions_url else None,
                    solution_source=solutions_url,
                )
            )


# The official solutions are a PDF: stored as a pointer so the bot can link it.
_SOLUTION_HTML = "<p>Official solutions (PDF)</p>"


def _clmc_answer(problem: Problem) -> str | None:
    """The letter from the "Answer: (D)" line of a CLMC solution, if there is exactly one."""
    answers = {m.group(1) for line in problem.tail if (m := _CLMC_ANSWER.match(line.text))}
    return answers.pop() if len(answers) == 1 else None


__all__ = ["CmsAdapter", "expected_labels", "parse_listing"]
