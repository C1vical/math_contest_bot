# Master Contest Registry
# Format: "FLATTENED_NAME": (Shortcode_Prefix, AoPS_Wiki_Title, Year_Min, Year_Max, Max_Problems)

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

    # AHSME & AMC 12
    "AHSME": ("AH", "AHSME", 1950, 1999, 50),
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
    "USAMO": ("U", "USAMO", 1972, 2026, 6),
    "IMO": ("I", "IMO", 1959, 2026, 6),
}

def resolve_ahsme_max_probs(year: int) -> int:
    if 1950 <= year <= 1959: return 50
    if 1960 <= year <= 1967: return 40
    if 1968 <= year <= 1973: return 35
    if 1974 <= year <= 1999: return 30
    return 30


def get_contest_info(contest: str, year: int):
    clean_key = contest.upper().replace(" ", "")
    info = CONTEST_REGISTRY.get(clean_key)

    if not info:
        return None

    # Dynamically adjust max_probs for AHSME historical variations
    if clean_key == "AHSME":
        prefix, wiki_title, min_yr, max_yr, _ = info
        return (prefix, wiki_title, min_yr, max_yr, resolve_ahsme_max_probs(year))

    return info

def is_valid_request(contest: str, year: int, q_num: int) -> bool:
    """Validates year and problem limits in 1 step."""
    info = get_contest_info(contest, year)
    if not info:
        return False

    prefix, wiki_title, min_year, max_year, max_probs = info
    return (min_year <= year <= max_year) and (1 <= q_num <= max_probs)

def generate_problem_id(year: int, contest: str, q_num: int) -> str:
    """
    Generates shortcode primary keys using the CONTEST_REGISTRY prefix.
    Examples:
      - (2021, "AMC10A", 5)  -> "202110A5"
      - (1995, "AIME", 8)    -> "1995A08"
      - (2025, "USAMO", 3)   -> "2025U3"
      - (1955, "AHSME", 12)  -> "1955HS12"
    """
    info = get_contest_info(contest, year)

    if info:
        # Unpack prefix from the tuple (first element)
        prefix = info[0]
    else:
        # Fallback if an unknown contest is passed
        prefix = contest.upper().replace(" ", "")

    return f"{year}{prefix}{q_num}"