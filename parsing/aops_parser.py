import asyncio
import re
from bs4 import BeautifulSoup
from curl_cffi import requests
from logger_config import get_file_logger
import random

logger = get_file_logger("parsing", "logs/parsing.log")

def extract_latex(text: str) -> str:
    """Clean HTML entities, strip delimiters, and normalize macros for KaTeX."""
    if not text:
        return ""

    text = (text.replace("&#160;", " ").replace("&#39;", "'").replace("&amp;", "&").replace("&quot;", '"'))
    text = text.strip()
    if (text.startswith("$$") and text.endswith("$$")) or (text.startswith(r"\[") and text.endswith(r"\]")):
        text = text[2:-2]
    elif (text.startswith("$") and text.endswith("$")) or (text.startswith(r"\(") and text.endswith(r"\)")):
        text = text[1:-1]

    text = re.sub(r"\\\[|\\\]|\\\(|\\\)", "", text)
    text = text.replace("&lt;", r"\lt ").replace("&gt;", r"\gt ")
    text = re.sub(r"\{tabular\}(\[\w\])*", "{array}", text)

    return (
        text.replace("align*", "aligned")
        .replace("eqnarray*", "aligned")
        .replace(r"\bold{", r"\mathbf{")
        .replace(r"\congruent", r"\cong")
        .replace(r"\overarc", r"\overgroup")
        .replace(r"\overparen", r"\overgroup")
        .replace(r"\underarc", r"\undergroup")
        .replace(r"\underparen", r"\undergroup")
        .replace(r"\mathdollar", r"\$")
        .replace(r"\textdollar", r"\$")
        .strip()
    )

def normalize_image_url(img):
    src = img.get("src")
    if src and src.startswith("//"):
        img["src"] = "https:" + src

async def fetch_aops_page(page_title: str, max_retries: int = 4) -> str:
    """Fetch raw HTML content from an AoPS wiki page using curl_cffi with exponential jitter backoff."""
    url = f"https://artofproblemsolving.com/wiki/index.php?title={page_title}"

    for attempt in range(1, max_retries + 1):
        try:
            async with requests.AsyncSession() as session:
                response = await session.get(url, impersonate="chrome", timeout=12)

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
            return response.text

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
            response = await session.get(url, impersonate="chrome", timeout=12)
        response.raise_for_status()
        logger.info(f"Successfully fetched AoPS page after 1-minute cool-off: {page_title}")
        return response.text
    except Exception as e:
        logger.error(f"Final attempt after 1-minute pause failed for {page_title}: {e}")
        return ""

def convert_aops_html(aops_html: str) -> str:
    """Convert AoPS image tags into inline and display KaTeX markup."""
    soup = BeautifulSoup(aops_html, "html.parser")

    for img in soup.find_all("img"):
        classes = img.get("class", [])
        alt = img.get("alt", "") or ""
        normalize_image_url(img)

        if alt.lstrip().startswith("[asy]"):
            img["class"] = classes + ["aops-block-image"]
            continue

        if "latex" in classes:
            img.replace_with(soup.new_string(f"\\({extract_latex(alt)}\\)"))
            continue

        if "latexcenter" in classes:
            img.replace_with(soup.new_string(f"\\[{extract_latex(alt)}\\]"))
            continue

        img["class"] = classes + ["aops-block-image"]

    return str(soup)

def extract_problems_from_html(raw_wikitext: str) -> list:
    """Extract individual problem snippets from raw wikitext HTML."""
    soup = BeautifulSoup(raw_wikitext, "html.parser")
    problems = []

    first_h3 = soup.find("h3")
    h3_text = ""

    if first_h3:
        tags = []
        curr = first_h3.previous_sibling
        while curr:
            if "Problems" in curr.text:
                break
            tags.insert(0, str(curr))
            curr = curr.previous_sibling
        h3_text = "".join(tags)

    for header in soup.find_all(["h2", "h3"]):
        headline = header.find(class_="mw-headline")

        if headline and "problem" in headline.get("id", "").lower():
            content = [str(header)]
            if header.name == "h3":
                content.append(h3_text)

            curr = header.next_sibling
            while curr:
                if curr.name in ["h2", "h3"]:
                    break
                if header.name == "h2" and first_h3 and "Problems" in curr.text:
                    break
                if curr.name == "p" and curr.find("a", string=re.compile(r"Solution", re.I)):
                    curr.a.decompose()

                content.append(str(curr))
                curr = curr.next_sibling

            problems.append("".join(content))

    return problems

async def fetch_aops_problem_set(year: int, wiki_title: str) -> list:
    """Fetch problem page HTML and parse into a list of problem snippets."""
    page = f"{year}_{wiki_title}_Problems"
    raw_wikitext = await fetch_aops_page(page)
    return extract_problems_from_html(raw_wikitext)