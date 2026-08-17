#!/usr/bin/env python
"""
Step 1b - Extract the population grid from Statistik Austria's INSPIRE GML.

The free download (pd_popreg_100m_*.gml, ~320 MB) is a single INSPIRE
`pd:StatisticalDistribution` feature with ~589,000 `pd:StatisticalValue`
records nested inside it. GDAL sees one feature, not 589,000, so ogr2ogr is no
help - we stream the XML instead.

Each record looks like:

    <pd:value>
      <pd:StatisticalValue>
         <pd:value>11</pd:value>
        <pd:status xlink:href=".../final"></pd:status>
        <pd:dimensions>
          <pd:Dimensions>
            <pd:spatial xlink:href=".../su.StatisticalGridCell/AT_CRS3035RES100mN2630300E4551400"></pd:spatial>
          </pd:Dimensions>
        </pd:dimensions>
      </pd:StatisticalValue>
    </pd:value>

Output: data/statistik_austria_population_grid.csv with `cell_id,residents`,
which script 2 picks up automatically.

Usage:
    python scripts/01b_extract_population.py
"""
from __future__ import annotations

import csv
import os
import re
import sys
from pathlib import Path

BUILD_DIR = Path(os.environ.get("SB_BUILD_DIR", Path.home() / "vienna"))
DATA_DIR = BUILD_DIR / "data"
OUT_CSV = DATA_DIR / "statistik_austria_population_grid.csv"

# Vienna bbox in EPSG:3035, used only for the sanity check at the end.
# (16.17, 48.09) - (16.60, 48.33) transformed to LAEA, rounded outwards.
VIE_E = (4_780_000, 4_820_000)
VIE_N = (2_780_000, 2_815_000)

VALUE_RE = re.compile(r"<pd:value>\s*(\d+(?:\.\d+)?)\s*</pd:value>")
CELL_RE = re.compile(r"StatisticalGridCell/(?:[A-Z]{2}_)?"
                     r"(CRS3035RES\d+k?mN\d+E\d+)")
COORD_RE = re.compile(r"CRS3035RES(\d+)(k?m)N(\d+)E(\d+)")


def find_gml() -> Path:
    hits = sorted(DATA_DIR.glob("*popreg*.gml")) or sorted(DATA_DIR.glob("*.gml"))
    if not hits:
        raise SystemExit(
            f"No .gml found in {DATA_DIR}\n\n"
            "Download and unzip STATISTIK AUSTRIA's INSPIRE 100 m population grid:\n"
            "  https://www.statistik.at/gs-inspire/www/inspire2/download/daten/"
            "pd_popreg_100m_7767c33f-302c-11e3-beb4-0000c1ab0db6.zip"
        )
    return hits[0]


def main() -> None:
    gml = find_gml()
    total_bytes = gml.stat().st_size
    print(f"Reading {gml.name} ({total_bytes / 1e6:.0f} MB)")

    n_cells = 0
    n_people = 0.0
    n_vienna = 0
    vienna_people = 0.0
    pending: float | None = None
    read_bytes = 0
    next_report = 20_000_000

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with gml.open("r", encoding="utf-8", errors="replace") as fh, \
            OUT_CSV.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(["cell_id", "residents"])

        for line in fh:
            read_bytes += len(line)

            # The count comes first, the cell id a few lines later.
            m = VALUE_RE.search(line)
            if m:
                pending = float(m.group(1))
                continue

            c = CELL_RE.search(line)
            if c and pending is not None:
                cell_id = c.group(1)
                writer.writerow([cell_id, int(pending) if pending.is_integer()
                                 else pending])
                n_cells += 1
                n_people += pending

                cm = COORD_RE.match(cell_id)
                if cm:
                    size = int(cm.group(1)) * (1000 if cm.group(2) == "km" else 1)
                    north, east = int(cm.group(3)), int(cm.group(4))
                    if north < 100_000:
                        north *= size
                    if east < 100_000:
                        east *= size
                    if (VIE_E[0] <= east <= VIE_E[1]
                            and VIE_N[0] <= north <= VIE_N[1]):
                        n_vienna += 1
                        vienna_people += pending
                pending = None

            if read_bytes >= next_report:
                pct = read_bytes * 100.0 / total_bytes
                sys.stdout.write(f"\r  {pct:5.1f}%  {n_cells:,} cells")
                sys.stdout.flush()
                next_report += 20_000_000

    print(f"\r  100.0%  {n_cells:,} cells" + " " * 20)

    print(f"\nWrote {OUT_CSV} ({OUT_CSV.stat().st_size / 1e6:.1f} MB)")
    print(f"  cells:            {n_cells:,}")
    print(f"  total residents:  {n_people:,.0f}")
    print(f"  cells near Vienna:{n_vienna:>10,}")
    print(f"  residents there:  {vienna_people:,.0f}")

    # --- sanity checks -----------------------------------------------------
    problems = []
    if n_cells < 500_000:
        problems.append(f"expected ~589,000 cells, got {n_cells:,}")
    if not 8_000_000 <= n_people <= 10_500_000:
        problems.append(f"Austria's population should be ~9.2M, got {n_people:,.0f}")
    if not 1_500_000 <= vienna_people <= 2_600_000:
        problems.append(f"the Vienna box should hold ~2M people, got "
                        f"{vienna_people:,.0f}")

    if problems:
        print("\nWARNING - these look off:")
        for p in problems:
            print(f"  - {p}")
        print("Paste this output back before running script 2.")
    else:
        print("\nSanity checks passed. Next: python scripts/02_base_demand.py")


if __name__ == "__main__":
    main()
