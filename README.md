# plant-funding-monitor

## Creating the database

Run `python3 init_db.py` in the project folder. It needs Python 3 and nothing else. It creates `data/dev.db` from `schema.sql` and loads `sources.csv` and `tags.csv` into it. `data/dev.db` is for local runs and is not committed; only the GitHub job writes the committed `data/funding.db`, by passing `--db data/funding.db`. Run the script again whenever you edit either CSV: it adds and updates rows, and never touches pipeline state, calls, the screening log or runs. A source or tag you delete from a CSV stays in the database, marked as `removed` or `retired`, because calls may still point to it. There is no migration system yet, so when `schema.sql` changes its `user_version`, delete the database and run the script again.

## Fetching the sources

The fetch step needs a few libraries. Install them once into a virtual environment with `python3 -m venv .venv` and `.venv/bin/pip install -r requirements.txt`. Then `.venv/bin/python fetch.py` checks every active source that is due, according to its `check_frequency`, and records each new, changed or removed item in the `item_changes` table for the screening step. Use `--force` to check every active source regardless, `--source ID` to check just one, and `--db` for a database other than `data/dev.db`. It respects robots.txt and pauses between requests to the same site, and it calls no language model. Because the database is public, personal contact details are removed from all text (see `monitor/scrub.py`), and the database keeps only fingerprints and titles of what it has seen, plus the text of each change until screening clears it.

## Tests

Run `python3 tests/test_init_db.py` and `.venv/bin/python tests/test_fetch.py` from the project folder. Both work on temporary copies and leave the databases in `data/` unchanged; the fetch tests use the saved pages in `tests/fixtures` and never touch the network.
