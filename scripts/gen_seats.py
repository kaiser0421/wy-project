#!/usr/bin/env python3
"""Generate an exact number of unique seats in zone A as CSV or JSON."""

import argparse
import csv
import json
from pathlib import Path

FIELDS = ("zone_code", "row_label", "seat_number")


def generate(path: Path, count: int, zone: str) -> None:
    rows = ((zone, f"R{index // 100 + 1}", str(index % 100 + 1)) for index in range(count))
    with path.open("w", encoding="utf-8", newline="") as output:
        if path.suffix == ".csv":
            writer = csv.writer(output, lineterminator="\n")
            writer.writerow(FIELDS)
            writer.writerows(rows)
        else:
            output.write("[\n")
            for index, row in enumerate(rows):
                if index:
                    output.write(",\n")
                output.write(json.dumps(dict(zip(FIELDS, row, strict=True)), separators=(",", ":")))
            output.write("\n]\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rows", type=int, help="Number of seat rows")
    parser.add_argument("output", type=Path, help="Output filename ending in .csv or .json")
    parser.add_argument("--zone", default="A", help="Zone code (default A)")
    args = parser.parse_args()
    if args.rows < 0 or args.output.suffix not in {".csv", ".json"}:
        parser.error("provide a nonnegative row count and a .csv or .json output")
    generate(args.output, args.rows, args.zone)


if __name__ == "__main__":
    main()
