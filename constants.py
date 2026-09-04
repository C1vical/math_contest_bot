DATABASE = "math_problems.db"

# Master Contest Registry
# Format: "KEY": (Shortcode_Prefix, AoPS_Wiki_Title, Year_Min, Year_Max, Max_Problems)
CONTEST_REGISTRY = {
    # AJHSME & AMC 8
    "AJHSME": ("AJ", "AJHSME", 1985, 1998, 25),
    "AMC8": ("8", "AMC_8", 1999, 2026, 25),

    # AMC 10
    "AMC10": ("10", "AMC_10", 2000, 2001, 25),
    "AMC10A": ("10A", "AMC_10A", 2002, 2025, 25),
    "AMC10B": ("10B", "AMC_10B", 2002, 2025, 25),
    "AMC10FA": ("10FA", "Fall_AMC_10A", 2021, 2021, 25),
    "AMC10FB": ("10FB", "Fall_AMC_10B", 2021, 2021, 25),

    # AHSME (Split by historical problem counts)
    "AHSME_1950": ("AH", "AHSME", 1950, 1959, 50),
    "AHSME_1960": ("AH", "AHSME", 1960, 1967, 40),
    "AHSME_1968": ("AH", "AHSME", 1968, 1973, 35),
    "AHSME_1974": ("AH", "AHSME", 1974, 1999, 30),

    # AMC 12
    "AMC12": ("12", "AMC_12", 2000, 2001, 25),
    "AMC12A": ("12A", "AMC_12A", 2002, 2025, 25),
    "AMC12B": ("12B", "AMC_12B", 2002, 2025, 25),
    "AMC12FA": ("12FA", "Fall_AMC_12A", 2021, 2021, 25),
    "AMC12FB": ("12FB", "Fall_AMC_12B", 2021, 2021, 25),

    # AIME
    "AIME": ("A", "AIME", 1983, 1999, 15),
    "AIME1": ("A1", "AIME_I", 2000, 2026, 15),
    "AIME2": ("A2", "AIME_II", 2000, 2026, 15),

    # Olympiads
    "USAJMO": ("UJ", "USAJMO", 2010, 2026, 6),
    "USAMO_EARLY": ("U", "USAMO", 1972, 1995, 5),  # 5 problems pre-1996
    "USAMO_MODERN": ("U", "USAMO", 1996, 2026, 6), # 6 problems 1996-present
    "IMO": ("I", "IMO", 1959, 2026, 6),
}


def get_contest_info(year: int, contest: str):
    """
    Finds the exact registry tuple for a contest and year, matching across split era entries.
    """

    # Direct match for single-range keys (e.g., "AMC10A")
    if contest in CONTEST_REGISTRY:
        info = CONTEST_REGISTRY[contest]
        if info[2] <= year <= info[3]:
            return info

    # Match split-era keys (e.g., matching "AHSME" to "AHSME_1950")
    for key, info in CONTEST_REGISTRY.items():
        base_name = key.split("_")[0]
        if base_name == contest:
            min_yr, max_yr = info[2], info[3]
            if min_yr <= year <= max_yr:
                return info

    return None


def is_valid_request(year: int, contest: str, q_num: int) -> bool:
    """Validates year and problem limits in 1 step."""
    info = get_contest_info(year, contest)
    if not info:
        return False

    _, _, min_year, max_year, max_probs = info
    return (min_year <= year <= max_year) and (1 <= q_num <= max_probs)


def generate_problem_id(year: int, contest: str, q_num: int) -> str:
    """Generates shortcode primary keys using the CONTEST_REGISTRY prefix."""
    info = get_contest_info(year, contest)
    prefix = info[0] if info else contest
    return f"{year}{prefix}{q_num}"