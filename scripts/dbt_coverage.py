"""Documentation and test coverage of the dbt models, per layer.

Reads dbt artifacts (run `dbt docs generate` first):
  - target/manifest.json: model and column descriptions, tests
  - target/catalog.json:  the columns that actually exist in the database

Columns are taken from the catalog, so a column that exists in the database but
is missing from the YAML counts as undocumented and untested.

Usage (from the repository root):
    python scripts/dbt_coverage.py            # prints a table, writes dbt/target/coverage.json
"""

import json
from collections import defaultdict
from pathlib import Path

TARGET = Path("dbt/target")


def load(name: str) -> dict:
    path = TARGET / name
    if not path.exists():
        raise SystemExit(f"{path} not found. Run `make coverage` (dbt docs generate) first.")
    return json.loads(path.read_text())


def pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def main() -> None:
    manifest, catalog = load("manifest.json"), load("catalog.json")

    models = {uid: n for uid, n in manifest["nodes"].items() if n["resource_type"] == "model"}
    tested_models, tested_columns = set(), set()
    for node in manifest["nodes"].values():
        if node["resource_type"] != "test" or not node.get("attached_node"):
            continue
        tested_models.add(node["attached_node"])
        if node.get("column_name"):
            tested_columns.add((node["attached_node"], node["column_name"].lower()))

    stats = defaultdict(lambda: defaultdict(int))
    gaps = []
    for uid, model in models.items():
        layer = model["schema"]
        s = stats[layer]
        s["models"] += 1
        s["models_documented"] += bool(model["description"].strip())
        s["models_tested"] += uid in tested_models

        documented = {name.lower() for name, col in model["columns"].items() if col["description"].strip()}
        db_columns = catalog["nodes"].get(uid, {}).get("columns", {})
        for col in (c.lower() for c in db_columns):
            s["columns"] += 1
            s["columns_documented"] += col in documented
            s["columns_tested"] += (uid, col) in tested_columns
            if col not in documented:
                gaps.append(f"{model['name']}.{col}")

    report = {}
    for layer in sorted(stats):
        s = stats[layer]
        report[layer] = {
            **s,
            "models_documented_pct": pct(s["models_documented"], s["models"]),
            "models_tested_pct": pct(s["models_tested"], s["models"]),
            "columns_documented_pct": pct(s["columns_documented"], s["columns"]),
            "columns_tested_pct": pct(s["columns_tested"], s["columns"]),
        }
    report["undocumented_columns"] = sorted(gaps)
    (TARGET / "coverage.json").write_text(json.dumps(report, indent=2))

    print(f"{'Layer':8} {'Models':>6} {'Documented':>11} {'Tested':>8} {'Columns':>8} {'Documented':>11} {'Tested':>8}")
    for layer in sorted(stats):
        r = report[layer]
        print(f"{layer:8} {r['models']:>6} {r['models_documented_pct']:>10}% {r['models_tested_pct']:>7}% "
              f"{r['columns']:>8} {r['columns_documented_pct']:>10}% {r['columns_tested_pct']:>7}%")
    if gaps:
        print("\nUndocumented columns: " + ", ".join(sorted(gaps)))
    print(f"\nWritten to {TARGET / 'coverage.json'}")


if __name__ == "__main__":
    main()
