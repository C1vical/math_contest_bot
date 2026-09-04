import asyncio

import cloudscraper
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


# ============================================================
# Extract LaTeX
# ============================================================

def extract_latex(alt: str):
    alt = alt.strip()

    if alt.startswith("$$") and alt.endswith("$$"):
        return alt[2:-2]

    if alt.startswith("$") and alt.endswith("$"):
        return alt[1:-1]

    if alt.startswith(r"\[") and alt.endswith(r"\]"):
        return alt[2:-2]

    return None


# ============================================================
# Normalize image URL
# ============================================================

def normalize_image_url(img):
    src = img.get("src")

    if src and src.startswith("//"):
        img["src"] = "https:" + src

def fetch_aops_page(page_title: str) -> str:
    # url = "https://artofproblemsolving.com/wiki/api.php"
    # scraper = cloudscraper.create_scraper()
    #
    # params = {
    #     "action": "parse",
    #     "page": page_title,
    #     "format": "json",
    #     "prop": "text",
    #     "disablelimitreport": True,
    # }
    #
    # response = scraper.get(url=url, params=params)
    # response.raise_for_status()
    #
    # #print(response.url)
    #
    # data = response.json()

    # return data.get("parse", {}).get("text", {}).get("*", "")

    url = f"https://artofproblemsolving.com/wiki/index.php?title={page_title}"
    scraper = cloudscraper.create_scraper()

    response = scraper.get(url)
    response.raise_for_status()

    print(f"Fetched AoPS page: {page_title}")
    return response.text

def extract_problems_from_html(raw_wikitext: str) -> list:
    soup = BeautifulSoup(raw_wikitext, "html.parser")
    problems = []

    # Find problem headers (h2)
    for header in soup.find_all("h2"):
        headline = header.find(class_="mw-headline")
        if headline and "problem" in headline.get("id").lower():
            content = []
            curr = header
            while curr:
                if curr.name == "p" and curr.find("a"):
                    break
                content.append(str(curr))
                curr = curr.next_sibling
            problems.append("".join(content))

    print(f"Extracted {len(problems)} problems from AoPS HTML.")
    return problems

def fetch_aops_problem_set(year: int, contest: str, edition: str) -> list:
    page = f"{year}_{contest}_{edition}_Problems"

    raw_wikitext = fetch_aops_page(page)

    problems = extract_problems_from_html(raw_wikitext)

    print(f"Fetched {len(problems)} problems from AoPS Wiki for {year} {contest} {edition}.")
    return problems

# ============================================================
# Convert AoPS HTML
# ============================================================

def convert_aops_html(aops_html: str) -> str:

    soup = BeautifulSoup(aops_html, "html.parser")

    for img in soup.find_all("img"):

        classes = img.get("class", [])
        alt = img.get("alt", "") or ""

        normalize_image_url(img)

        # asymptote
        if alt.lstrip().startswith("[asy]"):

            img["class"] = list(
                dict.fromkeys(
                    classes + ["aops-block-image"]
                )
            )

            continue

        # inline latex
        if "latex" in classes:

            latex = extract_latex(alt)

            if latex is None:

                img["class"] = list(
                    dict.fromkeys(
                        classes + ["aops-block-image"]
                    )
                )

                continue

            replacement = soup.new_string(
                r"\(" + latex + r"\)"
            )

            img.replace_with(replacement)

            continue

        # display latex
        if "latexcenter" in classes:

            latex = extract_latex(alt)

            if latex is None:

                img["class"] = list(
                    dict.fromkeys(
                        classes + ["aops-block-image"]
                    )
                )

                continue

            replacement = soup.new_string(
                r"\[" + latex + r"\]"
            )

            img.replace_with(replacement)

            continue

        # otherwise, normal image
        img["class"] = list(
            dict.fromkeys(
                classes + ["aops-block-image"]
            )
        )

    return str(soup)


# ============================================================
# HTML document
# ============================================================

def create_html_document(body_html: str) -> str:

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AoPS Problem</title>
        <style>
            body {{
                font-family: CMU Sans Serif;
                padding: 10px;
            }}
            img.aops-block-image{{
                display: block;
                
                margin-left: auto;
                margin-right: auto;
            }}
        </style>
    
        <!-- 1. Include the KaTeX CSS stylesheet -->
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.18.5/dist/katex.min.css" integrity="sha384-2dNi/m6JtSiviznrOIZ5fTiZ5As0In2QwkuXSgoqcQtCNplvJAbt+jveeN+8en73" crossorigin="anonymous">
            
        <!-- 2. Include the KaTeX JavaScript library -->
        <script defer src="https://cdn.jsdelivr.net/npm/katex@0.18.5/dist/katex.min.js" integrity="sha384-TTF8eEsEKInX2meLzP5V1z/npGYIElXYGksx93f0qBZHu6IL3PdzVB8objytx+TR" crossorigin="anonymous"></script>
            
        <!-- 3. Include the Auto-render extension to easily parse math delimiters -->
        <script defer src="https://cdn.jsdelivr.net/npm/katex@0.18.5/dist/contrib/auto-render.min.js" integrity="sha384-bjyGPfbij8/NDKJhSGZNP/khQVgtHUE5exjm4Ydllo42FwIgYsdLO2lXGmRBf5Mz" crossorigin="anonymous"
            onload="renderMathInElement(document.body);"></script>
    </head>
    <body>

    {body_html}

    </body>
    </html>
"""


# ============================================================
# Wait for images
# ============================================================

async def wait_for_images(page):

    await page.wait_for_function(
        """
        () => {
            return Array.from(
                document.images
            ).every(
                img => img.complete
            );
        }
        """
    )


# ============================================================
# Render
# ============================================================

async def render_aops_to_png(
    aops_html: str,
    output_path: str = "problem.png",
    width: int = 900,
    device_scale_factor: int = 2,
):

    converted_html = convert_aops_html(
        aops_html
    )

    document = create_html_document(
        converted_html
    )

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True
        )

        page = await browser.new_page(
            viewport={
                "width": width,
                "height": 1000,
            },
            device_scale_factor=device_scale_factor,
        )

        await page.set_content(
            document,
            wait_until="networkidle",
        )

        await wait_for_images(page)

        # Allow browser to finish layout.
        await page.evaluate(
            """
            () => new Promise(resolve => {
                requestAnimationFrame(() => {
                    requestAnimationFrame(resolve);
                });
            })
            """
        )

        await page.screenshot(
            path=output_path,
            full_page=True,
        )

        await browser.close()

# ============================================================
# Main
# ============================================================

if __name__ == "__main__":
    problem_set = fetch_aops_problem_set(
        year=2025,
        contest="AMC",
        edition="10A"
    )

    asyncio.run(
        render_aops_to_png(
            problem_set[14],
            output_path="problem.png",
            width=900,
            device_scale_factor=2,
        )
    )

    print("Done! Saved problem.png")