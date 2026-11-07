# plant-funding-monitor

## Creating the database

Run `python3 init_db.py` in the project folder. It needs Python 3 and nothing else. It creates `data/dev.db` from `schema.sql` and loads `sources.csv` and `tags.csv` into it. `data/dev.db` is for local runs and is not committed; only the GitHub job writes the committed `data/funding.db`, by passing `--db data/funding.db`. Run the script again whenever you edit either CSV: it adds and updates rows, and never touches pipeline state, calls, the screening log or runs. A source or tag you delete from a CSV stays in the database, marked as `removed` or `retired`, because calls may still point to it. There is no migration system yet, so when `schema.sql` changes its `user_version`, delete the database and run the script again.

## Fetching the sources

The fetch step needs a few libraries. Install them once into a virtual environment with `python3 -m venv .venv` and `.venv/bin/pip install -r requirements.txt`. Then `.venv/bin/python fetch.py` checks every active source that is due, according to its `check_frequency`, and records each new, changed or removed item in the `item_changes` table for the screening step. Use `--force` to check every active source regardless, `--source ID` to check just one, and `--db` for a database other than `data/dev.db`. It respects robots.txt and pauses between requests to the same site, and it calls no language model. Because the database is public, personal contact details are removed from all text (see `monitor/scrub.py`), and the database keeps only fingerprints and titles of what it has seen, plus the text of each change until screening clears it.

## Screening

`.venv/bin/python screen.py` sends the new and changed items from the fetch step to Claude Haiku 5.5 in batches of about 20 per source, using the instructions in `prompts/screening.md`. Each funding call it finds becomes a row in `candidates` for the extraction step; everything else goes to `screening_log`, and the screened items' texts are cleared. Start with `--dry-run`, which shows the batches and a token and cost estimate without calling the API; `--limit N` screens only the first N items. The API key comes from `ANTHROPIC_API_KEY` in the environment, or else from a `.env` file in the project folder (git-ignored); it is never printed.

## Tests

Run `python3 tests/test_init_db.py`, `.venv/bin/python tests/test_fetch.py` and `.venv/bin/python tests/test_screen.py` from the project folder. They work on temporary copies and leave the databases in `data/` unchanged. The fetch tests use the saved pages in `tests/fixtures`, and the screening tests a fake API client, so none of them use the network or spend money.
