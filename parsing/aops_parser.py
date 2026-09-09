import asyncio
import re
from bs4 import BeautifulSoup
from curl_cffi import requests
from logger_config import get_file_logger
import random

logger = get_file_logger("parsing", "logging/parsing.log")

async def fetch_problem_html(page_title: str, max_retries: int = 4) -> str:
    """Fetch raw HTML content from an AoPS wiki page using curl_cffi with exponential jitter backoff."""
    url = "https://artofproblemsolving.com/wiki/api.php"

    params = {
        "action": "parse",
        "page": page_title,
        "format": "json",
        "prop": "text",
        "disablelimitreport": "",
        "redirects": ""
    }

    for attempt in range(1, max_retries + 1):
        try:
            async with requests.AsyncSession() as session:
                response = await session.get(url=url, params=params, impersonate="chrome", timeout=12)

            # Handle 429 Rate Limits
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")

                if retry_after and retry_after.isdigit():
                    backoff = int(retry_after) + random.uniform(0.5, 1.5)
                else:
                    # Exponential backoff + jitter (Attempt 1: ~2.5s, 2: ~4.5s, 3: ~8.5s, 4: ~16.5s)
                    backoff = (2 ** attempt) + random.uniform(0.5, 2.0)

                logger.warning(
                    f"Rate limited (429) on {page_title} (Attempt {attempt}/{max_retries}). "
                    f"Backing off for {backoff:.2f}s..."
                )
                await asyncio.sleep(backoff)
                continue

            response.raise_for_status()
            logger.info(f"Fetched AoPS page: {page_title}")

            data = response.json()
            return data.get("parse", {}).get("text", {}).get("*", "")

        except Exception as e:
            backoff = (2 ** attempt) + random.uniform(0.5, 2.0)
            logger.warning(
                f"Error fetching {page_title} (Attempt {attempt}/{max_retries}): {e}. "
                f"Retrying in {backoff:.2f}s..."
            )
            await asyncio.sleep(backoff)

    # Cool-off period if all initial retries fail
    logger.error(
        f"All {max_retries} retries exhausted for {page_title}. "
        f"Pausing program for 60 seconds before final attempt..."
    )
    await asyncio.sleep(60)

    # Final attempt post-cooldown
    try:
        async with requests.AsyncSession() as session:
            response = await session.get(url=url, params=params, impersonate="chrome", timeout=12)
        response.raise_for_status()
        logger.info(f"Successfully fetched AoPS page after 1-minute cool-off: {page_title}")
        data = response.json()
        return data.get("parse", {}).get("text", {}).get("*", "")
    except Exception as e:
        logger.error(f"Final attempt after 1-minute pause failed for {page_title}: {e}")
        return ""

def extract_problem_statement(raw_wikitext: str, year: int, contest: str, q_num: int) -> str:
    """Extract individual problem snippets from raw wikitext HTML."""
    soup = BeautifulSoup(raw_wikitext, "html.parser")

    span_tag = soup.find('span', id=re.compile(r'^Problem', re.IGNORECASE))

    if not span_tag:
        return ""

    h2_tag = span_tag.parent

    curr = h2_tag.next_sibling

    contest_name = contest.replace("_", " ")

    custom_header = f"<h2>{year} {contest_name} Problem {q_num}</h2>"
    problem_statement = [custom_header]

    while curr:
        if curr.name == "h2":
            break
        problem_statement.append(str(curr))
        curr = curr.next_sibling

    raw_statement = "".join(problem_statement)

    return raw_statement.replace('src="//', 'src="https://')

async def fetch_problem_statement(year: int, wiki_title: str, q_num: int) -> str:
    """Fetch problem statement HTML and parse into a string."""
    page_title = f"{year}_{wiki_title}_Problems/Problem_{q_num}"
    raw_html = await fetch_problem_html(page_title)
    return extract_problem_statement(raw_html, year, wiki_title, q_num)