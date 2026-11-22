from datetime import date

import pytest

from mathbot.contests import (
    CEMC,
    ContestRegistry,
    UnknownContestError,
    edition_id,
    normalize_alias,
    problem_id,
)


@pytest.mark.parametrize(
    "text, key",
    [
        ("AMC10A", "AMC10A"),
        ("amc 10a", "AMC10A"),
        ("AMC_10A", "AMC10A"),
        ("Amc-10-A", "AMC10A"),
        ("aime i", "AIME1"),
        ("AIME II", "AIME2"),
        ("aime 2", "AIME2"),
        ("AIME", "AIME"),
        ("fall amc 10a", "AMC10FALLA"),
        ("AMC 12B Fall", "AMC12FALLB"),
        ("euclid", "EUCLID"),
        ("Canadian Mathematical Olympiad", "CMO"),
        ("usajmo", "USAJMO"),
    ],
)
def test_aliases_resolve_case_and_punctuation_insensitively(registry, text, key):
    assert registry.resolve(text).key == key


def test_original_source_name_is_preserved(registry):
    series = registry.resolve("amc 10a")
    assert series.source_name == "AMC_10A"  # exactly as the wiki spells it
    assert series.name == "AMC 10A"


def test_unknown_and_disabled_contests_do_not_resolve(registry):
    assert registry.resolve("putnam") is None
    assert registry.resolve("gauss 7") is None  # registered but disabled by default
    assert registry.resolve("gauss 7", include_disabled=True).key == "GAUSS7"


def test_optional_contests_must_be_enabled_explicitly():
    default_keys = {s.key for s in ContestRegistry().all()}
    assert {"CJMO", "CMOQR", "GAUSS7", "GAUSS8"}.isdisjoint(default_keys)
    enabled = ContestRegistry(enable=["cjmo"], disable=["IMO"])
    assert enabled.resolve("CJMO") is not None
    assert enabled.resolve("IMO") is None


def test_enabling_an_unknown_contest_is_an_error():
    with pytest.raises(UnknownContestError):
        ContestRegistry(enable=["NOPE"])


def test_default_scope_matches_the_supported_families(registry):
    assert set(registry.families()) == {
        "AMC",
        "AIME",
        "USAJMO",
        "USAMO",
        "IMO",
        "CMO",
        "EUCLID",
        "FGH",
        "PCF",
        "CSIMC",
    }
    assert {s.key for s in registry.select("cemc")} == {
        "EUCLID",
        "FRYER",
        "GALOIS",
        "HYPATIA",
        "PASCAL",
        "CAYLEY",
        "FERMAT",
        "CIMC",
        "CSMC",
    }


def test_select_expands_families_and_providers(registry):
    assert {s.key for s in registry.select("pcf")} == {"PASCAL", "CAYLEY", "FERMAT"}
    assert {s.key for s in registry.select("FGH")} == {"FRYER", "GALOIS", "HYPATIA"}
    assert {s.key for s in registry.select("aime")} == {"AIME", "AIME1", "AIME2"}
    assert [s.key for s in registry.select("amc 10b")] == ["AMC10B"]
    assert all(s.provider == CEMC for s in registry.select("cemc"))
    with pytest.raises(UnknownContestError):
        registry.select("putnam")


def test_expected_problem_counts_follow_historical_eras(registry):
    ahsme = registry.get("AHSME")
    assert [ahsme.expected_problems(y) for y in (1950, 1960, 1968, 1974, 1999)] == [50, 40, 35, 30, 30]
    assert ahsme.expected_problems(2005) is None
    usamo = registry.get("USAMO")
    assert usamo.expected_problems(1995) == 5 and usamo.expected_problems(1996) == 6


def test_fallback_years_are_open_ended_up_to_today(registry):
    years = registry.get("AMC8").fallback_years(today=date(2030, 1, 1))
    assert years[0] == 1999 and years[-1] == 2030
    assert registry.get("AMC10").fallback_years() == [2000, 2001]


def test_ids_are_deterministic():
    assert edition_id("AMC10A", 2024) == "amc10a-2024"
    assert problem_id("AMC10A", 2024, 5) == "amc10a-2024-p05"
    assert problem_id("AHSME", 1955, 50) == "ahsme-1955-p50"


def test_normalize_alias():
    assert normalize_alias("  aime-II (2nd) ") == "AIMEII2ND"


def test_early_cmo_problem_counts(registry):
    cmo = registry.get("CMO")
    assert [cmo.expected_problems(y) for y in (1969, 1973, 1975, 1977, 1978, 1979, 2026)] == [10, 7, 8, 7, 6, 5, 5]
