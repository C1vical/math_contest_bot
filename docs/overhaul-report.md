# Overhaul report (`overhaul` branch)

Branch `overhaul` starts from checkpoint `0d414a9`, the uncommitted work from
`import-saved-images` committed as it was. Nothing was pushed, and `master` was not
touched. The real database `data/problems.db` was **not modified**. Every check against
real data ran on a read-only copy in a scratch directory.

## 1. Toolchain: baseline vs final

| Check | Baseline (`0d414a9`) | Final (`HEAD`) |
|---|---|---|
| `compileall` | OK | OK |
| `ruff check` / `ruff format --check` | OK / 68 files | OK / 74 files |
| `vulture --min-confidence 80` | OK | OK |
| `pytest -m "not browser"` | 271 passed | **348 passed** |
| `pytest -m browser` | 2 passed | **3 passed** |

The baseline was green but hid the two worst bugs (B1, B2). **The bot and every
command crashed on this machine's database.**

## 2. Bugs fixed

| # | Where | Symptom | Root cause | Fix | Test |
|---|---|---|---|---|---|
| B1 | `db/schema.py`, `db/connection.py` | Bot and **every** CLI command failed: "database schema version 3 is newer than this code understands (2)" | A reverted experiment (session 585ce749, curl_cffi AoPS fallback, which is out of bounds per the access rules) had already migrated the real DB to v3 | Migration 3 recorded with exactly the SQL that was applied (unused columns), so every v3 database has the same schema. Migrations now take the write lock first and re-read the version. Too-new DBs give a clean CLI error | `test_db.py::test_database_at_version_3_from_the_reverted_experiment_...`, `test_concurrent_migration_...`, `test_cli.py::test_a_database_from_newer_code_...` |
| B2 | `sources/__init__.py:20` | `ingest`, `refresh` and `discover` crashed with `TypeError` | Leftover `fallback=` argument from the same reverted experiment | Removed | `test_ingest.py::test_build_adapters_constructs_every_provider` |
| P1 | `sources/cemc.py:80` | Pascal/Cayley/Fermat 2024-2026 (225 problems) had solutions but **no answers**, so there was no "Show answer" button | Since 2024 the label is `<span class="smallcaps">Answer</span>:` (text `Answer : (C)`), and Part C answers are TeX (`\(11\)`) | Tolerant regex. Checked against all 32 cached PCF solution pages: every one now gives 25/25 (was 0 or 20) | `test_cemc.py::test_answer_key_in_the_2024_smallcaps_format`, `test_numeric_part_c_answers_written_as_tex` |
| P1b | `sources/cemc.py:403` | A parser fix was never applied by an incremental ingest | Edition skipped when its page hash was unchanged | `PARSER_VERSION` is part of the edition fingerprint | `test_a_parser_change_reparses_an_unchanged_page` |
| P1c | `sources/cemc.py:472` | Answers could be attached to the wrong questions | Answers were kept when the solutions page's numbering did not match (solutions were not) | Both dropped together | `test_answers_are_dropped_with_the_solutions_when_...` |
| P3 | `legacy.py`, `sources/aops_wiki.py` | **40 AoPS problem images contained solution text**, about 25 of them the full solution (e.g. `imo-1976-p04` was 5,302 px tall, `usamo-2015-p05` 2,798 px) | The pre-2.0 scraper ran past the statement (`<h3>Solution 1</h3>...`, `<p>Solution</p>`, `==Video Solution`, "Modified Problem in Certain China Testpapers") | `trim_statement()` cuts at the first heading, solution-heading paragraph or unrendered wikitext heading, plus empty spacer paragraphs. Used for live wiki pages and legacy imports. Checked against all 7,310 stored AoPS statements: all 40 changes are genuine | `test_aops_wiki.py::test_solution_text_after_the_statement_is_trimmed` (6 real shapes), `test_legacy.py::test_legacy_aops_statements_lose_leaked_solutions` |
| H1 | `http.py:385` | robots.txt rules on query strings never applied to MediaWiki API / CEMC listing requests | Robots matched the URL without its `params=` | Matched against the full query | `test_http.py::test_robots_rules_apply_to_query_parameters_...` |
| H2 | `http.py:357` | `ChunkedEncodingError` etc. escaped untyped and aborted ingests | Only `ConnectionError`/`Timeout` were caught | Retry network-level errors; any other `requests` error becomes `TransportError` | `test_every_requests_failure_becomes_a_typed_error` |
| H3 | `http.py:77` | `LookupError` on `charset=utf8mb4` | Unknown codec label | Falls back to UTF-8 | `test_unknown_charset_label_falls_back_to_utf8` |
| I1 | `ingest.py:284` | One unexpected parser exception aborted the whole run | Only `HttpError` was caught per edition | Any exception fails that edition as `parse` | `test_unexpected_parser_exception_fails_one_edition_not_the_run` |
| S1 | `snapshot.py:155` | Bot host held ~2x the snapshot (220 MB today, cap 2 GB) in RAM | Whole download read into memory, then copied | Streamed to the temp file while hashing | `test_snapshot.py::test_http_snapshot_is_streamed_to_disk_and_verified`, `test_oversized_snapshot_...` |
| R1 | `render/pipeline.py:217` | After a page crash every later render on it failed | Broken page went back to the pool (only timeouts replaced it) | Any error replaces the page; an unexpected error building one job fails only that problem | `test_render.py::test_a_crashed_page_is_replaced_...`, `test_an_unexpected_error_building_one_job_...` |
| X1 | `htmlnorm.py:86` | `java<TAB>script:` URLs survived sanitizing | Scheme checked before removing characters browsers ignore | Control/space characters removed first; `xlink:href` checked | `test_htmlnorm.py::test_script_urls_hidden_by_whitespace_or_case_are_removed` |
| X2 | `render/assets.py:53` | WAV/AVI accepted as `image/webp` | Only `RIFF` was checked | Requires `WEBP` at offset 8 | `test_render.py::test_riff_files_are_only_images_when_they_are_webp` |
| C1 | `cli/common.py:57` | `--year 2019-` / `--year abc` gave a traceback | Parsed after argparse | argparse `type=` validator, usage error | `test_cli.py::test_malformed_ranges_are_usage_errors_not_tracebacks` |
| C2 | `cli/images.py` | `cleanup --remote` with an unreachable bucket gave a traceback | `StorageError` not caught | Reported, exit 1, resumable | `test_cleanup_reports_a_storage_failure_instead_of_crashing` |
| U1 | `bot/app.py:97` | "try again in 0s"; unbounded echo of user input | Formatting | `ui.seconds()`, escaped and truncated | (exercised by the cooldown tests) |
| V1 | `validate.py` (new check) | First version flagged `\\[2mm]` TeX line breaks as unbalanced delimiters (4 false positives on real data) | Naive count | Delimiters preceded by a backslash are ignored. Real data now shows 0 | `test_validate.py::test_unbalanced_tex_delimiters_are_reported` |

## 3. Refactors

Kept small and only where they removed an implicit rule or real duplication:

* **`ProblemFailure.warning`** replaces the implicit soft-failure rule
  (`stage == "fetch" and number is None and not invalid`). Adapters now say when a
  failure is only a note (no answer key, partial community category, solutions
  page mismatch), and stages are accurate again (a mismatch is `validate`).
* **`render/vendor.AssetReader`** replaces `read_asset()`: verified bytes are
  cached per renderer.
* **`bot/query.py`** (new, pure): parsing typed problem references and encoding
  `/random` filters into button ids. Kept out of the cog so it is testable.
* **`policy.interaction_context`** is now public, so the button handler applies
  exactly the same channel/role rules as the commands.
* `repo._placeholders()` replaces three hand-built `IN (?,?,...)` strings.
* **Not done, on purpose:** splitting `db/repo.py` (now ~750 lines). It is one
  cohesive set of queries with a single caller pattern; mixins or several
  repositories would add indirection without simplifying any call site.
  `iter_problems` (returns a list) was not renamed, to avoid churn across callers.

## 4. Bot commands

| Before | After |
|---|---|
| `/problem contest:AMC10A year:2019 number:25` only | Same, **or** `/problem contest:AMC 10A 2019 #25`, `2019 AIME I P5`, `amc10a-2019-p25`, `Euclid 2023 q6`. `AMC 8 2019 5` is AMC 8 problem 5, and `AIME 1 2019` stays AIME I. A missing year/number is explained, with the years available. |
| Buttons: Source, Official solution, Show answer | Plus **Previous / Next** (same contest and year, disabled at the ends) and **Another random** (repeats the `/random` filters; under `/problem`, that contest). All persistent across restarts, and subject to channel/role rules and a per-user cooldown. |
| `/random contest year number source` | Adds `year_from`, `year_to`, `difficulty` (Easy/Medium/Hard = third of the paper; place within the day for six-problem two-day olympiads). Skips the last 50 problems shown in the channel. **Prefers problems that have an image.** |
| `/random` took ~195 ms on the real database | ~15 ms (sorting ids only, plus two covering indexes, migration 4) |
| `/help` without `/admin` | Documents the input forms, filters, buttons (`/help command:buttons`) and `/admin` |

All command names and existing option names are unchanged. On `/problem`, `year`
and `number` became optional: still accepted exactly as before, now with the
one-box alternative. A test checks every command against Discord's description,
option and choice limits.

## 5. Parsing and rendering

* **CEMC**: answers for 2024+ pages (P1). Parser changes reach the database through
  normal incremental runs (P1b).
* **AoPS**: statements end at the first heading or solution marker (P3), both for
  the live wiki parser and for legacy imports.
* **validate** (and `/admin status`) now warns about leaked solutions, unbalanced
  `\( \)` / `\[ \]`, and multiple-choice editions without answers. On the database
  copy *before* these fixes it lists the 40 leaked statements and 10 PCF editions
  without answers. After the re-import it lists no leaked statements.
* **Template 7** (`TEMPLATE_VERSION` bumped, so `render` redraws every image):
  * AoPS formula PNGs at 1.25x to match the 20 px text: `amc10a-2020-p25`, `ahsme-1962-p23`.
  * No blank band under the last line (every sample is 6-60 px shorter).
  * Blocks wider than the page are shrunk to fit instead of cut off. This is a
    safety net: none of 158 sampled real problems needed it (150 random plus the 8
    widest). A browser test proves it on a 1,660 px block.
  * Samples: [docs/render-samples](render-samples). `*-before.png` is the current
    v6 image, `*-after.png` is v7: amc10a-2020-p25, ahsme-1962-p23, amc8-2026-p03,
    pascal-2019-p05, aime1-2024-p05, euclid-2017-p02, euclid-2025-p07, plus
    imo-1976-p04 and usamo-2015-p05 (data fix P3: the solutions are gone).
* Render speed on the same 150 problems: 6.9/s (v7) vs 5.6/s (v6). That is within
  run-to-run noise on this machine, so I do not claim a speed-up beyond removing
  the repeated MathJax hashing.

## 6. What you need to run (on your real data)

Nothing has been written to `data/problems.db`. The new code opens it as it is (v3,
then migrates to v4 by adding two indexes). To get the data fixes:

```bash
python -m mathbot import-legacy C:/Users/jonat/PycharmProjects/math_contest_bot/data/math_problems.db
#   -> on the copy: "0 inserted, 40 updated, 7270 unchanged"
python -m mathbot ingest PCF --year 2024-2026     # answers for the 225 problems (re-parses via PARSER_VERSION;
                                                  # live CEMC requests, rate-limited and robots-respecting)
python -m mathbot render                          # template 7: redraws all ~8,800 images (v6 stays in use meanwhile)
python -m mathbot validate                        # leaked-solution / missing-answer warnings should be gone
python -m mathbot cleanup                         # afterwards: removes the v6 images
```

The v7 render covers every problem. At the 3-7 renders/s measured here that is
roughly 20-45 minutes. AoPS images still come from the local asset cache, so no
new downloads are needed unless the cache expired (`ASSET_TTL`, 30 days).

## 7. Deferred, `TODO(verify)` and risks

* **Pascal 2020** (corrected after review): it had no solutions or answers because
  CEMC's live listing omits that edition, so it came from the seed manifest, which
  holds only contest URLs. Fixed in `9012832`: such editions read the solutions page
  beside the contest page (`.../2020/2020PascalSolution.html`). After re-ingesting,
  it has 25 solutions and 25 answers.
* **Not done (low value):** a robots.txt fetch that fails with a network error is
  retried on every request to that host instead of being cached for the run.
* **Proposals not built:** `/search` by keyword, a daily problem per channel,
  answer checking with a modal and per-user score. Each needs new persistent
  state, which deserves a design decision first.
* **Risk:** the 69 AoPS problems whose wiki-hosted images sit behind the Cloudflare
  challenge still render as text. `import-assets` remains the way to add them. No
  workaround was attempted, per the access constraints.
* **Risk, low:** template 7 changes every image. The samples were reviewed by eye.
  A full render plus a skim of `coverage` is the final check.
* No `TODO(verify)` notes were needed: no contest facts were invented.

## 8. Commits on `overhaul` (oldest first)

```
0d414a9 Checkpoint: work in progress from import-saved-images
5bb6610 docs: Phase 1 audit of every module
a00a119 fix(db): open databases at schema v3; make migrations safe to run concurrently
3e02aee fix(ingest): an adapter exception fails one edition, not the whole run
d6ad7f2 fix(cli): malformed --year/--number and too-new databases are clean errors
62ceab4 fix(cemc): parse 2024+ answers; parser changes re-parse unchanged pages
89da38a fix(aops): cut statements at the first heading or solution marker
9e7b8d3 fix(http): robots.txt sees query params; every requests error is typed
59a5f62 fix(security): script URLs hidden by whitespace; RIFF is only WebP with WEBP
8a5741c fix(snapshot): stream the database download to disk
7456d66 fix(render): replace a crashed page; one bad job never aborts the run
5367cf6 fix(cli): cleanup --remote reports a storage failure instead of a traceback
5146933 feat(db): random filters (year range, difficulty, exclusions), neighbours, fast /random
6b2420d feat(bot): navigation and re-roll buttons, one-box /problem, richer /random
66e1beb test(bot): every command fits Discord's description/option/choice limits
6984d6f feat(validate): flag leaked solutions, unbalanced TeX, missing MC answers
19f2eae feat(render): template 7 - formulas at text size, no trailing gap, fit-to-width
96d28ea fix(aops): drop spacer paragraphs left in front of a cut
09ac51b docs: bot buttons and filters, legacy solution cutting, parser version, migrations 3-4, template 7
(this report)
```
