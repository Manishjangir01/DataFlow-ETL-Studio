"""Regenerate the Parquet and JSON Lines sample files in data/samples.

Run from the project folder:  python examples/generate_samples.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

SAMPLES = Path(__file__).resolve().parents[1] / "data" / "samples"


def main() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    products = pd.DataFrame(
        {
            "sku": [f"SKU-{i}" for i in range(1, 11)],
            "name": ["Notebook", "Pen Set", "Backpack", "Desk Lamp", "Mug",
                     "Headphones", "Keyboard", "Monitor", "Mouse", "Webcam"],
            "category": ["stationery", "stationery", "bags", "home", "home",
                         "electronics", "electronics", "electronics", "electronics", None],
            "price": [4.99, 12.5, 49.0, 22.75, 8.0, 89.99, 45.0, 189.0, 19.99, 59.0],
            "stock": [120, 80, 15, 0, 200, 35, 50, 8, 75, 20],
            "launched": pd.to_datetime(
                ["2023-01-10", "2023-02-14", "2023-05-01", "2024-01-20", "2022-11-11",
                 "2024-06-30", "2024-03-03", "2025-02-02", "2023-09-09", "2025-07-15"]
            ),
            "active": [True, True, True, False, True, True, True, True, True, False],
        }
    )
    products.to_parquet(SAMPLES / "products.parquet", index=False)

    events = [
        {"event_id": i, "type": t, "user": {"id": 1000 + i % 5, "plan": p}, "ts": f"2026-10-0{1 + i % 9}T12:00:00Z"}
        for i, (t, p) in enumerate(
            [("login", "free"), ("purchase", "pro"), ("logout", "free"), ("login", "pro"),
             ("purchase", "free"), ("login", "team"), ("purchase", "team"), ("logout", "pro")],
            start=1,
        )
    ]
    with open(SAMPLES / "events.jsonl", "w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")
    print(f"Samples written to {SAMPLES}")


if __name__ == "__main__":
    main()
