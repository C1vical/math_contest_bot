"""AoPS Wiki adapter (AMC family, AIME, USAJMO, USAMO, IMO) via the MediaWiki API.

Access status - read this first
-------------------------------
On 2026-11-17 every ``/wiki/`` URL (``api.php`` and ``index.php``) answered an
honest, non-browser client with HTTP 403 and ``cf-mitigated: challenge`` (a
Cloudflare managed challenge). robots.txt *allows* ``/wiki/``, but the
challenge is an anti-bot control and this project does not circumvent it: no
TLS/browser impersonation, no stealth browser, no replayed clearance cookies.
:class:`mathbot.http.PoliteClient` raises ``SourceBlockedError`` and the run
reports the series as blocked.

Consequences:

* The response handling below follows the *documented* MediaWiki Action API
  (``action=parse`` / ``action=query``) and the page conventions the pre-2.0
  scraper relied on. It could not be re-verified against live responses, so the
  fixtures in ``tests/fixtures/aops_wiki`` are hand-written to that format and
  are labelled as such. Expect to adjust selectors once access is available.
* To ingest for real you need AoPS to permit your client (ask them, or run from
  a network/user agent they do not challenge). Pages you save yourself can be
  loaded with ``python -m mathbot cache-put <url> <file>``.

API usage (https://artofproblemsolving.com/wiki/api.php)
  * discovery   ``action=parse&page=<hub>&prop=links`` on the series hub page
                ("AMC 10 Problems and Solutions"); links titled
                ``<year> <name> Problems`` that exist are the editions.
  * problems    ``action=parse&page=<year>_<name>_Problems&prop=links`` lists
                the ``.../Problem N`` subpages that exist.
  * revisions   ``action=query&prop=revisions&rvprop=ids&titles=a|b|...`` (50
                titles per call) so incremental runs skip unchanged pages.
  * content     ``action=parse&page=<title>&prop=text|revid&redirects=1``.
                ``redirects`` matters: shared AMC 10/12 problems are redirects.
  * LaTeX is rendered server-side to ``<img class="latex" alt="$...$">``; the
    alt text is the TeX source and is preserved by normalization.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from mathbot.contests import AOPS_WIKI, ContestSeries, problem_id
from mathbot.http import HttpError, PoliteClient, SourceBlockedError
from mathbot.models import EditionRef, EditionResult, KnownEdition, ParsedProblem, ProblemFailure

log = logging.getLogger(__name__)

API_URL = "https://artofproblemsolving.com/wiki/api.php"
PAGE_URL = "https://artofproblemsolving.com/wiki/index.php/{title}"
TITLES_PER_QUERY = 50

_HEADING = re.compile(r"^h[1-6]$")
# Text that wiki users added below a statement with no heading or other marker, so that
# trim_statement can't recognise it: problem id -> how the first block to drop begins.
EXTRA_TEXT = {
    "aime1-2025-p01": "First do base conversion of both numbers",  # a whole solution
    "amc10b-2019-p22": "Did anyone else notice that the last Raashan",  # a comment
    "amc12b-2019-p19": "Did anyone else notice that the last Raashan",
    "usamo-1974-p04": "Note: there is a possible flaw in the original statement.",  # refers to a user's post
}
_ANSWER_ITEM = re.compile(r"^\(?([A-E]|\d{1,4})\)?$")


class WikiApiError(HttpError):
    """The API answered 200 with an ``error`` object (e.g. ``missingtitle``)."""


class WikiParseError(ValueError):
    """A page was fetched but no problem statement could be isolated."""


# --------------------------------------------------------------------------- parsing


def page_title(year: int, source_name: str, number: int | None = None) -> str:
    """``2024_AMC_10A_Problems`` or ``2024_AMC_10A_Problems/Problem_5``."""
    title = f"{year}_{source_name}_Problems"
    return f"{title}/Problem_{number}" if number is not None else title


def _spaces(title: str) -> str:
    return title.replace("_", " ")


def api_object(data: object, url: str) -> dict:
    """Return an API response as a dict, raising :class:`WikiApiError` for an ``error`` object."""
    if not isinstance(data, dict):
        raise WikiApiError("API response is not a JSON object", url=url)
    if "error" in data:
        error = data["error"] if isinstance(data["error"], dict) else {}
        raise WikiApiError(f"{error.get('code', 'error')}: {error.get('info', 'unknown API error')}", url=url)
    return data


def parse_payload(data: object, url: str) -> dict:
    """Validate an ``action=parse`` response and return its ``parse`` object."""
    parse = api_object(data, url).get("parse")
    if not isinstance(parse, dict):
        raise WikiApiError("API response has no 'parse' object", url=url)
    return parse


def payload_html(parse: dict) -> str:
    """Page HTML from either ``formatversion=2`` (string) or version 1 (``{"*": html}``)."""
    text = parse.get("text")
    if isinstance(text, dict):
        text = text.get("*")
    if not isinstance(text, str) or not text.strip():
        raise WikiParseError("API returned no page HTML")
    return text


def payload_links(parse: dict) -> list[str]:
    """Titles of existing pages linked from the parsed page (both format versions)."""
    titles = []
    for link in parse.get("links") or []:
        if not isinstance(link, dict):
            continue
        title = link.get("title") or link.get("*")
        exists = link.get("exists", True)
        if isinstance(title, str) and exists is not False:
            titles.append(title)
    return titles


def edition_years(link_titles: Iterable[str], source_name: str) -> list[int]:
    """Years of the ``<year> <name> Problems`` pages among ``link_titles``."""
    pattern = re.compile(rf"^(\d{{4}}) {re.escape(_spaces(source_name))} Problems$")
    return sorted({int(m.group(1)) for t in link_titles if (m := pattern.match(_spaces(t)))})


def problem_numbers(link_titles: Iterable[str], year: int, source_name: str) -> list[int]:
    """Numbers of the ``.../Problem N`` subpages of one edition among ``link_titles``."""
    prefix = re.escape(_spaces(page_title(year, source_name)))
    pattern = re.compile(rf"^{prefix}/Problem (\d+)$")
    return sorted({int(m.group(1)) for t in link_titles if (m := pattern.match(_spaces(t)))})


def _heading_block(heading: Tag) -> Tag:
    """MediaWiki >= 1.43 wraps headings in ``div.mw-heading``; older output does not."""
    parent = heading.parent
    if isinstance(parent, Tag) and "mw-heading" in (parent.get("class") or []):
        return parent
    return heading


def _is_heading_block(node: object) -> bool:
    if not isinstance(node, Tag):
        return False
    return bool(_HEADING.match(node.name)) or "mw-heading" in (node.get("class") or [])


# A block whose whole text is a solution heading the wiki did not render as a heading.
_SOLUTION_LINE = re.compile(r"^(?:(?:video|official|alternate|alternative)\s+)?solutions?(?:\s*\d+)?\s*:?$", re.I)
# Wikitext heading markup that failed to render ("==Video Solution", "== Note"): text from here on.
_WIKI_HEADING_MARKUP = re.compile(r"^[ \t]*==", re.M)


def _cut_point(soup: BeautifulSoup) -> tuple[Tag | NavigableString, int | None] | None:
    """The first node where non-statement content starts, and for a text node the offset in it."""
    for node in soup.descendants:
        if isinstance(node, Tag):
            if _is_heading_block(node):
                return node, None
            if node.name in {"p", "div"} and _SOLUTION_LINE.match(node.get_text(" ", strip=True)):
                return node, None
        elif isinstance(node, NavigableString) and not isinstance(node, Comment):
            match = _WIKI_HEADING_MARKUP.search(str(node))
            if match:
                return node, match.start()
    return None


def trim_statement(html: str) -> str:
    """Drop everything from the first heading or solution marker on.

    A problem page's statement ends at the next heading. Statements stored by the
    pre-2.0 scraper sometimes run on into the solutions (``<h3>Solution 1</h3>...``),
    end in a bare ``<p>Solution</p>``, or carry wikitext that failed to render
    (``==Video Solution``); all of that would otherwise be drawn into the problem
    image. If nothing meaningful would be left, ``html`` is returned unchanged.
    """
    soup = BeautifulSoup(html, "html.parser")
    found = _cut_point(soup)
    if found is None:
        return html
    node, offset = found
    current: Tag | NavigableString = node
    while current.parent is not None:  # remove everything after the cut, at every level
        for sibling in list(current.next_siblings):
            sibling.extract()
        current = current.parent
    if offset is None:
        node.extract()
    else:
        node.replace_with(str(node)[:offset])
    if not soup.get_text(strip=True) and soup.find("img") is None:
        return html
    # Spacer paragraphs (<p><br/></p>) that stood before the cut would leave a blank band.
    for last in reversed(list(soup.contents)):
        has_image = isinstance(last, Tag) and last.find("img") is not None
        if last.get_text(strip=True) or has_image:
            break
        last.extract()
    return str(soup).strip()


def drop_extra_text(html: str, pid: str) -> str:
    """``html`` without the text listed for this problem in ``EXTRA_TEXT`` and all that follows it.

    Returned unchanged for every other problem, and once the wiki page no longer has the text.
    """
    marker = EXTRA_TEXT.get(pid)
    if marker is None:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(True, recursive=False)[1:]:  # never the first block: that is the statement
        if " ".join(node.get_text().split()).startswith(marker):
            for sibling in list(node.next_siblings):
                sibling.extract()
            node.extract()
            return str(soup).strip()
    return html


def extract_statement(page_html: str) -> str:
    """Return the HTML between the "Problem" heading and the next heading.

    The pre-2.0 scraper also stopped at any element whose text contained
    "solution", which truncated statements such as "how many solutions...".
    Only headings end the statement here.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    root = soup.select_one(".mw-parser-output") or soup

    start: Tag | None = None
    for heading in root.find_all(_HEADING):
        if heading.get_text(" ", strip=True).lower().startswith("problem"):
            start = _heading_block(heading)
            break

    # Some pages put the statement first, with no "Problem" heading.
    nodes = list(start.next_siblings) if start is not None else list(root.children)

    parts: list[str] = []
    for node in nodes:
        if _is_heading_block(node):
            break
        if isinstance(node, Tag) and (node.get("id") == "toc" or "toc" in (node.get("class") or [])):
            continue
        parts.append(str(node))

    fragment = trim_statement("".join(parts).strip())
    if not BeautifulSoup(fragment, "html.parser").get_text(strip=True) and "<img" not in fragment:
        raise WikiParseError("no problem statement found before the first solution heading")
    return fragment


def parse_answer_key(page_html: str) -> dict[int, str]:
    """Answer key pages are a single ordered list of letters (AMC) or integers (AIME)."""
    soup = BeautifulSoup(page_html, "html.parser")
    best: dict[int, str] = {}
    for ol in soup.find_all("ol"):
        answers: dict[int, str] = {}
        for number, li in enumerate(ol.find_all("li", recursive=False), start=1):
            match = _ANSWER_ITEM.match(li.get_text(" ", strip=True).upper())
            if match:
                answers[number] = match.group(1)
        if len(answers) > len(best):
            best = answers
    return best


# --------------------------------------------------------------------------- adapter


class AopsWikiAdapter:
    provider = AOPS_WIKI

    def __init__(self, client: PoliteClient, *, refresh: bool = False) -> None:
        self.client = client
        self.refresh = refresh
        self._hub_links: dict[str, list[str]] = {}

    def _api(self, **params: object) -> dict:
        query = {"format": "json", "formatversion": 2, **params}
        response = self.client.get(API_URL, query, refresh=self.refresh)
        return api_object(response.json(), response.url)

    def _links(self, title: str) -> list[str]:
        data = self._api(action="parse", page=title, prop="links", redirects=1)
        return payload_links(parse_payload(data, API_URL))

    def discover(self, series: ContestSeries) -> list[EditionRef]:
        hub = str(series.source["hub_page"])
        years: list[int] = []
        origin = "discovered"
        try:
            if hub not in self._hub_links:
                self._hub_links[hub] = self._links(hub)
            years = edition_years(self._hub_links[hub], series.source_name)
        except SourceBlockedError:
            raise
        except HttpError as exc:
            log.warning(
                "hub page discovery failed; falling back to registry year ranges",
                extra={"provider": AOPS_WIKI, "contest": series.key, "url": exc.url, "error": str(exc)},
            )
        if not years:
            years, origin = series.fallback_years(), "fallback"

        refs = []
        for year in years:
            title = page_title(year, series.source_name)
            refs.append(
                EditionRef(
                    series_key=series.key,
                    year=year,
                    label=_spaces(title),
                    source_url=PAGE_URL.format(title=title),
                    source_identifier=title,
                    expected_problems=series.expected_problems(year),
                    origin=origin,
                )
            )
        return refs

    def _numbers(self, series: ContestSeries, edition: EditionRef, result: EditionResult) -> list[int]:
        title = page_title(edition.year, series.source_name)
        try:
            numbers = problem_numbers(self._links(title), edition.year, series.source_name)
        except SourceBlockedError:
            raise
        except HttpError as exc:
            numbers = []
            result.failures.append(ProblemFailure(None, exc.url, "discover", f"problem list unavailable: {exc}"))
        if numbers:
            return numbers
        if edition.expected_problems:
            return list(range(1, edition.expected_problems + 1))
        result.failures.append(
            ProblemFailure(None, edition.source_url, "discover", "no problem pages listed and no expected count")
        )
        return []

    def _revisions(self, titles: dict[int, str]) -> dict[int, str]:
        """Current revision id per problem number; empty when the lookup is unavailable."""
        by_title = {_spaces(title): number for number, title in titles.items()}
        revisions: dict[int, str] = {}
        names = list(titles.values())
        for offset in range(0, len(names), TITLES_PER_QUERY):
            chunk = names[offset : offset + TITLES_PER_QUERY]
            try:
                data = self._api(action="query", prop="revisions", rvprop="ids", titles="|".join(chunk), redirects=1)
            except SourceBlockedError:
                raise
            except HttpError as exc:
                log.warning("revision lookup failed; fetching all pages", extra={"url": exc.url, "error": str(exc)})
                return {}
            query = data.get("query") or {}
            # A redirect reports the target's revision under the target's title.
            target_to_source: dict[str, list[str]] = {}
            for entry in (query.get("normalized") or []) + (query.get("redirects") or []):
                target_to_source.setdefault(entry["to"], []).append(entry["from"])
            for page in query.get("pages") or []:
                revs = page.get("revisions") or []
                if not revs:
                    continue
                pending = [page["title"]]
                while pending:
                    name = pending.pop()
                    if _spaces(name) in by_title:
                        revisions[by_title[_spaces(name)]] = str(revs[0]["revid"])
                    pending.extend(target_to_source.get(name, []))
        return revisions

    def fetch_edition(
        self,
        series: ContestSeries,
        edition: EditionRef,
        *,
        known: KnownEdition | None = None,
        only_numbers: frozenset[int] | None = None,
    ) -> EditionResult:
        result = EditionResult(edition=edition)
        numbers = self._numbers(series, edition, result)
        if only_numbers is not None:
            numbers = [n for n in numbers if n in only_numbers]
        titles = {n: page_title(edition.year, series.source_name, n) for n in numbers}

        current = self._revisions(titles) if (known is not None and known.revisions and titles) else {}
        answers = self._answers(series, edition, result) if numbers else {}

        for number in numbers:
            if known is not None and current.get(number) and current[number] == known.revisions.get(number):
                result.skipped_numbers.append(number)
                continue
            title = titles[number]
            url = PAGE_URL.format(title=title)
            try:
                data = self._api(action="parse", page=title, prop="text|revid", redirects=1, disablelimitreport=1)
                parse = parse_payload(data, url)
                html = payload_html(parse)
                statement = drop_extra_text(extract_statement(html), problem_id(series.key, edition.year, number))
            except SourceBlockedError:
                raise
            except HttpError as exc:
                result.failures.append(ProblemFailure(number, url, "fetch", str(exc)))
                continue
            except WikiParseError as exc:
                result.failures.append(ProblemFailure(number, url, "parse", str(exc), invalid=True))
                continue
            revid = parse.get("revid")
            result.problems.append(
                ParsedProblem(
                    number=number,
                    statement_html=statement,
                    raw_payload=html,
                    source_url=url,
                    source_identifier=title,
                    source_revision=str(revid) if revid is not None else None,
                    answer=answers.get(number),
                    answer_source=PAGE_URL.format(title=self._answer_title(series, edition))
                    if answers.get(number)
                    else None,
                )
            )
        return result

    @staticmethod
    def _answer_title(series: ContestSeries, edition: EditionRef) -> str:
        return f"{edition.year}_{series.source_name}_Answer_Key"

    def _answers(self, series: ContestSeries, edition: EditionRef, result: EditionResult) -> dict[int, str]:
        if not series.source.get("answer_key"):
            return {}
        title = self._answer_title(series, edition)
        try:
            data = self._api(action="parse", page=title, prop="text", redirects=1, disablelimitreport=1)
            return parse_answer_key(payload_html(parse_payload(data, API_URL)))
        except SourceBlockedError:
            raise
        except (HttpError, WikiParseError) as exc:
            # Statements are still valid without a key; record the gap instead of hiding it.
            result.failures.append(
                ProblemFailure(
                    None, PAGE_URL.format(title=title), "fetch", f"answer key unavailable: {exc}", warning=True
                )
            )
            return {}
