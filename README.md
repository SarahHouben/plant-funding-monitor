# Plant Funding Monitor

A small prototype that checks research funders' websites every morning and lists the funding calls relevant to plant research at [IPK Gatersleben](https://www.ipk-gatersleben.de/en/), with deadlines, eligibility for IPK, relevance to plant science and short summaries in English and German. It is a focused take on services like [ELFI](https://www.elfi.info/): a short list of funders that matter for plant genetics, breeding and crop research, instead of all of science.

**Search page:** https://sarahhouben.github.io/plant-funding-monitor/

> This is a personal prototype for discussion, not an official IPK service. Entries are extracted by a machine; always check details against the original call before applying.

## How it works

1. **Fetch:** a daily GitHub Actions job reads the sources in `sources.csv` (funder pages, the DFG feed and the EU Funding & Tenders portal; 12 for now) and notes what is new or changed.
2. **Screen:** Claude Haiku 5.5 decides which new items are research funding calls relevant to plant research.
3. **Extract:** Claude Sonnet 5.5 reads each call's own page and extracts deadlines, eligibility, amount, summaries and tags. Every answer is checked against the source text, and each deadline is stored with the sentence it came from.
4. **Update:** duplicates are merged, changes recorded, closed calls marked, and anything uncertain flagged for review.
5. **Publish:** the results go into `data/funding.db`, which the search page reads directly through Datasette Lite.

Each day's run summary ends with a health report (failing or silent sources, open review flags, spend), and persistent problems open a GitHub issue.

## Costs and data

- **Costs:** about $2.50 in API fees for a full first scan; daily runs usually cost a few cents and are capped at $0.30. Hosting is free (GitHub Actions and GitHub Pages).
- **Data:** only public funding information is stored. Personal contact details are removed before anything is stored or sent to the model, and the database keeps no copies of funder pages.

## Project files

- `PLAN.md`: the project plan and the decisions made while building the prototype
- `sources.csv` and `tags.csv`: the watched sources and the tag vocabulary; edit these to change what is watched
- `prompts/`: the instructions for the two model steps
- `docs/`: the search page
- The sections below explain how to run each step locally.

## Creating the database

Run `python3 init_db.py` in the project folder. It needs Python 3 and nothing else. It creates `data/dev.db` from `schema.sql` and loads `sources.csv` and `tags.csv` into it. `data/dev.db` is for local runs and is not committed; only the GitHub job writes the committed `data/funding.db`, by passing `--db data/funding.db`. Run the script again whenever you edit either CSV: it adds and updates rows, and never touches pipeline state, calls, the screening log or runs. A source or tag you delete from a CSV stays in the database, marked as `removed` or `retired`, because calls may still point to it. There is no migration system yet. Changes that only add tables, views or indexes keep the schema version and reach a database through `init_db.py`. If `schema.sql` changes its `user_version`, delete your local `data/dev.db` and run the script again; never delete `data/funding.db`, which holds the live history and would need a proper migration.

## Fetching the sources

The fetch step needs a few libraries. Install them once into a virtual environment with `python3 -m venv .venv` and `.venv/bin/pip install -r requirements.txt`. Then `.venv/bin/python fetch.py` checks every active source that is due, according to its `check_frequency`, and records each new, changed or removed item in the `item_changes` table for the screening step. Use `--force` to check every active source regardless, `--source ID` to check just one, and `--db` for a database other than `data/dev.db`. It respects robots.txt and pauses between requests to the same site, keeps a cookie jar per site (the Bundesanzeiger only serves pages to clients that send its session cookie back), and it calls no language model. Because the database is public, personal contact details are removed from all text (see `monitor/scrub.py`), and the database keeps only fingerprints and titles of what it has seen, plus the text of each change until screening clears it. Every connection that writes turns on SQLite's `secure_delete`, so cleared texts are overwritten rather than left in the file's free space.

## Screening

`.venv/bin/python screen.py` sends the new and changed items from the fetch step to Claude Haiku 5.5 in batches of about 20 per source, using the instructions in `prompts/screening.md`. Each funding call it finds becomes a row in `candidates` for the extraction step; everything else goes to `screening_log`, and the screened items' texts are cleared. Start with `--dry-run`, which shows the batches and a token and cost estimate without calling the API; `--limit N` screens only the first N items. The API key comes from `ANTHROPIC_API_KEY` in the environment, or else from a `.env` file in the project folder (git-ignored); it is never printed.

## Extraction

`.venv/bin/python extract.py` turns each candidate from screening into a row in `calls`, using Claude Sonnet 5.5 (`--model` to change it) and the instructions in `prompts/extraction.md`. It fetches each call's own page, PDF or EU topic politely, removes personal contact details, and checks every answer before storing it: valid dates, a closed status once all deadlines have passed, the right number of tags, evidence quotes and links that really appear in the call text. A deadline stage with neither a date nor an annual date is dropped rather than failing the call, and noted in the run's `notes`. A call whose URL is already in `calls` is kept as it is and recorded in `call_updates` for the next step; if its changed page no longer describes a call, the next step flags it `not_a_call`. A call extracted from the listing's stored text (because its own page couldn't be read) or from a text under about 1,500 characters is flagged `thin_source` in `call_flags`. Start with `--dry-run`, which fetches the texts and estimates the cost without calling the API. `--batch` uses the Message Batches API at half price, `--max-cost` stops before the run's estimated spend passes a limit, and `--limit N` or `--candidate ID` pick what to extract.

## Duplicates, updates and closures

Calls are identified by a canonical URL (lower-case host, https, no fragment, trailing slash, tracking or session parameters), so the same call found again under another form of its URL isn't extracted twice; it is recorded in `call_listings`, which the search page can show as "also listed at". After extraction, a new call that is clearly the same as an existing one is merged into it: within one source, the same normalised title, even without a deadline; across the sources of a funder family (the `family` column in `sources.csv`), a nearly identical title and a shared deadline. A weaker match from another source of the family is stored and flagged in `call_flags` for the reviewer; within one source, calls with different titles are simply different calls. Two EU topics with different identifiers are never merged or flagged. A merged duplicate's extraction still fills the fields the existing call has empty or `unclear`; where the two give different deadlines, status or eligibility, nothing is overwritten and the call is flagged instead. If the existing call is known only by a listing page and the duplicate has a link of its own, such as the announcement's PDF, the call takes that link; the listing page stays among its listings. `.venv/bin/python update.py` then applies re-extracted calls (every changed field goes to `call_history`; a deadline that moves later makes the call `extended`; a checked call whose deadlines, status or eligibility changed goes back to `new`), closes EU topics that are no longer open or forthcoming, flags calls that are no longer listed anywhere, refreshes every call's next deadline and status, and flags `deadline_missing` once for each call that has no dated stage at all and isn't always open or withdrawn, closed calls included (resolved automatically when a dated stage appears or the call is withdrawn). It makes no model calls; `--dry-run` shows what it would change.

## Running the whole pipeline

`.venv/bin/python pipeline.py` runs fetch, screen, extract and update in that order and prints one summary with the cost. `--max-cost` sets one budget for screening and extraction together, `--batch` extracts through Message Batches at half price, `--limit N` extracts at most N candidates (0: none), `--force` fetches every source whether due or not, and `--dry-run` changes nothing and calls no API. A failing source or candidate doesn't stop the pipeline; a refused API key, a schema mismatch or a database error does.

## The daily job

The GitHub workflow `.github/workflows/daily.yml` runs the pipeline on `data/funding.db` every day at 04:23 UTC, with `--max-cost 0.30` on the standard API and the API key from the repository secret `ANTHROPIC_API_KEY`. It first runs `init_db.py`, so edits to `sources.csv` and `tags.csv` reach the live database, and it commits `data/funding.db` as `github-actions[bot]` if the database changed. Runs never overlap and stop after 45 minutes.

To start it by hand, open the repository's **Actions** tab, choose **Daily pipeline** and click **Run workflow** on `main`, or run `gh workflow run daily.yml` (and `gh run watch` to follow it).

Each day's result is on the run's summary page (**Actions** → **Daily pipeline** → the run), which shows the pipeline summary: what was fetched, screened and extracted, the update stage, the cost and any problems. The same summary is the body of that day's commit, `Daily run YYYY-MM-DD`, so `git log -- data/funding.db` lists the results too. A failing source or candidate only shows under Problems. If the pipeline stops or crashes, the job fails, GitHub sends a notification (for scheduled runs, to whoever last changed the schedule line), and nothing is committed. Because the job commits every day, pull before you work, and never commit `data/funding.db` by hand.

## Health report and alerts

Every pipeline run ends with a health section, so it is in each day's job summary:

- failing sources: those whose last check failed (`consecutive_failures` > 0), with the last error;
- silent sources: no new or changed item for more than 30 days (daily sources), 60 (weekly) or 120 (monthly);
- item drops: a source whose latest successful check found no items, or fewer than half as many as the check before (likely a redesigned page), shown until its next check;
- open review flags, counted by kind;
- this month's API spend against `--monthly-budget` (10 USD in the workflow, to match the Anthropic Console limit). It is worked out from the tokens in the runs table at standard prices: exact for the daily job, which uses the standard API, and an upper bound for runs that used Message Batches. It counts only the runs in that database, so local runs on `data/dev.db` aren't in the job's figure, although the Console limit covers them too.

The fetch step records every read of a source in `source_checks` (item count, or the error), which the item drops come from. After the pipeline, the job runs `alerts.py`, which turns two problems into GitHub issues with the `health-alert` label (created when first needed): one per source that has failed 3 checks in a row, closed automatically once the source works again, and one when this month's spend passes 80% of the budget. An open issue is never opened twice. The step can't fail the job, and the workflow gets only the extra `issues: write` permission for it. `alerts.py --dry-run` shows what it would open or close without changing anything.

The `source_health` view (linked from the search page as "Source health") lists every source with its status, check frequency, last check, last change, failures in a row, last error and item count, failing sources first.

## The search page

The search page is free and has no server: `docs/index.html` is a landing page on GitHub Pages, and its links open [Datasette Lite](https://lite.datasette.io/), which runs Datasette in the browser and loads `data/funding.db` and `datasette/metadata.json` straight from `main` on GitHub. So the page always shows the latest daily commit. The links pin Datasette 0.65.5 (`?ref=0.65.5`), so a new Datasette release can't break them.

The main view is `calls_search`: one row per call (rejected calls left out), with the English title, original title, funder, next deadline, days left, status, IPK eligibility, relevance, the four tag groups, the English summary and the link to the call, soonest deadline first and calls without one last. Its search box uses the full-text index `calls_fts` over titles, summaries and funders, which the update stage rebuilds at the end of every run; umlauts are optional, but German compounds count as one word, so "Züchtung" doesn't find "Pflanzenzüchtung". `datasette/metadata.json` holds the page title and descriptions, the column descriptions, the default facets and which internal tables to hide.

To switch on GitHub Pages: in the repository's **Settings**, open **Pages**, and under **Build and deployment** set **Source** to **Deploy from a branch**, the branch to **main** and the folder to **/docs**, then **Save**. After a minute or two the page is at https://sarahhouben.github.io/plant-funding-monitor/ (the Pages settings show the address).

A schema change that only adds tables or views, like the search index, reaches `data/funding.db` through the daily job's `init_db.py` step; for your local `data/dev.db`, run `python3 init_db.py` once.

## Reprocessing

`.venv/bin/python reprocess.py` redoes one step for part of the data after a prompt or rule changes, without a full run. `--rescreen-source ID` reads that source again, screens all its items as if they were new, and rejects calls whose items are now `not_relevant` (`review_status` `rejected`, recorded in `call_history`); calls found again wait for extraction as usual. `--reextract` extracts chosen calls again: those given with `--call ID` (repeatable, rejected ones included), those with an open flag of a kind (`--flag thin_source`, for example), and closed calls without any dated stage (`--closed-undated`). The results go through `call_updates` and the update stage, so every change is recorded in `call_history`, and a call whose page no longer describes a call is flagged `not_a_call`. Both take `--max-cost` and `--dry-run`, which calls no API and changes nothing; `--reextract` also takes `--batch`. Each records into a run of its own.

## Tests

Run `python3 tests/test_init_db.py`, `.venv/bin/python tests/test_fetch.py`, `.venv/bin/python tests/test_screen.py`, `.venv/bin/python tests/test_extract.py`, `.venv/bin/python tests/test_update.py`, `.venv/bin/python tests/test_reprocess.py` and `.venv/bin/python tests/test_health.py` from the project folder. They work on temporary copies and leave the databases in `data/` unchanged. The fetch tests use the saved pages in `tests/fixtures`, and the screening tests a fake API client, so none of them use the network or spend money.
