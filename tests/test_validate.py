"""Data-quality checks in ``validate`` that catch parser and import problems automatically."""

from __future__ import annotations

from mathbot.models import EditionRef, ParsedProblem
from mathbot.validate import validate
from tests.conftest import add_problem, store


def checks(report, name):
    return [f.message for f in report.findings if f.check == name]


def test_solution_text_left_in_a_statement_is_reported(repo, tmp_path):
    add_problem(repo, 1, "<p>Find the largest product.</p><h3>Solution 1</h3><p>Use threes.</p>")
    add_problem(repo, 2, "<p>How many integer solutions are there?</p>")  # mentions solutions: fine
    report = validate(repo, tmp_path, "x")
    assert checks(report, "leaked-solution") == ["imo-2020-p01 contains solution text after the statement"]


def test_unbalanced_tex_delimiters_are_reported(repo, tmp_path):
    add_problem(repo, 1, r"<p>Compute \(x^2 + 1 when x = 3.</p>")
    add_problem(repo, 2, r"<p>Compute \(x^2\) and \[\begin{array}{c} a \\[-2mm] b \end{array}\].</p>")
    assert checks(validate(repo, tmp_path, "x"), "tex-delimiters") == ["imo-2020-p01: 1 \\( but 0 \\)"]


def test_multiple_choice_problems_without_an_answer_are_reported(repo, registry, tmp_path):
    ref = EditionRef("PASCAL", 2024, "2024 Pascal Contest", "https://cemc.example/2024", None, 25)
    for number, answer in ((1, "B"), (2, None)):
        html = f"<p>Pascal question {number} of the contest.</p>"
        store(repo, ref, ParsedProblem(number, html, ref.source_url, raw_payload=html, answer=answer))
    report = validate(repo, tmp_path, "x", registry=registry)
    assert checks(report, "missing-answer") == ["pascal-2024: 1 of 2 multiple-choice problems have no answer"]
    assert checks(validate(repo, tmp_path, "x"), "missing-answer") == []  # needs the registry


def test_known_extra_text_and_unrendered_tex_are_reported(repo, tmp_path):
    leak = "<p>Find the sum.</p><p>First do base conversion of both numbers.</p>"
    add_problem(repo, 1, leak, key="AIME1", year=2025)
    add_problem(repo, 2, leak, key="AIME1", year=2025)  # not a listed problem
    add_problem(repo, 3, "<p>Solve \\begin{align*} x &amp;= 1 \\end{align*} now.</p>", key="AIME1", year=2025)
    add_problem(repo, 4, "<p>It costs $5 and $6, so \\(x = 1\\).</p>", key="AIME1", year=2025)
    report = validate(repo, tmp_path, "x")
    assert checks(report, "leaked-solution") == ["aime1-2025-p01 contains solution text after the statement"]
    assert checks(report, "unrendered-tex") == ["aime1-2025-p03: bare TeX environment, bare TeX command"]
