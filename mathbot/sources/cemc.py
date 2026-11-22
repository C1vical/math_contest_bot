"""CEMC (University of Waterloo) adapter: Euclid, Fryer/Galois/Hypatia, Pascal/Cayley/Fermat,
CIMC/CSMC.

Verified against the live site on 2026-11-17 (plain HTTP, honest User-Agent):

Listing  https://cemc.uwaterloo.ca/resources/past-contests
  * Server-rendered Drupal view. Filters are plain query parameters:
    ``contest_category`` (14 = PCF, 23 = CIMC/CSMC, 24 = Euclid, 25 = FGH, 13 = Gauss),
    ``academic_year=All``, ``grade=All`` and ``page=N`` (0-based, 10 rows/page).
    A page past the end returns HTTP 200 with zero rows; an unknown category
    also returns zero rows.
  * Each ``<table> tbody tr`` has cells Title / Year / Grade / Contest /
    Solution / Results. The Contest and Solution cells hold an ``.html`` link
    and a ``.pdf`` link.
  * Only 2016 onward is listed. Earlier contests are not offered as HTML.
  * The HTML link's directory is NOT derivable from the year (2019 Pascal is
    served from ``/documents/2026/2019PascalContest.html`` while its PDF is
    under ``/documents/2019/``; the "obvious" 2019 URL is a 404), so URLs are
    always taken from the listing, never constructed.
  * robots.txt allows all of the above.

Contest pages (pandoc output, UTF-8)
  * MathJax v3 TeX in ``<span class="math inline">\\(...\\)</span>``.
  * All images are inline ``data:`` URIs - there are no remote assets.
  * Questions live in body-level ``<ol>`` lists that follow an ``<h2>``:
    ``Questions`` (Euclid/FGH) or ``Part A|B|C: ...`` (PCF, with
    ``<ol start="11">`` continuing the numbering). Instructions and the
    ``div.infobox`` also contain ``<ol>`` lists and must be skipped.
  * Euclid/FGH pages state ``Number of Questions: N``; used for validation
    together with the registry's expected count, because the page is
    occasionally wrong (2016 Galois states 10 but has 4 questions).
  * Markup is not uniform. The 2016 Euclid page closes the question list early,
    leaving question 8(b), 9 and 10 as stray ``<li>`` elements. Stray items are
    collected as problems and known mis-nestings are repaired by
    :data:`MERGE_INTO_PREVIOUS`; any page whose count still disagrees with the
    expected count is rejected rather than ingested with wrong numbering.
  * The 2019 Pascal page closes ``<li>`` early for every question that has a
    diagram, so the closing sentence and the answer choices become loose
    children of the ``<ol>``. Loose children are appended to the preceding
    question (the pre-2.0 scraper did the same for a stray ``<ol>``).
  * The 2017 and 2021 Pascal pages never close the Part A list (2017 also
    opens it twice), so the Part B / C headings and lists are nested inside
    it. Headings found inside a list are honoured as if at body level.
  * CIMC / CSMC pages (Part A: 6 short-answer questions, Part B: 3 full-solution
    questions) often split Part B into one list per question and number them
    within the part (``<ol>``, ``<ol start="2">``, ``<ol start="3">``), whereas
    PCF uses the absolute number (``<ol start="11">``). A ``start`` that would
    make the numbering go backwards is read as relative to its part.
  * Choices appear as ``ol.horizontal``, ``ol.mc`` (image choices) or a
    ``div.tabbing`` block with explicit "(A)" labels; all are kept verbatim.

Solution pages
  * One body-level ``<ol>``; for multiple-choice contests each item ends with
    ``<p>Answer: (D)</p>`` (or a number for PCF Part C). That is the only
    answer data ingested.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from mathbot.contests import CEMC, ContestSeries, normalize_alias
from mathbot.http import HttpError, PoliteClient
from mathbot.models import EditionRef, EditionResult, KnownEdition, ParsedProblem, ProblemFailure
from mathbot.sources.base import load_manifest

log = logging.getLogger(__name__)

BASE_URL = "https://cemc.uwaterloo.ca"
LISTING_URL = f"{BASE_URL}/resources/past-contests"
MAX_LISTING_PAGES = 50

_QUESTION_HEADING = re.compile(r"^\s*(?:Part\s+([A-Z])\b|Questions?\b)", re.IGNORECASE)
_DECLARED_COUNT = re.compile(r"Number of Questions:\s*(\d+)", re.IGNORECASE)
_ANSWER = re.compile(r"^\s*Answer:\s*\(?\s*([A-Za-z]|-?[\d.,/]+)\s*\)?\s*\.?\s*$")

# (series key, year) -> 1-based indexes of parsed items that are really a
# continuation of the previous question (verified by reading the page).
MERGE_INTO_PREVIOUS: dict[tuple[str, int], frozenset[int]] = {
    ("EUCLID", 2016): frozenset({9}),  # stray <li> is question 8(b)
}


@dataclass(frozen=True)
class ListingRow:
    title: str
    year: int
    contest_url: str
    solution_url: str | None


@dataclass
class ContestPage:
    title: str | None
    declared_count: int | None
    problems: list[tuple[int, str | None, str]] = field(default_factory=list)  # (number, section, html)


class CemcParseError(ValueError):
    """The page does not have a recognisable question structure."""


# --------------------------------------------------------------------------- parsing


def parse_listing(html: str) -> tuple[list[ListingRow], bool]:
    """Return the rows of one listing page and whether a next page exists."""
    soup = BeautifulSoup(html, "html.parser")
    rows: list[ListingRow] = []
    for tr in soup.select("table tbody tr"):
        title_cell = tr.select_one("td.views-field-title")
        year_cell = tr.select_one("td.views-field-field-year-term")
        if title_cell is None or year_cell is None:
            continue
        year_match = re.search(r"\b(19|20)\d{2}\b", year_cell.get_text(" ", strip=True))
        contest_link = _html_link(tr.select_one("td.views-field-nothing"))
        if not year_match or not contest_link:
            continue  # row without an HTML contest (PDF-only) - nothing we can parse
        rows.append(
            ListingRow(
                title=title_cell.get_text(" ", strip=True),
                year=int(year_match.group(0)),
                contest_url=urljoin(BASE_URL, contest_link),
                solution_url=_abs(_html_link(tr.select_one("td.views-field-nothing-2"))),
            )
        )
    has_next = soup.select_one(".pager .pager__item--next a") is not None
    return rows, has_next


def _html_link(cell: Tag | None) -> str | None:
    if cell is None:
        return None
    for a in cell.find_all("a", href=True):
        if a["href"].split("?")[0].lower().endswith(".html"):
            return a["href"]
    return None


def _abs(href: str | None) -> str | None:
    return urljoin(BASE_URL, href) if href else None


def _inner_html(tag: Tag) -> str:
    return tag.decode_contents().strip()


def _parse_page(html: str) -> tuple[BeautifulSoup, Tag | None]:
    """The document and its ``<body>`` (None if missing), with the pages' "show" buttons removed."""
    # html5lib repairs the unbalanced lists found on some pages the way a browser would.
    soup = BeautifulSoup(html, "html5lib")
    body = soup.body
    if body is not None:
        for button in body.find_all("button"):
            button.decompose()
    return soup, body


def parse_contest_page(html: str, *, merge_into_previous: frozenset[int] = frozenset()) -> ContestPage:
    """Extract ``(number, section, html)`` for every question on a contest page."""
    soup, body = _parse_page(html)
    if body is None:
        raise CemcParseError("page has no <body>")

    declared = _DECLARED_COUNT.search(body.get_text(" "))
    page = ContestPage(
        title=soup.title.get_text(strip=True) if soup.title else None,
        declared_count=int(declared.group(1)) if declared else None,
    )

    # (explicit start number, section, <li>, markup that spilled out of the <li>)
    items: list[tuple[int | None, str | None, Tag, list[str]]] = []
    state = {"in_questions": False, "section": None, "saw_heading": False}

    def heading(tag: Tag) -> None:
        match = _QUESTION_HEADING.match(tag.get_text(" ", strip=True))
        state["in_questions"] = match is not None
        if match:
            state["saw_heading"] = True
            state["section"] = match.group(1).upper() if match.group(1) else None

    def question_list(ol: Tag) -> None:
        start = _int_attr(ol, "start")
        first = len(items)
        for node in ol.find_all(recursive=False):
            if node.name == "h2":
                # The list was never closed (2017 / 2021 Pascal): the next part's heading
                # and its list are nested inside this one. Carry on as if at body level.
                heading(node)
            elif not state["in_questions"] or node.name == "hr":
                continue
            elif node.name == "li":
                items.append((start, state["section"], node, []))
                start = None
            elif node.name == "ol" and (len(items) == first or _follows_heading(node)):
                # A nested list before this list's first question (2017 Pascal opens the
                # list twice) or right after a part heading holds questions, not choices.
                question_list(node)
            elif len(items) > first:
                # The source closed a <li> too early (2019 Pascal does this for every
                # question with a diagram), leaving the rest of the question - often the
                # answer choices - as loose children of the list. It belongs to the
                # question just before it.
                items[-1][3].append(str(node))

    for child in body.find_all(recursive=False):
        if child.name == "h2":
            heading(child)
        elif not state["in_questions"]:
            continue
        elif child.name == "ol":
            question_list(child)
        elif child.name == "li":  # list closed too early in the source; item belongs to the questions
            items.append((None, state["section"], child, []))
    saw_heading = state["saw_heading"]

    if not saw_heading:
        # Older layout without question headings: the list after the info box.
        infobox = body.find(class_="infobox")
        fallback = infobox.find_next_sibling("ol") if infobox else None
        if fallback is None:
            raise CemcParseError("no 'Questions' / 'Part A' heading and no question list after the info box")
        items = [(None, None, li, []) for li in fallback.find_all("li", recursive=False)]

    number = 0
    section_first: dict[str | None, int] = {}  # number of each section's first question
    for index, (start, item_section, li, spilled) in enumerate(items, start=1):
        fragment = "\n".join([_inner_html(li), *spilled])
        if index in merge_into_previous and page.problems:
            prev_number, prev_section, prev_html = page.problems[-1]
            parts = len(BeautifulSoup(prev_html, "html.parser").select("ol[type=a] > li")) if prev_html else 0
            merged = f'{prev_html}\n<ol start="{parts + 1}" type="a"><li>{fragment}</li></ol>'
            page.problems[-1] = (prev_number, prev_section, merged)
            continue
        if start is None:
            number += 1
        elif start > number:
            number = start  # absolute, e.g. PCF's <ol start="11"> for Part B
        else:
            # Numbering would go backwards: the start counts within the part (CIMC / CSMC
            # Part B lists are <ol>, <ol start="2">, <ol start="3"> for questions 7, 8, 9).
            number = section_first.get(item_section, number + 1) - 1 + start
        section_first.setdefault(item_section, number)
        page.problems.append((number, item_section, fragment))

    if not page.problems:
        raise CemcParseError("question section found but it contains no questions")
    return page


def _follows_heading(tag: Tag) -> bool:
    previous = tag.find_previous_sibling(True)
    return previous is not None and previous.name == "h2"


def _int_attr(tag: Tag, name: str) -> int | None:
    value = tag.get(name)
    return int(value) if isinstance(value, str) and value.strip().isdigit() else None


def _solution_items(html: str) -> list[tuple[int, Tag]]:
    """``(question number, <li>)`` for every top-level item of a solutions page.

    Handles the same quirk as the contest pages: some solution pages open the list
    twice (``<ol><ol><li>...``, e.g. 2018 Pascal), so a nested list that comes before
    any item of its parent holds the items.
    """
    _, body = _parse_page(html)
    if body is None:
        return []
    items: list[tuple[int, Tag]] = []
    number = 0

    def walk(ol: Tag) -> None:
        nonlocal number
        start = _int_attr(ol, "start")
        seen_item = False
        for node in ol.find_all(recursive=False):
            if node.name == "li":
                number = start if start is not None else number + 1
                start = None
                seen_item = True
                items.append((number, node))
            elif node.name == "ol" and not seen_item:
                walk(node)

    for ol in body.find_all("ol", recursive=False):
        walk(ol)
    return items


def parse_solutions(html: str) -> dict[int, str]:
    """Official solution per question from a solutions page: ``{question number: inner html}``."""
    return _solutions(_solution_items(html))


def parse_answers(html: str) -> dict[int, str]:
    """Answer key from a solutions page: ``{question number: "D"}``. Empty if none are stated."""
    return _answers(_solution_items(html))


def _solutions(items: list[tuple[int, Tag]]) -> dict[int, str]:
    return {number: _inner_html(li) for number, li in items}


def _answers(items: list[tuple[int, Tag]]) -> dict[int, str]:
    answers: dict[int, str] = {}
    for number, li in items:
        # The answer is normally the last paragraph, but stray trailing ones occur.
        for paragraph in reversed(li.find_all("p", recursive=False)):
            match = _ANSWER.match(paragraph.get_text(" ", strip=True))
            if match:
                answers[number] = match.group(1).upper()
                break
    return answers


# --------------------------------------------------------------------------- adapter


class CemcAdapter:
    provider = CEMC

    def __init__(self, client: PoliteClient, *, refresh: bool = False) -> None:
        self.client = client
        self.refresh = refresh
        self._listing_cache: dict[int, list[ListingRow]] = {}

    def _listing(self, category_id: int) -> list[ListingRow]:
        if category_id in self._listing_cache:
            return self._listing_cache[category_id]
        rows: list[ListingRow] = []
        for page in range(MAX_LISTING_PAGES):
            response = self.client.get(
                LISTING_URL,
                {"grade": "All", "academic_year": "All", "contest_category": category_id, "page": page},
                refresh=self.refresh,
            )
            page_rows, has_next = parse_listing(response.text)
            rows.extend(page_rows)
            if not page_rows or not has_next:
                break
        else:
            raise CemcParseError(f"listing for category {category_id} did not end after {MAX_LISTING_PAGES} pages")
        self._listing_cache[category_id] = rows
        return rows

    def discover(self, series: ContestSeries) -> list[EditionRef]:
        category_id = int(series.source["category_id"])  # type: ignore[call-overload]
        wanted = normalize_alias(series.source_name)
        editions: dict[int, EditionRef] = {}
        for row in self._listing(category_id):
            if normalize_alias(row.title) != wanted:
                continue
            title = row.title if row.title.endswith("Contest") else f"{row.title} Contest"
            editions[row.year] = EditionRef(
                series_key=series.key,
                year=row.year,
                label=f"{row.year} {title}",
                source_url=row.contest_url,
                source_identifier=row.contest_url.rsplit("/", 1)[-1],
                expected_problems=series.expected_problems(row.year),
                extra={"solution_url": row.solution_url},
            )

        # Seed manifest carried over from the pre-2.0 scraper: fills gaps only, and says so.
        for key, url in load_manifest("cemc_urls.json").items():
            year_text, _, name = key.partition("_")
            if normalize_alias(name) != normalize_alias(series.legacy_name or series.key):
                continue
            year = int(year_text)
            if year not in editions:
                log.warning(
                    "edition missing from live listing; using seed manifest",
                    extra={"provider": CEMC, "contest": series.key, "year": year, "url": url},
                )
                editions[year] = EditionRef(
                    series_key=series.key,
                    year=year,
                    label=f"{year} {series.name} Contest",
                    source_url=url,
                    source_identifier=url.rsplit("/", 1)[-1],
                    expected_problems=series.expected_problems(year),
                    origin="fallback",
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
        response = self.client.get(edition.source_url, refresh=self.refresh)
        result = EditionResult(edition=edition, source_hash=response.sha256)
        if known is not None and known.source_hash == response.sha256:
            result.unchanged = True
            return result

        try:
            page = parse_contest_page(
                response.text,
                merge_into_previous=MERGE_INTO_PREVIOUS.get((series.key, edition.year), frozenset()),
            )
        except CemcParseError as exc:
            result.failures.append(ProblemFailure(None, edition.source_url, "parse", str(exc), invalid=True))
            return result

        # The page's own "Number of Questions" is sometimes wrong (2016 Galois says 10 and
        # has 4), so a count is accepted when it matches either the page or the registry.
        acceptable = {n for n in (page.declared_count, edition.expected_problems) if n is not None}
        if acceptable and len(page.problems) not in acceptable:
            result.failures.append(
                ProblemFailure(
                    None,
                    edition.source_url,
                    "validate",
                    f"parsed {len(page.problems)} questions but expected {sorted(acceptable)}; "
                    "refusing to ingest with uncertain numbering",
                    invalid=True,
                )
            )
            return result

        answers, solutions = self._solution_page(series, edition, result, [n for n, _, _ in page.problems])
        solution_url = str(edition.extra.get("solution_url") or "") or None
        for number, section, fragment in page.problems:
            if only_numbers is not None and number not in only_numbers:
                continue
            result.problems.append(
                ParsedProblem(
                    number=number,
                    section=section,
                    statement_html=fragment,
                    raw_payload=fragment,
                    source_url=edition.source_url,
                    source_identifier=f"{edition.source_identifier}#q{number}",
                    source_revision=response.sha256,
                    answer=answers.get(number),
                    answer_source=solution_url if answers.get(number) else None,
                    solution_html=solutions.get(number),
                    solution_source=solution_url if solutions.get(number) else None,
                )
            )
        return result

    def _solution_page(
        self, series: ContestSeries, edition: EditionRef, result: EditionResult, numbers: list[int]
    ) -> tuple[dict[int, str], dict[int, str]]:
        """Answer key (multiple-choice contests) and official solutions from the solutions page.

        Statements are valid without either, so problems here are recorded as edition-level
        notes rather than failing the edition. Solutions are only kept when the page has
        exactly one per question, so a mis-parsed page can never attach the wrong solution.
        """
        solution_url = edition.extra.get("solution_url")
        if not solution_url:
            return {}, {}
        try:
            html = self.client.get(str(solution_url), refresh=self.refresh).text
        except HttpError as exc:
            result.failures.append(ProblemFailure(None, str(solution_url), "fetch", f"solutions unavailable: {exc}"))
            return {}, {}
        items = _solution_items(html)  # one html5lib parse for both
        answers = _answers(items) if series.source.get("multiple_choice") else {}
        solutions = _solutions(items)
        if sorted(solutions) != sorted(numbers):
            result.failures.append(
                ProblemFailure(
                    None,
                    str(solution_url),
                    "fetch",
                    f"solutions page has {len(solutions)} entries for {len(numbers)} questions; solutions not stored",
                )
            )
            solutions = {}
        return answers, solutions
