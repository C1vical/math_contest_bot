import asyncio
import os
import sqlite3
from playwright.async_api import async_playwright
from aops_parser import convert_aops_html
from constants import DATABASE
from logger_config import get_file_logger

logger = get_file_logger("renderer", "renderer.log")

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
    """Render a single problem into PNG format if the output file does not exist."""
    if os.path.exists(output_path):
        return

    async with semaphore:
        if os.path.exists(output_path):
            return

        page = await context.new_page()
        try:
            formatted_html = convert_aops_html(html)
            document = create_html_document(formatted_html)

            await page.set_content(document, wait_until="domcontentloaded")
            await page.locator("body").screenshot(path=output_path)
            logger.info(f"Successfully rendered problem id: {problem_id}")

        except Exception as e:
            logger.error(f"Failed to render problem id {problem_id}: {e}", exc_info=True)
        finally:
            await page.close()

async def render_all_problems(scraper_finished_event: asyncio.Event = None):
    """Poll database for records and render missing problem images."""
    logger.info("Starting Playwright image renderer...")
    os.makedirs("renders", exist_ok=True)
    in_flight = set()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 900, "height": 800}, device_scale_factor=2)
        semaphore = asyncio.Semaphore(4)

        while True:
            with sqlite3.connect(DATABASE) as conn:
                rows = conn.execute("SELECT question_statement, image_path, id FROM math_problems").fetchall()

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

            all_exist = all(os.path.exists(row[1]) for row in rows) if rows else False
            if all_exist and not in_flight:
                break

            await asyncio.sleep(1)

        await browser.close()
        logger.info("Done rendering all problems!")