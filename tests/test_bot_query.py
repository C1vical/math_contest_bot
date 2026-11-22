"""Parsing what users type, and the filters carried by the "Another random" button."""

from __future__ import annotations

import pytest

from mathbot.bot.query import ProblemRef, RandomFilters, parse_problem_reference


def resolves(registry):
    return lambda text: registry.resolve(text) is not None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("amc10a 2019 25", ProblemRef("amc10a", 2019, 25)),
        ("AMC 10A 2019 #25", ProblemRef("AMC 10A", 2019, 25)),
        ("2019 AMC 10A Problem 25", ProblemRef("AMC 10A", 2019, 25)),
        ("amc10a-2019-p25", ProblemRef("amc10a", 2019, 25)),  # the id shown in embeds and logs
        ("AIME I 2024 P5", ProblemRef("AIME I", 2024, 5)),
        ("AMC 8 2019 5", ProblemRef("AMC 8", 2019, 5)),  # "8" belongs to the name, "5" is the problem
        ("AIME 1 2019", ProblemRef("AIME 1", 2019, None)),  # not "AIME problem 1"
        ("Euclid 2023 q6", ProblemRef("Euclid", 2023, 6)),
        ("Euclid", ProblemRef("Euclid", None, None)),
        ("  pascal   2019  ", ProblemRef("pascal", 2019, None)),
    ],
)
def test_problem_references(registry, text, expected):
    assert parse_problem_reference(text, resolves(registry)) == expected


def test_out_of_range_numbers_are_left_in_the_name(registry):
    # A 3-digit "number" is not a problem number; it stays part of the (unknown) name.
    assert parse_problem_reference("amc10a 2019 #250", resolves(registry)).number is None


def test_random_filters_round_trip_through_a_button_id():
    filters = RandomFilters(contest="AMC10A", year_from=2010, year_to=2020, difficulty="hard", source="aops_wiki")
    custom_id = filters.custom_id()
    assert custom_id is not None and len(custom_id) <= 100
    assert RandomFilters.from_custom_id(custom_id) == filters
    assert RandomFilters.from_custom_id(RandomFilters().custom_id()) == RandomFilters()


def test_tampered_or_foreign_button_ids_are_rejected():
    assert RandomFilters.from_custom_id("mathbot:answer:x") is None
    assert RandomFilters.from_custom_id("mathbot:rand:y=notayear") is None
    assert RandomFilters.from_custom_id("mathbot:rand:d=impossible") is None
    assert RandomFilters.from_custom_id("mathbot:rand:zz=1") is None


def test_filters_that_do_not_fit_a_button_id_give_no_button():
    assert RandomFilters(contest="X" * 120).custom_id() is None


def test_filters_describe_themselves_for_messages():
    text = RandomFilters(contest="PCF", year_from=2016, year_to=2020, number=5, difficulty="easy").describe()
    assert text == "contest `PCF`, years 2016-2020, problem 5, easy"
    assert RandomFilters(year_from=2020).describe() == "years 2020 onward"
    assert RandomFilters().describe() == ""
