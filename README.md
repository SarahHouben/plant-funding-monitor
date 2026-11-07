# plant-funding-monitor

## Creating the database

Run `python3 init_db.py` in the project folder. It needs Python 3 and nothing else. It creates `data/funding.db` from `schema.sql` and loads `sources.csv` and `tags.csv` into it. Run it again whenever you edit either CSV: it adds and updates rows, and never touches pipeline state, calls, the screening log or runs. A source or tag you delete from a CSV stays in the database, marked as `removed` or `retired`, because calls may still point to it. There is no migration system yet, so when `schema.sql` changes its `user_version`, delete `data/funding.db` and run the script again.