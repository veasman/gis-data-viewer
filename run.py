#!/usr/bin/env python3
import os
import csv
import json
import re
import requests
from pathlib import Path
from datetime import datetime, timedelta, date


class GISCloudWeeklyExporter:
    """
    Exports weekly GISCloud data for selected maps/layers and generates:
      1) data/normalized/*.csv files (still compatible with the existing index.html viewer)
         - MUST include: stage, date_of_status_update, district, address, street_dir, street_name, city, cleared_by_employee
         - MAY include extra columns (we keep them), including:
           - photo_of_map / photo_map
           - site_photo / site_photos
           - crew_lead / crew_tech
         - strips internal keys starting with "__" (so no __fid, __confidence, __layer_id, etc.)

      2) data/errors/photo_errors.csv (photo validation errors only, for website viewing)
         - includes map/site photo fields + crew_lead/crew_tech for spreadsheet use

      3) data/manifest.json
         - includes "files" list (for main viewer)
         - includes "photo_errors_file" path (for errors viewer)
    """

    def __init__(self, api_key: str, out_root: str = "data"):
        self.api_key = (api_key or "").strip()
        self.base_url = "https://api.giscloud.com/1"
        self.headers = {"API-Key": self.api_key}
        self.out_root = Path(out_root)
        self.normalized_dir = self.out_root / "normalized"
        self.errors_dir = self.out_root / "errors"

        # Map name filter (restore your original behavior)
        self.keywords = ["ATMOS", "PRECON", "One Gas"]

        # Auto layers (as requested)
        self.auto_layers = {
            "1409686": ["3667832"],            # ATMOS KS
            "1843533": ["4688672"],            # ATMOS CO
            "1166936": ["3082877"],            # PRECON_Master
            "2934234": ["7127604"],            # Colorado 2025
            "2937762": ["7135311"],            # Kansas 2025
            "2662324": ["6515164", "6728851"], # One Gas 2024 and One Gas Line Master 2025
        }

        # The viewer expects these columns to exist (can have extras too)
        self.viewer_required_cols = [
            "stage",
            "date_of_status_update",
            "district",
            "address",
            "street_dir",
            "street_name",
            "city",
            "cleared_by_employee",
        ]

        # Date candidates used for filtering + normalization
        self.date_candidates = [
            "date_of_status_update",
            "date_of_last_status_update",
            "status_update",
            "last_status_update",
            "updated_at",
            "date",
        ]

        # Stage candidates (keep both; viewer uses "stage")
        self.stage_candidates = ["stage", "pv_stage"]

        # Truck candidates (for errors spreadsheet)
        self.truck_candidates = ["cleared_by_employee", "cleared_by", "cleared_by_team_number"]

    # ----------------------------
    # Date window: previous Mon..Sat; Sunday uses week that just ended.
    # ----------------------------
    def previous_week_monday_to_saturday(self):
        today = date.today()
        wd = today.weekday()  # Mon=0 ... Sun=6

        if wd == 6:
            end_sat = today - timedelta(days=1)
        else:
            # Previous Saturday (strictly before today). If today is Saturday, go back 7 days.
            if wd == 5:
                days_back = 7
            else:
                days_back = (wd - 5) % 7
            end_sat = today - timedelta(days=days_back)

        start_mon = end_sat - timedelta(days=5)
        return start_mon, end_sat

    # ----------------------------
    # API calls
    # ----------------------------
    def get_all_maps(self):
        urls_to_try = [
            f"{self.base_url}/maps.json?type=private,shared",
            f"{self.base_url}/maps.json?type=shared",
            f"{self.base_url}/maps.json",
        ]
        for url in urls_to_try:
            r = requests.get(url, headers=self.headers, timeout=60)
            if r.status_code == 200:
                data = r.json().get("data", [])
                if data:
                    return data
        return []

    def get_map_layers(self, map_id):
        url = f"{self.base_url}/maps/{map_id}/layers.json"
        r = requests.get(url, headers=self.headers, timeout=60)
        if r.status_code != 200:
            return []
        return r.json().get("data", [])

    def get_layer_features(self, layer_id):
        url = f"{self.base_url}/layers/{layer_id}/features.json"
        params = {"perpage": 1000, "geometry": "false"}

        all_features = []
        page = 1
        while True:
            params["page"] = page
            r = requests.get(url, headers=self.headers, params=params, timeout=60)
            if r.status_code != 200:
                break

            batch = r.json().get("data", [])
            if not batch:
                break

            all_features.extend(batch)
            if len(batch) < 1000:
                break
            page += 1

        return all_features

    def get_layer_column_order(self, layer_id):
        """
        Use layer metadata to preserve a stable, sane column order (NOT random set-order).
        """
        try:
            url = f"{self.base_url}/layers/{layer_id}.json"
            r = requests.get(url, headers=self.headers, params={"expand": "columns"}, timeout=60)
            if r.status_code != 200:
                return []
            payload = r.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                return []
            cols = data.get("columns", [])
            ordered = []
            for c in cols:
                if isinstance(c, dict) and c.get("name"):
                    ordered.append(str(c["name"]))
            return ordered
        except Exception:
            return []

    # ----------------------------
    # Normalization helpers
    # ----------------------------
    def safe_name(self, s):
        s = (s or "").strip()
        s = re.sub(r"[^\w\s\-_]", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s if s else "Unnamed"

    def pick_first(self, d, candidates):
        for k in candidates:
            if k in d:
                return d.get(k)
        return None

    def parse_date_to_iso(self, v):
        if v is None:
            return None
        s = str(v).strip()
        if not s:
            return None

        # strip time if present
        s = s.split("T")[0].split(" ")[0].strip()

        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt).date().isoformat()
            except ValueError:
                continue
        return None

    def strip_internal(self, row: dict):
        # remove any internal/system keys
        return {k: v for k, v in row.items() if not str(k).startswith("__")}

    def compute_stage(self, row: dict):
        v = self.pick_first(row, self.stage_candidates)
        return (str(v).strip() if v is not None else "")

    def compute_date_iso(self, row: dict):
        raw = self.pick_first(row, self.date_candidates)
        return self.parse_date_to_iso(raw)

    def in_week(self, iso_ymd: str, start: date, end: date):
        if not iso_ymd:
            return False
        try:
            d = datetime.strptime(iso_ymd, "%Y-%m-%d").date()
        except ValueError:
            return False
        return start <= d <= end

    def ordered_headers(self, rows: list, preferred_order: list):
        """
        Stable headers:
          - ensure viewer-required columns exist and appear first (in that order)
          - then preferred layer column order (metadata)
          - then extras sorted
        """
        keys = set()
        for r in rows:
            keys.update(self.strip_internal(r).keys())

        # Force required cols to exist in header (even if empty everywhere)
        for c in self.viewer_required_cols:
            keys.add(c)

        out = []
        seen = set()

        # required cols first
        for c in self.viewer_required_cols:
            if c in keys and c not in seen:
                out.append(c)
                seen.add(c)

        # then layer preferred order
        for c in preferred_order:
            if c in keys and not str(c).startswith("__") and c not in seen:
                out.append(c)
                seen.add(c)

        # then extras stable
        extras = sorted([k for k in keys if k not in seen and not str(k).startswith("__")])
        out.extend(extras)
        return out

    # ----------------------------
    # Photo validation (only)
    # ----------------------------
    def photo_column_names_for_layer(self, layer_id: str):
        """
        Default: photo_of_map, site_photo
        One Gas Line Master 2025 (layer 6728851): photo_map, site_photos
        """
        if str(layer_id) == "6728851":
            return ("photo_map", "site_photos")
        return ("photo_of_map", "site_photo")

    def get_truck(self, row: dict):
        v = self.pick_first(row, self.truck_candidates)
        return (str(v).strip() if v is not None else "")

    def _norm_stage_key(self, s: str) -> str:
        """
        Normalize stage strings so PRECON_COMPLETE == PRECON COMPLETE.
        Also collapses extra whitespace.
        """
        raw = (s or "").strip().upper()
        raw = raw.replace("_", " ")
        raw = re.sub(r"\s+", " ", raw).strip()
        return raw

    def validate_photos_rows(
        self,
        rows: list,
        dataset_label: str,
        layer_id: str,
        start: date,
        end: date,
    ):
        errors_out = []

        map_col, site_col = self.photo_column_names_for_layer(layer_id)

        for r in rows:
            # ------------------------
            # Date filter (CURRENT WEEK ONLY)
            # ------------------------
            iso_date = str(r.get("date_of_status_update", "")).strip()
            if not self.in_week(iso_date, start, end):
                continue

            # ------------------------
            # Ignore jetting rows
            # ------------------------
            pv_stage_raw = str(r.get("pv_stage", "")).strip().lower()
            if "jetting" in pv_stage_raw:
                continue

            jet_ft = str(r.get("jet_ft", "")).strip()
            if jet_ft:
                try:
                    if float(jet_ft) > 0:
                        continue
                except ValueError:
                    pass

            # ------------------------
            # Stage
            # ------------------------
            stage = str(r.get("stage", "")).strip().upper()
            if not stage:
                continue

            # ------------------------
            # Photos
            # ------------------------
            map_photo = str(r.get(map_col, "")).strip()
            site_photo = str(r.get(site_col, "")).strip()

            def push(msg: str, severity: str):
                errors_out.append({
                    "date": iso_date,
                    "truck": self.get_truck(r),
                    "lead": str(r.get("crew_lead", "")).strip(),
                    "tech": str(r.get("crew_tech", "")).strip(),
                    "address": str(r.get("address", "")).strip(),
                    "building": str(r.get("to_bldg", "")).strip(),
                    "street_dir": str(r.get("street_dir", "")).strip(),
                    "street_name": str(r.get("street_name", "")).strip(),
                    "city": str(r.get("city", "")).strip(),
                    "severity": severity,
                    "error_message": msg,
                    "map_photo_value": map_photo,
                    "site_photo_value": site_photo,
                    "map_photo_column": map_col,
                    "site_photo_column": site_col,
                    "dataset": dataset_label,
                })

            # ------------------------
            # VALIDATION RULES
            # ------------------------
            if stage == "PRECON COMPLETE":
                if map_photo == 'None' or not map_photo:
                    push(
                        "PRECON COMPLETE but map photo is missing",
                        "error",
                    )

            elif stage == "COMPLETE":
                if map_photo == 'None' or not map_photo:
                    push(
                        "COMPLETE but map photo is missing",
                        "error",
                    )
                if site_photo == 'None' or not site_photo:
                    push(
                        "COMPLETE but site photo is missing",
                        "warning",
                    )

        return errors_out

    # ----------------------------
    # Writers
    # ----------------------------
    def write_main_csv(self, map_name, layer_name, layer_id, rows):
        self.normalized_dir.mkdir(parents=True, exist_ok=True)

        filename = f"{self.safe_name(map_name)}__{self.safe_name(layer_name)}.csv"
        path = self.normalized_dir / filename

        preferred_order = self.get_layer_column_order(layer_id)
        headers = self.ordered_headers(rows, preferred_order)

        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(self.strip_internal(r))

        # min/max of normalized date column
        dates = []
        for r in rows:
            d = str(r.get("date_of_status_update", "")).strip()
            if d:
                dates.append(d)
        min_date = min(dates) if dates else None
        max_date = max(dates) if dates else None

        return {
            "file": f"normalized/{filename}".replace("\\", "/"),
            "map_name": map_name,
            "layer_name": layer_name,
            "row_count": len(rows),
            "min_date": min_date,
            "max_date": max_date,
        }

    def write_photo_errors_csv(self, start: date, end: date, error_rows: list):
        self.errors_dir.mkdir(parents=True, exist_ok=True)
        fname = f"photo_errors_{start.isoformat()}_to_{end.isoformat()}.csv"
        path = self.errors_dir / fname

        headers = [
            "date",
            "truck",
            "lead",
            "tech",
            "address",
            "building",
            "street_dir",
            "street_name",
            "city",
            "severity",
            "error_message",
            "map_photo_value",
            "site_photo_value",
            "map_photo_column",
            "site_photo_column",
            "dataset",
        ]

        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
            w.writeheader()
            for r in error_rows:
                w.writerow(r)

        return f"errors/{fname}".replace("\\", "/"), len(error_rows)

    def write_manifest(self, file_entries: list, photo_errors_file: str, start: date, end: date):
        self.out_root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "week_window": {"start": start.isoformat(), "end": end.isoformat()},
            "files": file_entries,
            "photo_errors_file": photo_errors_file,
        }
        (self.out_root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ----------------------------
    # Main
    # ----------------------------
    def run(self):
        if not self.api_key:
            raise RuntimeError("Missing GIS_API_KEY environment variable.")

        start, end = self.previous_week_monday_to_saturday()
        print(f"Weekly window: {start.isoformat()} → {end.isoformat()}")

        maps = self.get_all_maps()
        if not maps:
            print("No maps available.")
            return

        # Filter maps by keywords (your original behavior)
        maps = [m for m in maps if any(k.lower() in (m.get("name") or "").lower() for k in self.keywords)]
        if not maps:
            print("No maps matched keywords:", ", ".join(self.keywords))
            return

        selected = "all"
        if selected.lower() == "all":
            selected_maps = maps
        else:
            selected_indices = [int(x.strip()) - 1 for x in selected.split(",") if x.strip().isdigit()]
            selected_maps = [maps[i] for i in selected_indices if 0 <= i < len(maps)]

        file_entries = []
        photo_error_rows = []

        for map_data in selected_maps:
            raw_map_id = map_data.get("id")
            map_id = str(raw_map_id)
            map_name = map_data.get("name", f"Map_{map_id}")

            layer_ids = self.auto_layers.get(map_id, [])
            if not layer_ids:
                continue

            layers = self.get_map_layers(raw_map_id)
            if not layers:
                continue

            for layer_id in layer_ids:
                layer = next((l for l in layers if str(l.get("id")) == str(layer_id)), None)
                if not layer:
                    continue

                layer_name = layer.get("name", f"Layer_{layer_id}")
                dataset_label = f"{map_name} — {layer_name}"

                print(f"Downloading: {dataset_label} (layer_id={layer_id})")

                features = self.get_layer_features(str(layer_id))
                if not features:
                    print("  No features returned.")
                    continue

                rows = []
                for f in features:
                    data = f.get("data") if isinstance(f, dict) else None
                    if not isinstance(data, dict) or not data:
                        continue

                    # keep everything (minus internal)
                    row = self.strip_internal(data)

                    # ensure viewer-required columns exist (populate if possible)
                    stage = self.compute_stage(row)
                    date_iso = self.compute_date_iso(row)

                    row["stage"] = stage
                    row["date_of_status_update"] = date_iso or ""

                    rows.append(row)

                # KEEP ALL HISTORY (no date restriction for main CSV)
                rows_all = rows

                # Useful logging (how many rows have a usable normalized date?)
                dated = [r for r in rows_all if str(r.get("date_of_status_update", "")).strip()]
                min_date = min((r["date_of_status_update"] for r in dated), default=None)
                max_date = max((r["date_of_status_update"] for r in dated), default=None)

                print(f"  Features: {len(features)} | Rows kept (all): {len(rows_all)} | Date span: {min_date} → {max_date}")

                if not rows_all:
                    continue

                entry = self.write_main_csv(map_name, layer_name, str(layer_id), rows_all)
                file_entries.append(entry)

                # Photo validation errors: validate only the selected window to keep the errors list focused.
                rows_window = [r for r in rows_all if self.in_week(r.get("date_of_status_update", ""), start, end)]
                photo_error_rows.extend(self.validate_photos_rows(rows_all, dataset_label, str(layer_id), start, end))

        # write photo errors CSV (even if empty, write it so UI has a stable path)
        photo_errors_file, photo_err_count = self.write_photo_errors_csv(start, end, photo_error_rows)
        print(f"Photo errors: {photo_err_count} -> {photo_errors_file}")

        # write manifest
        self.write_manifest(file_entries, photo_errors_file, start, end)
        print(f"Wrote manifest: {self.out_root / 'manifest.json'}")
        print("Done.")


if __name__ == "__main__":
    api_key = os.getenv("GIS_API_KEY", "")
    GISCloudWeeklyExporter(api_key, out_root="data").run()
