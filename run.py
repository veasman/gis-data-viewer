#!/usr/bin/env python3
import requests
import json
import os
import csv
import re
from datetime import datetime, timedelta, date
from pathlib import Path

class GISCloudQCExporter:
    """
    Interactive CLI that:
      1) Lists shared maps (filtered by keywords)
      2) Auto-selects known layers by map_id OR prompts user to pick a layer
      3) Downloads all features for the chosen layer(s)
      4) Normalizes + filters rows:
         - stage lowercased must be in: complete, precon complete, po, strike
         - sorts by: stage, date_of_status_update, district, address, street_dir, street_name, city, cleared_by_employee
      5) Writes CSV(s) into repo folder: data/normalized/
      6) Writes manifest: data/manifest.json (for index.html)

    Output CSV columns (in order):
      stage, date_of_status_update, district, address, street_dir, street_name, city, cleared_by_employee
    """

    def __init__(self, api_key, out_root="data"):
        self.api_key = api_key
        self.base_url = "https://api.giscloud.com/1"
        self.headers = {"API-Key": api_key}
        self.out_root = Path(out_root)
        self.out_dir = self.out_root / "normalized"

        # Map filtering (same spirit as your script)
        self.keywords = ["ATMOS", "PRECON", "One Gas"]

        # Predefined layers for each map ID (your current ones)
        self.auto_layers = {
            "1409686": "3667832",  # ATMOS KS
            "1843533": "4688672",  # ATMOS CO
            "1166936": "3082877",  # PRECON_Master
            "2934234": "7127604",  # Colorado 2025
            "2937762": "7135311",  # Kansas 2025
            "2662324": "6515164",  # One Gas 2024
        }

        self.allowed_stages = {"complete", "precon complete", "po", "strike"}

        # Keep it deterministic: fixed output schema
        self.out_columns = [
            "stage",
            "date_of_status_update",
            "district",
            "address",
            "street_dir",
            "street_name",
            "city",
            "cleared_by_employee",
        ]

        # Candidate field names (because GIS Cloud data is never consistent)
        self.stage_candidates = ["stage", "pv_stage"]
        self.date_candidates = ["date_of_status_update", "date_of_last_status_update", "status_update", "last_status_update"]
        self.cleared_by_candidates = ["cleared_by_employee", "cleared_by", "cleared_by_team_number"]

    # ----------------------------
    # API calls (kept like your script)
    # ----------------------------
    def get_shared_maps(self):
        """Fetch shared maps from GIS Cloud API"""
        try:
            urls_to_try = [
                f"{self.base_url}/maps.json?type=shared",
                f"{self.base_url}/maps.json?type=private,shared",
                f"{self.base_url}/maps.json",
            ]

            for url in urls_to_try:
                response = requests.get(url, headers=self.headers, timeout=60)
                if response.status_code == 200:
                    data = response.json()
                    if "data" in data and data["data"]:
                        return data["data"]

            print("No shared maps found or API call failed.")
            return []

        except requests.RequestException as e:
            print(f"Error fetching maps: {e}")
            return []

    def get_map_layers(self, map_id):
        """Get layers for a specific map"""
        try:
            url = f"{self.base_url}/maps/{map_id}/layers.json"
            response = requests.get(url, headers=self.headers, timeout=60)

            if response.status_code == 200:
                data = response.json()
                return data.get("data", [])
            else:
                print(f"Error fetching layers for map {map_id}: {response.status_code}")
                return []

        except requests.RequestException as e:
            print(f"Error fetching layers: {e}")
            return []

    def get_layer_features(self, layer_id):
        """Get ALL features from a layer (paged)"""
        try:
            url = f"{self.base_url}/layers/{layer_id}/features.json"
            params = {"perpage": 1000, "geometry": "false"}

            all_features = []
            page = 1

            while True:
                params["page"] = page
                response = requests.get(url, headers=self.headers, params=params, timeout=60)

                if response.status_code != 200:
                    print(f"Error downloading layer {layer_id}: {response.status_code}")
                    return []

                data = response.json()
                features = data.get("data", [])

                if not features:
                    break

                all_features.extend(features)

                if len(features) < 1000:
                    break

                page += 1

            return all_features

        except Exception as e:
            print(f"Error downloading layer {layer_id}: {e}")
            return []

    # ----------------------------
    # Normalization rules (your requirements)
    # ----------------------------
    def safe_name(self, s):
        s = (s or "").strip()
        s = re.sub(r"[^\w\s\-_]", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s if s else "Unnamed"

    def parse_date_to_ymd(self, date_str):
        """Parse dates like 7/25/2025 and normalize to YYYY-MM-DD string."""
        if date_str is None:
            return None
        if not isinstance(date_str, str):
            date_str = str(date_str)
        date_str = date_str.strip()
        if not date_str:
            return None

        # strip time if present
        date_main = date_str.split("T")[0].split(" ")[0].strip()

        for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%Y/%m/%d"):
            try:
                d = datetime.strptime(date_main, fmt).date()
                return d.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    def pick_first(self, data_dict, candidates):
        for k in candidates:
            if k in data_dict:
                return data_dict.get(k, "")
        return ""

    def normalize_feature_row(self, feature):
        """
        feature is a GISCloud feature dict:
          { "data": { ...fields... } }
        Returns normalized row dict (OUT schema) OR None if dropped.
        """
        data = feature.get("data", {}) if isinstance(feature, dict) else {}
        if not isinstance(data, dict) or not data:
            return None

        stage_raw = (self.pick_first(data, self.stage_candidates) or "").strip()
        stage_lc = stage_raw.lower().strip()

        # REQUIRED: lowercase stage before comparing to allowed list
        if stage_lc not in self.allowed_stages:
            return None

        date_raw = self.pick_first(data, self.date_candidates)
        date_norm = self.parse_date_to_ymd(date_raw)
        if not date_norm:
            return None

        cleared_by = (self.pick_first(data, self.cleared_by_candidates) or "").strip()

        # Only the columns you want
        row = {
            "stage": stage_raw,
            "date_of_status_update": date_norm,
            "district": (data.get("district", "") or "").strip(),
            "address": (data.get("address", "") or "").strip(),
            "street_dir": (data.get("street_dir", "") or "").strip(),
            "street_name": (data.get("street_name", "") or "").strip(),
            "city": (data.get("city", "") or "").strip(),
            "cleared_by_employee": cleared_by,
        }
        return row

    def normalize_and_sort(self, features):
        rows = []
        for f in features:
            r = self.normalize_feature_row(f)
            if r:
                rows.append(r)

        # REQUIRED: sort by these cols, in order:
        # stage, date of status update, district, address, street dir, street name, city, cleared by employee
        def k(r):
            return (
                (r.get("stage") or "").lower(),
                r.get("date_of_status_update") or "",
                (r.get("district") or "").lower(),
                (r.get("address") or "").lower(),
                (r.get("street_dir") or "").lower(),
                (r.get("street_name") or "").lower(),
                (r.get("city") or "").lower(),
                (r.get("cleared_by_employee") or "").lower(),
            )

        rows.sort(key=k)
        return rows

    # ----------------------------
    # Output writers
    # ----------------------------
    def write_csv(self, map_name, layer_name, rows):
        self.out_dir.mkdir(parents=True, exist_ok=True)

        safe_map = self.safe_name(map_name)
        safe_layer = self.safe_name(layer_name)

        # filename contains map + layer (date range handled in UI; csv is "current snapshot")
        filename = f"{safe_map}__{safe_layer}.csv"
        path = self.out_dir / filename

        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=self.out_columns, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)

        dates = [r["date_of_status_update"] for r in rows if r.get("date_of_status_update")]
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

    def write_manifest(self, file_entries):
        self.out_root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "columns": self.out_columns,
            "files": sorted(file_entries, key=lambda x: (x.get("max_date") or "", x.get("file") or ""), reverse=True),
        }
        (self.out_root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # ----------------------------
    # Interactive selection (kept like your script)
    # ----------------------------
    def interactive_select(self):
        maps = self.get_shared_maps()
        if not maps:
            print("No maps available.")
            return

        # Filter maps by your keywords
        maps = [m for m in maps if any(k.lower() in (m.get("name") or "").lower() for k in self.keywords)]
        if not maps:
            print("No maps matched keywords:", ", ".join(self.keywords))
            return

        print("\nAvailable Maps:")
        for i, map_data in enumerate(maps, 1):
            print(f"{i}. {map_data.get('name', 'Unnamed Map')} (ID: {map_data.get('id')})")

        selected = input("\nEnter map numbers (comma separated) or 'all': ").strip()
        selected = "all"
        if selected.lower() == "all":
            selected_maps = maps
        else:
            selected_indices = [int(x.strip()) - 1 for x in selected.split(",") if x.strip().isdigit()]
            selected_maps = [maps[i] for i in selected_indices if 0 <= i < len(maps)]

        if not selected_maps:
            print("Nothing selected.")
            return

        file_entries = []

        for map_data in selected_maps:
            raw_map_id = map_data.get("id")
            map_id = str(raw_map_id)
            map_name = map_data.get("name", f"Map_{map_id}")

            print(f"\nFetching layers for: {map_name} (ID: {map_id})")

            # Pick layer
            if map_id in self.auto_layers:
                layer_id = str(self.auto_layers[map_id])
                print(f"Auto-selected layer ID: {layer_id}")
            else:
                print(f"⚠ No predefined layer for {map_name} — please select one:")
                layers = self.get_map_layers(raw_map_id)
                if not layers:
                    print(f"No layers found for {map_name}, skipping.")
                    continue

                for i, layer in enumerate(layers, 1):
                    print(f"{i}. {layer.get('name', 'Unnamed Layer')} (ID: {layer.get('id')})")

                while True:
                    choice = input("Enter layer number: ").strip()
                    if choice.isdigit() and 1 <= int(choice) <= len(layers):
                        layer_id = str(layers[int(choice) - 1].get("id"))
                        break
                    print("Invalid choice. Please enter a valid layer number.")

            # Resolve layer name for that ID
            layers = self.get_map_layers(raw_map_id)
            layer = next((l for l in layers if str(l.get("id")) == str(layer_id)), None)
            if not layer:
                print(f"Layer {layer_id} not found for {map_name}")
                continue

            layer_name = layer.get("name", f"Layer_{layer_id}")
            print(f"Selected Layer: {layer_name} (ID: {layer_id})")

            features = self.get_layer_features(layer_id)
            if not features:
                print("No data found or error downloading.")
                continue

            rows = self.normalize_and_sort(features)

            print(f"Downloaded {len(features)} features; kept {len(rows)} rows after stage/date filtering.")
            entry = self.write_csv(map_name, layer_name, rows)
            file_entries.append(entry)
            print(f"Wrote: {self.out_dir / Path(entry['file']).name}")

        if file_entries:
            self.write_manifest(file_entries)
            print(f"\nWrote manifest: {self.out_root / 'manifest.json'}")
            print("Done.")
        else:
            print("\nNo files written (no data matched filters).")


if __name__ == "__main__":
    # Stop hardcoding your API key in source. That's trash opsec.
    api_key = os.getenv("GIS_CLOUD_API_KEY", "").strip()
***REMOVED***
        # api_key = input("Enter GIS Cloud API key: ").strip()
***REMOVED***

    exporter = GISCloudQCExporter(api_key, out_root="data")

    try:
        exporter.interactive_select()
    except KeyboardInterrupt:
        print("\nOperation cancelled by user")
    except Exception as e:
        print(f"An error occurred: {e}")
