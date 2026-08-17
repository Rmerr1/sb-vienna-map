#!/usr/bin/env python
"""
Step 3 - Add Vienna's special demand, route every commute, write config.json.

Takes the base demand_data.json from step 2 and layers on the places that
generate traffic beyond ordinary home-to-work commuting:

    * Flughafen Wien-Schwechat (passengers + airport staff)
    * 18 university / Fachhochschule campuses
    * 12 major hospitals
    * stadiums, Messe, Prater, Schönbrunn, shopping centres, government
    * 7 outside connections for the ~250k daily Umland commuters

Then it runs OSRM to compute real driving times and distances for every pop
(this is what makes the in-game "would people actually take the train?"
comparison meaningful), and writes config.json and description.md.

Usage:
    conda activate depot
    python scripts/03_special_demand.py

Requires Docker running - the script spins up a local OSRM server itself.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from depot.demand import DemandData

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "vienna.json").read_text(encoding="utf-8"))
DCFG = CFG["demand_generation"]

BUILD_DIR = Path(os.environ.get("SB_BUILD_DIR", Path.home() / "vienna"))
OUT_DIR = BUILD_DIR / "build" / CFG["map_code"]
PBF = BUILD_DIR / "data" / "austria-latest.osm.pbf"

CODE = CFG["map_code"]
BBOX = CFG["bbox"]


def main() -> None:
    demand_file = OUT_DIR / "demand_data.json"
    if not demand_file.exists():
        raise SystemExit(f"Missing {demand_file}. Run 02_base_demand.py first.")

    vie = DemandData(str(demand_file), CODE, bbox=BBOX, outputdir=str(OUT_DIR))

    print("\n=== Baseline ===")
    print("Points:", len(vie["points"]), " Pops:", len(vie["pops"]))
    print("\nAvailable special demand types:")
    print("  " + ", ".join(sorted(vie.special_demand_codes.keys())))

    # ---------------------------------------------------------------- airport
    for air in CFG["airports"]:
        # Two populations at an airport: passengers heading to/from the terminal,
        # and the people who work there. Airport staff mostly live off-site, so
        # they enter as an ordinary job destination via residential_split.
        staff_modelled = air["staff"] * air["staff_perc_travel"]
        total = air["daily_passengers"] + staff_modelled
        vie.add_points({
            "type": air["type"],
            "name": air["name"],
            "code": air["code"],
            "location": air["location"],
            "total_capacity": total,
            "pop_size": air["pop_size"],
            "merge_within": air["merge_within"],
            "max_distance": air["max_distance"],
        })
        print(f"  + {air['name']}: {total:,.0f} daily")

    # ----------------------------------------------------------- universities
    on_travel, off_travel = CFG["univ_perc_travel"]
    for u in CFG["universities"]:
        on = u["students"] * u["perc_oncampus"] * on_travel
        off = u["students"] * (1 - u["perc_oncampus"]) * off_travel
        modelled = on + off
        if modelled <= 0:
            continue
        poi = {
            "type": u["type"],
            "name": u["name"],
            "code": u["code"],
            "location": u["location"],
            "total_capacity": modelled,
            "pop_size": u["pop_size"],
            "merge_within": u["merge_within"],
            "residential_split": on / modelled,
        }
        if u.get("max_distance"):
            poi["max_distance"] = u["max_distance"]
        vie.add_points(poi)
    print(f"  + {len(CFG['universities'])} university campuses")

    # -------------------------------------------------------------- hospitals
    for h in CFG["hospitals"]:
        poi = {
            "type": h["type"],
            "name": h["name"],
            "code": h["code"],
            "location": h["location"],
            "total_capacity": h["daily_people"],
            "pop_size": h["pop_size"],
            "merge_within": h["merge_within"],
        }
        if h.get("max_distance"):
            poi["max_distance"] = h["max_distance"]
        vie.add_points(poi)
    print(f"  + {len(CFG['hospitals'])} hospitals")

    # ----------------------------------------------- stadiums, venues, retail
    for v in CFG["venues"]:
        poi = {
            "type": v["type"],
            "name": v["name"],
            "code": v["code"],
            "location": v["location"],
            "total_capacity": v["size"],
            "pop_size": v["pop_size"],
            "merge_within": v["merge_within"],
        }
        if v.get("max_distance"):
            poi["max_distance"] = v["max_distance"]
        vie.add_points(poi)
    print(f"  + {len(CFG['venues'])} venues / attractions")

    # --------------------------------------------------- outside connections
    for oc in CFG["outside_connections"]:
        vie.add_points({
            "type": oc["type"],
            "name": oc["name"],
            "code": oc["code"],
            "location": oc["location"],
            "total_capacity": oc["size"],
            "pop_size": oc["pop_size"],
            "merge_within": oc["merge_within"],
            # These people live outside the map and work inside it.
            "residential_split": 1.0,
        })
    print(f"  + {len(CFG['outside_connections'])} outside connections")

    # ------------------------------------------------------------- hygiene
    vie.enforce_max_pop_size(DCFG["MAXPOPSIZE"])
    vie.merge_identical_commutes()

    # -------------------------------------------------------------- routing
    print("\n=== Routing (OSRM) ===")
    print("Starting a local OSRM server in Docker. First run takes 10-25 min "
          "while it extracts and contracts the road graph.")
    vie.prepare_osrm(str(PBF), bbox=BBOX, port=DCFG["osrm_port"])
    vie.calculate_routes(DCFG["routing_method"], BBOX,
                         osrm_port=DCFG["osrm_port"])

    print("\n=== Final statistics ===")
    vie.print_stats()

    # --------------------------------------------------------------- outputs
    vie.save(str(OUT_DIR / "demand_data.json"))
    vie.save_schemas()

    vie.create_config(
        name=CFG["map_name"],
        bbox=BBOX,
        description=CFG["description"],
        creator=CFG["creator"],
        version=CFG["version"],
        country=CFG["country"],
        initial_view_state=CFG["initial_view_state"],
    )

    vie.create_description(
        "vienna",
        methodology=[
            "<li>Residential demand from the STATISTIK AUSTRIA register-based "
            "population grid (Regionalstatistische Rastereinheiten, ETRS89-LAEA).</li>",
            "<li>Workplace demand distributed with a building-type dasymetric "
            "over OSM office / retail / industrial / institutional features, "
            "constrained to Vienna's published employment total.</li>",
            "<li>Commute flows from a doubly-constrained gravity model (Furness "
            "balancing, exponential distance decay) calibrated against Vienna's "
            "observed median commute.</li>",
            "<li>Driving times and distances routed with OSRM on the OSM road "
            "network.</li>",
            '<li>Map geometry built with <a href="https://github.com/'
            'Subway-Builder-Modded/depot">Depot</a>.</li>',
        ],
        data_sources=[
            # NOT OGDEXT_RASTER_1 - that dataset is grid *geometry* only and
            # carries no population attribute. The residents actually come
            # from the INSPIRE register-based 100 m population grid below.
            '<li><a href="https://www.statistik.at/gs-inspire/www/inspire2/'
            'download/daten/pd_popreg_100m_7767c33f-302c-11e3-beb4-'
            '0000c1ab0db6.zip">STATISTIK AUSTRIA - INSPIRE register-based '
            "100 m population grid (Wohnbevolkerung, ETRS89-LAEA)</a></li>",
            '<li><a href="https://www.data.gv.at/">data.gv.at - Open Government '
            "Data Österreich</a></li>",
            '<li><a href="https://www.openstreetmap.org/copyright">OpenStreetMap '
            "contributors</a></li>",
            '<li><a href="https://overturemaps.org/">Overture Maps Foundation - '
            "building footprints</a></li>",
        ],
    )

    print(f"\nWrote demand_data.json, config.json and description.md to {OUT_DIR}")
    print("Next: python scripts/04_package.py")


if __name__ == "__main__":
    main()
