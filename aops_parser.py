import asyncio
import os
import re

from curl_cffi import requests
import sqlite3

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

def extract_latex(text: str) -> str:
    """Clean HTML entities, strip delimiters, and normalize macros for KaTeX."""
    if not text:
        return ""

    # 1. Replace basic HTML entities
    text = (text.replace("&#160;", " ").replace("&#39;", "'").replace("&amp;", "&").replace("&quot;", '"'))

    # 2. Strip outer delimiters ($$, $, \[\], \(\))
    text = text.strip()
    if (text.startswith("$$") and text.endswith("$$")) or (text.startswith(r"\[") and text.endswith(r"\]")):
        text = text[2:-2]
    elif (text.startswith("$") and text.endswith("$")) or (text.startswith(r"\(") and text.endswith(r"\)")):
        text = text[1:-1]

    # Remove any remaining loose delimiter escape tags
    text = re.sub(r"\\\[|\\\]|\\\(|\\\)", "", text)

    # 3. Escape HTML comparison operators and raw dollar signs
    text = text.replace("&lt;", r"\lt ").replace("&gt;", r"\gt ")

    # 4. Transform environments and non-standard LaTeX macros
    text = re.sub(r"\{tabular\}(\[\w\])*", "{array}", text)

    text = (
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
    )

    return text.strip()

def normalize_image_url(img):
    """Ensure relative protocol URLs convert to HTTPS."""
    src = img.get("src")
    if src and src.startswith("//"):
        img["src"] = "https:" + src

# def fetch_aops_page(page_title: str, max_retries: int = 3) -> str:
#     """Fetch raw HTML content from an AoPS wiki page with retries."""
#     url = f"https://artofproblemsolving.com/wiki/index.php?title={page_title}"
#
#     headers = {
#         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
#         "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
#         "Accept-Language": "en-US,en;q=0.9",
#         "Referer": "https://artofproblemsolving.com/wiki/index.php",
#         "Sec-Ch-Ua": '"Not-A.Brand";v="99", "Chromium";v="124", "Google Chrome";v="124"',
#         "Sec-Ch-Ua-Mobile": "?0",
#         "Sec-Ch-Ua-Platform": '"Windows"',
#         "Sec-Fetch-Dest": "document",
#         "Sec-Fetch-Mode": "navigate",
#         "Sec-Fetch-Site": "same-origin",
#     }
#
#     scraper = cloudscraper.create_scraper()
#
#     for attempt in range(1, max_retries + 1):
#         try:
#             time.sleep(random.uniform(2.5, 4.5))
#             response = scraper.get(url, headers=headers)
#
#             if response.status_code == 403:
#                 print(f"403 Forbidden for {page_title} on attempt {attempt}.")
#                 if attempt == max_retries:
#                     return asyncio.run(fetch_aops_page_playwright(page_title))
#
#                 time.sleep(attempt * 5)
#                 continue
#
#             response.raise_for_status()
#             print(f"Fetched AoPS page: {page_title}")
#             return response.text
#
#         except Exception as e:
#             if attempt == max_retries:
#                 print(f"Failed to fetch {page_title}: {e}")
#                 return asyncio.run(fetch_aops_page_playwright(page_title))
#             time.sleep(attempt * 3)
#
#     return ""

async def fetch_aops_page(page_title: str, max_retries: int = 3) -> str:
    """Fetch raw HTML content from an AoPS wiki page with retries."""
    url = f"https://artofproblemsolving.com/wiki/index.php?title={page_title}"

    for attempt in range(1, max_retries + 1):
        try:
            async with requests.AsyncSession() as session:
                response = await session.get(url, impersonate="chrome", timeout=10)
            response.raise_for_status()
            print(f"Fetched AoPS page: {page_title}")
            return response.text
        except Exception as e:
            if hasattr(e, "response") and getattr(e.response, "status_code", None) == 429:
                print(f"Rate limited (429) on {page_title}. Backing off for 10s...")
                await asyncio.sleep(10)

            if attempt == max_retries:
                print(f"Cloudscraper/curl_cffi failed for {page_title}: {e}. Falling back to Playwright.")
                return await fetch_aops_page_playwright(page_title)
            await asyncio.sleep(attempt * 2)

    return ""

async def fetch_aops_page_playwright(page_title: str) -> str:
    """Fallback browser fetcher using Playwright."""
    url = f"https://artofproblemsolving.com/wiki/index.php?title={page_title}"
    print(f"Falling back to Playwright for: {page_title}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = await context.new_page()

        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        if response and response.status == 403:
            print(f"Playwright received 403 on {page_title}")
            await browser.close()
            return ""

        content = await page.content()
        await browser.close()
        return content

def convert_aops_html(aops_html: str) -> str:
    """Convert AoPS image tags into inline and display KaTeX markup."""
    soup = BeautifulSoup(aops_html, "html.parser")

    for img in soup.find_all("img"):
        classes = img.get("class", [])
        alt = img.get("alt", "") or ""

        normalize_image_url(img)

        # Asymptote visual diagrams
        if alt.lstrip().startswith("[asy]"):
            img["class"] = classes + ["aops-block-image"]
            continue

        # Inline LaTeX math tags
        if "latex" in classes:
            latex = extract_latex(alt)
            replacement = soup.new_string(f"\\({latex}\\)")
            img.replace_with(replacement)
            continue

        # Display/Centered LaTeX math tags
        if "latexcenter" in classes:
            latex = extract_latex(alt)
            replacement = soup.new_string(f"\\[{latex}\\]")
            img.replace_with(replacement)
            continue

        # otherwise, normal image
        img["class"] = classes + ["aops-block-image"]

    return str(soup)

def extract_problems_from_html(raw_wikitext: str) -> list:
    """Extract individual problem snippets and convert LaTeX math tags."""
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

                if header.name == "h2" and first_h3:
                    # Check if curr is one of the intro elements or contains the intro text
                    if "Problems" in curr.text:
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

def create_html_document(body_html: str) -> str:
    return f"""<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AoPS Problem</title>

        <link rel="stylesheet" type="text/css" href="https://cdn.jsdelivr.net/gh/dreampulse/computer-modern-web-font@master/fonts.css">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.18.5/dist/katex.min.css">

        <script defer src="https://cdn.jsdelivr.net/npm/katex@0.18.5/dist/katex.min.js"></script>
        <script defer src="https://cdn.jsdelivr.net/npm/katex@0.18.5/dist/contrib/auto-render.min.js" onload="renderMathInElement(document.body);"></script>

        <style>
            * {{ box-sizing: border-box; }}
            html, body {{ margin: 0; padding: 0; background: #ffffff; }}
            body {{
                width: 900px;
                padding: 32px 42px;
                background: #ffffff;
                color: #171717;
                font-family: "Computer Modern Serif";
                font-size: 20px;
                line-height: 1.45;
            }}
            h2, h3 {{ margin: 0 0 18px 0; padding: 0 0 8px 0; font-size: 25px; font-weight: bold; border-bottom: 1px solid #cccccc; }}
            p {{ margin: 0 0 0.75em 0; }}
            .katex {{ font-size: 1.05em; }}
            .katex-display {{ margin: 0.75em 0; }}
            img {{ max-width: 100%; height: auto; }}
            img.aops-block-image {{ display: block; max-width: 90%; height: auto; margin: 14px auto; }}
            ul, ol {{ margin-top: 0.4em; margin-bottom: 0.7em; padding-left: 1.4em; }}
            table {{ border-collapse: collapse; margin: 0.75em auto; }}
            td, th {{ padding: 4px 10px; }}
            .mw-editsection {{ display: none; }}
            a {{ color: inherit; text-decoration: none; }}
        </style>
    </head>
    <body>
        {body_html}
    </body>
    </html>"""


async def render_problem(semaphore, context, html: str, output_path: str, problem_id: str):
    """Render a single problem into PNG format if the output file does not already exist."""
    if os.path.exists(output_path):
        return

    async with semaphore:
        # Double-check file existence in case another task rendered it while waiting on semaphore
        if os.path.exists(output_path):
            return

        page = await context.new_page()
        try:
            formatted_html = convert_aops_html(html)
            document = create_html_document(formatted_html)

            await page.set_content(document, wait_until="domcontentloaded")

            await page.locator("body").screenshot(path=output_path)
            print(f"Successfully rendered problem id: {problem_id}!")

        except Exception as e:
            print(f"Failed to render problem id {problem_id}: {e}")
        finally:
            await page.close()


async def render_all_problems(scraper_finished_event: asyncio.Event = None):
    """Poll database for records and render missing problem images, exiting when all target image files exist."""
    print("Starting Playwright image renderer...")
    os.makedirs("renders", exist_ok=True)

    in_flight = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 900, "height": 800}, device_scale_factor=2)
        semaphore = asyncio.Semaphore(4)

        while True:
            with sqlite3.connect("math_problems.db") as conn:
                rows = conn.execute("SELECT question_statement, image_path, id FROM math_problems").fetchall()

            # Filter for problems where the output PNG does not exist on disk and is not currently rendering
            unrendered_rows = [
                row for row in rows
                if not os.path.exists(row[1]) and row[2] not in in_flight
            ]

            for html, output_path, problem_id in unrendered_rows:
                in_flight.add(problem_id)

                async def task_wrapper(h=html, op=output_path, pid=problem_id):
                    try:
                        await render_problem(semaphore, context, h, op, pid)
                    finally:
                        in_flight.remove(pid)

                asyncio.create_task(task_wrapper())

            # Check if all image files exist on disk
            all_exist = all(os.path.exists(row[1]) for row in rows) if rows else False

            # Exit condition: every target file exists on disk and no tasks are active in flight
            if all_exist and not in_flight:
                break

            await asyncio.sleep(1)

        await browser.close()
        print("Done rendering all problems!")

# async def render_specific_problem(problem_id: int):
#     with sqlite3.connect("math_problems.db") as conn:
#         row = conn.execute("SELECT question_statement, image_path FROM math_problems WHERE id = ?", (problem_id,)).fetchone()
#
#     async with async_playwright() as p:
#         browser = await p.chromium.launch(headless=True)
#         context = await browser.new_context(viewport={"width": 900, "height": 800}, device_scale_factor=2)
#         semaphore = asyncio.Semaphore(1)
#
#         task = render_problem(semaphore, context, html=row[0], output_path=row[1], id=problem_id)
#
#         await asyncio.gather(task)
#
#         await browser.close()
#         print("Done!")


if __name__ == "__main__":
    asyncio.run(render_all_problems())