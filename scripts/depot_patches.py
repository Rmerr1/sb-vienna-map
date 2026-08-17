"""
Runtime patches for depot bugs.

These are applied by importing this module and calling `apply(CFG)` before
using MapGen. They are deliberately *runtime* patches rather than edits to
depot's installed source: a patched site-packages silently disappears the next
time you `pip install .`, and you will not remember it existed.

Two bugs are fixed:

1. **Dead STAC endpoint.** `_get_latest_overture_release` fetches
   https://stac.overturemaps.org/catalog.json to discover the newest Overture
   release. Overture restructured their STAC; that URL now 404s (as does the
   catalog root), so `process_buildings` dies immediately. We pin the release
   from config instead.

2. **Crash on unparseable building geometry.** `_fetch_overture_buildings`
   converts every geometry with a bare `wkb.loads(bytes(x))`. A single
   geometry DuckDB hands back in an unexpected form takes down the whole run
   after the download has already happened. We convert defensively and drop
   the failures, reporting how many.

The reimplementation below also asks DuckDB for `ST_AsWKB(geometry)`
explicitly rather than hoping the raw column is WKB, loads `httpfs` (needed
for s3://) rather than `azure`, and sets the bucket region.
"""
from __future__ import annotations

import os

import duckdb
import geopandas as gpd
import pandas as pd
import shapely
from depot.maps import MapGen


_SETTINGS_FILE = (__import__("pathlib").Path(__file__).resolve().parent.parent
                  / "config" / "overture_duckdb.json")


def _duckdb_settings() -> list:
    """
    DuckDB settings that make the S3 read work on this machine.

    Written by scripts/check_overture.py, which probes several configurations
    because DuckDB's httpfs external file cache throws an internal error
    ("Information loss on integer cast") on some builds.
    """
    if not _SETTINGS_FILE.exists():
        return []
    import json as _json
    data = _json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    stmts = data.get("statements", [])
    if stmts:
        print(f"[patch] DuckDB workaround: {data.get('strategy')}")
    return stmts


def _pin_release(release: str) -> None:
    MapGen._get_latest_overture_release = staticmethod(lambda: release)


def _fetch_overture_buildings(self) -> None:
    """Replacement for MapGen._fetch_overture_buildings."""
    buildings_pkl = os.path.join(self.city_dir, "buildings.pkl")
    self.buildings_geojson = os.path.join(self.city_dir, "buildings.geojson")

    if not os.path.exists(buildings_pkl) or self.REFETCH_BUILDINGS:
        release = self._get_latest_overture_release()
        if self.verb:
            print(f"***** Querying Overture buildings for {self.city} "
                  f"(release {release}) *****", flush=True)

        s3_path = (f"s3://overturemaps-us-west-2/release/{release}"
                   f"/theme=buildings/type=building/*")

        con = duckdb.connect()
        con.execute("INSTALL spatial; LOAD spatial;")
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("SET s3_region='us-west-2';")
        for stmt in _duckdb_settings():
            try:
                con.execute(stmt)
            except Exception as exc:
                print(f"  (DuckDB rejected '{stmt.strip()}': {exc})")

        query = f"""
        SELECT
            id,
            ST_AsWKB(geometry) AS geometry,
            names.primary AS name,
            height
        FROM read_parquet('{s3_path}', hive_partitioning=1)
        WHERE bbox.xmin >= {self.bbox[0]} AND bbox.xmax <= {self.bbox[2]}
          AND bbox.ymin >= {self.bbox[1]} AND bbox.ymax <= {self.bbox[3]}
        """

        try:
            df = con.query(query).to_df()
        except Exception as exc:
            raise RuntimeError(
                f"Overture data fetch failed: {exc}\n"
                f"If the path does not exist, release '{release}' has been "
                "retired - pick a newer one from "
                "https://docs.overturemaps.org/release-calendar/ and update "
                "overture_release in config/vienna.json."
            ) from exc
        finally:
            con.close()

        if df.empty:
            print(f"WARNING: No buildings found in Overture for bbox "
                  f"{self.bbox}")
            return

        if self.verb:
            print(f"Converting {len(df):,} geometries from WKB...", flush=True)

        def safe_wkb(x):
            try:
                if isinstance(x, (bytes, bytearray, memoryview)):
                    return shapely.from_wkb(bytes(x))
                return x
            except Exception:
                return None

        df["geometry"] = df["geometry"].apply(safe_wkb)
        before = len(df)
        df = df.dropna(subset=["geometry"])
        dropped = before - len(df)
        if dropped:
            print(f"  dropped {dropped:,} buildings with unreadable geometry "
                  f"({dropped / before:.3%} of the total)")

        if df.empty:
            raise RuntimeError(
                "Every building geometry failed to parse. The Overture schema "
                "has probably changed shape again - inspect the raw column "
                "with scripts/check_overture.py before going further."
            )

        df.to_pickle(buildings_pkl)
    else:
        if self.verb:
            print("***** Loading previously downloaded buildings file: *****")
            print("    " + buildings_pkl)
        df = pd.read_pickle(buildings_pkl)

    gdf = gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326")
    if self.verb:
        print(f"Saving {len(gdf):,} buildings to {self.buildings_geojson}...",
              flush=True)
    gdf.to_file(self.buildings_geojson, driver="GeoJSON")


def apply(cfg: dict) -> None:
    """Apply all patches. Call once, before constructing MapGen."""
    release = cfg["map_generation"].get("overture_release")
    if release:
        _pin_release(release)
        print(f"[patch] Overture release pinned to {release}")
    else:
        print("[patch] No overture_release set - using depot's STAC lookup, "
              "which currently 404s.")

    MapGen._fetch_overture_buildings = _fetch_overture_buildings
    print("[patch] Overture fetch replaced (WKB errors tolerated, httpfs "
          "loaded)")
