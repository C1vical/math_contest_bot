from curl_cffi import requests
from bs4 import BeautifulSoup
import re
import asyncio
from playwright.async_api import async_playwright
from url import extract_urls

def fetch_raw_html() -> str:
    urls = extract_urls()
    url = urls[("Fermat", "2017")]
    response = requests.get(url, impersonate="chrome")
    # response.raise_for_status()
    print(response.status_code)

    return response.text

def extract_problems(raw_html: str) -> list[str]:
    soup = BeautifulSoup(raw_html, "html.parser")
    problems = []

    for header in soup.find_all("h2", string=re.compile(r"Part [A-C]")):
        header = header.find_next_sibling()
        for child in header.children:
            if not child.name: # newline
                continue
            elif child.name == "h2": # next section
                break
            elif child.name == "li": # problem
                problems.append(str(child).removeprefix("<li>").removesuffix("</li>"))
            elif child.name == "ol": # other part of previous problem, don't append, instead add to previous
                problems[-1] = problems[-1] + str(child).removeprefix("<ol>").removesuffix("</ol>")
    return problems

def extract_head(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    return str(soup.find("head"))

def create_html_document(head_html: str, body_html: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    {head_html}
    <body>
    {body_html}
    </body>
    </html>
    """

async def render(semaphore, context, raw_html: str, problem: str, num: int):
    async with semaphore:
        page = await context.new_page()

        head = extract_head(raw_html)
        document = create_html_document(head, problem)

        await page.set_content(document)

        output_path = f"PCF_renders/{num}.png"
        await page.locator("body").screenshot(path=output_path)

        await page.close()
        print(f"Rendered problem {num}!")

async def render_problems():
    print("Rendering problems...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # context = await browser.new_context(viewport={"width": 900, "height": 800}, device_scale_factor=2)
        context = await browser.new_context(device_scale_factor=2)
        semaphore = asyncio.Semaphore(8)

        raw = fetch_raw_html()
        problems = extract_problems(raw)
        print(len(problems))

        tasks = [render(semaphore, context, raw, problem, num) for num, problem in enumerate(problems, start=1)]
        await asyncio.gather(*tasks)

        await browser.close()
        print("Done rendering!")

if __name__ == "__main__":
    asyncio.run(render_problems())

    # raw = fetch_raw_html()
    # problems = extract_problems(raw)
    #
    # print(len(problems))
    # # print(problems)
    # for problem in problems:
    #     print(problems)
    # print(len(problems))