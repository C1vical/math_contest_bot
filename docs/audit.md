# Audit (overhaul branch)

Status: all phases complete (see docs/overhaul-report.md). The checklist at the bottom is done;
the findings below are kept as the record of what was found.

## Baseline (checkpoint commit `0d414a9`)

| Check | Result |
|---|---|
| `compileall` | OK |
| `ruff check` / `ruff format --check` | OK / 68 files formatted |
| `vulture --min-confidence 80` | OK |
| `pytest -m "not browser"` | 271 passed (slowest: health server 2.6 s) |
| `pytest -m browser` | 2 passed (Chromium + assets installed) |

Green tests notwithstanding, the two most important bugs were invisible to them:
**every command crashes** (see B1, B2).

## Real data examined (read-only copy of `data/problems.db`)

* 8,880 problems: 7,310 AoPS Wiki (legacy import), 323 AoPS Community (legacy), 1,247 CEMC (live).
* Renders: 8,811 ok, 69 failed (all: AoPS wiki images behind the Cloudflare challenge; fixable with `import-assets`).
* No edition has a count mismatch or numbering gap. Duplicate content hashes are the AMC 10/12 shared problems (legitimate).
* DB is 220 MB: raw source payloads 70 MB, version history 42 MB, statements 45 MB, solutions 17 MB.
* **Schema version 3** is recorded, from a reverted experiment (see B1).

## Inventory and findings

Tags: BUG, RISK, SMELL, UX, PERF, TEST-GAP. Severity high/med/low.

### config.py, logging_setup.py, models.py
Typed env settings, `key=value` log formatter, dataclasses passed between layers.
* SMELL low: soft vs hard ingest failures are encoded implicitly (`stage == "fetch" and number is None and not invalid`) instead of a flag on `ProblemFailure` (see ingest.py).

### contests.py
Registry, aliases, deterministic IDs, eras. Clean. No issues beyond UX items in the bot.

### http.py (PoliteClient)
* BUG med `http.py:385` robots.txt is checked against the URL *without* `params`, so a rule on the query string (`Disallow: /*?action=`) is never applied to requests made with `params=` (the CEMC listing and the whole MediaWiki API path).
* BUG low `http.py:77` `Response.text` raises `LookupError` for an unknown `charset=` label instead of falling back to UTF-8.
* BUG med `http.py:357` only `ConnectionError`/`Timeout` are caught: `ChunkedEncodingError`, `ContentDecodingError`, `InvalidURL` etc. escape as raw `requests` exceptions, breaking the "failures raise typed exceptions" contract and the ingest error handling (which only catches `HttpError`).
* PERF low: a robots.txt fetch that fails with a transport error is retried (with full backoff) on every request to that host.

### htmlnorm.py
* RISK low `htmlnorm.py:86` `javascript:` detection runs before removing the tabs/newlines browsers strip from URLs (`java\tscript:`). Anchors are unwrapped anyway, so only non-`<a>` URL attributes are affected.

### sources/__init__.py
* **BUG high (B2)** `sources/__init__.py:20` `build_adapters` passes `fallback=` to the AoPS adapters, which do not accept it: `python -m mathbot ingest`, `refresh` and `discover` all die with `TypeError`. Leftover of the reverted curl_cffi experiment (session 585ce749, not part of this project's design and declined per the AoPS access constraints). Untested path: no test calls `build_adapters`.

### sources/cemc.py
* **BUG high (P1)** `cemc.py:80` 2024-2026 solution pages write `<span class="smallcaps">Answer</span>: (C)`; `get_text(" ")` yields `Answer : (C)` and the regex requires `Answer:`. Result: Pascal/Cayley/Fermat 2024-2026 (225 problems) have solutions but **no answers** (no "Show answer" button). Reproduced from the HTTP cache.
* BUG med `cemc.py:472` answers are kept even when the solutions page's item count does not match the questions (solutions are dropped in that case, answers are not), so a mis-parsed page could attach answers to the wrong questions.
* RISK med `cemc.py:403` incremental runs skip an edition whose page hash is unchanged, so a *parser* fix (like P1) is never applied without `--full`. Should include a parser version in the edition hash.
* SMELL low: solution-count mismatch is recorded with stage `fetch` (it is a validation note).

### sources/aops_wiki.py
* BUG med (P2) `extract_statement` stops at headings but not at the wiki's broken-heading paragraphs (`<p>==Video Solution</p>`, `<p>==Sol ~user</p>`) that render as text.
* RISK low `_revisions` indexes `entry["to"]`; a malformed API entry raises `KeyError`, which (see ingest) aborts the whole run.

### sources/aops_community.py
* Low: `int(edition.source_identifier or 0)` silently asks for category 0 when the identifier is missing.

### ingest.py
* BUG med `ingest.py:284` any non-`HttpError` exception from an adapter (parser `KeyError`, `ValueError`, `AttributeError` on unexpected markup) propagates out of `future.result()` and aborts the entire run as `failed`, losing the other editions. One bad page should fail one edition.
* SMELL low: soft-failure rule (see models.py).

### db/connection.py, db/schema.py
* **BUG high (B1)** The real database is at schema version 3 (columns added to `source_records` by the reverted experiment), the code knows only 2, so `open_database` raises `RuntimeError` and **every command and the bot fail to start** on this machine. Fix: record migration 3 with exactly the SQL that was applied (the columns are unused but harmless), so all databases at v3 agree.
* RISK low `connection.py:52` migrations use a deferred `BEGIN`; two processes migrating at once (bot + CLI) can both try to apply the same version. Use `BEGIN IMMEDIATE` and re-read the version inside the lock.
* UX low: CLI `main` does not catch that `RuntimeError`, so the user sees a traceback.

### db/repo.py (680 lines)
Cohesive but large. Queries are parameterized (the only f-strings build `IN (?,?,...)` placeholders).
* UX: `random_problem` returns unrendered problems as often as rendered ones (text fallback); no exclusion of recently shown problems; no year range / difficulty.
* SMELL low: name `iter_problems` returns a list.

### legacy.py
* **BUG high (P3)** Legacy AoPS statements contain solution text: 30 statements include a heading (`Solution 1`, `Modified Problem in Certain China Testpapers`, `Sidenote`, ...) followed by the solution itself, and others end in `<p>Solution</p>` or `==Video Solution`. These are **rendered into the problem images** (spoilers), e.g. `imo-1976-p04`, `usamo-2015-p05`, `amc10b-2024-p01`. 37 problems affected.

### snapshot.py
* RISK med `snapshot.py:155` the snapshot (220 MB today, cap 2 GB) is downloaded entirely into memory and then copied (`bytes(body)`), ~2x the file size in RAM on the bot host, which is often the smallest machine. Stream to a temp file while hashing.

### validate.py, coverage.py
* TEST-GAP / feature: no check for solution text leaking into statements (P3), unbalanced TeX delimiters, or multiple-choice CEMC problems without an answer (P1).

### render/template.py, render/aops_tex.py, render/vendor.py
* UX med (R1): AoPS formula PNGs are sized for 16 px text but the page uses 20 px, so inline formulas look ~20% too small next to the text (visible in `amc10a-2020-p25`: the "7"). The PNGs are stored at ~2.5x, so they can be scaled 1.25x without blurring.
* UX low (R2): a trailing paragraph margin leaves extra blank space under every image.
* RISK med (R3): content wider than the page (wide tables, long display equations) is clipped by the body screenshot rather than scaled to fit.
* PERF low `vendor.py:130` `read_asset` re-reads and re-hashes the 2 MB MathJax bundle for every rendered page.

### render/pipeline.py
* RISK med `pipeline.py:217` a page is only replaced after a timeout. After a crash or "Target closed" error the broken page goes back into the pool and every later render on it fails.
* Low: an unexpected exception building one job aborts the whole render run (`gather`).

### render/assets.py, render/storage.py
* BUG low `assets.py:53` any `RIFF` file (WAV, AVI) is accepted as `image/webp`; should check `WEBP` at offset 8.
* UX low: `cleanup --remote` lets a `StorageError` escape as a traceback.

### cli/
* UX low `cli/common.py:57` `--year 2019-` or `--year abc` raises a bare `ValueError` traceback; should be an argparse error.
* UX low `cli/__init__.py:146` only `ConfigError`/`UnknownContestError` get a clean message.

### bot/
* UX `cogs/problems.py`: no navigation (next / previous problem), no "another random with the same filters", combined input like `amc10a 2019 25` is not understood, `/random` has no year range or difficulty filter and can repeat the same problem.
* UX low `app.py:97` cooldown message shows "try again in 0s" for sub-second waits; `TransformerError` echoes unbounded user input.
* UX low `/help` does not document `/admin`.
* Correct already: defers before slow work, autocomplete capped at 25 / 100 chars, embed limits respected, global error handler, persistent answer button, DB access off the event loop.

### Tests
* TEST-GAP: `build_adapters` (B2), opening a v3 database (B1), new-format CEMC answers (P1), legacy cleaning (P3), adapter exceptions in ingest, robots + params, page replacement.

## Fix checklist

- [x] B1 migration 3 recorded; DB opens again
- [x] B2 build_adapters
- [x] P1 CEMC answer regex + parser version in edition hash
- [x] P3/P2 statement trimming (legacy + live wiki)
- [x] http: robots with params, typed transport errors, charset fallback
- [x] ingest: adapter exceptions fail one edition; explicit warning flag
- [x] cemc: answers guarded by solution count
- [x] migrate BEGIN IMMEDIATE; CLI error handling; int_set errors
- [x] snapshot streaming download
- [x] assets RIFF/WEBP; htmlnorm URL scheme check
- [x] renderer: page replacement on any error; vendored bytes cached
- [x] render template v7: formula scale, trailing space, fit-to-width
- [x] bot: navigation, re-roll, combined input, filters, no-repeat, prefer rendered
- [x] validate: leaked solutions, TeX delimiters, missing MC answers
- [x] docs

Deferred (see the report): caching a failed robots.txt fetch per host; splitting
`db/repo.py`; renaming `iter_problems`.
