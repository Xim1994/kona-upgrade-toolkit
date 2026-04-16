# Kona BSP Upgrade Toolkit

SSH-based, NS-agnostic upgrade automation for Tektelic Kona LoRaWAN gateways (Micro / Macro / Mega / Enterprise / Photon).

## Files

| File | Purpose |
|------|---------|
| [`kona_upgrade.py`](./kona_upgrade.py) | Per-GW orchestrator, 10 phases, fully idempotent |
| [`kona_bulk_upgrade.py`](./kona_bulk_upgrade.py) | Fleet-wide wrapper (5 target-selection modes, parallel, safety rails) |
| [`UPGRADE_PROCEDURE.md`](./UPGRADE_PROCEDURE.md) | Full procedure documentation (customer-defensible) |
| [`create_confluence_page.py`](./create_confluence_page.py) | Publishes the procedure as a Confluence page |
| [`bsp/`](./bsp/) | Local BSP cache (zips + `.sha256` sidecars + release notes) — git-ignored |
| [`upgrades/`](./upgrades/) | Per-run logs for audit trail — git-ignored |

## Quick start

```bash
# 1. Single-GW upgrade, local BSP zip
python kona_upgrade.py --host <gateway-ip> --bsp bsp/BSP_7.1.16.3.zip

# 2. Single GW via NS name resolution (script queries Tektelic NS for IP)
python kona_upgrade.py --gw-name BCNNIOTGW04 --target 7.1.16.3

# 3. Auto-fetch latest BSP from Tektelic FTP
python kona_upgrade.py --host <gateway-ip> --fetch-latest

# 4. List available BSPs on Tektelic FTP
python kona_upgrade.py --list-bsps

# 5. Dry-run (read-only, validates everything without touching GW)
python kona_upgrade.py --host <gateway-ip> --target 7.1.16.3 --dry-run

# 6. Fleet-wide, only GWs not on target, sequential
python kona_bulk_upgrade.py --not-at-version 7.1.16.3 --target 7.1.16.3

# 7. Fleet-wide, pre-flight all first, default 4 parallel but max 1 per site
python kona_bulk_upgrade.py --all --target 7.1.16.3 --pre-flight-all-first

# 8. Force sequential (safest, slowest)
python kona_bulk_upgrade.py --all --target 7.1.16.3 --parallel 1
```

## Environment (reads from `.env` at repo root)

```
TEKTELIC_GW_USER=root
TEKTELIC_GW_PASS=<gw-ssh-password>

TEKTELIC_NS_EU_URL=https://lorawan-ns-eu.tektelic.com
TEKTELIC_NS_USER=<ns-account-email>
TEKTELIC_NS_PASS=<ns-account-password>
TEKTELIC_CUSTOMER_ID=<customer-uuid>

TEKTELIC_FTP_USER=customer
TEKTELIC_FTP_PASS=vU6_ATR3
```

IPs are never stored. For any GW registered in a Tektelic NS, pass `--gw-name <NAME>`
and the script resolves the IP via `/api/.../getGatewayInfo`. The `--host <ip>` flag
exists only as an escape hatch for standalone GWs not registered in any NS.

## What the per-GW script does (10 phases)

1. **Pre-flight** — SSH check, version, upgrader idle, mounts, corruption, disk, NTP, network, firmware dir
2. **Risk assessment** — detects the two bugs that caused the reported production failure (`/backup/000/` full, admin user leftover)
3. **Cleanup** — auto-remediates risks from phase 2
4. **Staging** — SFTP upload, unzip, verify GPG-signed manifest
5. **Opkg refresh** — clears cache, `opkg update` (enforces GPG signature), dry-run upgrade check
6. **Gate** — Go/No-Go confirmation (skippable with `--yes`)
7. **Upgrade** — `tektelic-dist-upgrade -Du` (daemon mode, survives SSH disconnect)
8. **Monitor** — reconnect loop, progress polling, failure-signature scanning
9. **Post-verify** — 11 checks including component diff, kernel panic, `verify-bsp-installation.sh`, 2-min MQTT bridge stability (validates KGW-2547)
10. **Reporting** — archived log + optional JSON to stdout

## Key design decisions

### NS-agnostic
The upgrade procedure is independent of the LoRaWAN Network Server. Tektelic NS is only used (optionally) to translate gateway name → IP in Phase 0. If the customer migrates to Actility, ChirpStack, AWS IoT Core, or a self-hosted NS, only the name-resolution helper needs to change.

### Artefact provenance
BSP zips come from the **official Tektelic FTP** (`ftpes://74.3.134.34`, user `customer`). Every download is stored with a `.sha256` sidecar that records hash + source URL + timestamp + size. The script verifies the hash before every upgrade.

### GPG-signed opkg feed
The Tektelic feed ships with `Packages.asc`. The Kona has `/etc/opkg/tek-signing.conf` with `check_signature 1`. Our Phase 5 enforces this — if the signature verification fails, `opkg update` does not emit `Updated source 'bsp'` and the script aborts.

### Two production bugs preempted (from the 2026-04-09 internal support ticket)
- **UBI eraseblock exhaustion** (`ubimkvol: error!`) → Phase 2 detects `/backup/000/ > 100MB`, Phase 3 clears it.
- **Admin user leftover** (`useradd: user 'admin' already exists`) → Phase 2 detects user admin, Phase 3 removes it + orphan home.

Full rationale in [`UPGRADE_PROCEDURE.md`](./UPGRADE_PROCEDURE.md).

### Audit trail
Every run produces a timestamped log under `upgrades/`. Format: `YYYY-MM-DD_HHMMSS_<gw-name>_to_<target>.log`. For bulk runs: `upgrades/bulk-YYYY-MM-DD_HHMMSS/` with `summary.json` + per-GW logs. Attachable to the customer change-management tickets as evidence.

## Troubleshooting cheat sheet

| Error signature | Cause | Recovery |
|-----------------|-------|----------|
| `ubimkvol: error!: UBI device does not have free logical eraseblocks` | `/backup/000/` full | `rm -rf /backup/000/* && sync` on GW, re-run |
| `preinst script returned status 1` (tektelic-add-users) | admin user from old install | `userdel admin && rm -rf /home/admin` on GW, re-run |
| `Restoring from the latest backup` in log | Auto-rollback mid-upgrade | Wait 5min for restore, check `tektelic-dist-upgrade -s`, investigate root cause |
| `rootfs free < 140 MB` | Insufficient space | `rm -fr /var/lib/opkg/lists/* /lib/firmware/bsp*.zip`, check `/var/log/` |
| `NTP not synced` | Wrong clock → GPG verify fails | `/etc/init.d/ntpd restart`, wait ~5min for `ntpq -pn` to show `*` peer |
| `Phase 8 timeout after 40 min` | Usually upgrade did succeed, script monitor missed the 0% tick | SSH to GW, `cat /etc/tektelic-versions/tektelic-bsp-version` to confirm |
| `gateway is OFFLINE in NS` during `--gw-name` resolve | GW not reporting to NS | Use `--host <ip>` directly instead |

## Architecture

```
                      ┌──────────────────────────┐
                      │  Tektelic FTP            │
                      │  ftpes://74.3.134.34     │
                      │  /Universal_Kona_SW/     │
                      └───────────┬──────────────┘
                                  │ 1. SIZE, SHA256
                                  │    verify
                                  ▼
                      ┌──────────────────────────┐
                      │  bsp/ local cache        │
                      │  BSP_X.Y.Z.zip + .sha256 │
                      └───────────┬──────────────┘
                                  │ 2. SFTP upload
                                  │    /lib/firmware/bsp.zip
                                  ▼
  ┌──────────────┐    ┌──────────────────────────┐    ┌────────────────────┐
  │  Tektelic NS │───▶│  Kona Gateway            │    │  upgrades/ log dir │
  │ (optional:   │NS  │  opkg update (GPG check) │───▶│  audit trail       │
  │  name→IP)    │    │  tektelic-dist-upgrade   │    │  (git-ignored)     │
  └──────────────┘    └──────────────────────────┘    └────────────────────┘

                                  ▲
                                  │ SSH + SFTP (no NS dependency here)
                          ┌───────┴───────────┐
                          │ kona_upgrade.py   │ ← 1 GW, 10 phases
                          │ kona_bulk_...     │ ← N GWs, wraps above
                          └───────────────────┘
```

## References

- Kona release notes v7.1.16.3 (2025-11-28, GIT `2e261c53`) → `bsp/BSP_7.1.16.3_release-notes.pdf`
- internal onboarding training — §5 Gateway Onboarding / BSP Upgrade
- KGW-2547 (7.1.9): *Improve data plane restart logic in TGB* — MQTT bridge auto-recovery
- KGW-3061 (7.1.5.1): *tektelic-add-users not installed during upgrade*
- Tektelic FTP access: https://knowledgehub.tektelic.com/access-tektelic-ftp-server
- Tektelic support: https://supporthub.tektelic.com/tickets-view
