# Kona BSP Upgrade Procedure — NS-agnostic, SSH-based

**Target audience:** IoT Platform product team · **Applicable to:** any Tektelic Kona gateway (Micro / Macro / Mega) · **Channel:** SSH only (independent of Network Server vendor)

---

## 1. Scope and rationale

### Why SSH channel instead of Tektelic NS push

The Tektelic onboarding SOP — *internal onboarding training (Gateway Onboarding / BSP Upgrade section)* (slides 44–52) — documents **two equivalent execution paths**:
- **Flow A (NS push)**: NS → SW MANAGEMENT tab → READ UPGRADABLE → UPGRADE BSP (slides 49–50)
- **Flow B (SSH)**: `tektelic-dist-upgrade -Du` (slide 51)

We standardise on **Flow B (SSH)** because:

1. **NS independence.** the customer may in the future migrate to a different LoRaWAN Network Server (Actility ThingPark, ChirpStack, AWS IoT Core LoRaWAN, private LNS). The upgrade procedure must not depend on the current NS vendor.
2. **Audit trail.** SSH-driven upgrades log every command, its return code, and its output locally. NS-driven upgrades depend on Tektelic's backend state which we don't own.
3. **Deterministic binary.** SHA256-verified zip taken from the official Tektelic FTP is the exact same artefact across all gateways, not subject to NS-side mutation.
4. **Fleet parity with third-party gateways.** If the customer eventually mixes Kona with other LoRaWAN gateways, only SSH-based automation will be generic enough.

### Mapping of this script to the internal onboarding training PDF

| internal training step (slide) | Script phase | Status |
|---|---|---|
| Pre-check `/lib/firmware/`, remove residual dirs (slide 44) | Phase 1 pre-flight + Phase 3 cleanup | Implemented |
| Upload BSP.zip (slide 45, via NS FILE TRANSFER) | Phase 4 staging via SFTP directly to `/lib/firmware/bsp.zip` | **Divergence — documented below** |
| On upload failure: `rm -r /lib/firmware/` + retry (slide 46) | Phase 3 cleanup runs automatically | Implemented |
| `opkg update` → "Updated source 'bsp'" (slide 48) | Phase 5 opkg refresh + GPG enforcement | Implemented (stricter: enforces GPG signature) |
| **`tektelic-dist-upgrade -Du` (slide 51)** | **Phase 7 upgrade (literal command)** | Implemented — exact command |
| Monitor `BSP upgrade progress: 16 → 100` (slide 52) | Phase 8 monitor | Implemented |
| NS READ VERSION post-upgrade (slide 50) | Phase 9 post-verify via SSH `cat /etc/tektelic-versions/tektelic-bsp-version` | Implemented (NS-agnostic equivalent) |
| `userdel admin` as standard onboarding step (slide 53) | Phase 2 risk detect + Phase 3 preemptive cleanup | Implemented (handled before it can fail the upgrade) |

**Documented divergence — slide 45 upload.** The training uses NS FILE TRANSFER to push the zip + unzip on the GW. We use SFTP directly for the same result. Rationale: (a) keeps the NS out of the data path so the procedure works against any LNS; (b) does not require the GW Group to have "Upgrade Servers" JSON configured in the NS (slide 47); (c) the resulting state on the GW (unpacked `/lib/firmware/bsp/`, `/lib/firmware/fe-fpga/`, `/lib/firmware/gpio-fpga/`) is identical — the same `opkg update` + `tektelic-dist-upgrade -Du` commands run against it.

### Mapping to the internal change request our internal BSP-upgrade change request

The latest the customer CHG for "upgrade the BSP version of LoRaWAN gateways to the latest standard the customer BSP version" (target BSP 7.1.12.1) documents a 13-step implementation plan plus reversion and test plans. Mapping of its steps to our script:

| internal CHG step | Script phase | Status |
|---|---|---|
| 1. `system_version | grep -E 'Release|Product' && ... && cat syslog | grep corrupt` | Phase 1 pre-flight (version + syslog integrity) | Implemented |
| 2. `df -h && cd /lib/firmware/ && ls -l && rm -r * && ls -l && df -h` + ubi0:rootfs ≥113MB | Phase 1 disk-free (≥140MB post-cleanup, stricter) + Phase 3 cleanup | Implemented |
| 2. `curl -k ftp://<internal-ftp-host>/NEVER_DELETE_LORAWAN/LORAWAN/BSP/BSP_X.Y.Z.zip -o /lib/firmware/bsp.zip && unzip bsp.zip` | Phase 4 staging (SFTP from local workstation) | Divergence — source is Tektelic FTP (SHA256-pinned) rather than internal customer FTP, so the procedure works from any workstation and is reproducible off-prem |
| 3. `curl -k ftp://<internal-ftp-host>/.../snmp-feed.conf -o /etc/opkg/snmp-feed.conf` + `rm snmpManaged-feed.conf` | Not in script (site-specific monitoring feed) | **Post-upgrade manual** — requires internal customer FTP |
| 4. `rm -fr /var/lib/opkg/lists/*` | Phase 5 opkg refresh | Implemented |
| **5. `opkg update` + `tektelic-dist-upgrade -Du`** | **Phase 5 + Phase 7 (literal commands)** | Implemented |
| 6. `tektelic-dist-upgrade -s` + `tektelic-dist-upgrade -p` | Phase 8 monitor (loop) | Implemented |
| 7. Post-upgrade system_version check | Phase 9 post-verify | Implemented |
| 8. `opkg remove --force-depends tektelic-web-server lighttpd openvpn` | Not in script (security hardening) | **Post-upgrade manual** — the customer hardening |
| **9. `userdel admin`** | **Phase 2 risk + Phase 3 cleanup** | Implemented |
| 10. `opkg install pam-plugin-radius libnss-ato2` | Not in script | **Post-upgrade manual** — the customer radius auth |
| 11. `adduser radius-user --disabled-password ...` | Not in script | **Post-upgrade manual** — the customer radius auth |
| 12. `curl -k ftp://<internal-ftp-host>/NEVER_DELETE_LORAWAN/LORAWAN/RAD/*` | Not in script | **Post-upgrade manual** — requires internal customer FTP + PAM config |
| 13. `vi /etc/raddb/server` (insert password from PAM Portal) | Not in script | **Post-upgrade manual** — secret injection |
| Test plan: `system_version`, `tail -f /var/log/pkt_fwd.log`, `tail -f /var/log/gwbridge.log`, NS Active/Online | Phase 9 post-verify — includes `pkt_fwd.log` and `gwbridge.log` tail, NS 8883 ESTABLISHED | Implemented |
| Reversion plan: `system-backup -r 000` | **Not workable — see §2.4 below** | **Documented as non-functional** |

**Steps 3, 8, 10-13 are site-specific post-upgrade hardening / onboarding** (internal customer FTP, Radius auth, web-server removal). They are out of scope for a generic BSP upgrader and are performed separately after the BSP change succeeds. A future `--bi-onboarding` flag could chain them once an accessible the customer FTP mirror is available to the runner.

### Scope

- Gateways: Tektelic Kona family (Micro, Macro, Mega). Same procedure applies regardless of variant (Gen1/Gen2/Gen2.1, PoE/outdoor, indoor/outdoor).
- Source BSP: any Kona BSP ≥ minimum-direct-to-7.x as per [release notes 7.0.9 "Official"](./bsp/BSP_7.1.16.3_release-notes.pdf):
  - Micro v4.0.2 | Micro-PoE v2.5.1 | Enterprise/Photon v2.1.2 | Macro v5.1.3 | Mega v5.0.2
  - Older source BSPs require an intermediate upgrade first (script detects and warns).
- Target BSP: any Kona BSP in `/Universal_Kona_SW/` of Tektelic FTP.

---

## 2. Known failure modes and mitigation

### 2.1 UBI eraseblocks exhaustion (*"`ubimkvol: error!: UBI device does not have free logical eraseblocks`"*)

**Root cause.** The upgrade tool creates a pre-install backup in `/backup/000/` (UBI partition `ubi1:backup`, 248 MB). If a previous `/backup/000/` from an earlier upgrade still occupies >100 MB of that volume, the new backup cannot be written and the upgrade aborts.

**Observed in production.** An internal support ticket on a fleet gateway (2026-04-09): identical error during 7.1.12.1 → 7.1.16.3 attempt. Caused cascade failures in opkg postinst (`tektelic-add-users`), automatic rollback kicked in, upgrade reverted.

**Mitigation (in our procedure).** Phase 2 detects `/backup/000/` >100 MB and flags it; Phase 3 explicitly does `rm -rf /backup/000/*` before staging. Content of `/backup/000/` is inspected and confirmed to be the rootfs snapshot of the previous BSP (not user data), safe to delete. The new pre-install backup created by `tektelic-dist-upgrade -u` will take its place and provide rollback to the current version.

### 2.2 `useradd: user 'admin' already exists`

**Root cause.** The `tektelic-add-users` package preinst script tries to create user `admin` unconditionally. If the user already exists with uid/gid drift from older BSPs, `useradd` fails and opkg aborts the transaction.

**Release-notes reference.** KGW-3061 *"tektelic-add-users not installed during upgrade"* was fixed in 7.1.5.1 release notes. Symptom persists in edge cases where a manual `admin` user remained in `/etc/passwd` from pre-7.1.5.1 installations.

**Mitigation.** Phase 2 inspects `/etc/passwd` for admin user. Phase 3 runs `userdel admin 2>/dev/null || true` before staging. Any orphan `/home/admin/` directory is also removed. The new package's preinst will recreate the user cleanly.

### 2.3 Interrupted previous attempt leaves stale markers

**Root cause.** Files like `/var/lib/tektelic-dist-upgrade/fpga-programming-workaround-attempted` can be left from a prior failed attempt and cause the upgrade tool to skip required fixups.

**Mitigation.** Phase 2 detects these; Phase 3 removes them.

### 2.4 Corrupt partition or read-only mount

**Root cause.** Power loss during a previous operation can corrupt the ubifs journal or force a remount read-only.

**Mitigation.** Phase 1 pre-flight refuses to proceed if `mount` shows any read-only mountpoint.

---

## 2.5 Downgrade is not reliably supported by Tektelic tooling

Validated in the home GW on 2026-04-16 against BSP 7.1.16.3 → 7.1.12.1. Three distinct failure modes exist — none yield a working rollback via stock Tektelic tools:

**(a) `system-backup -r 000` (the customer internal CHG reversion plan)** aborts with `Backup '/backup/000/backup' not found`. Cause: `tektelic-dist-upgrade` stores its auto-backup at `/backup/000/bak.<random>/` but `system-backup -r` expects `/backup/000/backup/` — format mismatch between the two Tektelic tools.

**(b) `tektelic-dist-upgrade -Du` with an older BSP zip** ends in Phase 5: `tektelic-dist-upgrade -c` reports "No BSP upgrade available" because the feed version is older than installed. The script recognises this path under `--allow-downgrade` and skips the pre-check.

**(c) `tektelic-dist-upgrade -Duf` (force)** starts, auto-creates its pre-install backup, reaches progress=16%, then fails fatally: `get_pkg_url: Package tektelic-bsp-version is not available from any configured src. Unrecoverable Opkg failure during command 'install --force-reinstall tektelic-bsp-version'`. The tool's auto-rollback then reverts to the original version. The opkg install path does not pick a lower-version candidate from the feed, even with `-f`.

**Conclusion.** Downgrade on Kona is not guaranteed to succeed via any combination of `tektelic-dist-upgrade` flags or `system-backup`. The script exposes `--allow-downgrade` for completeness (it unblocks Phase 1 and Phase 5 and uses `-Duf` in Phase 7) but the flag is **best-effort**: it depends on whether the older BSP's package set is fully reinstallable from the feed. For field use, plan upgrades as one-way and validate on a lab gateway before rolling to production.

### About manual pre-upgrade backups

A tempting mitigation is to run `system-backup -b` before the upgrade so a rollback point exists. Validated empirically on 2026-04-16: `system-backup` v1.8.0 on this platform **supports only slot 0** — `-b` does not accept a slot argument (help text: `"-b: Perform backup"` with no parameter). Since `tektelic-dist-upgrade` also uses slot 0 for its auto-backup and runs during Phase 7, any manual pre-upgrade snapshot in slot 0 is overwritten by Phase 7 before any rollback would be useful. A Phase 0 flag was prototyped and removed for this reason — it created a false sense of safety. Operators who need a verified rollback point must take it off-device (e.g. via `dd` over SSH on the UBI partition) before running the upgrade.

---

## 3. Upgrade-path compatibility matrix

Derived from Kona release notes, "NB!" sections of version 7.0.9 ("Official"):

| Current BSP | Target BSP | Direct? | Action |
|-------------|------------|---------|--------|
| Same major (e.g. 7.x → 7.x) | any | Yes | direct |
| Micro ≥ 4.0.2 | 7.x | Yes | direct |
| Micro < 4.0.2 | 7.x | No | intermediate via 4.0.2 |
| Macro ≥ 5.1.3 | 7.x | Yes | direct |
| Macro < 5.1.3 | 7.x | No | intermediate via 5.1.3 (or 6.1.x) |
| Mega ≥ 5.0.2 | 7.x | Yes | direct |
| Mega < 5.0.2 | 7.x | No | intermediate via 5.0.2 |
| Enterprise/Photon ≥ 2.1.2 | 7.x | Yes | direct |
| Enterprise/Photon < 2.1.2 | 7.x | No | intermediate via 2.1.2 |

Encoded in `kona_upgrade.py` (`MIN_DIRECT_TO_7X` + `check_upgrade_path()`). Script aborts with explicit suggested intermediate if path is not direct (can be overridden with `--force`).

---

## 4. Artefact provenance

| Item | Source | Verification |
|------|--------|--------------|
| `BSP_7.1.16.3.zip` | `ftpes://ftp.tektelic.com/Universal_Kona_SW/BSP_7.1.16.3_NOT_FOR_ACTILITY/` (host 74.3.134.34, user `customer`) | SHA256 `5b944f1757acb7d7f7bedf15d4d14add040241cb8161928c81b1948668ee1da6` (stored as sidecar `.sha256` for audit) |
| `BSP_7.1.16.3_release-notes.pdf` | Same FTP path | 138,176 bytes, archived |
| `snmpManaged-feed.conf` on GW | Pre-existing on Kona Micro, points to `file:///lib/firmware/bsp` (local file feed) | No change required |
| GPG public key for BSP signature | `/etc/opkg/pubring.kbx` on GW: `Tektelic build server (package signing) <swsupport@tektelic.com>`, 4096-bit RSA, fingerprint `9B024787 328631F2 F99E7CB4 19DE882A 960C0ABD` | opkg `check_signature 1` enforced |

The `NOT_FOR_ACTILITY` tag in the FTP folder name means Tektelic does not certify this BSP against the Actility ThingPark NS. the customer uses Tektelic's own NS, so this is not a blocker.

---

## 5. Procedure (10 phases)

Implemented as `references/homelab/lorawan-qa/kona_upgrade.py`. Full source is version-controlled in the repository.

### Phase 1 — Pre-flight (read-only, abort if any check fails)

| Check | Command / file | Pass condition |
|---|---|---|
| SSH + root | `uname -a` | returns kernel string |
| Current BSP version | `cat /etc/tektelic-versions/tektelic-bsp-version` | parseable `X.Y.Z[.W]` |
| Upgrade-path compatibility | parse current + target, platform detection | entry in §3 matrix |
| Upgrader idle | `tektelic-dist-upgrade -s` | `ok` |
| No progress in flight | `tektelic-dist-upgrade -p` | `0` |
| No scheduled upgrade | `tektelic-dist-upgrade -t` | `request time: 0` |
| No read-only mounts | `mount \| grep 'ro[,)]'` | empty |
| No corruption | `grep -c corrupt /var/log/syslog` | `0` |
| rootfs free ≥ 140 MB | `df -m /` | 4th column ≥ 140 |
| NTP synchronised | `ntpq -pn \| grep -cE '^\*'` | ≥ 1 |
| `/lib/firmware/` clean | `ls /lib/firmware/` | only `opkg/` (no stale BSP) |
| No upgrade lock | `ls /run/lock/upgrade` | not found |

### Phase 2 — Risk assessment (read-only, soft warnings)

| Signal | Trigger | Auto-remediation in Phase 3 |
|---|---|---|
| `/backup/000/` > 100 MB | `du -sm /backup/000` | `rm -rf /backup/000/* && sync` |
| user `admin` exists | `grep '^admin:' /etc/passwd` | `userdel admin 2>/dev/null; rm -rf /home/admin` |
| `/home/admin/` orphan | dir exists without user | `rm -rf /home/admin` |
| Stale upgrade markers | `fpga-*` markers in `/var/lib/tektelic-dist-upgrade/` | remove |
| opkg lists cached | content in `/var/lib/opkg/lists/` | `rm -fr /var/lib/opkg/lists/*` |

### Phase 3 — Cleanup (confirmed, idempotent)

Executes the Phase 2 auto-remediations. Ends with `sync && echo 3 > /proc/sys/vm/drop_caches && sleep 2`.

### Phase 4 — Staging (SFTP upload + verify)

1. Verify local `BSP_X.Y.Z.zip` SHA256 matches pinned value (if `--sha256` passed).
2. `sftp` put to `/lib/firmware/bsp.zip`.
3. Compare remote byte count vs local.
4. `cd /lib/firmware && unzip -o bsp.zip && rm bsp.zip`.
5. Verify `/lib/firmware/bsp/Packages.gz` and `/lib/firmware/bsp/Packages.asc` exist (GPG-signed manifest).

### Phase 5 — opkg refresh

1. `rm -fr /var/lib/opkg/lists/*`.
2. `opkg update` — must emit `"Updated source 'bsp'"` (this confirms the GPG signature verified).
3. `tektelic-dist-upgrade -c` — must list packages to update. If it still reports `No BSP upgrade available`, the feed did not load (abort).

### Phase 6 — Go/No-Go gate

Displays: current → target, BSP size, free space, expected duration (10–20 min), impact (1–3 reboots, LoRaWAN traffic interruption). Requires operator to type literal `GO` unless `--yes` is passed (for automated rollouts with CI/CD).

### Phase 7 — Upgrade invocation

`tektelic-dist-upgrade -Du` (daemon mode: survives SSH disconnect). Script captures initial stdout then closes the SSH channel.

### Phase 8 — Monitor (reconnect loop)

- Every 20 s, opens a fresh SSH session (tolerant to mid-upgrade reboots).
- Reads `tektelic-dist-upgrade -p` (0–100) and `-s` (ok/error).
- Tails the live log `/var/log/tektelic-dist-upgrade-*.log` (last 40 lines) and scans for known failure signatures:
  - `"Unrecoverable Opkg failure"`
  - `"BSP upgrade failed"` / `"Failed to upgrade BSP"`
  - `"ubimkvol: error"`
  - `"preinst script returned status 1"`
  - `"Aborting installation"`
  - `"Restoring from the latest backup"` (= auto-rollback triggered)
  - `"UBI device does not have free"`
- Timeout 40 min. Success = progress returns to 0 with status `ok` after having been >0.

### Phase 9 — Post-verify

| Check | Command | Pass condition |
|---|---|---|
| BSP version file | `cat /etc/tektelic-versions/tektelic-bsp-version` | first line contains target |
| system_version | `system_version \| grep Release` | Release: `<target>` |
| Upgrader idle | `tektelic-dist-upgrade -s` and `-p` | `ok` and `0` |
| MQTT bridge running | `pgrep -f tek_mqtt_bridge` | PID returned |
| NS link | `netstat -tn \| grep 8883` | ESTABLISHED |
| MQTT bridge stability (KGW-2547 validation) | watch PID for 2 min | PID unchanged (no self-restart loop) |

### Phase 10 — Reporting

Full log archived at `references/homelab/lorawan-qa/upgrades/YYYY-MM-DD_HHMMSS_<gw>_to_<target>.log`. Contains every command, stdout, stderr, timing. Can be attached to the customer change-management ticket as evidence.

---

## 6. Recovery / rollback

### 6.1 Automatic rollback (built into tektelic-dist-upgrade)

If any opkg transaction fails during the upgrade, the tool automatically restores from `/backup/000/` (the pre-install snapshot taken at the start of Phase 7). This restores the gateway to its previous BSP (the one it had before we ran the upgrade). The script's Phase 8 detects the "Restoring from the latest backup" log signature and reports failure.

### 6.2 Manual rollback

If automatic rollback fails and the gateway is in an inconsistent state:

1. SSH to the gateway.
2. `tektelic-dist-upgrade -r` (resume from last known state if interrupted), or
3. `tektelic-dist-upgrade -s` to read last operation status.
4. If the restore did not complete, bring the gateway to Tektelic RMA channel (ticket via https://supporthub.tektelic.com/tickets-view).

### 6.3 Data preservation

- LoRaWAN device activations (DevEUI, keys, counters) are stored on the NS, not on the gateway. Gateway reboots / upgrades do not affect this data.
- Gateway-local configuration (hostname, NTP, MQTT credentials) is preserved across BSP upgrades by the `/var/lib/tektelic-dist-upgrade/fixup.d/` scripts.
- `/etc/opkg/snmpManaged-feed.conf` is preserved.

---

## 7. Validation on reference gateway (home Kona Micro)

Pre-flight + risk assessment validated via `--dry-run` on Kona Micro at <gateway-ip> (BSP 7.1.12.1):

```
[OK  ] SSH + root: Linux kona-micro-00A511 5.10.223-tektelic0.2-yocto-standard
[OK  ] Current BSP version: '7.1.12.1'
[OK  ] Upgrade path 7.1.12.1 -> 7.1.16.3 on Kona micro: direct upgrade within major 7.x
[OK  ] tektelic-dist-upgrade status: 'ok'
[OK  ] no upgrade in progress: progress=0
[OK  ] no scheduled upgrade: request time: 0
[OK  ] no read-only mounts: (clean)
[OK  ] no 'corrupt' in syslog: count=0
[OK  ] rootfs free >= 140MB: 161MB free
[OK  ] NTP synchronized: synced peers=1
[OK  ] MQTT 8883 ESTABLISHED (not blocking)
[OK  ] /lib/firmware/ clean: (only opkg/)
[OK  ] no upgrade lock: none

[RISK] /backup/000/ = 184MB - WILL BLOCK upgrade via 'ubimkvol: no free logical eraseblocks'
       Auto-cleanup needed (see phase 3)
[OK]   user 'admin' does not exist
[INFO] /home/admin/ is orphan (no user owns it) - will be removed
[INFO] opkg lists cached - will be cleared
```

The risk that triggered the reported production failure on the affected gateway (`/backup/000/` = 184 MB) is present and detected. Phase 3 will remediate it automatically before Phase 4 starts.

---

## 8. References

- Tektelic Kona release notes v7.1.16.3 (2025-11-28, GIT revision 2e261c53) — `references/homelab/lorawan-qa/bsp/BSP_7.1.16.3_release-notes.pdf`
- internal onboarding training — §Gateway Onboarding / BSP Upgrade Procedure (slides 30–58; core upgrade steps: slides 44–52; standard credentials / userdel admin: slide 53)
- Internal support ticket (2026-04-09) re: 7.1.12.1→7.1.16.3 failure — UBI eraseblocks
- KGW-2547 (7.1.9): *Improve data plane restart logic in TGB* — addresses MQTT bridge auto-recovery
- KGW-3061 (7.1.5.1): *tektelic-add-users not installed during upgrade*
- Source: `references/homelab/lorawan-qa/kona_upgrade.py`
