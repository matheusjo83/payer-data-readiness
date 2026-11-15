"""Reads the data quality history from the lakehouse (gold.quality_runs and
gold.quality_history), read-only."""

import lakehouse


def load():
    con = lakehouse.connect(read_only=True)
    try:
        runs = con.execute("select * from gold.quality_runs order by run_at").df()
        history = con.execute("select * from gold.quality_history order by run_at").df()
    finally:
        con.close()
    for df in (runs, history):
        df["series"] = df["source"] + " · " + df["branch"].fillna("?") + " · " + df["scenario"]
    return runs, history
