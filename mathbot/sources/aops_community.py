"""AoPS Community adapter (Contest Collections): CMO, optionally CJMO / CMOQR.

Access status - read this first
-------------------------------
The pre-2.0 scraper POSTed to ``/m/community/ajax.php``
(``a=fetch_category_data``) with a hard-coded guest session id, from a stealth
browser. Checked on 2026-11-17:

* robots.txt disallows that endpoint twice over (``Disallow: /m/`` and
  ``Disallow: /*ajax``), and without a session it answers
  ``{"error_code": "E_INVALID_SESSION_NONE"}``. This adapter therefore does
  **not** call it, and carries no session id.
* The public category page ``/community/c<id>`` is allowed by robots.txt but,
  like the wiki, answers non-browser clients with a Cloudflare managed
  challenge (HTTP 403, ``cf-mitigated: challenge``). That is not bypassed;
  ``PoliteClient`` raises ``SourceBlockedError``.

So this adapter reads the only robots-permitted source - the category page and
the JSON it embeds for its own front end (``AoPS.bootstrap_data``) - and will
work only where AoPS serves that page to your client. The embedded-JSON layout
could not be verified live; the item fields (``item_id``, ``item_text``,
``item_score``, ``post_data.post_rendered``) are the ones the pre-2.0 scraper
consumed from the same category object. Fixtures in
``tests/fixtures/aops_community`` are hand-written to that shape and labelled.

Known limits: the page embeds only the first batch of a category's items;
further batches need the disallowed ajax endpoint. If a category reports more
items than were delivered, that is recorded as a failure rather than silently
ingesting a partial contest. Edition category ids from the pre-2.0 manifest
(``data/aops_community_ids.json``) fill discovery gaps.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from mathbot.contests import AOPS_COMMUNITY, ContestSeries
from mathbot.http import HttpError, PoliteClient, SourceBlockedError
from mathbot.models import EditionRef, EditionResult, KnownEdition, ParsedProblem, ProblemFailure
from mathbot.sources.base import load_manifest

log = logging.getLogger(__name__)

CATEGORY_URL = "https://artofproblemsolving.com/community/c{category_id}"
_BOOTSTRAP_MARKER = "AoPS.bootstrap_data"
_PROBLEM_LABEL = re.compile(r"^(?:Problem|P|#)?\s*(\d{1,2})\.?$", re.IGNORECASE)
_YEAR = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
_COUNT_FIELDS = ("num_items", "item_count", "items_count", "total_items")


class CommunityParseError(ValueError):
    """The category page did not contain a usable category payload."""


# --------------------------------------------------------------------------- parsing


def extract_bootstrap(page_html: str) -> dict:
    """Decode the JSON object assigned to ``AoPS.bootstrap_data`` in the page."""
    marker = page_html.find(_BOOTSTRAP_MARKER)
    if marker < 0:
        raise CommunityParseError(f"page has no {_BOOTSTRAP_MARKER} block")
    start = page_html.find("{", marker)
    if start < 0:
        raise CommunityParseError(f"{_BOOTSTRAP_MARKER} is not followed by a JSON object")
    try:
        data, _ = json.JSONDecoder().raw_decode(page_html[start:])
    except ValueError as exc:
        raise CommunityParseError(f"{_BOOTSTRAP_MARKER} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CommunityParseError(f"{_BOOTSTRAP_MARKER} is not a JSON object")
    return data


def find_category(data: object, category_id: int) -> dict:
    """Locate the category object (a dict with ``items``) for ``category_id`` anywhere in ``data``.

    Also accepts the old ajax envelope ``{"response": {"category": {...}}}``.
    """
    stack = [data]
    fallback: dict | None = None
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("items"), list):
                if str(node.get("category_id")) == str(category_id):
                    return node
                if "category_id" not in node and fallback is None:
                    fallback = node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    if fallback is not None:
        return fallback
    raise CommunityParseError(f"no category object with items found for category {category_id}")


def parse_category_page(page_html: str, category_id: int) -> dict:
    return find_category(extract_bootstrap(page_html), category_id)


def item_year(item: dict) -> int | None:
    """Year of an edition folder: from its label, else from ``item_score`` (the sort key)."""
    for field in ("item_text", "item_subtitle", "category_name"):
        match = _YEAR.search(str(item.get(field) or ""))
        if match:
            return int(match.group(1))
    score = item.get("item_score")
    if isinstance(score, (int, float)) or (isinstance(score, str) and score.isdigit()):
        year = int(score)
        if 1800 <= year <= 2200:
            return year
    return None


def parse_edition_items(category: dict) -> list[tuple[int, int]]:
    """``(year, category id)`` for each edition folder of a contest category."""
    editions = []
    for item in category.get("items") or []:
        if not isinstance(item, dict):
            continue
        year, item_id = item_year(item), item.get("item_id")
        if year is not None and str(item_id or "").isdigit():
            editions.append((year, int(item_id)))
    return editions


def parse_problem_items(category: dict, source_url: str) -> tuple[list[ParsedProblem], list[ProblemFailure]]:
    """Problems of one edition category. Non-numeric items ("Day 1") become section labels."""
    problems: list[ParsedProblem] = []
    failures: list[ProblemFailure] = []
    seen: set[int] = set()
    section: str | None = None

    for item in category.get("items") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("item_text") or "").strip()
        match = _PROBLEM_LABEL.match(label)
        if not match:
            if label:
                section = label  # e.g. "Day 1", "Part A"
            continue
        number = int(match.group(1))
        post = item.get("post_data")
        rendered = post.get("post_rendered") if isinstance(post, dict) else None
        if number < 1 or number in seen:
            failures.append(ProblemFailure(number, source_url, "parse", "duplicate or invalid problem number", True))
            continue
        if not isinstance(rendered, str) or not rendered.strip():
            failures.append(ProblemFailure(number, source_url, "parse", "item has no post_data.post_rendered", True))
            continue
        seen.add(number)
        topic_id = post.get("topic_id") if isinstance(post, dict) else None
        post_id = post.get("post_id") if isinstance(post, dict) else None
        url = f"https://artofproblemsolving.com/community/h{topic_id}p{post_id}" if topic_id and post_id else source_url
        problems.append(
            ParsedProblem(
                number=number,
                section=section,
                statement_html=rendered,
                raw_payload=json.dumps(item, sort_keys=True, ensure_ascii=False),
                source_url=url,
                source_identifier=str(post_id or item.get("item_id") or ""),
                source_revision=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            )
        )

    for field in _COUNT_FIELDS:
        total = category.get(field)
        if isinstance(total, int) and total > len(category.get("items") or []):
            failures.append(
                ProblemFailure(
                    None,
                    source_url,
                    "fetch",
                    f"category reports {total} items but the page delivered {len(category['items'])}; "
                    "the rest are only available from an endpoint robots.txt disallows",
                )
            )
            break
    problems.sort(key=lambda p: p.number)
    return problems, failures


# --------------------------------------------------------------------------- adapter


class AopsCommunityAdapter:
    provider = AOPS_COMMUNITY

    def __init__(self, client: PoliteClient, *, refresh: bool = False) -> None:
        self.client = client
        self.refresh = refresh

    def _category(self, category_id: int) -> tuple[dict, str]:
        url = CATEGORY_URL.format(category_id=category_id)
        response = self.client.get(url, refresh=self.refresh)
        return parse_category_page(response.text, category_id), url

    def _seed_editions(self, series: ContestSeries) -> dict[int, int]:
        seed: dict[int, int] = {}
        for key, category_id in load_manifest("aops_community_ids.json").items():
            year_text, _, name = key.partition(" ")
            if name == series.key and year_text.isdigit():
                seed[int(year_text)] = int(category_id)
        return seed

    def discover(self, series: ContestSeries) -> list[EditionRef]:
        parent_id = int(series.source["category_id"])  # type: ignore[call-overload]
        found: dict[int, tuple[int, str]] = {}
        try:
            category, _ = self._category(parent_id)
            for year, category_id in parse_edition_items(category):
                found[year] = (category_id, "discovered")
        except SourceBlockedError:
            raise
        except (HttpError, CommunityParseError) as exc:
            log.warning(
                "category discovery failed; falling back to seed manifest",
                extra={"provider": AOPS_COMMUNITY, "contest": series.key, "error": str(exc)},
            )
        for year, category_id in self._seed_editions(series).items():
            found.setdefault(year, (category_id, "fallback"))

        return [
            EditionRef(
                series_key=series.key,
                year=year,
                label=f"{year} {series.name}",
                source_url=CATEGORY_URL.format(category_id=category_id),
                source_identifier=str(category_id),
                expected_problems=series.expected_problems(year),
                origin=origin,
            )
            for year, (category_id, origin) in sorted(found.items())
        ]

    def fetch_edition(
        self,
        series: ContestSeries,
        edition: EditionRef,
        *,
        known: KnownEdition | None = None,
        only_numbers: frozenset[int] | None = None,
    ) -> EditionResult:
        result = EditionResult(edition=edition)
        try:
            category, url = self._category(int(edition.source_identifier or 0))
        except CommunityParseError as exc:
            result.failures.append(ProblemFailure(None, edition.source_url, "parse", str(exc), invalid=True))
            return result

        payload = json.dumps(category.get("items") or [], sort_keys=True, ensure_ascii=False)
        result.source_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if known is not None and known.source_hash == result.source_hash:
            result.unchanged = True
            return result

        problems, failures = parse_problem_items(category, url)
        result.failures.extend(failures)
        if not problems and not failures:
            result.failures.append(ProblemFailure(None, url, "parse", "category contains no problem items", True))
        result.problems = [p for p in problems if only_numbers is None or p.number in only_numbers]
        return result
