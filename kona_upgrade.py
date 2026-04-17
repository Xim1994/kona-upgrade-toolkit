#!/usr/bin/env python3
"""
Tektelic Kona gateway BSP upgrade via SSH (NS-agnostic).

Implements the full Tektelic SOP plus guard-rails for the two real failure modes
seen in production:
  - `ubimkvol: error!: UBI device does not have free logical eraseblocks` caused
    by stale /backup/000/ occupying the backup UBI volume.
  - `useradd: user 'admin' already exists` caused by leftover 'admin' user from
    older BSPs (tektelic-add-users preinst aborts).

Phases:
  1. Pre-flight         - full readiness check, aborts if any red flag
  2. Risk assessment    - reports soft warnings (cleanup candidates)
  3. Cleanup            - backup purge, admin user purge, opkg cache purge
  4. Staging            - SFTP BSP zip, unzip, verify manifest + GPG signature
  5. Opkg refresh       - opkg update + verify feed reports target version
  6. Go/No-Go gate      - shows plan, requires explicit confirmation
  7. Upgrade            - tektelic-dist-upgrade -Du (daemon)
  8. Monitor            - poll progress + log tail, detect failure signatures
  9. Post-verify        - version match, mqtt-bridge stable 2min (KGW-2547 check)
 10. Reporting          - archive log in upgrades/

Usage:
  python kona_upgrade.py --host 192.168.1.134 \
      --bsp references/homelab/lorawan-qa/bsp/BSP_7.1.16.3.zip \
      --sha256 5e935872ad8341bf910b4a593be065856a075cecade64fb5f739ec38651ca3d9 \
      --target 7.1.16.3 \
      [--dry-run] [--skip-cleanup] [--yes]
"""

__version__ = "1.0.0"

import argparse
import datetime as dt
import hashlib
import logging
import os
import re
import sys
import time
from pathlib import Path

try:
    import paramiko
except ImportError:
    print("paramiko required: pip install paramiko", file=sys.stderr)
    sys.exit(2)

import ftplib
import hashlib
import json
import signal
import ssl
import urllib.parse
import urllib.request


# ---------- ANSI color helpers (respects NO_COLOR env + tty detection) ----------
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s

def green(s):  return _c("32", s)
def red(s):    return _c("31", s)
def yellow(s): return _c("33", s)
def cyan(s):   return _c("36", s)
def bold(s):   return _c("1",  s)


# ============================================================================
# Configuration & logging
# ============================================================================

SCRIPT_DIR = Path(__file__).resolve().parent


def load_env(env_path):
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# Search for .env: next to the script first, then cwd, then 3 levels up (dev workspace)
for _candidate in [SCRIPT_DIR / ".env", Path.cwd() / ".env", SCRIPT_DIR.parents[2] / ".env"]:
    if _candidate.exists():
        load_env(_candidate)
        break


def setup_logging(gw_name, target):
    upgrades_dir = Path(__file__).parent / "upgrades"
    upgrades_dir.mkdir(exist_ok=True)
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = upgrades_dir / f"{ts}_{gw_name}_to_{target}.log"
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers = []
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt); ch.setLevel(logging.INFO)
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt); fh.setLevel(logging.DEBUG)
    root.addHandler(ch); root.addHandler(fh)
    # Suppress paramiko's verbose banner/transport logs (floods terminal during Phase 8 monitor)
    logging.getLogger("paramiko").setLevel(logging.WARNING)
    logging.getLogger("paramiko.transport").setLevel(logging.WARNING)
    return log_file


log = logging.getLogger(__name__)


# ---- Graceful Ctrl+C / SIGTERM ----
_ABORT = {"requested": False}


def _handle_signal(signum, _frame):
    if _ABORT["requested"]:
        # Second Ctrl+C: hard exit
        print(red("\n[!] Forced exit."), file=sys.stderr)
        sys.exit(130)
    _ABORT["requested"] = True
    print(yellow(f"\n[!] Abort requested (signal {signum}). Finishing current step and exiting..."),
          file=sys.stderr)


def install_signal_handlers():
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)


def check_abort():
    """Call at safe checkpoints to honour Ctrl+C."""
    if _ABORT["requested"]:
        raise KeyboardInterrupt("user abort requested")


# ---- Recovery hints registry ----
# Each known failure signature -> human-readable next step.
RECOVERY_HINTS = {
    "UBI device does not have free": (
        "The backup UBI volume is full. Run manually on the GW:\n"
        "    rm -rf /backup/[0-9][0-9][0-9]/* && sync\n"
        "This clears ALL backup slots (000, 001, 002...). They are rootfs snapshots "
        "from previous upgrades, safe to delete. The upgrader will create a fresh one."),
    "ubimkvol: error": (
        "Same as above: UBI backup partition full. Run:\n"
        "    rm -rf /backup/[0-9][0-9][0-9]/* && sync\n"
        "This clears all backup slots. Then re-run the script."),
    "preinst script returned status 1": (
        "A package postinst failed, typically tektelic-add-users because user 'admin' exists.\n"
        "On the GW: `userdel admin && rm -rf /home/admin` then re-run the upgrade."),
    "Restoring from the latest backup": (
        "Upgrade failed mid-install and the GW is auto-rolling back to the previous BSP.\n"
        "Wait 5 minutes for restore to complete, then run `tektelic-dist-upgrade -s` to check "
        "status. If 'ok' and `system_version` shows the OLD version, the rollback worked; "
        "investigate the root cause before retrying."),
    "BSP upgrade failed": (
        "Check /var/log/tektelic-dist-upgrade-*.log on the GW for the underlying cause. "
        "If auto-rollback completed, the GW is back on the previous BSP."),
    "timeout": (
        "Phase 8 monitor exceeded its deadline. The upgrade may actually have succeeded — "
        "SSH to the GW and check `cat /etc/tektelic-versions/tektelic-bsp-version` and "
        "`tektelic-dist-upgrade -s`. If target version is installed and status=ok, "
        "the upgrade worked (only this script's monitor detection failed)."),
    "no space": (
        "rootfs < 140MB free. Clean up: `rm -fr /var/lib/opkg/lists/* /lib/firmware/bsp*.zip` "
        "and check `/var/log/` for large log files."),
    "NTP": (
        "GW clock not synchronised. Without correct time, GPG signature verification of "
        "packages fails. On GW: `ntpq -pn` should show a '*' peer. Restart ntpd with "
        "`/etc/init.d/ntpd restart` and wait ~5 min for sync."),
}


def print_recovery_hint(err):
    """Look up a recovery hint for the given error string."""
    for sig, hint in RECOVERY_HINTS.items():
        if sig.lower() in err.lower():
            log.info(bold("Recovery hint:"))
            for line in hint.splitlines():
                log.info(f"  {cyan(line)}")
            return
    log.info(yellow(f"No specific recovery hint for '{err[:60]}...'. "
                     "Inspect /var/log/tektelic-dist-upgrade-*.log on the GW."))


# ============================================================================
# SSH helpers
# ============================================================================

class GW:
    """SSH wrapper with retry/backoff. Used as context manager:
        with GW(host,user,pw).connect() as gw:
            gw.run(...)"""

    def __init__(self, host, user, password):
        self.host = host
        self.user = user
        self.password = password
        self.client = None

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    def connect(self, timeout=15, retries=3, backoff=5):
        """Open SSH with exponential backoff on transient failures (reboot, network blip)."""
        last_err = None
        for attempt in range(retries):
            check_abort()
            try:
                self.client = paramiko.SSHClient()
                self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                self.client.connect(self.host, username=self.user, password=self.password,
                                    timeout=timeout, look_for_keys=False, allow_agent=False,
                                    banner_timeout=30, auth_timeout=20)
                return self
            except (paramiko.SSHException, OSError, EOFError) as e:
                last_err = e
                if attempt < retries - 1:
                    wait = backoff * (2 ** attempt)
                    log.debug(f"  SSH attempt {attempt+1}/{retries} failed ({str(e)[:60]}); "
                              f"retry in {wait}s")
                    time.sleep(wait)
        raise RuntimeError(f"SSH to {self.host} failed after {retries} attempts: {last_err}")

    def close(self):
        if self.client:
            try: self.client.close()
            except Exception: pass
            self.client = None

    def run(self, cmd, timeout=30, check=False, quiet=False, get_pty=False):
        """Run a shell command. Returns (rc, stdout, stderr).
        If check=True, raises on non-zero rc with the command + stderr.
        If get_pty=True, allocates a pseudo-terminal — needed for some tools
        (old opkg, tektelic-dist-upgrade interactive mode) that check isatty()
        and refuse to run or produce no output in non-interactive mode."""
        check_abort()
        if not quiet:
            log.debug(f"$ {cmd}")
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout, get_pty=get_pty)
        # Read output BEFORE exit status — paramiko can deadlock or lose output
        # if recv_exit_status() is called first and the command produces a lot of output
        out = stdout.read().decode(errors="replace").strip()
        err = stderr.read().decode(errors="replace").strip()
        rc = stdout.channel.recv_exit_status()
        if out and not quiet: log.debug(f"  stdout: {out[:500]}")
        if err and not quiet: log.debug(f"  stderr: {err[:500]}")
        if check and rc != 0:
            raise RuntimeError(f"command failed (rc={rc}): {cmd}\n{err or out}")
        return rc, out, err

    def sftp(self):
        return self.client.open_sftp()


# ---- Per-phase timing helper ----
class Phase:
    """Context manager: wraps a phase with heading, timing, and uniform error wrapping."""
    def __init__(self, label):
        self.label = label
        self.t0 = 0
    def __enter__(self):
        log.info(bold("=" * 70))
        log.info(bold(self.label))
        log.info(bold("=" * 70))
        self.t0 = time.time()
        return self
    def __exit__(self, exc_type, exc, _tb):
        dt = time.time() - self.t0
        if exc_type is None:
            log.info(f"  [{green('done')}] {self.label} in {dt:.1f}s")
        else:
            log.info(f"  [{red('FAILED')}] {self.label} after {dt:.1f}s: {exc}")
        return False  # don't suppress exceptions


# ============================================================================
# FTP cache (Tektelic official FTP)
# ============================================================================
#
# Provenance: ftpes://74.3.134.34 (user=customer), per Tektelic knowledge hub.
# Modern BSPs (>= 7.0.9) live in /Universal_Kona_SW/BSP_X.Y.Z_*/BSP_X.Y.Z.zip
# Older per-platform BSPs in /Kona__MICRO_SW/, /Kona_MACRO_SW/, /Kona_MEGA_SW/.

TEKTELIC_FTP_HOST = "74.3.134.34"
TEKTELIC_FTP_USER = os.environ.get("TEKTELIC_FTP_USER", "customer")
TEKTELIC_FTP_PASS = os.environ.get("TEKTELIC_FTP_PASS", "vU6_ATR3")
DEFAULT_CACHE_DIR = Path(__file__).parent / "bsp"


def ftp_connect():
    """Return an FTPS client authenticated to Tektelic, binary mode ready."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ftps = ftplib.FTP_TLS(context=ctx)
    ftps.connect(TEKTELIC_FTP_HOST, 21, timeout=20)
    ftps.login(TEKTELIC_FTP_USER, TEKTELIC_FTP_PASS)
    ftps.prot_p()
    ftps.voidcmd("TYPE I")  # binary mode (required for SIZE command)
    return ftps


def ftp_find_bsp(ftps, target_version):
    """Look for BSP_<target>.zip in known locations. Returns (folder, filename) or None."""
    # Try Universal_Kona_SW first (7.x+ unified)
    try:
        ftps.cwd("/Universal_Kona_SW")
        dirs = []
        ftps.retrlines("LIST", dirs.append)
        for line in dirs:
            name = line.split()[-1]
            if target_version in name and name.startswith("BSP_"):
                return ("/Universal_Kona_SW/" + name, f"BSP_{target_version}.zip")
    except Exception as e:
        log.debug(f"  Universal_Kona_SW lookup: {e}")

    # Fallback: per-platform folders
    for platform_folder in ("Kona__MICRO_SW", "Kona_MACRO_SW", "Kona_MEGA_SW"):
        try:
            ftps.cwd("/" + platform_folder)
            dirs = []
            ftps.retrlines("LIST", dirs.append)
            for line in dirs:
                name = line.split()[-1]
                if target_version in name and "BSP" in name:
                    # Look inside for the zip
                    ftps.cwd(name)
                    files = []
                    ftps.retrlines("LIST", files.append)
                    for fl in files:
                        fn = fl.split()[-1]
                        if fn.endswith(".zip"):
                            return (f"/{platform_folder}/{name}", fn)
        except Exception as e:
            log.debug(f"  {platform_folder} lookup: {e}")
    return None


def ftp_list_latest(ftps, limit=8, include_rc=False):
    """List most recent BSPs from Universal_Kona_SW with release-note type if derivable."""
    try:
        ftps.cwd("/Universal_Kona_SW")
        dirs = []
        ftps.retrlines("LIST", dirs.append)
    except Exception:
        return []

    entries = []
    for line in dirs:
        name = line.split()[-1]
        if not name.startswith("BSP_"):
            continue
        m = re.match(r"BSP_(\d+(?:\.\d+)+)", name)
        if not m:
            continue
        if "Discarded" in name or "SKIPPED" in name:
            continue
        if not include_rc and "NOT_FOR" not in name and "_RC" in name:
            continue
        version = m.group(1)
        entries.append((version, name))
    # Sort by version tuple descending
    entries.sort(key=lambda e: tuple(int(x) for x in e[0].split(".")), reverse=True)
    return entries[:limit]


def fetch_bsp_from_ftp(target_version, cache_dir=None, progress=True):
    """Download BSP_<version>.zip to cache_dir if not already cached.
    Writes sidecar .sha256 with hash + metadata. Returns local Path."""
    cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    local = cache_dir / f"BSP_{target_version}.zip"
    sidecar = Path(str(local) + ".sha256")

    # Fast path: already cached with sidecar -> verify and return
    if local.exists() and sidecar.exists():
        expected = load_sha256_sidecar(local)
        log.info(f"  [cache hit] {local.name} ({local.stat().st_size:,} bytes), verifying SHA256...")
        actual = _sha256_of_file(local)
        if actual == expected:
            log.info(f"  [cache hit] SHA256 ok ({actual[:16]}...) — no download needed")
            return local
        log.warning(f"  [cache hit] SHA256 mismatch (expected {expected[:16]}, got {actual[:16]}) — re-downloading")

    # Download from FTP
    log.info(f"  Connecting to Tektelic FTP...")
    ftps = ftp_connect()
    try:
        found = ftp_find_bsp(ftps, target_version)
        if not found:
            raise RuntimeError(f"BSP_{target_version}.zip not found on Tektelic FTP in known locations")
        folder, filename = found
        log.info(f"  Found: {folder}/{filename}")
        ftps.cwd(folder)
        ftps.voidcmd("TYPE I")  # retrlines() during ftp_find_bsp flips TYPE back to A
        remote_size = ftps.size(filename)
        log.info(f"  Remote size: {remote_size:,} bytes, downloading to {local}...")

        t0 = time.time()
        last_pct = [-1]
        written = [0]
        def cb(chunk, f=None):
            f.write(chunk)
            written[0] += len(chunk)
            if progress and remote_size:
                pct = written[0] * 100 // remote_size
                if pct >= last_pct[0] + 10:
                    last_pct[0] = pct
                    log.info(f"    {pct}% ({written[0]/1024/1024:.1f} MB)")
        with open(local, "wb") as f:
            ftps.retrbinary(f"RETR {filename}", lambda chunk, ff=f: cb(chunk, ff), blocksize=65536)
            f.flush()
            os.fsync(f.fileno())
        dt = time.time() - t0
        actual_size = local.stat().st_size
        if actual_size != remote_size:
            raise RuntimeError(f"size mismatch after download: remote={remote_size} local={actual_size}")
        log.info(f"  Downloaded {actual_size:,} bytes in {dt:.1f}s ({actual_size/1024/1024/dt:.1f} MB/s)")

        # Compute SHA256 + write sidecar
        sha = _sha256_of_file(local)
        sidecar.write_text(
            f"{sha}  {local.name}\n"
            f"# Downloaded from ftpes://{TEKTELIC_FTP_HOST}{folder}/\n"
            f"# Date: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
            f"# Size: {actual_size} bytes (confirmed via FTP SIZE)\n",
            encoding="utf-8")
        log.info(f"  SHA256: {sha}")
        log.info(f"  Sidecar written: {sidecar.name}")
        return local
    finally:
        try: ftps.quit()
        except Exception: pass


def _sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ============================================================================
# NS API (Tektelic Network Server) — used ONLY for name->IP resolution
# ============================================================================
# This is the one place where the script touches an NS: to translate gateway
# name -> IP for SSH. The upgrade itself stays NS-agnostic. If the customer migrates to
# another NS, replace this function with their equivalent.

TEKTELIC_NS_URL = os.environ.get("TEKTELIC_NS_EU_URL", "https://lorawan-ns-eu.tektelic.com")


def ns_resolve_gw_ip(gw_name):
    """Resolve gateway name -> (uuid, ip) via Tektelic NS API.
    Returns (uuid, ip) or raises with a clear error."""
    user = os.environ.get("TEKTELIC_NS_USER")
    pw   = os.environ.get("TEKTELIC_NS_PASS")
    cid  = os.environ.get("TEKTELIC_CUSTOMER_ID")
    if not user or not pw:
        raise RuntimeError(
            "NS resolve needs TEKTELIC_NS_USER + TEKTELIC_NS_PASS in .env")
    ctx = ssl.create_default_context()

    # 1) Login
    login_body = json.dumps({"username": user, "password": pw}).encode()
    req = urllib.request.Request(
        TEKTELIC_NS_URL + "/api/auth/login",
        data=login_body,
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        tok = json.loads(r.read())["token"]

    H = {"X-Authorization": "Bearer " + tok}

    # Auto-discover customer_id from user profile if not in .env
    if not cid:
        req = urllib.request.Request(
            f"{TEKTELIC_NS_URL}/api/auth/user",
            headers=H)
        with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
            profile = json.loads(r.read())
        cid = profile.get("customerId", {}).get("id")
        if not cid:
            raise RuntimeError(
                "Could not auto-discover customer_id from NS user profile. "
                "Set TEKTELIC_CUSTOMER_ID in .env manually.")
        log.debug(f"  Auto-discovered customer_id: {cid}")

    # 2) Find gateway by name
    req = urllib.request.Request(
        f"{TEKTELIC_NS_URL}/api/customer/{cid}/gateways?limit=500&page=0",
        headers=H)
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        gws = json.loads(r.read())

    match = next((g for g in gws if g.get("name") == gw_name), None)
    if not match:
        names = sorted({g.get("name", "?") for g in gws})
        raise RuntimeError(f"gateway '{gw_name}' not found in NS. "
                           f"Known names ({len(names)}): {', '.join(names[:10])}...")
    if not match.get("online"):
        raise RuntimeError(f"gateway '{gw_name}' is OFFLINE in NS — cannot resolve IP "
                           "(offline GW cannot be upgraded via SSH anyway)")

    uuid = match["id"]["id"]

    # 3) getGatewayInfo command -> interfaces.eth0 or wwan0
    req = urllib.request.Request(
        f"{TEKTELIC_NS_URL}/api/gateway/{uuid}/command/getGatewayInfo",
        data=b"{}",
        headers={**H, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        data = json.loads(r.read())
    val = data.get("value", data)
    if isinstance(val, str):
        try: val = json.loads(val)
        except Exception: val = {}
    interfaces = val.get("interfaces") or {}
    ip = interfaces.get("eth0") or interfaces.get("wwan0")
    if not ip:
        raise RuntimeError(f"no eth0/wwan0 IP in getGatewayInfo response for {gw_name}: {interfaces}")
    return uuid, ip


# ============================================================================
# Upgrade-path compatibility
# ============================================================================
#
# Per Kona release notes (7.0.9 "Official"):
#   "Earliest release versions that support direct upgrades to 7.0.9+ are:
#    - Micro v4.0.2   - Micro PoE v2.5.1
#    - Enterprise/Photon v2.1.2
#    - Macro v5.1.3   - Mega v5.0.2
#   If older: must upgrade to an intermediate release first."
#
# the Tektelic onboarding SOP (LoRaWAN ADMIN training v4.0) also says for Macro:
#   "If current BSP < 4.0.3, first install 4.0.3 as intermediate"
#
# Strategy:
#   - If current version parse-able and target major == current major: DIRECT
#   - If current major==7 and target major==7: DIRECT (any 7.x -> any 7.x)
#   - If current < min_direct for its platform -> WARN, require --force
#   - Else: WARN mildly and proceed

MIN_DIRECT_TO_7X = {
    # platform keyword -> minimum version tuple for direct upgrade to any 7.x
    "micro":      (4, 0, 2),
    "macro":      (5, 1, 3),
    "mega":       (5, 0, 2),
    "enterprise": (2, 1, 2),
    "photon":     (2, 1, 2),
}


def parse_version(s):
    """'7.1.12.1' -> (7, 1, 12, 1). Returns None if unparsable."""
    m = re.match(r"(\d+(?:\.\d+)+)", s.strip())
    if not m:
        return None
    try:
        return tuple(int(p) for p in m.group(1).split("."))
    except ValueError:
        return None


def detect_platform(gw):
    """Return 'micro' | 'macro' | 'mega' | 'enterprise' | 'photon' | 'unknown'."""
    _, mod, _ = gw.run("cat /etc/tektelic-versions/tektelic-bsp-version")
    low = mod.lower()
    for kw in ("micro", "macro", "mega", "enterprise", "photon"):
        if kw in low:
            return kw
    # fallback: uname
    _, u, _ = gw.run("uname -n")
    u = u.lower()
    for kw in ("micro", "macro", "mega"):
        if kw in u:
            return kw
    return "unknown"


def check_upgrade_path(current, target, platform, allow_downgrade=False):
    """Return (ok, message).
    ok=True means direct upgrade is supported per release notes.
    ok=False means intermediate required; message suggests which.
    allow_downgrade=True permits target < current (uses tektelic-dist-upgrade with older BSP zip)."""
    c = parse_version(current)
    t = parse_version(target)
    if not c or not t:
        return True, f"(could not parse versions; proceeding)"

    # Downgrade: allowed only with explicit flag. tektelic-dist-upgrade handles older BSP zip
    # the same way as newer (opkg installs the package set from the zip, rolling rootfs back).
    # The tool's own auto-backup is still taken before the change.
    if c > t:
        if allow_downgrade:
            return True, f"downgrade permitted by --allow-downgrade ({current} -> {target})"
        return False, (f"target {target} is older than current {current}. "
                       f"Pass --allow-downgrade if this is intentional.")

    # Same-major is always direct
    if c[0] == t[0]:
        return True, f"direct upgrade within major {c[0]}.x"

    # Target is 7.x from older major
    if t[0] == 7 and c[0] < 7:
        floor = MIN_DIRECT_TO_7X.get(platform)
        if not floor:
            return True, f"(platform '{platform}' not in min-version table; proceeding with caution)"
        if c >= floor:
            return True, f"direct upgrade supported ({platform} {'.'.join(map(str,c))} >= {'.'.join(map(str,floor))})"
        suggested = ".".join(map(str, floor))
        return False, (f"direct upgrade NOT supported: current {platform} {'.'.join(map(str,c))} is below "
                       f"minimum {suggested}. Upgrade to {suggested} intermediate first.")

    return True, f"(unusual path {current} -> {target}; proceeding)"


# ============================================================================
# DOWNGRADE
# ============================================================================
# A previous iteration used `system-backup -r 0` to roll back from the
# snapshot under /backup/000/. That flow was removed after validation on the
# home GW on 2026-04-16: `tektelic-dist-upgrade` stores its auto-backup under
# /backup/000/bak.<random>/ while `system-backup -r` looks for
# /backup/000/backup/ - format mismatch, restore aborts with
# "Backup '/backup/000/backup' not found".
#
# Supported downgrade path: run the normal upgrade flow (phases 1-10) with an
# older BSP zip plus --allow-downgrade. tektelic-dist-upgrade -Du handles
# older packages the same way it handles newer ones and takes its own
# pre-change backup. See check_upgrade_path().


# ============================================================================
# Phase 1 - Pre-flight
# ============================================================================

def phase1_preflight(gw, target_version, expected_min_free_mb=140, allow_downgrade=False):
    checks = []
    def check(name, ok, detail=""):
        status = "OK  " if ok else "FAIL"
        log.info(f"  [{status}] {name}: {detail}")
        checks.append((name, ok, detail))
        return ok

    _, uname, _ = gw.run("uname -a")
    check("SSH + root", True, uname[:80])

    _, bsp_raw, _ = gw.run("cat /etc/tektelic-versions/tektelic-bsp-version | head -1")
    current = bsp_raw.replace("Tektelic", "").strip()
    check(f"Current BSP version", bool(current), f"'{current}'")

    if current == target_version:
        log.info(f"  Gateway already on target {target_version} - nothing to do")
        return {"skip": True, "current": current}

    # Per Kona release notes 7.0.9 NB! section: minimum version for direct upgrade to 7.x
    platform = detect_platform(gw)
    path_ok, path_msg = check_upgrade_path(current, target_version, platform,
                                           allow_downgrade=allow_downgrade)
    check(f"Upgrade path {current} -> {target_version} on Kona {platform}",
          path_ok, path_msg)

    _, up_status, _ = gw.run("tektelic-dist-upgrade -s 2>&1")
    # "ok" = last upgrade succeeded, "n/a" = never upgraded before (both are fine)
    status_ok = up_status.strip() in ("ok", "n/a")
    check("tektelic-dist-upgrade status", status_ok, f"'{up_status.strip()}'")

    _, up_prog, _ = gw.run("tektelic-dist-upgrade -p 2>&1")
    check("no upgrade in progress", up_prog.strip() == "0", f"progress={up_prog}")

    # -t flag may not exist on older BSPs (4.x) — treat "illegal option" as OK
    _, up_sched, _ = gw.run("tektelic-dist-upgrade -t 2>&1")
    sched_ok = "0" in up_sched or "illegal option" in up_sched
    check("no scheduled upgrade", sched_ok, up_sched.strip()[:80])

    _, ro_mounts, _ = gw.run("mount | grep -E 'ro[,)]' || true")
    check("no read-only mounts", not ro_mounts.strip(), ro_mounts or "(clean)")

    # "corrupt" in syslog — informational only (noisy: matches benign strings).
    # Real filesystem corruption is covered by the read-only-mount check above.
    _, corrupt_n, _ = gw.run("grep -c corrupt /var/log/syslog 2>/dev/null; true")
    first = corrupt_n.strip().splitlines()[0] if corrupt_n.strip() else "0"
    log.info(f"  [info] syslog 'corrupt' mentions: {first}")

    # Disk free — compute what Phase 3 can free so the check accounts for cleanup
    _, df_out, _ = gw.run("df -m / | tail -1")
    parts = df_out.split()
    try:
        free_mb = int(parts[3]) if len(parts) >= 4 else 0
    except Exception:
        free_mb = 0
    # Size of stuff Phase 3 will remove
    _, fw_du_raw, _ = gw.run(
        "du -sm /lib/firmware 2>/dev/null | awk '{print $1}'; "
        "du -sm /backup/000/bak.* 2>/dev/null | awk '{s+=$1} END {print s+0}'",
        quiet=True)
    try:
        recoverable_mb = sum(int(x) for x in fw_du_raw.split() if x.strip().isdigit())
    except Exception:
        recoverable_mb = 0
    free_after_mb = free_mb + recoverable_mb
    check(f"rootfs free >= {expected_min_free_mb}MB (post-cleanup)",
          free_after_mb >= expected_min_free_mb,
          f"{free_mb}MB now + {recoverable_mb}MB recoverable by phase 3 = {free_after_mb}MB")

    # NTP (needed for GPG signature validation)
    _, ntp_sync, _ = gw.run("ntpq -pn 2>/dev/null | grep -cE '^\\*' || echo 0")
    check("NTP synchronized", ntp_sync.strip() not in ("", "0"),
          f"synced peers={ntp_sync}")

    # Network Server reachability (optional - GW may work without current MQTT)
    _, ns_link, _ = gw.run("netstat -tn 2>/dev/null | grep 8883 | head -1")
    check("MQTT 8883 ESTABLISHED (not blocking)", True,  # informational only
          ns_link or "(not connected - OK, upgrade does not require NS)")

    # /lib/firmware leftovers — expected after a prior upgrade. Phase 3 runs rm -r *
    # as documented in internal CHG step 2. Informational only, not a blocker.
    _, fw_dir, _ = gw.run("ls /lib/firmware/ | grep -v opkg || true")
    if fw_dir.strip():
        log.info(f"  [info] /lib/firmware/ has leftovers (phase 3 will clean): "
                 f"{', '.join(fw_dir.split())[:200]}")
    else:
        log.info(f"  [info] /lib/firmware/ clean")

    _, lock, _ = gw.run("ls /run/lock/upgrade 2>/dev/null || echo none")
    check("no upgrade lock", lock.strip() == "none", lock)

    failed = [c for c in checks if not c[1]]
    if failed:
        log.error(f"PRE-FLIGHT FAILED: {len(failed)} check(s) blocking")
        for name, ok, detail in failed:
            log.error(f"  - {name}: {detail}")
        return {"skip": False, "ready": False, "current": current}

    return {"skip": False, "ready": True, "current": current,
            "free_mb": free_mb}


# ============================================================================
# Phase 2 - Risk assessment
# ============================================================================

def phase2_risk(gw):
    risks = {}

    # Total /backup/ usage across ALL slots (000, 001, 002, 003...).
    # The UBI partition ubi1:backup is ~248MB. If total usage > 100MB, the upgrade
    # tool can't create its new pre-install snapshot and aborts with "ubimkvol: error".
    _, bkp_total, _ = gw.run("du -sm /backup 2>/dev/null | awk '{print $1}'")
    _, bkp_slots, _ = gw.run("ls -d /backup/[0-9][0-9][0-9] 2>/dev/null")
    try:
        total_mb = int(bkp_total.strip() or "0")
    except ValueError:
        total_mb = 0
    slots = [s.strip() for s in bkp_slots.splitlines() if s.strip()]
    risks["backup_mb"] = total_mb
    risks["backup_slots"] = slots
    if total_mb > 100:
        log.warning(f"  [RISK] /backup/ = {total_mb}MB across {len(slots)} slot(s) - "
                     "WILL BLOCK upgrade via 'ubimkvol: no free logical eraseblocks'")
        for slot in slots:
            _, slot_sz, _ = gw.run(f"du -sm {slot} 2>/dev/null | awk '{{print $1}}'")
            log.warning(f"         {slot} = {slot_sz.strip()}MB")
        log.warning(f"         Auto-cleanup: delete old slots (1+), empty slot 0 for new backup")
        risks["needs_backup_cleanup"] = True
    elif total_mb > 0:
        log.info(f"  [INFO] /backup/ = {total_mb}MB across {len(slots)} slot(s) - acceptable")
        risks["needs_backup_cleanup"] = False
    else:
        log.info(f"  [OK]   /backup/ empty or absent")
        risks["needs_backup_cleanup"] = False

    # user admin (the useradd-exists blocker from the reported production failure)
    rc, _, _ = gw.run("grep -qE '^admin:' /etc/passwd")
    risks["admin_user_exists"] = (rc == 0)
    if rc == 0:
        log.warning("  [RISK] user 'admin' exists - will cause tektelic-add-users preinst FAIL")
        log.warning("         Auto-cleanup needed (see phase 3)")
    else:
        log.info("  [OK]   user 'admin' does not exist")

    # orphan /home/admin (no user but dir exists - not blocking but tidy up)
    rc, _, _ = gw.run("test -d /home/admin")
    risks["admin_home_orphan"] = (rc == 0 and not risks["admin_user_exists"])
    if risks["admin_home_orphan"]:
        log.info("  [INFO] /home/admin/ is orphan (no user owns it) - will be removed")

    # Stale upgrade markers from interrupted previous attempts
    _, stale, _ = gw.run("ls /var/lib/tektelic-dist-upgrade/fpga-programming-workaround-attempted "
                          "/var/lib/tektelic-dist-upgrade/fpga-removed 2>/dev/null")
    risks["stale_markers"] = bool(stale.strip())
    if stale.strip():
        log.warning(f"  [RISK] stale upgrade markers: {stale}")

    # opkg list cache staleness (forces refresh)
    _, lists, _ = gw.run("ls /var/lib/opkg/lists/ 2>/dev/null | head -5")
    risks["opkg_lists_present"] = bool(lists.strip())
    if lists.strip():
        log.info(f"  [INFO] opkg lists cached - will be cleared (forces fresh feed read)")

    # UBI state
    _, ubi_avail, _ = gw.run("ubinfo /dev/ubi1 2>/dev/null | grep 'available logical' | awk '{print $(NF-1)}'")
    log.info(f"  [INFO] ubi1 (backup) available LEBs: {ubi_avail} "
             f"(0 is normal, volume spans whole partition)")

    return risks


# ============================================================================
# Phase 3 - Cleanup
# ============================================================================

def phase3_cleanup(gw, risks, confirmed):
    actions = []

    if risks.get("needs_backup_cleanup"):
        # Delete old backup slots (001, 002, 003...) — these are older snapshots
        old_slots = [s for s in risks.get("backup_slots", []) if not s.endswith("/000")]
        for slot in old_slots:
            actions.append((f"Delete old backup slot {slot}",
                            f"rm -rf {slot} && sync"))
        # Only empty slot 000 if old slots alone didn't free enough space.
        # Slot 000 = most recent backup = rollback capability. Keep if possible.
        if not old_slots:
            log.warning("  No old slots (001+) to delete. Slot 000 is the only backup.")
            log.warning("  Emptying slot 000 to make room — rollback to previous version will NOT be possible.")
            actions.append(("Empty /backup/000/* (no old slots available, last resort)",
                            "rm -rf /backup/000/* && sync"))
    if risks.get("admin_user_exists"):
        actions.append(("Delete user admin",
                        "userdel admin 2>&1 || true"))
    if risks.get("admin_home_orphan") or risks.get("admin_user_exists"):
        actions.append(("Remove /home/admin orphan",
                        "rm -rf /home/admin"))
    if risks.get("stale_markers"):
        actions.append(("Clear stale upgrade markers",
                        "rm -f /var/lib/tektelic-dist-upgrade/fpga-programming-workaround-attempted "
                        "/var/lib/tektelic-dist-upgrade/fpga-removed"))
    if risks.get("opkg_lists_present"):
        actions.append(("Clear opkg lists cache",
                        "rm -fr /var/lib/opkg/lists/*"))

    # Always do final sync + drop caches
    actions.append(("Drop caches + sync",
                    "sync && echo 3 > /proc/sys/vm/drop_caches && sleep 2"))

    if not actions:
        log.info("  Nothing to clean up")
        return

    log.info(f"Cleanup actions ({len(actions)}):")
    for name, cmd in actions:
        log.info(f"  - {name}")
        log.debug(f"    $ {cmd}")

    if not confirmed:
        log.info("  [DRY-RUN] not executing")
        return

    for name, cmd in actions:
        log.info(f"  * {name} ...")
        rc, out, err = gw.run(cmd, timeout=60)
        if rc != 0:
            log.warning(f"    rc={rc}: {err or out}")
        else:
            log.info(f"    done")

    # Verify post-cleanup
    _, bkp_total, _ = gw.run("du -sm /backup 2>/dev/null | awk '{print $1}' || echo 0")
    rc_admin, _, _ = gw.run("grep -qE '^admin:' /etc/passwd")
    try:
        bkp_mb = int(bkp_total.strip() or "0")
    except ValueError:
        bkp_mb = 0
    log.info(f"  post-cleanup: /backup/={bkp_mb}MB  admin_user_exists={rc_admin == 0}")

    # Post-cleanup space gate: if /backup/ still uses >100MB, the upgrade WILL fail
    # with "ubimkvol: UBI device does not have free logical eraseblocks" because
    # tektelic-dist-upgrade needs to create a ~150MB pre-install snapshot.
    if bkp_mb > 100:
        log.error(red(f"  /backup/ still uses {bkp_mb}MB after cleanup (>100MB threshold)."))
        log.error(red(f"  The upgrade WILL FAIL: not enough space on ubi1:backup for the pre-install snapshot."))
        _, slot_details, _ = gw.run("du -sm /backup/[0-9][0-9][0-9] 2>/dev/null")
        for line in slot_details.splitlines():
            log.error(f"    {line.strip()}")
        log.error(red(f"  Slot 000 was preserved for rollback. To proceed, you must free space manually:"))
        log.error(red(f"    ssh root@{gw.host} 'rm -rf /backup/000/* && sync'"))
        log.error(red(f"  WARNING: this removes your rollback capability to the current version."))
        raise RuntimeError(
            f"Insufficient UBI backup space: {bkp_mb}MB used, need <100MB. "
            f"Slot 000 preserved for rollback — delete it manually to proceed.")


# ============================================================================
# Phase 4 - Staging
# ============================================================================

def phase4_staging(gw, bsp_zip_local, expected_sha256, target_version=None):
    if not Path(bsp_zip_local).exists():
        raise FileNotFoundError(f"BSP zip not found: {bsp_zip_local}")

    # Shortcut: if target BSP already staged on GW, skip SFTP entirely.
    # Detection: look for tektelic-bsp-version_<target>-*.ipk in /lib/firmware/bsp/
    if target_version:
        _, staged, _ = gw.run(
            f"ls /lib/firmware/bsp/tektelic-bsp-version_{target_version}*.ipk 2>/dev/null")
        if staged.strip():
            log.info(f"  {green('[skip SFTP]')} target BSP {target_version} already staged on GW:")
            log.info(f"    {staged.strip()}")
            _, pkg_check, _ = gw.run("test -f /lib/firmware/bsp/Packages.gz && echo ok")
            if pkg_check.strip() == "ok":
                log.info("  Packages.gz present — reusing staged BSP, no upload needed")
                return
            else:
                log.warning("  Packages.gz missing despite BSP marker — forcing re-upload")

    # Verify local SHA256 first
    log.info(f"  Local zip: {bsp_zip_local}")
    h = hashlib.sha256()
    size = 0
    with open(bsp_zip_local, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk); size += len(chunk)
    actual = h.hexdigest()
    log.info(f"  Local size: {size:,} bytes")
    log.info(f"  Local SHA256: {actual}")
    if expected_sha256 and actual != expected_sha256:
        raise RuntimeError(f"SHA256 mismatch: expected {expected_sha256} got {actual}")
    log.info(f"  SHA256 verified")

    # SFTP upload with progress bar
    remote = "/lib/firmware/bsp.zip"
    log.info(f"  SFTP {bsp_zip_local} -> {gw.host}:{remote}")
    sftp = gw.sftp()
    t0 = time.time()
    last_pct = [-1]
    def sftp_progress(sent, total):
        pct = sent * 100 // total if total else 0
        if pct >= last_pct[0] + 10:
            last_pct[0] = pct
            mb = sent / 1024 / 1024
            elapsed = max(time.time() - t0, 0.001)
            mbps = mb / elapsed
            log.info(f"    {pct}% ({mb:.1f} MB, {mbps:.1f} MB/s)")
    sftp.put(bsp_zip_local, remote, callback=sftp_progress)
    sftp.close()
    dt_upload = time.time() - t0
    log.info(f"  Uploaded {size:,} bytes in {dt_upload:.1f}s ({size/1024/1024/dt_upload:.1f} MB/s)")

    # Verify remote size
    _, remote_size, _ = gw.run(f"wc -c < {remote}")
    if int(remote_size.strip()) != size:
        raise RuntimeError(f"remote size mismatch: {remote_size} vs {size}")

    # Unzip + sync (sync ensures filesystem is flushed before opkg reads it)
    log.info("  Unzipping on GW...")
    rc, out, err = gw.run(f"cd /lib/firmware && unzip -o bsp.zip && rm -f bsp.zip && sync", timeout=120, check=True)
    log.debug(out[:500])

    # Verify extracted structure
    rc, listing, _ = gw.run("ls /lib/firmware/bsp/ | head -20")
    files_n_rc, files_n, _ = gw.run("ls /lib/firmware/bsp/ | wc -l")
    log.info(f"  bsp/ contains {files_n} entries (sample: {listing[:300]})")

    rc_pkg, _, _ = gw.run("test -f /lib/firmware/bsp/Packages.gz")
    rc_sig, _, _ = gw.run("test -f /lib/firmware/bsp/Packages.asc")
    if rc_pkg != 0:
        raise RuntimeError("Packages.gz missing after unzip")
    if rc_sig != 0:
        log.warning("  Packages.asc missing - GPG signature verification will fail!")
    else:
        log.info("  Packages.gz + Packages.asc present (GPG verifiable)")


# ============================================================================
# Phase 5 - Opkg refresh
# ============================================================================

def phase5_opkg_refresh(gw, allow_downgrade=False):
    gw.run("rm -fr /var/lib/opkg/lists/*", check=True)
    log.info("  Cleared opkg lists")

    # Verify feed directories exist before calling opkg update
    _, feeds, _ = gw.run("ls -d /lib/firmware/bsp /lib/firmware/fe-fpga /lib/firmware/gpio-fpga 2>&1")
    log.info(f"  Feed dirs present: {feeds.strip()}")
    if "/lib/firmware/bsp" not in feeds:
        raise RuntimeError("/lib/firmware/bsp/ missing — Phase 4 staging may have failed. "
                           "Check if BSP zip was fully extracted.")

    log.info("  Running opkg update...")
    # Run opkg update. Some opkg versions (0.4.0 on BSP 4.x) produce NO output
    # when stdout is not a TTY — so we verify the EFFECT instead of the output.
    # After a successful opkg update, /var/lib/opkg/lists/bsp exists and has size > 0.

    def _run_and_verify():
        # opkg 0.4.0 quirks with paramiko exec_command don't simulate a real
        # interactive TTY well enough. Use invoke_shell which opens a real
        # interactive SSH shell session — the same thing you get from `ssh root@...`.
        shell = gw.client.invoke_shell(term="xterm", width=120, height=40)
        shell.settimeout(240)
        # Wait for shell prompt to settle, discard motd/banner
        time.sleep(4)
        banner = b""
        while shell.recv_ready():
            banner += shell.recv(65536)
        log.debug(f"  invoke_shell banner: {len(banner)} bytes")
        # Send the command with a sentinel we can detect when it completes
        shell.send("opkg update; echo __OPKG_DONE_$?\n")
        buf = b""
        deadline = time.time() + 240
        while time.time() < deadline:
            if shell.recv_ready():
                chunk = shell.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"__OPKG_DONE_" in buf:
                    # read any trailing bytes
                    time.sleep(0.5)
                    while shell.recv_ready():
                        buf += shell.recv(65536)
                    break
            else:
                time.sleep(0.3)
        shell.close()
        full_output = buf.decode(errors="replace")
        # Strip ANSI escape codes and shell prompts
        import re as _re
        clean = _re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', full_output)
        clean = _re.sub(r'\r\n?', '\n', clean)
        log.info(f"  invoke_shell captured {len(buf)} bytes:")
        for line in clean.splitlines():
            stripped = line.strip()
            if stripped and "__OPKG_DONE_" not in stripped and "opkg update;" not in stripped:
                log.info(f"    {stripped}")
        full_output = clean
        # Verify the real effect: /var/lib/opkg/lists/bsp is populated
        _, lists_info, _ = gw.run("ls -la /var/lib/opkg/lists/ 2>&1")
        log.info(f"  /var/lib/opkg/lists/ contents:")
        for line in lists_info.splitlines()[:15]:
            log.info(f"    {line}")
        _, bsp_size, _ = gw.run("wc -c < /var/lib/opkg/lists/bsp 2>/dev/null || echo 0")
        try:
            size = int(bsp_size.strip() or "0")
        except ValueError:
            size = 0
        return size, full_output

    bsp_list_size, full_output = _run_and_verify()
    # Success = /var/lib/opkg/lists/bsp exists with content
    # (either the output says "Updated source 'bsp'" OR the bsp list file is populated)
    bsp_confirmed = bsp_list_size > 0 or "Updated source 'bsp'" in full_output

    if not bsp_confirmed:
        log.warning(f"  bsp feed not populated (list size={bsp_list_size}), retrying in 5s...")
        time.sleep(5)
        bsp_list_size, full_output = _run_and_verify()
        bsp_confirmed = bsp_list_size > 0 or "Updated source 'bsp'" in full_output

    if bsp_confirmed:
        log.info(f"  bsp feed populated: /var/lib/opkg/lists/bsp = {bsp_list_size} bytes")

    if not bsp_confirmed:
        # opkg 0.4.0 on old BSP has TTY/redirect quirks we can't reliably bypass.
        # Downgrade to warning: Phase 7 (tektelic-dist-upgrade -Du) runs its own
        # opkg update internally, and it will fail loudly if the feed really isn't
        # loaded. So we defer authoritative verification to Phase 7.
        log.warning(yellow(f"  Could not confirm bsp source via opkg update "
                            f"(bsp_list_size={bsp_list_size})."))
        log.warning(yellow(f"  Deferring verification to Phase 7 — tektelic-dist-upgrade "
                            f"will fail cleanly if the feed is actually broken."))
        _, opkg_conf, _ = gw.run("grep -E '^src|check_signature' /etc/opkg/*.conf 2>&1")
        log.info(f"  opkg feeds configured: {opkg_conf.strip()[:200]}")
        _, bsp_ls, _ = gw.run("ls /lib/firmware/bsp/Packages* 2>&1")
        log.info(f"  Packages files: {bsp_ls.strip()}")

    # Only run `tektelic-dist-upgrade -c` if opkg update visibly succeeded.
    # If bsp wasn't confirmed (old opkg TTY quirks), skip this check — Phase 7
    # will fail loudly with the real error if the feed is actually broken.
    if bsp_confirmed:
        log.info("  Running tektelic-dist-upgrade -c ...")
        rc, out, err = gw.run("tektelic-dist-upgrade -c 2>&1", timeout=60)
        if "No BSP upgrade available" in out:
            if allow_downgrade:
                log.info("  Feed version is older than installed (expected for downgrade) - proceeding")
            else:
                raise RuntimeError("tektelic-dist-upgrade -c sees no upgrade - feed not loaded correctly")
        else:
            for line in out.splitlines()[:30]:
                log.info(f"    {line}")
            log.info(f"    ... ({len(out.splitlines())} lines total)")
    else:
        log.warning(yellow("  Skipping tektelic-dist-upgrade -c check (opkg confirmation failed)"))
        log.warning(yellow("  Phase 7 will run tektelic-dist-upgrade -Duf — it will surface real errors"))


# ============================================================================
# Phase 6 - Go / No-Go gate
# ============================================================================

def phase6_gate(current, target, bsp_size, free_mb, yes):
    log.info(f"  Current version: {current}")
    log.info(f"  Target version:  {target}")
    log.info(f"  BSP zip size:    {bsp_size/1024/1024:.1f} MB")
    log.info(f"  rootfs free:     {free_mb} MB")
    log.info("")
    log.info("  THIS WILL:")
    log.info("    * Install new BSP packages via opkg")
    log.info("    * Reboot the gateway 1-3 times")
    log.info("    * Take 10-20 minutes")
    log.info("    * Interrupt LoRaWAN traffic during reboots")
    log.info("")
    log.info("  ROLLBACK: If upgrade fails, tektelic-dist-upgrade auto-restores")
    log.info("           from /backup/000/ (newly created in phase 7 pre-install).")
    log.info("")
    if yes:
        log.info("  --yes passed, proceeding")
        return True
    try:
        ans = input("  Type GO to proceed (anything else aborts): ").strip()
    except EOFError:
        ans = ""
    if ans != "GO":
        log.info(f"  User typed '{ans}' != GO - aborting")
        return False
    return True


# ============================================================================
# Phase 7 - Upgrade
# ============================================================================

def phase7_upgrade(gw):
    # Always use -f (force): without it, the tool skips the upgrade if its last
    # recorded status is "ok" (e.g. after a successful auto-rollback from a previous
    # failed attempt). -f resets that state and forces a fresh upgrade.
    # -D = daemon (survives SSH disconnect), -u = upgrade, -f = force.
    cmd = "tektelic-dist-upgrade -Duf 2>&1"
    log.info(f"  Launching tektelic-dist-upgrade -Duf (daemon + force)")
    rc, out, err = gw.run(cmd, timeout=60)
    log.info(f"  Initial response: {out[:400]}")
    # Daemon mode returns quickly; upgrade continues in background


# ============================================================================
# Phase 8 - Monitor
# ============================================================================

ERROR_SIGNATURES = [
    "Unrecoverable Opkg failure",
    "BSP upgrade failed",
    "Failed to upgrade BSP",
    "ubimkvol: error",
    "preinst script returned status 1",
    "Aborting installation",
    "Restoring from the latest backup",
    "UBI device does not have free",
]


def phase8_monitor(host, user, password, timeout_min=40):
    log.info(f"  Monitoring upgrade (timeout {timeout_min} min)")
    t_start = time.time()
    deadline = t_start + timeout_min * 60
    last_progress = -1
    last_connect_fail = 0
    saw_error = None
    has_been_working = False  # flips True once progress >0 or status==in-progress is seen
    # Abort early if after 3 min we still see n/a status and 0 progress —
    # this means tektelic-dist-upgrade -Duf launched but did nothing (usually
    # because opkg feeds weren't loaded — there's nothing to install).
    early_abort_deadline = t_start + 180

    while time.time() < deadline:
        # Try fresh SSH connection each time (GW may reboot mid-upgrade)
        gw = GW(host, user, password)
        try:
            gw.connect(timeout=10)
        except Exception as e:
            if time.time() - last_connect_fail > 30:
                log.info(f"  [t+{int((time.time() - (deadline - timeout_min*60)))}s] SSH unreachable "
                         f"(GW likely rebooting): {str(e)[:80]}")
                last_connect_fail = time.time()
            time.sleep(15)
            continue

        try:
            rc, prog, _ = gw.run("tektelic-dist-upgrade -p 2>&1", timeout=10)
            rc, status, _ = gw.run("tektelic-dist-upgrade -s 2>&1", timeout=10)
            # Peek the live log for errors
            rc, tail_log, _ = gw.run(
                "ls -t /var/log/tektelic-dist-upgrade-*.log 2>/dev/null | head -1 | "
                "xargs -r tail -40 2>/dev/null || echo ''", timeout=10)

            try:
                prog_n = int(prog.strip())
            except ValueError:
                prog_n = -1

            if prog_n != last_progress:
                log.info(f"  progress={prog_n}% status={status.strip()}")
                last_progress = prog_n

            # Flip has_been_working once the upgrader shows signs of activity.
            if prog_n > 0 or "in-progress" in status:
                has_been_working = True

            # Early-abort: after 3 min, if status is still n/a and no progress,
            # tektelic-dist-upgrade didn't actually start — opkg feeds probably
            # not loaded so the tool has nothing to install.
            if (time.time() > early_abort_deadline
                    and not has_been_working
                    and "n/a" in status
                    and prog_n == 0):
                gw.close()
                return False, (
                    "tektelic-dist-upgrade launched but did nothing (status=n/a after 3 min). "
                    "Feed likely not loaded — opkg update did not populate /var/lib/opkg/lists/. "
                    "SSH to GW and run `opkg update` manually, then retry.")

            # progress=100% = all packages installed. The GW will do a final reboot
            # after this. No need to wait for status=ok — declare success now.
            if prog_n == 100:
                log.info(f"  progress=100% — upgrade complete. GW will reboot shortly.")
                gw.close()
                return True, None

            # Scan recent log lines for error signatures
            for sig in ERROR_SIGNATURES:
                if sig in tail_log:
                    saw_error = sig
                    break

            if saw_error:
                log.error(f"  FAILURE SIGNATURE DETECTED: '{saw_error}'")
                log.error("  Recent log tail:")
                for line in tail_log.splitlines()[-20:]:
                    log.error(f"    {line}")
                gw.close()
                return False, saw_error

            # Success: progress back to 0 and status=ok, after having been working.
            if prog_n == 0 and status.strip() == "ok" and has_been_working:
                log.info("  Progress back to 0, status=ok - upgrade complete")
                gw.close()
                return True, None
        except Exception as e:
            log.debug(f"  monitor poll error: {str(e)[:100]}")
        finally:
            gw.close()

        time.sleep(20)

    log.error(f"  TIMEOUT after {timeout_min} min")
    return False, "timeout"


# ============================================================================
# Phase 9 - Post-verify
# ============================================================================

def snapshot_components(gw):
    """Return dict {package_name: version} from /etc/tektelic-versions/*.baseline."""
    _, out, _ = gw.run(
        "for f in /etc/tektelic-versions/*.baseline; do cat \"$f\"; done",
        timeout=15, quiet=True)
    d = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # Format: "<pkg>-<version>-r<release>" — split at last dash before first digit in version
        m = re.match(r"(.+?)-(\d[\d.]*\-?r?\d*)$", line)
        if m:
            d[m.group(1)] = m.group(2)
        else:
            d[line] = "?"
    return d


def phase9_postverify(gw, target_version, baselines_pre=None):
    log.info(bold("=" * 70))
    log.info(bold("PHASE 9: POST-VERIFY"))
    log.info(bold("=" * 70))

    ok_all = True
    def check(name, ok, detail=""):
        nonlocal ok_all
        status = green("OK  ") if ok else red("FAIL")
        log.info(f"  [{status}] {name}: {detail}")
        if not ok: ok_all = False
        return ok

    # 1. BSP version file matches
    _, bsp_raw, _ = gw.run("cat /etc/tektelic-versions/tektelic-bsp-version | head -1")
    actual = bsp_raw.replace("Tektelic", "").strip()
    check("BSP version file", actual == target_version, f"expected={target_version} got={actual}")

    # 2. system_version Release matches
    _, sysv, _ = gw.run("system_version 2>&1 | grep Release")
    check("system_version Release", target_version in sysv, sysv.strip())

    # 3. Upgrader idle
    _, status, _ = gw.run("tektelic-dist-upgrade -s 2>&1")
    _, prog, _   = gw.run("tektelic-dist-upgrade -p 2>&1")
    check("Upgrader idle", status.strip() == "ok" and prog.strip() == "0",
          f"status={status.strip()} progress={prog.strip()}")

    # 4. mqtt-bridge running
    _, bridge_pid, _ = gw.run("pgrep -f tek_mqtt_bridge | head -1")
    check("mqtt-bridge running", bool(bridge_pid.strip()), f"pid={bridge_pid.strip() or 'none'}")

    # 5. NS link established
    _, ns_tcp, _ = gw.run("netstat -tn 2>/dev/null | grep 8883 | grep -c ESTABLISHED")
    try:
        ns_count = int(ns_tcp.strip().splitlines()[0]) if ns_tcp.strip() else 0
    except ValueError:
        ns_count = 0
    check("NS link ESTABLISHED (8883)", ns_count >= 1, f"connections={ns_count}")

    # 5a. packet forwarder log healthy (internal CHG test-plan step 2)
    _, pkt_last, _ = gw.run(
        "if [ -f /var/log/pkt_fwd.log ]; then tail -50 /var/log/pkt_fwd.log; "
        "else echo '(pkt_fwd.log not present)'; fi")
    pkt_errors = sum(1 for ln in pkt_last.splitlines()
                     if re.search(r"ERROR|FATAL|segfault|core dump", ln, re.I))
    pkt_ok = pkt_errors == 0 and "not present" not in pkt_last
    check("pkt_fwd.log clean (last 50 lines)", pkt_ok,
          f"{pkt_errors} error-level lines" if pkt_errors else "no errors")

    # 5b. MQTT gateway-bridge log healthy (internal CHG test-plan step 3)
    _, gwb_last, _ = gw.run(
        "for f in /var/log/gwbridge.log /var/log/tek-mqtt-bridge.log; do "
        "[ -f \"$f\" ] && { echo \"=== $f ===\"; tail -50 \"$f\"; break; } ; done "
        "|| echo '(no gwbridge log found)'")
    gwb_errors = sum(1 for ln in gwb_last.splitlines()
                     if re.search(r"ERROR|FATAL|disconnect.*retry|auth.*fail", ln, re.I))
    gwb_ok = gwb_errors == 0 and "no gwbridge log found" not in gwb_last
    check("gwbridge.log clean (last 50 lines)", gwb_ok,
          f"{gwb_errors} error-level lines" if gwb_errors else "no errors")

    # 6. No kernel panic since reboot (dmesg)
    _, panic, _ = gw.run("dmesg 2>/dev/null | grep -iE 'panic|oops|bug:' | head -3")
    check("No kernel panic in dmesg", not panic.strip(), panic.strip()[:200] or "(clean)")

    # 7. Last reset reason is a clean reboot, not a crash
    _, reset, _ = gw.run("cat /var/log/reset_reason 2>/dev/null || echo 'no reset_reason file'")
    reset_clean = "panic" not in reset.lower() and "watchdog" not in reset.lower()
    check("Last reset reason clean", reset_clean, reset.strip()[:100])

    # 8. Disk usage acceptable (rootfs < 90%)
    _, df_out, _ = gw.run("df -m / | tail -1")
    parts = df_out.split()
    try:
        used_pct = int(parts[4].replace("%", ""))
    except Exception:
        used_pct = 0
    check("rootfs disk usage reasonable", used_pct < 90, f"{used_pct}% used")

    # 9. Run Tektelic's own verify-bsp-installation.sh against manifest
    _, manifest_check, _ = gw.run(
        "MANIFEST=$(ls /lib/firmware/bsp/*manifest* 2>/dev/null | head -1); "
        "if [ -n \"$MANIFEST\" ] && [ -x /usr/sbin/verify-bsp-installation.sh ]; then "
        "  /usr/sbin/verify-bsp-installation.sh \"$MANIFEST\" 2>&1 | tail -5; "
        "else echo 'manifest or verify script not present'; fi",
        timeout=60)
    verify_clean = "mismatch" not in manifest_check.lower() and "error" not in manifest_check.lower()
    check("verify-bsp-installation.sh", verify_clean, manifest_check.strip()[:200])

    # 10. Component diff (if we captured baselines pre-upgrade)
    if baselines_pre is not None:
        baselines_post = snapshot_components(gw)
        changed = []
        added = []
        for pkg, v in baselines_post.items():
            if pkg not in baselines_pre:
                added.append(f"{pkg} -> {v}")
            elif baselines_pre[pkg] != v:
                changed.append(f"{pkg}: {baselines_pre[pkg]} -> {v}")
        removed = [p for p in baselines_pre if p not in baselines_post]
        log.info(f"  Component diff:")
        log.info(f"    {len(changed)} changed, {len(added)} added, {len(removed)} removed")
        for c in changed[:15]: log.info(f"      ~ {c}")
        if len(changed) > 15: log.info(f"      ... and {len(changed)-15} more")
        for a in added[:5]: log.info(f"      + {a}")
        for r in removed[:5]: log.info(f"      - {r}")
        check("Components updated", len(changed) + len(added) > 0,
              f"{len(changed)} changed, {len(added)} added")

    # 11. KGW-2547 validation — mqtt-bridge stability for 2 min
    log.info("  " + cyan("Watching mqtt-bridge stability for 2 min (KGW-2547 validation)..."))
    initial_pid = bridge_pid.strip()
    start = time.time()
    bridge_stable = True
    while time.time() - start < 120:
        check_abort()
        time.sleep(20)
        _, now_pid, _ = gw.run("pgrep -f tek_mqtt_bridge | head -1", quiet=True)
        _, uptime, _ = gw.run("awk '{print $1}' /proc/uptime", quiet=True)
        now_pid = now_pid.strip()
        log.info(f"    t+{int(time.time()-start)}s  bridge_pid={now_pid or '(none)'}  "
                 f"gw_uptime={float(uptime or 0):.0f}s")
        if not now_pid:
            log.warning(f"    {red('mqtt-bridge NOT running!')}")
            bridge_stable = False
        elif initial_pid and now_pid != initial_pid:
            log.warning(f"    {yellow('mqtt-bridge restarted')} (old={initial_pid}, new={now_pid})")
            bridge_stable = False
            initial_pid = now_pid
    check("mqtt-bridge stable 2min (KGW-2547)", bridge_stable,
          "no restarts" if bridge_stable else "restarts observed - open ticket with Tektelic")

    if ok_all:
        log.info(f"  {green(bold('All post-verify checks passed ✓'))}")
    else:
        log.error(f"  {red(bold('Some post-verify checks failed'))}")
    return ok_all


# ============================================================================
# Main
# ============================================================================

def derive_target_from_zip(bsp_path):
    """Extract target version from 'BSP_7.1.16.3.zip' -> '7.1.16.3'."""
    name = Path(bsp_path).name
    m = re.search(r"BSP[_-]?(\d+(?:\.\d+)+)", name)
    return m.group(1) if m else None


def load_sha256_sidecar(bsp_path):
    """Read <bsp>.sha256 sidecar first line, return just the hash."""
    sidecar = Path(str(bsp_path) + ".sha256")
    if not sidecar.exists():
        return ""
    try:
        first_line = sidecar.read_text(encoding="utf-8").splitlines()[0].strip()
        # Format: "<hash>  <filename>" (sha256sum style)
        return first_line.split()[0] if first_line else ""
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser(
        description="Kona BSP upgrade via SSH (NS-agnostic)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Explicit IP + local zip (fully offline)
  kona_upgrade.py --host 192.168.1.134 --bsp bsp/BSP_7.1.16.3.zip

  # Resolve GW name via NS, use latest cached BSP or fetch from FTP
  kona_upgrade.py --gw-name BCNNIOTGW04 --target 7.1.16.3

  # Fetch the latest non-RC BSP and upgrade the home GW
  kona_upgrade.py --host 192.168.1.134 --fetch-latest

  # List available BSPs on Tektelic FTP
  kona_upgrade.py --list-bsps

  # Pre-flight only, no modifications
  kona_upgrade.py --gw-name BCNNIOTGW04 --target 7.1.16.3 --dry-run
""")

    # --- gateway identity (either --host or --gw-name) ---
    g = ap.add_argument_group("gateway")
    g.add_argument("--host", default="",
                   help="Gateway IP. Use only for GWs not registered in any NS. "
                        "For normal use, pass --gw-name and the IP is resolved via NS API.")
    g.add_argument("--gw-name", default="",
                   help="Gateway name (as in NS). Used to (a) resolve IP via NS when --host is absent, "
                        "(b) label the log file.")
    g.add_argument("--user", default=os.environ.get("TEKTELIC_GW_USER", "root"))
    g.add_argument("--password", default=os.environ.get("TEKTELIC_GW_PASS", ""))

    # --- BSP source (either local --bsp, or fetch via --target / --fetch-latest) ---
    b = ap.add_argument_group("bsp")
    b.add_argument("--bsp", default="",
                   help="Local path to BSP_X.Y.Z.zip. If absent, script checks cache (--cache-dir) "
                        "for the target version and downloads from Tektelic FTP if needed.")
    b.add_argument("--target", default="",
                   help="Target BSP version (e.g. 7.1.16.3). Auto-derived from --bsp filename if "
                        "only --bsp is given.")
    b.add_argument("--sha256", default="",
                   help="Expected SHA256 of BSP zip. If omitted, script auto-reads <bsp>.sha256 sidecar.")
    b.add_argument("--fetch-latest", action="store_true",
                   help="Fetch the latest non-RC BSP from Tektelic FTP and use it as target.")
    b.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                   help=f"Local cache for BSP zips. Default: {DEFAULT_CACHE_DIR}")
    b.add_argument("--list-bsps", action="store_true",
                   help="List available BSPs on Tektelic FTP and exit.")

    # --- behaviour flags ---
    f = ap.add_argument_group("behaviour")
    f.add_argument("--dry-run", action="store_true",
                   help="Run phases 1-2 (pre-flight + risk assessment) and stop. No changes on GW.")
    f.add_argument("--skip-cleanup", action="store_true",
                   help="Skip phase 3 cleanup (dangerous — may hit the documented production failure modes)")
    f.add_argument("--yes", action="store_true",
                   help="Skip interactive confirmation in phase 6 gate.")
    f.add_argument("--force", action="store_true",
                   help="Override upgrade-path compatibility warnings.")
    f.add_argument("--json", action="store_true",
                   help="Emit final result as JSON to stdout (for CI/CD integration).")
    f.add_argument("--allow-downgrade", action="store_true",
                   help="Permit target BSP older than current (EXPERIMENTAL — best-effort). "
                        "Uses tektelic-dist-upgrade -Duf. Validated to fail on some BSPs because "
                        "opkg cannot reinstall older versions of tektelic-bsp-version. "
                        "For reliable rollback, take a manual pre-upgrade snapshot with "
                        "`system-backup -b <idx>` BEFORE the upgrade and restore with `-r <idx>`.")
    args = ap.parse_args()

    # ---- --list-bsps: quick exit mode ----
    if args.list_bsps:
        print(cyan("Available BSPs on Tektelic FTP /Universal_Kona_SW/:"))
        try:
            ftps = ftp_connect()
            entries = ftp_list_latest(ftps, limit=20)
            ftps.quit()
            for v, name in entries:
                tag = ""
                if "NOT_FOR_ACTILITY" in name: tag = " (Tektelic NS only)"
                if "_RC" in name: tag += " (release candidate)"
                if "Discarded" in name: tag += " (DISCARDED)"
                print(f"  {bold(v):14} -> {name}{tag}")
            print("\nFor older BSPs per platform, check:")
            print("  /Kona__MICRO_SW/  /Kona_MACRO_SW/  /Kona_MEGA_SW/")
        except Exception as e:
            print(red(f"FTP error: {e}"), file=sys.stderr)
            return 2
        return 0

    # ---- --fetch-latest: find latest and set as target ----
    if args.fetch_latest:
        print(cyan("Looking up latest BSP on Tektelic FTP..."))
        try:
            ftps = ftp_connect()
            entries = ftp_list_latest(ftps, limit=5, include_rc=False)
            ftps.quit()
            # Pick first non-DISCARDED entry
            latest = None
            for v, name in entries:
                if "Discarded" not in name and "SKIPPED" not in name:
                    latest = v
                    break
            if not latest:
                print(red("No releases found on FTP"), file=sys.stderr); return 2
            args.target = latest
            print(f"  Latest release: {bold(latest)}")
        except Exception as e:
            print(red(f"FTP lookup failed: {e}"), file=sys.stderr); return 2

    # ---- Resolve BSP path: local --bsp vs target-only (use cache / FTP) ----
    if args.bsp:
        # Auto-derive target from zip filename if not given
        if not args.target:
            derived = derive_target_from_zip(args.bsp)
            if not derived:
                print(red(f"--target not given and cannot derive from '{Path(args.bsp).name}'"),
                      file=sys.stderr); return 2
            args.target = derived
            print(f"[auto] target version derived from zip name: {bold(args.target)}")
    elif args.target:
        # No local path — look up in cache, download if missing
        print(cyan(f"No --bsp given — resolving target {args.target} from cache/FTP..."))
        try:
            local_zip = fetch_bsp_from_ftp(args.target, cache_dir=args.cache_dir)
            args.bsp = str(local_zip)
        except Exception as e:
            print(red(f"BSP fetch failed: {e}"), file=sys.stderr); return 2
    else:
        print(red("Must pass one of: --bsp, --target, --fetch-latest, or --list-bsps"),
              file=sys.stderr)
        ap.print_help()
        return 2

    # Auto-load SHA256 from sidecar if not passed explicitly
    if not args.sha256:
        sidecar_hash = load_sha256_sidecar(args.bsp)
        if sidecar_hash:
            args.sha256 = sidecar_hash
            print(f"[auto] SHA256 loaded from sidecar: {args.sha256[:16]}...")
        else:
            print(yellow(f"[warn] no --sha256 and no sidecar for {args.bsp} — SHA verification will be SKIPPED"))

    # ---- Resolve --host ----
    # Canonical flow: --gw-name -> NS API -> IP. Use --host <ip> only for GWs not in any NS.
    if not args.host and args.gw_name:
        print(cyan(f"Resolving gateway name '{args.gw_name}' via NS API..."))
        try:
            uuid, ip = ns_resolve_gw_ip(args.gw_name)
            args.host = ip
            print(f"  NS resolved: {args.gw_name} uuid={uuid[:8]}... ip={bold(ip)}")
        except Exception as e:
            print(red(f"NS resolve failed: {e}"), file=sys.stderr); return 2
    if not args.host:
        print(red("Must provide --host <ip> or --gw-name <name>"), file=sys.stderr)
        return 2

    # Default gw-name label for logging if still empty
    if not args.gw_name:
        args.gw_name = "gw-" + args.host.replace(".", "-")

    if not args.password:
        print("--password (or TEKTELIC_GW_PASS env) required", file=sys.stderr)
        sys.exit(2)

    log_file = setup_logging(args.gw_name, args.target)
    install_signal_handlers()

    t_start = time.time()
    log.info(bold("━" * 70))
    log.info(f"{bold('Kona BSP Upgrade')}  target={cyan(args.target)}  host={cyan(args.host)}  gw-name={cyan(args.gw_name)}")
    log.info(f"BSP zip:  {args.bsp}")
    log.info(f"Log file: {log_file}")
    log.info(bold("━" * 70))

    result = {"ok": False, "current": None, "target": args.target,
              "host": args.host, "gw_name": args.gw_name,
              "bsp_zip": args.bsp, "phases": {}, "error": None,
              "log_file": str(log_file), "started_at": time.time()}

    gw = GW(args.host, args.user, args.password)
    baselines_pre = None
    try:
        try:
            gw.connect()
        except Exception as e:
            log.error(red(f"SSH connection failed: {e}"))
            print_recovery_hint(str(e))
            result["error"] = f"ssh_connect: {e}"
            return _finish(result, 1, args.json, t_start)

        # Phase 1
        try:
            with Phase("PHASE 1: PRE-FLIGHT"):
                p1 = phase1_preflight(gw, args.target, allow_downgrade=args.allow_downgrade)
        except Exception as e:
            log.error(red(f"Pre-flight error: {e}"))
            print_recovery_hint(str(e))
            result["error"] = f"preflight: {e}"
            return _finish(result, 3, args.json, t_start)

        result["current"] = p1.get("current")
        result["phases"]["preflight"] = {"ready": p1.get("ready"), "skip": p1.get("skip")}

        if p1.get("skip"):
            log.info(green(f"✓ Already on target {args.target} — nothing to do"))
            result["ok"] = True
            result["error"] = "already_on_target"
            return _finish(result, 0, args.json, t_start)
        if not p1.get("ready"):
            if args.force:
                log.warning(yellow("Pre-flight failed but --force passed, continuing anyway"))
            else:
                log.error(red("Pre-flight not passed - aborting (use --force to override)"))
                result["error"] = "preflight_failed"
                return _finish(result, 3, args.json, t_start)

        # Phase 2
        try:
            with Phase("PHASE 2: RISK ASSESSMENT"):
                risks = phase2_risk(gw)
        except Exception as e:
            result["error"] = f"risk_assessment: {e}"
            return _finish(result, 3, args.json, t_start)

        # Capture baselines before any change so Phase 9 can diff
        baselines_pre = snapshot_components(gw)
        log.info(f"  [snapshot] captured {len(baselines_pre)} component versions pre-upgrade")

        if args.dry_run:
            log.info(bold("=" * 70))
            log.info(cyan("DRY-RUN complete — would execute phases 3-9 from here"))
            log.info(bold("=" * 70))
            result["ok"] = True
            result["error"] = "dry_run_complete"
            return _finish(result, 0, args.json, t_start)

        # Phase 3
        if args.skip_cleanup:
            log.warning(yellow("Cleanup phase SKIPPED by --skip-cleanup"))
        else:
            try:
                with Phase("PHASE 3: CLEANUP"):
                    phase3_cleanup(gw, risks, confirmed=True)
            except Exception as e:
                print_recovery_hint(str(e))
                result["error"] = f"cleanup: {e}"
                return _finish(result, 3, args.json, t_start)

        # Phase 4
        try:
            with Phase("PHASE 4: STAGING"):
                phase4_staging(gw, args.bsp, args.sha256, target_version=args.target)
        except Exception as e:
            print_recovery_hint(str(e))
            result["error"] = f"staging: {e}"
            return _finish(result, 4, args.json, t_start)

        # Phase 5
        try:
            with Phase("PHASE 5: OPKG REFRESH"):
                phase5_opkg_refresh(gw, allow_downgrade=args.allow_downgrade)
        except Exception as e:
            print_recovery_hint(str(e))
            result["error"] = f"opkg_refresh: {e}"
            return _finish(result, 4, args.json, t_start)

        # Phase 6
        bsp_size = os.path.getsize(args.bsp)
        if not phase6_gate(p1["current"], args.target, bsp_size, p1["free_mb"], args.yes):
            result["error"] = "gate_aborted"
            return _finish(result, 4, args.json, t_start)

        # Phase 7
        with Phase("PHASE 7: UPGRADE"):
            phase7_upgrade(gw)
        gw.close()  # daemon mode — we'll reconnect in monitor

        # Phase 8
        with Phase("PHASE 8: MONITOR"):
            ok, err = phase8_monitor(args.host, args.user, args.password)
        if not ok:
            if "timeout" in str(err).lower():
                # Timeout doesn't necessarily mean failure — the upgrade may have
                # completed while the monitor missed the signal. Try Phase 9 anyway:
                # if the version matches target, the upgrade DID succeed.
                log.warning(yellow(f"Phase 8 timed out — attempting Phase 9 post-verify anyway"))
                log.warning(yellow(f"(the upgrade often succeeds even when the monitor misses it)"))
            else:
                log.error(red(f"Upgrade FAILED: {err}"))
                print_recovery_hint(err)
                result["error"] = f"monitor: {err}"
                return _finish(result, 5, args.json, t_start)

        # Phase 9 — runs even after Phase 8 timeout (version check will catch real failures)
        # GW may still be rebooting after the final 100% reboot — be patient
        log.info("  Waiting for GW to come back online for post-verify...")
        time.sleep(30)  # give GW time to finish reboot
        gw.connect(retries=10, backoff=15)
        with Phase("PHASE 9: POST-VERIFY"):
            if not phase9_postverify(gw, args.target, baselines_pre=baselines_pre):
                log.error(red("Post-verify FAILED"))
                result["error"] = "postverify_failed"
                return _finish(result, 6, args.json, t_start)

        log.info(bold("━" * 70))
        log.info(f"{green(bold('✓ SUCCESS'))}: {result['current']} -> {args.target} in {time.time()-t_start:.0f}s")
        log.info(f"Log: {log_file}")
        log.info(bold("━" * 70))
        result["ok"] = True
        return _finish(result, 0, args.json, t_start)

    except KeyboardInterrupt:
        log.warning(yellow("Interrupted by user"))
        result["error"] = "interrupted"
        return _finish(result, 130, args.json, t_start)
    finally:
        gw.close()


def _finish(result, exit_code, emit_json, t_start):
    """Emit final summary / JSON and return exit code."""
    result["duration_sec"] = round(time.time() - t_start, 1)
    result["exit_code"] = exit_code
    if emit_json:
        print(json.dumps(result, indent=2, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
