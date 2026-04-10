# Mist WLAN Minimum Data Rate Automation

Automatically configures per-site WLAN minimum data rates on a **Juniper Mist** organisation by reading live AP neighbour RSSI and density statistics directly from the Mist REST API — no manual per-site tuning required.

---

## How It Works

The script runs five sequential steps across every site in your Mist org:

1. **Fetch all sites** — `GET /api/v1/orgs/{org_id}/sites`
2. **Collect live AP neighbour stats** — `GET /api/v1/sites/{site_id}/stats/devices?type=ap`
   Walks each AP's `radio_stat[].neighbors[]` array to compute per-site average neighbour count and average neighbour RSSI.
3. **Classify each site** into a density tier (Dense / Medium / Sparse) based on the computed averages.
4. **Fetch org-level WLANs** — `GET /api/v1/orgs/{org_id}/wlans` (source of truth / template).
5. **Apply site-level WLAN overrides** — `PUT` or `POST` to `/api/v1/sites/{site_id}/wlans[/{id}]` with the appropriate `band_stgs` payload.

---

## Density Tiers & Data-Rate Mapping

| Tier | Condition | Mist `band_stgs` mode | Effective min basic rate |
|---|---|---|---|
| **DENSE** | avg neighbours/AP ≥ 4 **or** avg RSSI ≥ −70 dBm | `high_density` | ≈ 24 Mbps (no 802.11b/g) |
| **MEDIUM** | avg neighbours/AP ≥ 2 **or** avg RSSI ≥ −78 dBm | `no_legacy` | ≈ 6 Mbps (no 802.11b) |
| **SPARSE** | below both thresholds | `default` | all rates (802.11b/g/n/ac/ax) |

Applied to **2.4 GHz, 5 GHz, and 6 GHz** simultaneously. All thresholds are tunable constants at the top of the script.

---

## API Endpoints Used

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/api/v1/orgs/{org_id}/sites` | List all sites |
| `GET` | `/api/v1/sites/{site_id}/stats/devices?type=ap` | Live AP stats + per-radio neighbour RSSI |
| `GET` | `/api/v1/orgs/{org_id}/wlans` | Org-level WLAN template |
| `GET` | `/api/v1/sites/{site_id}/wlans` | Existing site-level overrides |
| `POST` | `/api/v1/sites/{site_id}/wlans` | Create new site override |
| `PUT` | `/api/v1/sites/{site_id}/wlans/{wlan_id}` | Update existing site override |

---

## Installation

```bash
pip install requests python-dotenv
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```env
MIST_API_TOKEN=your_mist_api_token_here
MIST_ORG_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
MIST_API_HOST=api.mist.com        # or api.eu.mist.com / api.gc1.mist.com
TARGET_SSID=                       # optional — leave blank for all WLANs
```

Your Mist API token is generated from **My Profile → API Tokens** in the Mist dashboard.

---

## Usage

```bash
# Preview all changes without writing anything (dry-run — default)
python mist_min_datarate_automation.py

# Apply to all sites
python mist_min_datarate_automation.py --no-dry-run

# Apply to a single site only
python mist_min_datarate_automation.py --no-dry-run --site-id <site-uuid>

# Apply to one SSID only
TARGET_SSID="CorpWLAN" python mist_min_datarate_automation.py --no-dry-run
```

---

## Example Output

```
2026-04-08 10:32:01 [INFO] Fetching org sites …
2026-04-08 10:32:02 [INFO]   ↳ 12 site(s) found
2026-04-08 10:32:03 [INFO]   ├─ Site 'HQ-Building-A': 24 APs | avg_nbr/AP=6.2 | avg_nbr_RSSI=-67.3 dBm → tier=DENSE
2026-04-08 10:32:04 [INFO]   ├─ Site 'Remote-Branch-3': 2 APs | avg_nbr/AP=0.5 | avg_nbr_RSSI=-84.1 dBm → tier=SPARSE

═══════════════════════════════════════════════════════════════════════════════
EXECUTION SUMMARY
═══════════════════════════════════════════════════════════════════════════════
Site                          APs   Avg Nbr/AP    Avg RSSI  Tier     Mode
───────────────────────────────────────────────────────────────────────────────
HQ-Building-A                  24          6.2   -67.3 dBm  dense    High Density (24 Mbps min)
Remote-Branch-3                 2          0.5   -84.1 dBm  sparse   Compatible (default)
```

---

## Files

| File | Description |
|---|---|
| `mist_min_datarate_automation.py` | Main automation script |
| `.env.example` | Credentials template — copy to `.env` |
| `requirements.txt` | Python dependencies |

---

## Notes

- **Dry-run by default** — safe to run without `--no-dry-run` to preview changes first.
- **Idempotent** — skips API writes when `band_stgs` is already correct.
- **Schedulable** — run nightly via cron or a task scheduler to keep settings aligned with RF environment changes.
- **Firmware compatibility** — neighbour data is read from `radio_stat[].neighbors[]` with automatic fallback to top-level `ap.neighbors[]` for older firmware.

> ⚠️ Never commit your `.env` file. Your API token grants write access to the entire Mist organisation.
