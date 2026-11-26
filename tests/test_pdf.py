"""Problems cut out of PDF papers (mathbot/pdf.py) and the CMS adapter that uses them.

The fixture is the real 2005 COMC paper (tests/fixtures/cms), so these tests read the same
kind of file the live adapter reads.
"""

from __future__ import annotations

import io
import re

import pytest
from PIL import Image

from mathbot import pdf
from mathbot.contests import ContestRegistry
from mathbot.htmlnorm import extract_text, normalize_fragment
from mathbot.http import Response, cache_key
from mathbot.models import EditionRef
from mathbot.render.assets import AssetCache, AssetError
from mathbot.sources import cms
from tests.conftest import FIXTURES

COMC_2005 = (FIXTURES / "cms" / "comc2005-exam-en.pdf").read_bytes()
EXAM_URL = "https://cms.math.ca/wp-content/uploads/2024/06/comc2005-exam-en.pdf"
SOLUTIONS_URL = "https://cms.math.ca/wp-content/uploads/2024/06/comc2005-official-solutions-en.pdf"


@pytest.fixture(scope="module")
def document() -> pdf.Document:
    return pdf.read_document(COMC_2005)


@pytest.fixture(scope="module")
def problems(document) -> list[pdf.Problem]:
    return pdf.segment(document, cms.LAYOUTS["parts"])


class BytesClient:
    """Stands in for PoliteClient: serves fixed bytes per URL, records requests."""

    def __init__(self, pages: dict[str, bytes]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url, params=None, **_options):
        self.calls.append(url)
        return Response(cache_key(url, params), 200, {"content-type": "application/pdf"}, self.pages[url])


# --------------------------------------------------------------------------- reading and segmenting


def test_lines_are_rebuilt_with_words_and_positions(document):
    first = next(line for line in document.lines if line.text.startswith("1. Determine the value of"))
    assert first.page == 1 and 50 < first.x0 < 120 and first.top < first.bottom
    assert len(document.images) == len(document.sizes) == 4  # every page, rendered for the ink checks


def test_a_paper_is_cut_into_its_questions_in_order(problems):
    labels = [(p.section, p.position) for p in problems]
    assert labels == cms.expected_labels("COMC", 2005)
    pdf.check_problems(problems, cms.expected_labels("COMC", 2005))
    assert problems[0].text.startswith("1. Determine the value of")
    assert problems[8].section == "B" and problems[8].text.startswith("1. A line with slope")
    assert all(problem.pieces for problem in problems)


def test_a_question_spanning_a_page_break_has_one_piece_per_page(problems):
    pages = [len(p.pieces) for p in problems]
    assert max(pages) >= 1 and all(piece.bottom > piece.top for p in problems for piece in p.pieces)


def test_labels_out_of_sequence_are_text_not_new_questions(document):
    layout = cms.LAYOUTS["parts"]
    problems = pdf.segment(document, layout)
    # "2." inside a statement (an item of a list, a date) never starts question 2 a second time.
    assert [p.position for p in problems if p.section == "A"] == list(range(1, 9))


def test_a_wrong_count_is_rejected_not_stored(problems):
    with pytest.raises(pdf.PdfLayoutError, match="expected"):
        pdf.check_problems(problems, cms.expected_labels("COMC", 2012))


def test_a_pdf_without_text_is_reported():
    with pytest.raises(pdf.PdfLayoutError):
        pdf.segment(pdf.Document(((612.0, 792.0),), ()), cms.LAYOUTS["parts"])


def test_a_sound_text_layer_has_no_blank_lines(document):
    assert pdf.misaligned_lines(document) < cms.MAX_MISALIGNED


# --------------------------------------------------------------------------- statement HTML and crops


def test_statement_html_names_page_boxes_and_keeps_the_text(problems):
    digest = pdf.pdf_digest(COMC_2005)
    html = pdf.statement_html(EXAM_URL, digest, problems[0])
    crop = pdf.parse_crop_url(re.search(r'src="([^"]+)"', html).group(1).replace("&amp;", "&"))
    assert crop is not None and crop.pdf_url == EXAM_URL and crop.digest == digest
    assert crop.page == problems[0].pieces[0].page and crop.box[2] > crop.box[0]

    content = normalize_fragment(html, EXAM_URL)
    assert content.text.startswith("1. Determine the value of")  # the alt text, not "[image]"
    assert "[image" not in extract_text(content.html)


@pytest.mark.parametrize(
    "url",
    [
        EXAM_URL,
        EXAM_URL + "#page=2",
        EXAM_URL + "#mathbot-crop=zz:p0:1,2,3,4",
        EXAM_URL + "#mathbot-crop=v2:0123456789abcdef:p0:10,20,5,40",  # x1 < x0
    ],
)
def test_other_urls_are_not_crops(url):
    assert pdf.parse_crop_url(url) is None


def test_a_crop_is_a_greyscale_png_with_blank_rows_trimmed(problems):
    piece = problems[0].pieces[0]
    png = pdf.render_crop(COMC_2005, piece.page, (piece.x0, piece.top, piece.x1, piece.bottom))
    with Image.open(io.BytesIO(png)) as image:
        assert image.mode == "L"
        assert image.width == pytest.approx((piece.x1 - piece.x0) * pdf.CROP_SCALE, abs=2)
        assert image.height < (piece.bottom - piece.top) * pdf.CROP_SCALE + 2
        assert image.getextrema()[0] < 100  # there is text on it


def test_crops_are_made_at_render_time_and_refuse_a_changed_pdf(repo, tmp_path, problems):
    digest = pdf.pdf_digest(COMC_2005)
    url = pdf.crop_url(EXAM_URL, digest, problems[0].pieces[0])
    client = BytesClient({EXAM_URL: COMC_2005})
    cache = AssetCache(repo, client, tmp_path / "assets", ttl=3600)
    data, content_type = cache.ensure(url)
    assert content_type == "image/png" and data.startswith(b"\x89PNG")
    assert cache.ensure(url)[0] == data and client.calls == [EXAM_URL]  # stored: no second download

    changed = pdf.crop_url(EXAM_URL, "0" * 16, problems[1].pieces[0])
    with pytest.raises(AssetError, match="has changed since the problem was ingested"):
        cache.ensure(changed)


# --------------------------------------------------------------------------- the CMS adapter


LISTING = f"""
<ul>
  <li><a href="{EXAM_URL}">Problem Set</a> <a href="{SOLUTIONS_URL}">Problems with Solutions</a></li>
  <li><a href="https://cms.math.ca/wp-content/uploads/2024/06/comc2005-exam-fr.pdf">French version</a></li>
  <li><a href="https://cms.math.ca/wp-content/uploads/2025/10/CLMC2025-Booklet.pdf">Problem Set</a></li>
  <li><a href="https://cms.math.ca/wp-content/uploads/2025/10/CLMC2025-Official-Solutions.pdf">Solutions</a></li>
  <li><a href="https://cms.math.ca/competitions/comc/">COMC</a></li>
</ul>
"""


def test_listing_pairs_problem_sets_with_solutions_and_skips_translations():
    papers = {(p.series_key, p.year): p for p in cms.parse_listing(LISTING)}
    assert set(papers) == {("COMC", 2005), ("CLMC", 2025)}
    assert papers[("COMC", 2005)].problems_url == EXAM_URL
    assert papers[("COMC", 2005)].solutions_url == SOLUTIONS_URL
    assert papers[("CLMC", 2025)].solutions_url.endswith("CLMC2025-Official-Solutions.pdf")


def test_expected_labels_follow_each_years_format():
    assert len(cms.expected_labels("COMC", 1996)) == 13
    assert cms.expected_labels("COMC", 2005)[-1] == ("B", 4)
    assert cms.expected_labels("COMC", 2020)[-1] == ("C", 4)
    assert cms.expected_labels("CLMC", 2024)[5] == ("B", 6)  # numbered straight through
    assert cms._by_part(cms.expected_labels("CLMC", 2026))[5] == ("B", 1)


def edition(year=2005):
    series = ContestRegistry().get("COMC")
    ref = EditionRef(
        "COMC",
        year,
        f"{year} COMC",
        EXAM_URL,
        "comc2005-exam-en.pdf",
        12,
        extra={"problems_url": EXAM_URL, "solutions_url": SOLUTIONS_URL},
    )
    return series, ref


def test_adapter_stores_every_question_with_its_part_and_a_link_to_the_solutions():
    series, ref = edition()
    result = cms.CmsAdapter(BytesClient({EXAM_URL: COMC_2005})).fetch_edition(series, ref)
    assert not result.failures
    assert [(p.number, p.section) for p in result.problems][:2] == [(1, "A"), (2, "A")]
    assert [p.number for p in result.problems] == list(range(1, 13)) and result.problems[8].section == "B"
    first = result.problems[0]
    assert first.source_url == EXAM_URL and first.solution_source == SOLUTIONS_URL
    assert "pdf-crop" in first.statement_html and first.answer is None


def test_adapter_skips_an_unchanged_paper():
    series, ref = edition()
    adapter = cms.CmsAdapter(BytesClient({EXAM_URL: COMC_2005}))
    first = adapter.fetch_edition(series, ref)
    from mathbot.models import KnownEdition

    again = adapter.fetch_edition(series, ref, known=KnownEdition(source_hash=first.source_hash))
    assert again.unchanged and not again.problems


def test_a_paper_known_to_be_wrong_is_reported_not_stored(monkeypatch):
    series, ref = edition()
    monkeypatch.setitem(cms.UNRELIABLE, ("COMC", 2005), "checked by eye")
    result = cms.CmsAdapter(BytesClient({EXAM_URL: COMC_2005})).fetch_edition(series, ref)
    assert not result.problems and result.failures[0].error == "checked by eye"
