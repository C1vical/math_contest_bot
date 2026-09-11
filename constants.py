from pathlib import Path

R2_BUCKET_URL = "https://pub-c5ba57c8ddcb4baf8d2fc7de913627cf.r2.dev"

BASE_DIR = Path(__file__).resolve().parent

DATABASE = BASE_DIR / "data" / "math_problems.db"
RENDERS_DIR = BASE_DIR / "data" / "renders"

RENDERS_DIR.mkdir(parents=True, exist_ok=True)

# Contest Registry
# "KEY": (AoPS_Wiki_Title, min_year, max_year, max_probs)
CONTEST_REGISTRY = {
    "AJHSME": ("AJHSME", 1985, 1998, 25),
    "AMC8": ("AMC_8", 1999, 2026, 25),
    "AMC10": ("AMC_10", 2000, 2001, 25),
    "AMC10A": ("AMC_10A", 2002, 2025, 25),
    "AMC10B": ("AMC_10B", 2002, 2025, 25),
    "AMC10FALLA": ("Fall_AMC_10A", 2021, 2021, 25),
    "AMC10FALLB": ("Fall_AMC_10B", 2021, 2021, 25),
    # AHSME (Split by historical problem counts)
    "AHSME_1950": ("AHSME", 1950, 1959, 50),
    "AHSME_1960": ("AHSME", 1960, 1967, 40),
    "AHSME_1968": ("AHSME", 1968, 1973, 35),
    "AHSME_1974": ("AHSME", 1974, 1999, 30),
    "AMC12": ("AMC_12", 2000, 2001, 25),
    "AMC12A": ("AMC_12A", 2002, 2025, 25),
    "AMC12B": ("AMC_12B", 2002, 2025, 25),
    "AMC12FALLA": ("Fall_AMC_12A", 2021, 2021, 25),
    "AMC12FALLB": ("Fall_AMC_12B", 2021, 2021, 25),
    "AIME": ("AIME", 1983, 1999, 15),
    "AIME1": ("AIME_I", 2000, 2026, 15),
    "AIME2": ("AIME_II", 2000, 2026, 15),
    "USAJMO": ("USAJMO", 2010, 2026, 6),
    "USAMO_1972": ("USAMO", 1972, 1995, 5),  # 5 problems pre-1996
    "USAMO_1996": ("USAMO", 1996, 2026, 6), # 6 problems 1996-present
    "IMO": ("IMO", 1959, 2026, 6),
}

def get_contest_info(year: int, contest: str):
    """Finds the exact registry tuple for a contest and year"""
    if contest in CONTEST_REGISTRY:
        info = CONTEST_REGISTRY[contest]
        if info[1] <= year <= info[2]:
            return info

    for key, info in CONTEST_REGISTRY.items():
        base_name = key.split("_")[0]
        if base_name == contest:
            min_yr, max_yr = info[1], info[2]
            if min_yr <= year <= max_yr:
                return info

    return None

def generate_problem_id(year: int, contest: str, q_num: int) -> str:
    """Generates shortcode primary keys using the CONTEST_REGISTRY prefix."""
    return f"{year}_{contest}_{q_num}"