# Math Contest Bot

A Discord bot that posts competition maths problems as images, plus the tools that
collect, render and publish those problems.

| Source | Contests | Problems | Live updates |
|---|---|---|---|
| [CEMC](https://cemc.uwaterloo.ca/resources/past-contests) | Euclid, Fryer, Galois, Hypatia, Pascal, Cayley, Fermat, CIMC, CSMC | 1,247 (with 1,247 official solutions and 825 answers) | Yes |
| [AoPS Wiki](https://artofproblemsolving.com/wiki/) | AMC 8/10/12, AJHSME, AHSME, AIME, USAJMO, USAMO, IMO | 7,310 | No, AoPS blocks automated access |
| [AoPS Community](https://artofproblemsolving.com/community/c3277) | CMO (CJMO and CMOQR optional) | 323 | No, AoPS blocks automated access |

Counts are from 2026-11-22, when all 8,880 problems had an image. The AoPS problems
come from the [pre-2.0 database](#importing-the-pre-20-database).

## Setup

Requires Python 3.10 or newer.

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.lock
python -m playwright install chromium      # only needed for rendering
python -m mathbot install-render-assets    # only needed for rendering
cp .env.example .env                       # then set DISCORD_TOKEN
python -m mathbot migrate                  # creates data/problems.db
```

On Linux servers, rendering may also need `python -m playwright install-deps chromium`.

Every setting is listed in [.env.example](.env.example). The ones you're most likely
to change:

| Variable | Purpose |
|---|---|
| `DISCORD_TOKEN` | Bot token |
| `GUILD_ID` | Sync slash commands to one server instantly instead of globally |
| `ALLOWED_CHANNEL_IDS`, `ALLOWED_ROLE_IDS` | Where and by whom commands work (empty: no restriction) |
| `ADMIN_ROLE_IDS`, `ADMIN_USER_IDS` | Who may use `/admin` (both empty: members with Manage Server) |
| `ENABLE_CONTESTS`, `DISABLE_CONTESTS` | Turn optional contests on (`CJMO,CMOQR,GAUSS7`) or default ones off |
| `HTTP_USER_AGENT` | Identifies your crawler; include contact details |
| `STORAGE_BACKEND`, `S3_*`, `IMAGE_BASE_URL` | Where images and database snapshots are published |

## Running the bot

```bash
python -m mathbot bot        # or: python main.py
```

| Command | What it does |
|---|---|
| `/problem` | Show one problem. Accepts shorthand such as `AMC 10A 2019 #25` or `amc10a-2019-p25`. |
| `/random` | Show a random problem, filtered by contest, year, number, difficulty or source |
| `/contests`, `/contest` | List all contests, or show one in detail |
| `/stats` | Problem totals |
| `/help` | Help |
| `/admin status`, `/admin sync` | Admins: database health; re-sync commands |

Buttons under each problem give the previous and next problem, the answer (if stored)
and links to the solution (CEMC) and source. Previous, Next and Another random work
only for the person who ran the command.

Difficulty is estimated from a problem's position in its paper. If a problem has no
image, the bot shows its text.

When the bot joins a server, it posts one welcome message in the main channel: the one
Discord uses for join messages, or else the first channel where everyone can chat. For
this, invite it with the `bot` scope and the View Channels and Send Messages permissions.

For uptime monitoring, `GET /` always answers and `GET /healthz` returns 503 until
Discord and the database are ready. The port is `PORT` (default 8080).

## Updating problems

```bash
python -m mathbot refresh       # fetch new and changed problems
python -m mathbot render        # make images for them
python -m mathbot upload        # publish the images
python -m mathbot validate      # check the result
python -m mathbot cleanup       # delete outdated local images
```

Only new or changed content is processed, so these are safe to repeat. Earlier
versions of a statement are kept. Failures are reported and make the command exit
with an error; an empty or unreadable problem is never stored.

Other useful commands (`python -m mathbot COMMAND --help` lists the options):

```bash
python -m mathbot contests                                 # contests and which are enabled
python -m mathbot ingest PCF                               # one family, contest or source
python -m mathbot ingest "aime i" --year 2024 --number 7   # one problem
python -m mathbot refresh --dry-run                        # fetch and report, write nothing
python -m mathbot coverage --csv gaps.csv                  # what is missing
python -m mathbot cache-put URL FILE                       # use a page you saved yourself
```

### Importing the pre-2.0 database

```bash
python -m mathbot import-legacy path/to/old.db --dry-run   # report only
python -m mathbot import-legacy path/to/old.db
```

The old file is opened read-only, and problems from a live source are never
overwritten. Old answers are ignored because they were all the placeholder `0`. A few
old statements may be cut short: `validate` warns about AMC problems with no visible
answer choices, though some of those have their choices in an image.

### AoPS images

AoPS's formulas and diagrams are images on `latex.artofproblemsolving.com`, whose
robots.txt disallows all crawling. They are downloaded only if you choose to list that
host in `.env`:

```bash
ROBOTS_OVERRIDE_HOSTS=latex.artofproblemsolving.com
```

Without it, AoPS problems appear as text. There are about 19,000 images, roughly
5 hours at the default `ASSET_MIN_INTERVAL` of one per second.
`python -m mathbot prefetch-assets` downloads them in resumable batches. They are
cached for 30 days (`ASSET_TTL`) and fetched again only if they are needed after that,
for example to re-render a problem.

### Images you save yourself

Images on the AoPS wiki itself sit behind an anti-bot challenge, so the tools can't
download them. Save them in your own browser and import them:

```bash
python -m mathbot import-assets --html images.html   # page of links to images still needed
python -m mathbot import-assets path/to/folder       # keep each file's original name
python -m mathbot render
```

Imported images don't expire. Keep the originals; the ones saved so far are in
`data/blocked_images`, which is not in git.

## Deploying

**One machine:** keep `STORAGE_BACKEND=local`. The bot serves images straight from
`data/renders`.

**Separate bot host:** set `STORAGE_BACKEND=s3` with any S3-compatible bucket, such as
Cloudflare R2.

1. On the machine that renders, run the [update commands](#updating-problems) and then
   `python -m mathbot publish-db`. Only the newest snapshot is kept, in the bucket and
   in `data/snapshots`.
2. On the bot host, install `requirements.lock`, set `DISCORD_TOKEN`, `IMAGE_BASE_URL`
   and `DATABASE_SNAPSHOT_URL=<IMAGE_BASE_URL>/db/latest.json`, and start the bot. It
   downloads and verifies the newest snapshot at startup. Chromium isn't needed there.

[.github/workflows/refresh.yml](.github/workflows/refresh.yml) can do step 1 weekly. It
stays off until you set the repository variable `MATHBOT_REFRESH_ENABLED=true` and the
secrets listed in that file. Publish the first snapshot yourself.

## Source access and attribution

Problems remain the property of their publishers: the Mathematical Association of
America (AMC, AIME, USAMO, USAJMO), the IMO, the Canadian Mathematical Society (CMO)
and the CEMC at the University of Waterloo, with transcriptions by the Art of Problem
Solving community. Every problem links to its source. Use this for study groups, not
for republishing.

All requests go through one client that identifies itself honestly, obeys robots.txt
(except hosts you override) and limits its rate. AoPS's wiki and community pages answer
with a Cloudflare challenge. **This project does not bypass it**: the source is reported
as `BLOCKED` and the other contests continue. The AoPS adapters have therefore only been
tested on [hand-written fixtures](tests/fixtures/README.md), not the live site.

## Development

```bash
pip install -r requirements-dev.lock
pytest                                       # offline
ruff check . && ruff format --check .
vulture mathbot tests --min-confidence 80    # dead code
```

Tests marked `browser` need Chromium and the render assets, and are skipped without
them. After changing `requirements*.txt`, regenerate the lockfiles with the command at
the top of each one.

Never edit a database migration that has been applied; add a new one in
[mathbot/db/schema.py](mathbot/db/schema.py), which also describes the tables.

```
mathbot/
  contests.py    contest registry and IDs (amc10a-2024-p05)
  sources/       CEMC, AoPS Wiki and AoPS Community adapters
  ingest.py      fetch, normalize and store problems
  http.py        rate-limited HTTP client
  db/            schema, migrations and queries
  render/        HTML to PNG, image cache and storage
  snapshot.py    publish-db and pull-db
  cli/           python -m mathbot commands
  bot/           Discord bot
```
