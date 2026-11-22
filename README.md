# Math Contest Bot

A Discord bot that serves competition maths problems as rendered images, plus
the ingestion and rendering pipeline that builds its database.

| Source | Contests | How it is read | Status (checked 2026-11-17) |
|---|---|---|---|
| [CEMC](https://cemc.uwaterloo.ca/resources/past-contests) | Euclid; Fryer, Galois, Hypatia (FGH); Pascal, Cayley, Fermat (PCF); CIMC, CSMC | Server-rendered listing + pandoc HTML contest pages | **Working.** 97 editions / 1,247 problems, 2016-2026 (CIMC/CSMC 2016-2025), plus 1,222 official solutions and 575 answers, ingested and rendered end to end |
| [AoPS Wiki](https://artofproblemsolving.com/wiki/) | AMC 8/10/10A/10B/12/12A/12B, Fall 2021 AMC, AJHSME, AHSME, AIME / I / II, USAJMO, USAMO, IMO | MediaWiki Action API (`api.php`) | **Live access blocked** for automated clients (see [Source access](#source-access-and-attribution)). 7,310 problems available by [importing the pre-2.0 database](#importing-the-pre-20-database) |
| [AoPS Community](https://artofproblemsolving.com/community/c3277) | CMO (CJMO, CMOQR optional) | Category page's embedded JSON | **Live access blocked**. 323 CMO problems available from the pre-2.0 database |

## Contents

- [Setup](#setup)
- [Configuration](#configuration)
- [Ingestion](#ingestion)
- [Importing the pre-2.0 database](#importing-the-pre-20-database)
- [Rendering and image storage](#rendering-and-image-storage)
- [Running the bot](#running-the-bot)
- [Database schema](#database-schema)
- [Source access and attribution](#source-access-and-attribution)
- [Development](#development)

## Setup

Python 3.10 or newer.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.lock           # exact, tested versions (requirements.txt has the ranges)
python -m playwright install chromium      # headless browser used for rendering
# Linux servers may also need: python -m playwright install-deps chromium
python -m mathbot install-render-assets    # pinned MathJax + fonts, verified by SHA-256
cp .env.example .env                       # then edit .env
python -m mathbot migrate                  # creates data/problems.db
```

`requirements.lock` / `requirements-dev.lock` pin every package for Python 3.10+ on
all platforms. After changing `requirements*.txt`, regenerate them with the
command at the top of each lock file.

Rendering is fully offline once `install-render-assets` has run (see
[Rendering](#rendering-and-image-storage)). The bot host needs neither Chromium nor
the render assets.

## Configuration

All settings come from environment variables (or `.env`); see
[.env.example](.env.example) for the full list with comments. Invalid values are
rejected at startup with the variable's name in the error.

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token. Only needed by `python -m mathbot bot`. |
| `ALLOWED_CHANNEL_IDS`, `ALLOWED_ROLE_IDS` | Restrict where and by whom commands work. Empty means no restriction. |
| `ADMIN_ROLE_IDS`, `ADMIN_USER_IDS` | Who may use `/admin`. If both are empty, members with **Manage Server** are admins. |
| `GUILD_ID` | Sync slash commands to one server instantly instead of globally. |
| `ANNOUNCE_CHANNEL_ID` | Post "online" once per process start (not on every reconnect). |
| `ENABLE_CONTESTS`, `DISABLE_CONTESTS` | Switch optional contests on (e.g. `CJMO,CMOQR,GAUSS7`) or defaults off. |
| `HTTP_USER_AGENT` | Identify your crawler honestly, with contact details. |
| `HTTP_MIN_INTERVAL` | Seconds between page requests to one host (default 1). |
| `ROBOTS_OVERRIDE_HOSTS` | Hosts whose robots.txt you choose to ignore. Set to `latex.artofproblemsolving.com` to get AoPS's images (see [AoPS images](#aops-images)). Never affects anti-bot challenges. |
| `ASSET_MIN_INTERVAL` | Seconds between statement-image downloads (default 1), separate from page scraping. |
| `STORAGE_BACKEND`, `S3_*`, `IMAGE_BASE_URL` | Where rendered images and database snapshots are published and fetched from. |
| `DATABASE_SNAPSHOT_URL` | Bot host: install the newest published database at startup (see [Deploying](#deploying)). |
| `COMMAND_COOLDOWN_SECONDS` | Per-user pause between `/problem` and `/random` (default 3, `0` disables, admins exempt). |

The contest registry lives in [mathbot/contests.py](mathbot/contests.py): every
series has a canonical key (`AMC10A`), a display name, the exact name the source
uses (`AMC_10A`), aliases, and its historical problem counts. Optional series are
registered disabled so they can be switched on deliberately once their source has
been checked.

## Ingestion

```bash
python -m mathbot contests                     # registry: what exists and what is enabled
python -m mathbot discover cemc -v             # editions each source offers (no writes)
python -m mathbot ingest PCF                   # a family...
python -m mathbot ingest euclid --year 2016-2019
python -m mathbot ingest "aime i" --year 2024 --number 7     # one problem
python -m mathbot refresh                      # every enabled contest
python -m mathbot refresh --dry-run            # fetch + parse, write nothing, show the report
python -m mathbot refresh --full               # re-fetch everything, ignoring caches
python -m mathbot refresh --resume             # skip editions an interrupted run finished
python -m mathbot validate                     # integrity, data quality, missing renders (exit 1 on errors)
python -m mathbot coverage --csv gaps.csv      # what's missing: images, years, problem numbers, AMC choices
python -m mathbot stats
```

`CONTEST` arguments accept a series, a family (`AMC`, `AIME`, `FGH`, `PCF`, `CSIMC`) or a
source (`cemc`, `aops_wiki`, `aops_community`), case- and punctuation-insensitively.

How it behaves:

- **Incremental by default.** CEMC pages and AoPS Community categories are skipped
  when their content hash is unchanged; AoPS Wiki pages are skipped when their
  revision id is unchanged. A CEMC page's fingerprint also includes the parser
  version (`PARSER_VERSION` in `sources/cemc.py`), so after a parser fix an ordinary
  incremental run re-parses every page once, from the HTTP cache where possible. HTTP responses are cached in `data/http_cache` with a
  TTL and revalidated with ETag / Last-Modified.
- **Corrections replace stale data.** A changed statement updates the row, bumps
  its `version` and keeps every earlier version in `problem_versions`.
- **Nothing fake is stored.** Empty or unparseable statements are counted as
  *invalid* and never inserted. Answers are stored only when the source publishes
  them (AMC/AIME answer-key pages, CEMC multiple-choice solution pages).
- **Official solutions.** For CEMC, each question's official solution is stored
  (versioned, like statements) from the solution page, but only when that page has
  exactly one solution per question; otherwise none are stored and the run says so.
  The bot links to it. After upgrading from an earlier 2.0 build, run
  `python -m mathbot ingest cemc --full` once: incremental runs skip unchanged
  contest pages, so they would not pick solutions up.
- **Failures are visible.** Every run writes a report (discovered / inserted /
  updated / skipped / failed / invalid per contest) to the terminal, the
  `ingestion_runs` table and optionally a JSON file (`--report FILE`). Each failure
  is logged and stored with its source, contest, year, problem number, URL, stage
  and reason. The exit code is non-zero when anything failed.
- **Polite.** One shared HTTP client enforces robots.txt, a per-host delay
  (`HTTP_MIN_INTERVAL`, default 1 s), bounded concurrency (`INGEST_CONCURRENCY`)
  and exponential backoff that honours `Retry-After`.

CEMC layout quirks the parser handles (each covered by a test using the real page):
a question list closed early (2016 Euclid), `<li>` closed early so the answer
choices fall outside it (2019 Pascal), Part A never closed so Parts B and C nest
inside it (2017, 2021 Pascal), a wrong "Number of Questions" (2016 Galois), and
CIMC/CSMC Part B lists numbered within the part (`<ol start="2">` for question 8,
in several 2016-2020 papers). A page whose question count still disagrees with the
expected count is rejected, not stored with uncertain numbering.

CIMC and CSMC (Canadian Intermediate / Senior Mathematics Contests, written in
November) share listing category 23 (family `CSIMC`). Each has 9 questions (Part A:
6 short-answer, Part B: 3 full-solution) and an official solution for each. Their
answers are not stored: the solution pages don't state them in a consistent form.

**Coverage.** `python -m mathbot coverage` lists what is missing: problems the bot
can only show as text, grouped by cause and contest; contest years the registry
expects but the database lacks (sittings that never happened, the 2021 AMC 8 and
the 1980 IMO, are not gaps, and the current year only counts once its edition is
normally published); editions with missing problem numbers; and AMC statements
with no visible answer choices. `--csv FILE` writes one row per gap.

## Importing the pre-2.0 database

The old bot kept everything in one `math_problems` table. Those rows are the only
AoPS content you can have while AoPS blocks automated clients, so they can be
imported:

```bash
python -m mathbot import-legacy path/to/old/math_problems.db --dry-run   # report only
python -m mathbot import-legacy path/to/old/math_problems.db
python -m mathbot import-legacy path/to/old/problems.db                  # another old file
python -m mathbot import-legacy OLD.db --include-disabled                # also Gauss / CJMO / CMOQR
```

The old database is opened read-only. The import:

- maps the old contest names (`AMC10A`, `AIME1`, `Euclid`, `Gauss7`, `CMO`, ...) to
  the registry and lists any it can't map instead of guessing;
- strips the `<h2>... Problem N</h2>` titles the old scrapers prepended;
- cuts AoPS Wiki statements at the first heading or solution marker: the old
  scraper sometimes kept going into the solutions (`<h3>Solution 1</h3>` and the
  proof, a bare `Solution` paragraph, `==Video Solution`), which then showed up in
  the problem image. 40 of the 7,310 rows were affected;
- **ignores every old answer** (the old code only ever stored the default `"0"`);
- rejects statements that are empty once the title is gone;
- never overwrites a problem that a live ingest already stored, and never
  downgrades a live edition;
- marks what it imports: edition `origin = 'legacy_import'`, and source records
  whose identifier starts with `legacy_import:`, with the original row kept as the
  raw payload.

A later live ingest replaces an imported statement through the normal versioned
upsert. Rerunning the import changes only rows whose cleaned statement differs
(after an import-rule fix, such as the solution cutting above, run it again; the
changed problems get a new version and `render` redraws them).

Quality caveat: the old AoPS scraper stopped at the first element containing the
word "solution", so a few statements may be cut short. `validate` warns about every
AMC-family problem that shows no "(A)" answer choice (`no-answer-choices`, also in
`coverage`). In the import checked on 2026-11-17, that was 32 of 5,435, several of
them false alarms where the choices are an image.

## Rendering and image storage

```bash
python -m mathbot render                 # only problems whose content or renderer changed
python -m mathbot render PCF --limit 20
python -m mathbot render --dry-run       # how many need rendering
python -m mathbot upload                 # publish renders not yet in storage (local: nothing to copy)
python -m mathbot cleanup --dry-run      # superseded renders and orphan files
python -m mathbot cleanup --remote       # also delete superseded objects from the bucket
```

- Each render is keyed by **(problem, content hash, renderer version)** in the
  `renders` table, so unchanged problems are never rendered twice, and a corrected
  statement or a template change (`TEMPLATE_VERSION` in
  [mathbot/render/template.py](mathbot/render/template.py)) is picked up automatically.
  The previous renderer's image stays in use until the new one exists; only then does
  `cleanup` delete it. A failed render never takes an image away.
- Paths are deterministic and include the hash, e.g.
  `pascal/pascal-2019-p03-423bd5e391b5-r7-w1000-s2.png`, so a new render never
  overwrites the one currently being served.
- **Layout (template 7).** AoPS formula PNGs are drawn at 1.25x so they match the
  20 px text (the files are about 2.5x their HTML size, so they stay sharp);
  diagrams keep their size. A block wider than the page is shrunk to fit instead of
  being cut off. Before/after examples are in [docs/render-samples](docs/render-samples).
- **Robust browser pool.** A page that crashes or times out is replaced, so one bad
  problem never breaks the renders after it. An unexpected error building one page
  fails only that problem.
- **Offline and deterministic.** MathJax 3.2.2 (SVG output) and the Computer
  Modern fonts are pinned by URL and SHA-256 in
  [mathbot/render/vendor.py](mathbot/render/vendor.py), installed once by
  `install-render-assets`, and served to the page from disk. Every other request
  the page makes is blocked, so a render can neither depend on nor leak to the
  network. The renderer refuses to start if the assets are missing or altered.
- **AoPS statements are rendered as stored**, with AoPS's own PNGs for formulas and
  Asymptote diagrams, as the pre-2.0 bot did. Every remote image is downloaded in a
  separate step *before* rendering, through the polite client (robots.txt, rate
  limit), validated by its bytes, cached by SHA-256 in `data/assets` and embedded
  into the page as a `data:` URI. The browser workers never do network I/O.
- **Fallback for formulas only.** Each AoPS formula image's `alt` text is its TeX
  source. If a formula's PNG is permanently unavailable (404, challenge), the
  renderer turns it back into TeX and the pinned MathJax typesets it locally
  ([mathbot/render/aops_tex.py](mathbot/render/aops_tex.py)). A TeX error there
  fails the render rather than producing an image with an error box in it.
- **A missing diagram or other image fails that problem's render**, recorded per
  problem, instead of producing a picture with a hole. So does an image that may
  still arrive (network error, 5xx, robots.txt refusal): the problem is retried on
  the next run rather than rendered with a stand-in that would never be replaced.
  Until it has an image, the bot shows the problem as text with a link to the source.
- Download failures are remembered so `render` doesn't hit the same URL again:
  permanent ones for a day, temporary ones for an hour. robots.txt refusals are not
  remembered, so adding a host to `ROBOTS_OVERRIDE_HOSTS` takes effect at once.

#### AoPS images

AoPS's formula and diagram PNGs live on `latex.artofproblemsolving.com`. That host
is plain S3 with no anti-bot check, but its robots.txt is `Disallow: /`, so the
images are only downloaded if you list it in `ROBOTS_OVERRIDE_HOSTS`. That is your
decision; this project's `.env` has it set. The first download from an overridden
host logs a warning naming it. Without it, AoPS problems aren't rendered (the bot
shows their text); CEMC is unaffected.

```bash
# in .env
ROBOTS_OVERRIDE_HOSTS=latex.artofproblemsolving.com
ASSET_MIN_INTERVAL=1.0                                # seconds between image downloads

python -m mathbot prefetch-assets --count             # how many images, how long
python -m mathbot prefetch-assets --limit 2000        # resumable batches; Ctrl+C is safe
python -m mathbot render                              # downloads anything still missing, then renders
python -m mathbot cleanup                             # removes previous-renderer images once replaced
python -m mathbot coverage
```

- 19,053 distinct images (counted 2026-11-17), each downloaded once, ever. At the
  default `ASSET_MIN_INTERVAL=1.0` that is about 5.3 hours. Lowering it (for example
  to `0.25`, about 1.3 hours) is faster but puts more load on AoPS's host; that's
  your call. `HTTP_MIN_INTERVAL` (page scraping) is not affected.
- `render` downloads whatever `prefetch-assets` hasn't fetched yet, so running
  `render` alone also works, but it can't be stopped and resumed in batches.
- Renders made before this change (formulas typeset from TeX) stay in use until
  `render` replaces them; `cleanup` then deletes them.
- The override never touches anti-bot challenge detection. Images hosted on the AoPS
  wiki itself (`artofproblemsolving.com/wiki/images/...`, behind a Cloudflare
  challenge) are never downloaded by the pipeline, and neither is one image on
  `cdn.artofproblemsolving.com`, whose robots.txt disallows it. Add those yourself
  (below).

#### Images you save yourself

For images the pipeline can't download (73 files, used by 69 problems, as of
2026-11-17), open them in your own browser and import them:

```bash
python -m mathbot import-assets --html images.html   # a page linking every image still needed
# open images.html, save each image into one folder, keeping its file name
python -m mathbot import-assets path/to/folder       # matched by file name, validated as images
python -m mathbot render
```

Imported images are checked by their bytes (a saved challenge page is rejected), are
never downloaded again and don't expire. `import-assets` with no arguments prints
the list.

Measured on 2026-11-17 before the AoPS images were downloaded (8,880 problems:
1,247 live CEMC + 7,633 imported AoPS): **7,745 with an image (87%)**. The rest:

- 880 with a diagram refused by robots.txt (879 on `latex.artofproblemsolving.com`,
  1 on `cdn.artofproblemsolving.com`);
- 180 CIMC/CSMC problems, new and not rendered yet at that point;
- 68 whose image is a wiki file behind the anti-bot challenge;
- 7 using LaTeX that MathJax can't typeset (`\multicolumn`, `\makebox`,
  `\underline` in text). With AoPS's own PNGs these formulas are no longer
  typeset, so they should render.

Expected once the images are downloaded: about 8,811 of 8,880 (99%), all but the 68
challenge-blocked wiki images and the one `cdn.artofproblemsolving.com` image, less
any image AoPS no longer serves. With those 69 problems' images imported by hand,
all 8,880.

Common LaTeX that MathJax lacks (`\emph`, `\hdots`, `\textdollar`, `tabular`,
layout-only commands, ...) is mapped to MathJax equivalents in
[mathbot/render/template.py](mathbot/render/template.py), for the fallback and CEMC.

- One Chromium instance with a fixed pool of pages (`RENDER_CONCURRENCY`). Each page
  waits for MathJax, fonts, every image and two animation frames before the
  screenshot, for at most `RENDER_TIMEOUT_MS`. Output is checked to be a complete PNG of plausible size and written
  atomically. Failures are retried (`RENDER_RETRIES`) and then recorded per problem.

**Storage.** `upload` is a separate step from rendering.

- `STORAGE_BACKEND=local` (default): the bot serves images straight from
  `data/renders`, so `upload` copies nothing; it only marks renders as published.
  Only `publish-db` snapshots go to `data/object_store`. Good for development and
  for a single machine that renders and runs the bot.
- `STORAGE_BACKEND=s3`: any S3-compatible store. For **Cloudflare R2**, create an
  API token with Object Read & Write, then set
  `S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com`, `S3_BUCKET`,
  `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, and `IMAGE_BASE_URL` to the bucket's
  public URL (r2.dev or a custom domain). Objects are uploaded as immutable.

The bot looks for an image in this order: the local render file, then
`IMAGE_BASE_URL/<object key>`.
Remote fetches use a 15 s timeout and check status, content type, size and PNG
signature. If there is no valid image, the bot shows the problem as text and says so.

## Running the bot

```bash
python -m mathbot bot        # or: python main.py
```

At startup the bot checks its configuration, creates and migrates the database,
starts the health server and then connects. Slash commands:

| Command | What it does |
|---|---|
| `/problem contest [year] [number]` | A specific problem. Contest autocompletes; years autocomplete from the database. Everything can also go in the contest box: `AMC 10A 2019 #25`, `2024 AIME I P5`, `amc10a-2019-p25`. |
| `/random [contest] [year] [year_from] [year_to] [number] [difficulty] [source]` | Random problem; `contest` may be a family such as `PCF` or a source such as `cemc`. |
| `/contests` | Every contest with years and counts, paginated. |
| `/contest contest` | Years available, format, aliases. |
| `/stats` | Totals by source and largest contests. |
| `/help [command]` | Help. |
| `/admin status`, `/admin sync` | Admin only: validation summary and last ingestion run; re-sync commands. |

Under every problem:

* **Show answer** (when an answer is stored): shown privately, in a spoiler.
* **Official solution** (CEMC) and **Source**: links to the original pages.
* **Previous / Next**: the neighbouring problems of the same contest and year.
* **Another random**: the same `/random` filters again (under `/problem`, another
  problem from that contest).

The buttons carry what they need in their Discord `custom_id`, so messages posted
before a restart keep working. Presses obey `ALLOWED_CHANNEL_IDS` / `ALLOWED_ROLE_IDS`.
Commands and button presses each have a short per-user cooldown
(`COMMAND_COOLDOWN_SECONDS`, admins exempt).

`/random` details: `difficulty` is the problem's place in its paper (first, middle
or last third; for six-problem two-day olympiads, its place within the day), since
contests order problems by difficulty. A problem that has an image is chosen
whenever one matches, so the text fallback is rare, and the last 50 problems shown
in a channel are skipped while others match.

Invalid contests, years and numbers get an ephemeral error that names what is
available. Any unexpected error is logged with a short reference code that is
also shown to the user. The bot never fetches from the sources while answering a
command; ingestion and rendering are CLI jobs (run them on a schedule, e.g. cron).

**Health checks.** `GET /` returns `Bot is alive!` (unchanged, for existing
keep-alive pingers). `GET /healthz` returns JSON and answers 503 until Discord is
connected and the database is reachable. The port is `PORT` (default 8080).

### Deploying

The usual split: a **build machine** (your PC, or the scheduled GitHub Action below)
with Chromium ingests, renders and publishes; the **bot host** (Render, Railway, a
VPS) only runs the bot. They share the S3/R2 bucket:

```bash
# build machine
python -m mathbot refresh && python -m mathbot render && python -m mathbot upload
python -m mathbot validate
python -m mathbot publish-db        # consistent snapshot -> db/problems-<time>-<hash>.db + db/latest.json
```

On the bot host, install `requirements.lock`, set the environment variables
(including `DATABASE_SNAPSHOT_URL=<IMAGE_BASE_URL>/db/latest.json`), and start it
with `python -m mathbot bot`. At startup it downloads the snapshot `latest.json`
points to (unless it already has it), checks its SHA-256 and SQLite integrity, and
swaps it in atomically. The previous file is kept as `problems.db.prev`. If the
download fails, it keeps using the database it has. `python -m mathbot pull-db`
does the same by hand. A single machine that does everything needs none of this.

The snapshot is the full database, provenance included: with all 8,880 problems
it is about 210 MB (measured 2026-11-17), half of it statement history and raw
source payloads. The bot
host downloads it only when `latest.json` changes.

**Scheduled refresh.** [.github/workflows/refresh.yml](.github/workflows/refresh.yml)
runs weekly (and on demand): pull-db, refresh, render, upload, validate,
publish-db, cleanup. It stays off until you set the repository variable
`MATHBOT_REFRESH_ENABLED=true` and the secrets listed in the file. Publish the first
snapshot from your own machine, since `pull-db` needs one to exist.

## Database schema

SQLite by default (WAL mode, foreign keys on). The schema is created by versioned
migrations in [mathbot/db/schema.py](mathbot/db/schema.py) and applied
automatically. Apart from the integer primary keys, the SQL is portable to PostgreSQL.

| Table | Purpose |
|---|---|
| `contest_series` | One row per contest (`amc10a`, `euclid`, ...): name, family, provider, the source's own spelling, enabled flag. |
| `contest_aliases` | Normalized alias → series, for case-insensitive lookup. |
| `contest_editions` | One sitting (`amc10a-2024`): label, canonical URL, source identifier, source hash, expected problem count, and whether it was discovered live or from fallback data. |
| `problems` | Current statement (`amc10a-2024-p05`): number, section, normalized HTML, plain text with TeX, content hash, version, answer and where the answer came from. |
| `problem_versions` | Every normalized statement each problem has had. |
| `source_records` | Raw source payload (full wiki HTML, CEMC fragment, community JSON item), its hash, URL, identifier and revision, first/last seen, and the run that saw it. |
| `assets`, `problem_assets` | Remote images: status, SHA-256, content type, size, local path, expiry. |
| `renders` | The render manifest: content hash, renderer version, status, local path, object key, dimensions, attempts, error, rendered/uploaded times. |
| `ingestion_runs`, `ingestion_errors`, `ingestion_checkpoints` | Run history and counters, per-problem failures, and finished editions for `--resume`. A run left `running` by a killed process is closed as `failed` when the next run starts (concurrent ingests against one database are not supported). |
| `solutions` | Official solutions: one per (problem, provider), with source URL, normalized HTML, text, content hash and version. |
| `problem_tags`, `problems.difficulty` | Reserved for tags and difficulty ratings; nothing fills them yet. |

IDs are deterministic (`<series>-<year>-p<NN>`). `(series, year)` and
`(edition, number)` are unique, and statements cannot be empty (a `CHECK`
constraint). Indexes cover lookups by year, number, content hash and render status,
and two covering indexes (migration 4) keep `/random` at about 15 ms on the full
database. Migration 3 adds a few unused columns to `source_records`: an experiment
that was reverted had already applied it to a real database, so it is kept so that
every database at version 3 has the same schema. Migrations take the write lock
first, so the bot and a CLI command starting at once cannot apply one twice.
A legacy `math_problems` table in an old database is left untouched. Rebuild the new
tables by running `refresh` (the CEMC portion takes about 2 minutes; rendering it
takes about 1.5 minutes).

## Source access and attribution

Problems remain the property of their publishers: the Mathematical Association of
America (AMC, AIME, USAMO, USAJMO), the IMO, the Canadian Mathematical Society
(CMO), the CEMC at the University of Waterloo, and the Art of Problem Solving
community that transcribes them. Every problem keeps its source URL, and the bot
links to it. Use this for study groups, not for republishing.

**What was verified on 2026-11-17**, with plain HTTP and an honest User-Agent:

- **CEMC.** robots.txt allows the listing and the document pages. The listing at
  `/resources/past-contests` filters on `contest_category` (14 = PCF, 23 = CIMC/CSMC,
  24 = Euclid, 25 = FGH, 13 = Gauss) and paginates with `page=N` (10 rows per page; a page past
  the end is HTTP 200 with no rows). The listing's year filter only offers
  2016-2026, and every listed contest has both HTML and PDF. Earlier years are not
  listed in any format, so there is no PDF backlog to extract. Constructing guessed
  URLs for unlisted years was ruled out: the URL directory doesn't follow the year
  (see below), so guesses would be unreliable. The directory in a document URL is not
  the contest year (2019 Pascal is served from `/documents/2026/`), so URLs are
  always taken from the listing. Pages use MathJax 3 TeX and inline `data:` images.
  The pre-2.0 URL manifest only fills gaps (e.g. 2020 Pascal is missing from the
  listing), and those editions are marked `fallback`. Gauss is registered but
  disabled.
- **AoPS.** robots.txt allows `/wiki/` and `/community/c…` but disallows `/m/` and
  `/*ajax`, which covers `/m/community/ajax.php`, the endpoint the old scraper used
  with a hard-coded guest session id. Every wiki and community page answered with a
  Cloudflare managed challenge (HTTP 403, `cf-mitigated: challenge`). **This project
  does not bypass that**: no browser impersonation, stealth browsers or replayed
  clearance cookies (the old `curl_cffi` / `patchright` dependencies are gone).
  The client raises `SourceBlockedError`, the run reports the provider as
  `BLOCKED`, and the run's other contests continue.
- **AoPS images.** `latex.artofproblemsolving.com`, which hosts every formula image
  and Asymptote diagram, has a robots.txt of `Disallow: /`. It is plain S3 with no
  challenge. Its images are downloaded only because this project's owner listed the
  host in `ROBOTS_OVERRIDE_HOSTS` (see [AoPS images](#aops-images)); without that,
  they are never fetched. The override changes nothing else: challenges are still
  detected and never bypassed.

**What is not verified.** The AoPS adapters follow the documented MediaWiki API
(`action=parse` for hub links, edition links and page HTML, with `redirects=1` for
shared AMC 10/12 problems; `action=query&prop=revisions` in batches of 50 for
incremental runs) and the category fields the old scraper consumed. Their fixtures
in [tests/fixtures](tests/fixtures/README.md) are hand-written and labelled as such.
To use these sources, get permission or an allow-listed client from AoPS, then
re-capture fixtures and adjust selectors if needed. A page you saved yourself can be
fed in with `python -m mathbot cache-put <url> <file>`. CJMO and CMOQR stay disabled
until their category data has been checked against the live site.

## Development

```bash
pip install -r requirements-dev.lock
pytest                                       # offline; uses fixtures only
ruff check . && ruff format --check .
python -m compileall -q mathbot tests main.py
vulture mathbot tests --min-confidence 80    # dead code
```

Tests marked `browser` use a real Chromium and the pinned render assets (from
`RENDER_ASSETS_DIR`, default `data/render_assets`). They check that TeX renders
offline, that the AoPS formula fallback is typeset, and that remote requests and
TeX errors fail a render. They are skipped when Chromium or the assets are not
installed. CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs a syntax
check, ruff, vulture and the offline tests on Python 3.10 and 3.13, plus a separate
job that installs Chromium and the assets and runs the browser tests.

Layout:

```
mathbot/
  config.py          typed settings from the environment
  logging_setup.py   log format with key=value context
  contests.py        contest registry, aliases, deterministic IDs
  models.py          data passed between sources, ingestion and the database
  http.py            polite client: robots.txt, rate limit, retries, cache
  htmlnorm.py        sanitizing + hashing of statements, remote image URLs
  sources/           cemc.py, aops_wiki.py, aops_community.py (+ seed manifests)
  ingest.py          discover -> fetch -> normalize -> upsert -> report
  db/                schema migrations, connection, repository
  render/            template, pinned assets (vendor.py), AoPS formula fallback
                     (aops_tex.py), asset cache, Playwright renderer, storage
  legacy.py          import of the pre-2.0 math_problems table
  snapshot.py        publish-db / pull-db
  validate.py        integrity checks
  coverage.py        what is missing (python -m mathbot coverage)
  cli/               python -m mathbot ...: ingest.py (getting problems), images.py
                     (downloading, rendering, publishing images), database.py
                     (validate, coverage, snapshots), common.py (shared helpers)
  bot/               app startup, health, policy, images, ui (embeds, buttons),
                     query (parsing "AMC 10A 2019 #25", /random filters in button ids), cogs/
main.py              python main.py runs the bot (same as python -m mathbot bot)
```
