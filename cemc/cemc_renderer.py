import asyncio
import os
from playwright.async_api import async_playwright
from constants import CEMC_RENDERS_DIR
from cemc.cemc_parser import fetch_raw_html, extract_problems

def create_html_document(body_html: str) -> str:
    return f"""<!DOCTYPE html>
    <html lang="en">

    <head>
        <meta charset="utf-8" />
        <meta content="pandoc" name="generator" />
        <meta content="width=device-width, initial-scale=1.0, user-scalable=yes" name="viewport" />

        <title>parser</title>

        <link
            rel="stylesheet"
            type="text/css"
            href="https://cdn.jsdelivr.net/gh/dreampulse/computer-modern-web-font@master/fonts.css"
        />

        <style type="text/css">
            html,
            body {{
                margin: 0;
                padding: 0;
                background: #ffffff;
            }}

            html {{
                line-height: 1.45;
                font-family: "Computer Modern Serif";
                font-size: 20px;
                color: #171717;
                background-color: #fdfdfd;
            }}

            body {{
                max-width: 960px;
                padding: 32px 42px;
                hyphens: auto;
                word-wrap: normal;
                text-rendering: optimizeLegibility;
                font-kerning: normal;
                counter-set: list;
            }}

            @media (max-width: 720px) {{
                body {{
                    font-size: 16px;
                    padding: 1em;
                }}
            }}

            @media print {{
                body {{
                    background-color: transparent;
                    color: black;
                    font-size: 12pt;
                }}

                p,
                h2,
                h3 {{
                    orphans: 3;
                    widows: 3;
                }}

                h2,
                h3,
                h4 {{
                    page-break-after: avoid;
                }}
            }}

            p {{
                margin: 0 0 0.75em 0;
            }}

            a {{
                color: inherit;
                text-decoration: none;
            }}

            a:visited {{
                color: #008094;
                text-decoration: underline;
            }}

            a:hover {{
                color: #004752;
                text-decoration: underline;
            }}

            img {{
                max-width: 100%;
                min-width: 320px;
            }}

            .center {{
                text-align: center;
            }}

            .static {{
                max-width: 100%;
                min-width: 0%;
            }}

            h3 {{
                font-size: 1.1em;
            }}

            ol,
            ul {{
                padding-left: 1.7em;
                margin-top: 1em;
            }}

            li > ol,
            li > ul {{
                margin-top: 0;
            }}

            blockquote {{
                margin: 1em 0 1em 1.7em;
                padding-left: 1em;
                border-left: 2px solid #e6e6e6;
                color: #575757;
            }}

            code {{
                font-family: Menlo, Monaco, 'Lucida Console', Consolas, monospace;
                font-size: 85%;
                margin: 0;
                white-space: pre-wrap;
            }}

            pre {{
                margin: 1em 0;
                overflow: auto;
            }}

            pre code {{
                padding: 0;
                overflow: visible;
            }}

            .sourceCode {{
                background-color: transparent;
                overflow: visible;
            }}

            hr {{
                background-color: #1a1a1a;
                border: none;
                height: 1px;
                margin: 1em 0;
            }}

            table {{
                margin-left: auto;
                margin-right: auto;
                border-collapse: collapse;
                overflow-x: hidden;
                font-variant-numeric: lining-nums tabular-nums;
            }}

            table caption {{
                margin-bottom: 0.75em;
            }}

            tbody {{
                margin-top: 0.5em;
                border-bottom: 1px solid #1a1a1a;
            }}

            th {{
                border-top: 1px solid #1a1a1a;
                padding: 0.25em 0.5em;
                border: 1px solid #808080;
            }}

            td {{
                padding: 0.125em 0.5em 0.25em;
                border: 1px solid #808080;
            }}

            header {{
                margin-bottom: 4em;
                text-align: center;
            }}

            #TOC li {{
                list-style: none;
            }}

            #TOC a:not(:hover) {{
                text-decoration: none;
            }}

            span.smallcaps {{
                font-variant: small-caps;
            }}

            span.underline {{
                text-decoration: underline;
            }}

            div.hanging-indent {{
                margin-left: 1.5em;
                text-indent: -1.5em;
            }}

            ul.task-list {{
                list-style: none;
            }}

            h1 {{
                text-align: center;
                font-size: 34px;
            }}

            h2 {{
                margin: 0 0 18px 0;
                padding: 0 0 8px 0;
                font-size: 25px;
                font-weight: bold;
                line-height: 1.25;
                border-bottom: 1px solid #cccccc;
            }}

            .images {{
                width: 100%;
                margin: 0 auto;
            }}

            .block {{
                width: 200px;
                display: inline-block;
            }}

            .infobox {{
                border-style: solid;
                padding: 10px;
            }}

            #highlight {{
                background-color: lightgrey;
                border-radius: 25px;
                padding: 10px;
                border: 2px solid black;
            }}

            ol.nostyle li {{
                list-style-type: none;
            }}

            ol.mc {{
                counter-reset: list list-item;
            }}

            ol.mc li {{
                list-style: none;
                padding: 7px 0;
            }}

            ol.mc li:before {{
                content: " (" counter(list, upper-alpha) ") ";
                counter-increment: list;
            }}

            ol.horizontal {{
                width: 100%;
                counter-reset: list list-item;
                display: flex;
                justify-content: flex-start;
                align-items: baseline;
                flex-wrap: wrap;
            }}

            ol.horizontal li {{
                list-style: none;
                width: 185px;
                padding: 2px 0;
            }}

            ol.horizontal li:before {{
                content: " (" counter(list, upper-alpha) ") ";
                counter-increment: list;
            }}

            button {{
                border-radius: 8px;
                background-color: #00BDDA;
                border: 0;
                border-bottom: 2px solid #1a1a1a;
                color: #1a1a1a;
                padding: 8px;
                text-align: center;
                text-decoration: none;
                display: inline-block;
                font-size: 16px;
            }}

            button:hover {{
                background-color: #008094;
                border-bottom-color: #008094;
                color: #fdfdfd;
            }}

            hr.longdesc {{
                background-color: #fdfdfd;
                border-top: dashed 1px #1a1a1a;
            }}

            .column-five {{
                float: left;
                width: 20%;
            }}

            .column-three {{
                float: left;
                width: 33%;
            }}

            .row:after {{
                content: "";
                display: table;
                clear: both;
            }}

            @media screen and (max-width: 960px) {{
                .column-three {{
                    width: 100%;
                }}

                .column-five {{
                    width: 50%;
                }}
            }}
        </style>

        <script
            src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml-full.js"
            type="text/javascript"
        ></script>

        <!--[if lt IE 9]>
        <script src="//cdnjs.cloudflare.com/ajax/libs/html5shiv/3.7.3/html5shiv-printshiv.min.js"></script>
        <![endif]-->
    </head>

    <body>
        {body_html}
    </body>

    </html>
    """

async def render(semaphore, context, year: str, contest: str, problem: str, num: int):
    output_path = os.path.join(CEMC_RENDERS_DIR, f"{year}_{contest}_{num}.png")

    if os.path.exists(output_path):
        return

    async with semaphore:
        page = await context.new_page()
        try:
            document = create_html_document(problem)
            await page.set_content(document)
            await page.locator("body").screenshot(path=output_path)
            print(f"Successfully rendered problem {num}!")
        except Exception as e:
            print(f"Failed to rendered problem {num}!")
        finally:
            await page.close()

async def render_all_problems(contest: str, year: str):
    print("Rendering problems...")
    os.makedirs(CEMC_RENDERS_DIR, exist_ok=True)

    raw = fetch_raw_html(contest, year)
    problems = extract_problems(raw)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # context = await browser.new_context(viewport={"width": 900, "height": 800}, device_scale_factor=2)
        context = await browser.new_context(device_scale_factor=2)
        semaphore = asyncio.Semaphore(8)

        tasks = [render(semaphore, context, year, contest, problem, num) for num, problem in enumerate(problems, start=1)]

        await asyncio.gather(*tasks)
        await browser.close()

    print("Done rendering all problems!")

if __name__ == "__main__":
    asyncio.run(render_all_problems(contest="Fermat", year="2024"))