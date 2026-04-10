#!/usr/bin/env python3
"""
mist_min_datarate_automation.py
================================
Automates the configuration of per-site WLAN minimum data rates on a
Juniper Mist org WLAN template by inspecting live AP neighbor RSSI / density
statistics from the Mist API.

Logic
-----
1. Pull every site in the org (or a single site via --site-id).
2. For each site, fetch AP device stats and extract per-radio neighbor data
   (count + RSSI) from the Mist stats/devices endpoint.
3. Classify each site into one of three RF density tiers:
     DENSE   → avg neighbor count >= DENSE_NEIGHBOR_COUNT
                 OR avg neighbor RSSI >= DENSE_RSSI_DBM
     MEDIUM  → avg neighbor count >= MEDIUM_NEIGHBOR_COUNT
                 OR avg neighbor RSSI >= MEDIUM_RSSI_DBM
     SPARSE  → everything else
4. Map the tier to a Mist WLAN band_stgs data-rate mode:
     DENSE   → high_density  (min basic rate ≈ 24 Mbps; no 802.11b/g)
     MEDIUM  → no_legacy     (min basic rate ≈ 6 Mbps;  no 802.11b)
     SPARSE  → default       (compatible; all rates incl. 802.11b)
5. For every org-level WLAN (filtered by TARGET_SSID if set), create or
   update a site-level override that applies the computed band_stgs to the
   2.4 GHz, 5 GHz, and 6 GHz bands.

Running in dry-run mode (default) prints every API call without executing it.
Pass --no-dry-run to push changes live.

Usage
-----
    # Preview changes (no writes)
    python mist_min_datarate_automation.py

    # Apply changes for all sites
    python mist_min_datarate_automation.py --no-dry-run

    # Apply to a single site only
    python mist_min_datarate_automation.py --no-dry-run --site-id <site-id>

    # Apply only to one SSID
    TARGET_SSID="CorpWLAN" python mist_min_datarate_automation.py --no-dry-run

Environment / .env
------------------
    MIST_API_TOKEN   — Mist API token (required)
    MIST_ORG_ID      — Mist organisation ID (required)
    MIST_API_HOST    — API host (default: api.mist.com)
                       Other regions: api.eu.mist.com | api.gc1.mist.com
    TARGET_SSID      — Restrict to one SSID name (optional; default: all)

Requirements
------------
    pip install requests python-dotenv
"""

import os
import sys
import json
import logging
import argparse
from dataclasses import dataclass
from typing import Optional

import requests
from dotenv import load_dotenv

# ─────────────────────────────────────────────────────────────────────────────
# Load environment
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()  # reads .env in the current working directory if present

# ─────────────────────────────────────────────────────────────────────────────
# Credentials & connectivity
# ─────────────────────────────────────────────────────────────────────────────

API_TOKEN: str = os.getenv("MIST_API_TOKEN", "")
ORG_ID: str    = os.getenv("MIST_ORG_ID", "")
API_HOST: str  = os.getenv("MIST_API_HOST", "api.mist.com")
BASE_URL: str  = f"https://{API_HOST}/api/v1"

# ─────────────────────────────────────────────────────────────────────────────
# WLAN filter (optional — leave blank to configure all org WLANs)
# ─────────────────────────────────────────────────────────────────────────────

TARGET_SSID: str = os.getenv("TARGET_SSID", "")

# ─────────────────────────────────────────────────────────────────────────────
# Neighbor-density classification thresholds
# ─────────────────────────────────────────────────────────────────────────────
#
# Neighbor COUNT (average number of other Mist APs heard per radio per AP):
#   >= DENSE_NEIGHBOR_COUNT   → dense
#   >= MEDIUM_NEIGHBOR_COUNT  → medium
#   < MEDIUM_NEIGHBOR_COUNT   → sparse (unless RSSI overrides)
#
# Neighbor RSSI (average signal strength of heard neighbours, wBm — higher = closer):
#   >= DENSE_RSSI_DBM         → dense
#   >= MEDIUM_RSSI_DBM        → medium
#
# Tune these for your environment.

DENSE_NEIGHBOR_COUNT:  int   = 4      # ≥ 4 neighbours/AP/radio → dense
MEDIUM_NEIGHBOR_COUNT: int   = 2      # 2–3 neighbours/AP/radio → medium
DENSE_RSSI_DBM:        float = -70.0  # avg RSSI ≥ -70 dBm → dense
MEDIUM_RSSI_DBM:       float = -78.0  # avg RSSI ≥ -78 dBm → medium

# ─────────────────────────────────────────────────────────────────────────────
# Data-rate tier → Mist band_stgs mapping
# ─────────────────────────────────────────────────────────────────────────────
#
# Mist WLAN band_stgs.{band}.type accepts:
#   "default"      Compatible — all rates (802.11b/g/n/ac/ax)
#   "no_legacy"    Disable 802.11b; minimum basic rate = 6 Mbps
#   "high_density" Disable 802.11b/g + raise minimum; basic rate = 24 Mbps
#   "custom"       Manual rate list (extend DATA_RATE_TIERS if needed)
#
# Reference:
#   https://www.juniper.net/documentation/us/en/software/mist/mist-wireless/
#     topics/ref/mist-data-rates.html

DATA_RATE_TIERS: dict = {
    "dense": {
        "label":   "High Density (24 Mbps min)",
        "band_24": "high_density",
        "band_5":  "high_density",
        "band_6":  "high_density",
    },
    "medium": {
        "label":   "No Legacy (6 Mbps min)",
        "band_24": "no_legacy",
        "band_5":  "no_legacy",
        "band_6":  "no_legacy",
    },
    "sparse": {
        "label":   "Compatible (default)",
        "band_24": "default",
        "band_5":  "default",
        "band_6":  "default",
    },
}

# Global flag — overridden by --no-dry-run at runtime
DRY_RUN: bool = True

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Mist REST helpers
# ─────────────────────────────────────────────────────────────────────────────

class MistAPIError(Exception):
    """Raised when the Mist API returns a non-2xx status."""


def _headers() -> dict:
    return {
        "Authorization": f"Token {API_TOKEN}",
        "Content-Type":  "application/json",
    }


def _get(path: str, params: dict = None) -> dict | list:
    """HTTP GET → parsed JSON."""
    url  = f"{BASE_URL}{path}"
    resp = requests.get(url, headers=_headers(), params=params, timeout=30)
    if not resp.ok:
        raise MistAPIError(
            f"GET {url} returned {resp.status_code}: {resp.text[:400]}"
        )
    return resp.json()


def _post(path: str, body: dict) -> dict:
    """HTTP POST → parsed JSON.  Skipped in dry-run mode."""
    url = f"{BASE_URL}{path}"
    if DRY_RUN:
        log.info(f"  [DRY-RUN] POST {url}\n{json.dumps(body, indent=4)}")
        return {}
    resp = requests.post(url, headers=_headers(), json=body, timeout=30)
    if not resp.ok:
        raise MistAPIError(
            f"POST {url} returned {resp.status_code}: {resp.text[:400]}"
        )
    log.info(f"  [CREATED] POST {url} → {resp.status_code}")
    return resp.json()


def _put(path: str, body: dict) -> dict:
    """HTTP PUT → parsed JSON.  Skipped in dry-run mode."""
    url = f"{BASE_URL}{path}"
    if DRY_RUN:
        log.info(f"  [DRY-RUN] PUT {url}\n{json.dumps(body, indent=4)}")
        return {}
    resp = requests.put(url, headers=_headers(), json=body, timeout=30)
    if not resp.ok:
        raise MistAPIError(
            f"PUT {url} returned {resp.status_code}: {resp.text[:400]}"
        )
    log.info(f"  [UPDATED] PUT {url} → {resp.status_code}")
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — Fetch all org sites
# ─────────────────────────────────────────────────────────────────────────────

def get_org_sites() -> list[dict]:
    """Return all sites belonging to the org."""
    log.info("Fetching org sites …")
    # GET /api/v1/orgs/{org_id}/sites
    sites = _get(f"/orgs/{ORG_ID}/sites")
    if not isinstance(sites, list):
        sites = sites.get("results", [])
    log.info(f"  ↳ {len(sites)} site(s) found")
    return sites


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — Compute per-site neighbor density from AP stats
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SiteNeighborStats:
    site_id:   str
    site_name: str
    ap_count:             int   = 0
    total_neighbor_count: int   = 0   # Σ neighbor counts across all APs + all radio bands
    total_rssi_samples:   int   = 0   # number of individual RSSI readings
    total_rssi_sum:       float = 0.0 # Σ of RSSI readings (dBm)

    @property
    def avg_neighbors_per_ap(self) -> float:
        """Average number of neighbours heard per AP (across all radios)."""
        return self.total_neighbor_count / max(self.ap_count, 1)

    @property
    def avg_neighbor_rssi(self) -> Optional[float]:
        """Average RSSI of heard neighbours (dBm), or None if no data."""
        if self.total_rssi_samples == 0:
            return None
        return self.total_rssi_sum / self.total_rssi_samples

    def tier(self) -> str:
        """Classify the site as 'dense', 'medium', or 'sparse'."""
        avg_n = self.avg_neighbors_per_ap
        avg_r = self.avg_neighbor_rssi

        if avg_n >= DENSE_NEIGHBOR_COUNT:
            return "dense"
        if avg_n >= MEDIUM_NEIGHBOR_COUNT:
            return "medium"
        # When neighbour count is low, lean on RSSI strength as a secondary signal
        if avg_r is not None:
            if avg_r >= DENSE_RSSI_DBM:
                return "dense"
            if avg_r >= MEDIUM_RSSI_DBM:
                return "medium"
        return "sparse"


def _accumulate_neighbors(ap: dict, stats: SiteNeighborStats) -> None:
    """
    Extract neighbour data from an AP stats object and add to the running totals.

    Mist AP stats structure (per radio):
      ap.radio_stat[n].neighbors[] → list of { mac, rssi, channel, band, ... }

    Some firmware builds also surface a top-level ap.neighbors[] list.
    Both locations are handled here; only one will be populated per AP.
    """
    found_in_radio = False

    for radio in ap.get("radio_stat", []):
        neighbours = radio.get("neighbors", [])
        if neighbours:
            found_in_radio = True
        for nbr in neighbours:
            stats.total_neighbor_count += 1
            rssi = nbr.get("rssi")
            if rssi is not None:
                stats.total_rssi_sum     += float(rssi)
                stats.total_rssi_samples += 1

    # Fall back to top-level neighbours only if radio_stat had none
    if not found_in_radio:
        for nbr in ap.get("neighbors", []):
            stats.total_neighbor_count += 1
            rssi = nbr.get("rssi")
            if rssi is not None:
                stats.total_rssi_sum     += float(rssi)
                stats.total_rssi_samples += 1


def get_site_neighbor_stats(site: dict) -> SiteNeighborStats:
    """
    Call GET /api/v1/sites/{site_id}/stats/devices?type=ap and compute
    the aggregate neighbour-density statistics for the site.
    """
    site_id   = site["id"]
    site_name = site.get("name", site_id)
    stats     = SiteNeighborStats(site_id=site_id, site_name=site_name)

    try:
        # GET /api/v1/sites/{site_id}/stats/devices?type=ap
        devices = _get(f"/sites/{site_id}/stats/devices", params={"type": "ap"})
    except MistAPIError as exc:
        log.warning(f"  [WARN] AP stats unavailable for site '{site_name}': {exc}")
        return stats

    if not isinstance(devices, list):
        devices = devices.get("results", [])

    for ap in devices:
        if ap.get("type") != "ap":
            continue
        stats.ap_count += 1
        _accumulate_neighbors(ap, stats)

    rssi_str = (
        f"{stats.avg_neighbor_rssi:.1f} dBm"
        if stats.avg_neighbor_rssi is not None
        else "N/A"
    )
    log.info(
        f"  ├─ Site '{site_name}': {stats.ap_count} APs | "
        f"avg_nbr/AP={stats.avg_neighbors_per_ap:.1f} | "
        f"avg_nbr_RSSI={rssi_str} → tier={stats.tier().upper()}"
    )
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — Build WLAN band_stgs payload
# ─────────────────────────────────────────────────────────────────────────────

def build_band_stgs(tier_name: str) -> dict:
    """
    Return a Mist WLAN band_stgs object for all three bands.

    Example output (dense tier):
    {
        "24": { "type": "high_density" },
        "5":  { "type": "high_density" },
        "6":  { "type": "high_density" }
    }
    """
    tier = DATA_RATE_TIERS[tier_name]
    return {
        "24": {"type": tier["band_24"]},
        "5":  {"type": tier["band_5"]},
        "6":  {"type": tier["band_6"]},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — Fetch org-level WLANs (source of truth)
# ─────────────────────────────────────────────────────────────────────────────

def get_org_wlans() -> list[dict]:
    """
    Return the list of WLANs defined at the org level (from the WLAN template).
    GET /api/v1/orgs/{org_id}/wlans
    """
    log.info("Fetching org-level (template) WLANs …")
    wlans = _get(f"/orgs/{ORG_ID}/wlans")
    if not isinstance(wlans, list):
        wlans = wlans.get("results", [])
    if TARGET_SSID:
        log.info(f"  ↳ Filtering to SSID '{TARGET_SSID}'")
        wlans = [w for w in wlans if w.get("ssid") == TARGET_SSID]
    log.info(f"  ↳ {len(wlans)} WLAN(s) will be configured")
    return wlans


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — Apply site-level WLAN overrides
# ─────────────────────────────────────────────────────────────────────────────

def apply_site_wlan_overrides(
    stats: SiteNeighborStats,
    org_wlans: list[dict],
) -> None:
    """
    For each org WLAN, create or update the matching site-level WLAN override
    with the appropriate band_stgs (minimum data rate mode).

    Site WLAN override endpoints:
      GET  /api/v1/sites/{site_id}/wlans
      POST /api/v1/sites/{site_id}/wlans              ← create new override
      PUT  /api/v1/sites/{site_id}/wlans/{wlan_id}    ← update existing override
    """
    tier_name = stats.tier()
    band_stgs = build_band_stgs(tier_name)
    label     = DATA_RATE_TIERS[tier_name]["label"]

    log.info(
        f"\n▶ Site '{stats.site_name}' — "
        f"tier={tier_name.upper()} → applying '{label}'"
    )

    # Fetch existing site-level WLAN overrides
    try:
        site_wlans = _get(f"/sites/{stats.site_id}/wlans")
    except MistAPIError as exc:
        log.warning(
            f"  [WARN] Cannot fetch site WLANs for '{stats.site_name}': {exc}"
        )
        return

    if not isinstance(site_wlans, list):
        site_wlans = site_wlans.get("results", [])

    # Build SSID → list[site_wlan] lookup for quick matching
    site_wlan_by_ssid: dict[str, list[dict]] = {}
    for sw in site_wlans:
        ssid = sw.get("ssid", "")
        site_wlan_by_ssid.setdefault(ssid, []).append(sw)

    for org_wlan in org_wlans:
        ssid          = org_wlan.get("ssid", "")
        template_id   = org_wlan.get("template_id", "")
        org_wlan_id   = org_wlan.get("id", "")

        log.info(f"  ├─ WLAN '{ssid}'")

        existing_overrides = site_wlan_by_ssid.get(ssid, [])

        # Payload — only the fields we are changing
        payload: dict = {
            "ssid":      ssid,
            "band_stgs": band_stgs,
        }

        if existing_overrides:
            # Update the first (most recently created) matching override
            existing    = existing_overrides[0]
            override_id = existing["id"]
            current_stgs = existing.get("band_stgs", {})

            if current_stgs == band_stgs:
                log.info(
                    f"  │   No change needed (band_stgs already correct)"
                )
                continue

            log.info(
                f"  │   Updating site override id={override_id}"
                f" (was: {current_stgs})"
            )
            # PUT /api/v1/sites/{site_id}/wlans/{wlan_id}
            _put(f"/sites/{stats.site_id}/wlans/{override_id}", payload)

        else:
            # No site override yet — inherit from org template, add band_stgs
            log.info(f"  │   Creating new site override")
            if template_id:
                payload["template_id"] = template_id
            if org_wlan_id:
                payload["org_id"] = ORG_ID
            # POST /api/v1/sites/{site_id}/wlans
            _post(f"/sites/{stats.site_id}/wlans", payload)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    global DRY_RUN

    parser = argparse.ArgumentParser(
        description=(
            "Automate Mist WLAN minimum data rates per site "
            "based on live AP neighbour RSSI / density."
        )
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Push changes to the Mist API (default: dry-run / preview only)",
    )
    parser.add_argument(
        "--site-id",
        default="",
        metavar="SITE_ID",
        help="Limit execution to a single Mist site ID",
    )
    args = parser.parse_args()

    if args.no_dry_run:
        DRY_RUN = False
        log.info("=" * 65)
        log.info("  *** LIVE MODE — changes WILL be pushed to the Mist API ***")
        log.info("=" * 65)
    else:
        log.info("=" * 65)
        log.info("  *** DRY-RUN MODE — no changes will be pushed (use --no-dry-run) ***")
        log.info("=" * 65)

    # Credential check
    if not API_TOKEN:
        sys.exit(
            "ERROR: MIST_API_TOKEN is not set.\n"
            "  Set it as an environment variable or add it to a .env file."
        )
    if not ORG_ID:
        sys.exit(
            "ERROR: MIST_ORG_ID is not set.\n"
            "  Set it as an environment variable or add it to a .env file."
        )

    # ── Step 1: sites ────────────────────────────────────────────────────────
    sites = get_org_sites()
    if args.site_id:
        sites = [s for s in sites if s["id"] == args.site_id]
        if not sites:
            sys.exit(f"ERROR: site_id '{args.site_id}' not found in this org.")

    # ── Step 4: org WLANs ────────────────────────────────────────────────────
    org_wlans = get_org_wlans()
    if not org_wlans:
        sys.exit("No org-level WLANs found (after SSID filter). Nothing to do.")

    # ── Steps 2, 3, 5: per-site processing ───────────────────────────────────
    summary: list[dict] = []
    for site in sites:
        stats = get_site_neighbor_stats(site)           # Step 2
        apply_site_wlan_overrides(stats, org_wlans)     # Steps 3 + 5
        summary.append({
            "site":         stats.site_name,
            "aps":          stats.ap_count,
            "avg_nbr":      round(stats.avg_neighbors_per_ap, 2),
            "avg_rssi_dbm": (
                round(stats.avg_neighbor_rssi, 1)
                if stats.avg_neighbor_rssi is not None
                else None
            ),
            "tier":  stats.tier(),
            "mode":  DATA_RATE_TIERS[stats.tier()]["label"],
        })

    # ── Summary table ─────────────────────────────────────────────────────────
    log.info("\n" + "═" * 78)
    log.info("EXECUTION SUMMARY")
    log.info("═" * 78)
    hdr = f"{'Site':<28} {'APs':>4}  {'Avg Nbr/AP':>10}  {'Avg RSSI':>10}  {'Tier':<8}  Mode"
    log.info(hdr)
    log.info("─" * 78)
    for row in summary:
        rssi_str = f"{row['avg_rssi_dbm']} dBm" if row["avg_rssi_dbm"] is not None else "N/A"
        log.info(
            f"{row['site']:<28} {row['aps']:>4}  "
            f"{row['avg_nbr']:>10}  {rssi_str:>10}  "
            f"{row['tier']:<8}  {row['mode']}"
        )
    log.info("═" * 78)
    action = "previewed (dry-run)" if DRY_RUN else "applied"
    log.info(f"Done — changes {action} for {len(summary)} site(s).")


if __name__ == "__main__":
    main()
