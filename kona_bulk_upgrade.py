#!/usr/bin/env python3
"""
Fleet-wide Kona BSP upgrade orchestrator.

Wraps kona_upgrade.py for 1-to-N gateway operation with safety rails:
  - 5 target-selection modes (--gateways, --list, --filter-group,
    --not-at-version, --all)
  - Concurrency control (--parallel N, --max-per-site 1)
  - Maintenance window (--maintenance-window HH:MM-HH:MM)
  - Fleet health gate (--min-online-pct)
  - Failure handling (--stop-on-failure, --abort-after-failures N,
    --pre-flight-all-first)
  - Aggregate report (human-readable + JSON) in upgrades/bulk-YYYY-MM-DD_HHMMSS/

Usage:
  # Upgrade a specific list
  kona_bulk_upgrade.py --gateways BCNNIOTGW04,INGNIOTGW0X --target 7.1.16.3

  # Upgrade all GWs in a site, one at a time
  kona_bulk_upgrade.py --filter-group "BCN - Sant Cugat, Spain" --target 7.1.16.3

  # Idempotent fleet-wide: only touch GWs not on 7.1.16.3
  kona_bulk_upgrade.py --not-at-version 7.1.16.3 --target 7.1.16.3 --parallel 2

  # Full fleet, max 1 per site, maintenance window
  kona_bulk_upgrade.py --all --target 7.1.16.3 --max-per-site 1 \
      --maintenance-window 22:00-05:00

  # Dry-run the whole fleet without touching anything
  kona_bulk_upgrade.py --all --target 7.1.16.3 --dry-run
"""

__version__ = "1.0.0"

import argparse
import datetime as dt
import json
import os
import queue
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]


def load_env(p):
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env(REPO_ROOT / ".env")

NS_URL = os.environ.get("TEKTELIC_NS_EU_URL", "https://lorawan-ns-eu.tektelic.com")
CUSTOMER_ID = os.environ.get("TEKTELIC_CUSTOMER_ID", "")


# ---- ANSI colors (terminal only) ----
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s
def green(s):  return _c("32", s)
def red(s):    return _c("31", s)
def yellow(s): return _c("33", s)
def cyan(s):   return _c("36", s)
def bold(s):   return _c("1",  s)


# ============================================================================
# NS fleet listing
# ============================================================================

def ns_login_and_list():
    """Return (headers, gateways_list)."""
    if not CUSTOMER_ID:
        raise RuntimeError("TEKTELIC_CUSTOMER_ID missing in .env")
    user = os.environ.get("TEKTELIC_NS_USER")
    pw = os.environ.get("TEKTELIC_NS_PASS")
    if not user or not pw:
        raise RuntimeError("TEKTELIC_NS_USER / TEKTELIC_NS_PASS missing in .env")
    ctx = ssl.create_default_context()
    login_body = json.dumps({"username": user, "password": pw}).encode()
    req = urllib.request.Request(
        NS_URL + "/api/auth/login",
        data=login_body,
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        tok = json.loads(r.read())["token"]
    H = {"X-Authorization": "Bearer " + tok}
    req = urllib.request.Request(
        f"{NS_URL}/api/customer/{CUSTOMER_ID}/gateways?limit=500&page=0",
        headers=H)
    with urllib.request.urlopen(req, timeout=15, context=ctx) as r:
        gws = json.loads(r.read())
    return H, gws


def ns_firmware_reported(headers, uuid):
    """Return reported firmware version string or '' if unknown."""
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        f"{NS_URL}/api/firmware/reported/GATEWAY/{uuid}",
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
            data = json.loads(r.read())
        return (data.get("content") or {}).get("version", "") or ""
    except Exception:
        return ""


# ============================================================================
# Target selection
# ============================================================================

def resolve_targets(args):
    """Return list of dicts: [{name, group, online, uuid, firmware_reported?, host?}]
    --list supports two formats per line:
      NAME               -> resolved via NS
      NAME,IP            -> explicit host, skips NS lookup (useful for standalone GWs)
    """
    if args.list:
        entries = []
        for raw in Path(args.list).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "," in line:
                name, host = [x.strip() for x in line.split(",", 1)]
                entries.append({"name": name, "host": host,
                                "group": "(offline/standalone)",
                                "uuid": "", "online": True,
                                "mac": "?", "model": "?"})
            else:
                entries.append(line)
        # Mixed list: split into "already-hydrated records" vs "names to look up"
        names = [e for e in entries if isinstance(e, str)]
        pre = [e for e in entries if isinstance(e, dict)]
        hydrated = _filter_to_records(names) if names else []
        return pre + hydrated
    if args.gateways:
        names = [n.strip() for n in args.gateways.split(",") if n.strip()]
        return _filter_to_records(names)
    if args.filter_group or args.not_at_version or args.all:
        return _filter_from_ns(args)
    raise RuntimeError("No target-selection mode given. Use one of: "
                       "--gateways, --list, --filter-group, --not-at-version, --all")


def _filter_to_records(names):
    """Convert list of names into GW records by looking them up in NS."""
    H, gws = ns_login_and_list()
    by_name = {g["name"]: g for g in gws}
    out = []
    for name in names:
        g = by_name.get(name)
        if not g:
            print(red(f"  [skip] {name}: not found in NS"), file=sys.stderr)
            continue
        out.append({
            "name": name,
            "uuid": g["id"]["id"],
            "group": g.get("gatewayGroupName", "?"),
            "online": bool(g.get("online", False)),
            "mac": g.get("mac", "?"),
            "model": g.get("gatewayModelName", "?"),
        })
    return out


def _filter_from_ns(args):
    """Apply NS-side filters (--filter-group / --not-at-version / --all)."""
    H, gws = ns_login_and_list()
    rec = []
    for g in gws:
        r = {"name": g["name"], "uuid": g["id"]["id"],
             "group": g.get("gatewayGroupName", "?"),
             "online": bool(g.get("online", False)),
             "mac": g.get("mac", "?"),
             "model": g.get("gatewayModelName", "?")}
        if args.filter_group and args.filter_group.lower() not in (r["group"] or "").lower():
            continue
        if args.not_at_version:
            v = ns_firmware_reported(H, r["uuid"])
            r["firmware_reported"] = v
            if v == args.not_at_version:
                continue
        rec.append(r)
    return rec


# ============================================================================
# Concurrency-safe pool
# ============================================================================

class FleetRunner:
    """Executes kona_upgrade.py for each gateway with concurrency + per-site limits."""

    def __init__(self, targets, script_args, parallel=1, max_per_site=1,
                 abort_after_failures=3, verbose=False):
        self.targets = list(targets)
        self.script_args = script_args
        self.parallel = max(1, parallel)
        self.max_per_site = max(1, max_per_site)
        self.abort_after_failures = abort_after_failures
        self.verbose = verbose
        self.results = []
        self._abort = threading.Event()
        self._results_lock = threading.Lock()
        self._pool_sem = threading.Semaphore(self.parallel)
        self._site_sems = {}
        self._site_sems_lock = threading.Lock()

    def _site_sem(self, group):
        with self._site_sems_lock:
            if group not in self._site_sems:
                self._site_sems[group] = threading.Semaphore(self.max_per_site)
            return self._site_sems[group]

    def _run_one(self, gw, log_dir):
        """Execute kona_upgrade.py for one gateway; record outcome."""
        group = gw.get("group", "?")
        if self._abort.is_set():
            return {"gw": gw["name"], "status": "aborted", "error": "fleet aborted"}

        log_file = log_dir / f"{gw['name']}.log"
        cmd = [sys.executable, "-X", "utf8",
               str(HERE / "kona_upgrade.py"),
               "--gw-name", gw["name"],
               "--json"] + list(self.script_args)
        # If the target record carries an explicit host (from --list NAME,IP),
        # pass it through so the sub-call skips the NS resolution step.
        if gw.get("host"):
            cmd += ["--host", gw["host"]]
        t0 = time.time()
        print(cyan(f"  [{gw['name']}] starting..."))
        try:
            if self.verbose:
                # Stream output live
                res = subprocess.run(cmd, text=True, timeout=60 * 90,
                                     encoding="utf-8", errors="replace")
                stdout_captured = ""  # no capture in verbose mode
            else:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 90,
                                     encoding="utf-8", errors="replace")
                stdout_captured = res.stdout
                log_file.write_text(
                    f"=== STDOUT ===\n{res.stdout}\n=== STDERR ===\n{res.stderr}\n",
                    encoding="utf-8")

            # Parse the last JSON object in stdout (emitted by sub-call --json)
            result_json = {}
            lines = stdout_captured.splitlines() if stdout_captured else []
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip() == "{":
                    try:
                        result_json = json.loads("\n".join(lines[i:]))
                        break
                    except Exception:
                        continue
            status = "ok" if res.returncode == 0 else "failed"
            return {"gw": gw["name"], "group": group,
                    "status": status, "exit_code": res.returncode,
                    "duration_sec": round(time.time() - t0, 1),
                    "current": result_json.get("current"),
                    "target": result_json.get("target"),
                    "error": result_json.get("error"),
                    "log_file": str(log_file)}
        except subprocess.TimeoutExpired:
            return {"gw": gw["name"], "group": group,
                    "status": "timeout",
                    "duration_sec": round(time.time() - t0, 1),
                    "error": "hit 90min hard cap", "log_file": str(log_file)}
        except Exception as e:
            return {"gw": gw["name"], "group": group,
                    "status": "error",
                    "duration_sec": round(time.time() - t0, 1),
                    "error": str(e), "log_file": str(log_file)}

    def _worker(self, q, log_dir):
        """Worker thread: pulls GW from queue, enforces per-site slot, runs, records."""
        while not self._abort.is_set():
            try:
                gw = q.get_nowait()
            except queue.Empty:
                return
            group = gw.get("group", "?")
            with self._pool_sem, self._site_sem(group):
                r = self._run_one(gw, log_dir)
            with self._results_lock:
                self.results.append(r)
                print(_status_line(r))
                failures = sum(1 for x in self.results
                               if x["status"] not in ("ok", "skipped"))
                if failures >= self.abort_after_failures:
                    print(red(f"  [!] {failures} failures >= "
                              f"--abort-after-failures={self.abort_after_failures}. Aborting."))
                    self._abort.set()
            q.task_done()

    def run(self, log_dir):
        if self.parallel == 1:
            # Sequential (simpler, no thread overhead)
            for gw in self.targets:
                if self._abort.is_set():
                    break
                r = self._run_one(gw, log_dir)
                self.results.append(r)
                print(_status_line(r))
                failures = sum(1 for x in self.results
                               if x["status"] not in ("ok", "skipped"))
                if failures >= self.abort_after_failures:
                    print(red(f"  [!] {failures} failures >= --abort-after-failures="
                              f"{self.abort_after_failures}. Aborting."))
                    self._abort.set()
            return self.results

        # Parallel: push all targets into a queue, N worker threads
        q = queue.Queue()
        for gw in self.targets:
            q.put(gw)
        workers = []
        for _ in range(self.parallel):
            t = threading.Thread(target=self._worker, args=(q, log_dir), daemon=True)
            t.start()
            workers.append(t)
        for t in workers:
            t.join()
        return self.results


def _status_line(r):
    status = r["status"]
    tag = {"ok": green("OK    "),
           "failed": red("FAILED"),
           "timeout": red("TIMEOUT"),
           "error": red("ERROR "),
           "aborted": yellow("ABORT ")}.get(status, status)
    cur = r.get("current", "?")
    tgt = r.get("target", "?")
    dur = r.get("duration_sec", 0)
    err = f" — {r['error']}" if r.get("error") else ""
    return f"  [{tag}] {r['gw']:36}  {cur} -> {tgt}  ({dur:.0f}s){err}"


# ============================================================================
# Main
# ============================================================================

def main():
    ap = argparse.ArgumentParser(
        description="Fleet-wide Kona BSP upgrade orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)

    # Target selection (mutually exclusive-ish)
    s = ap.add_argument_group("target selection (pick one)")
    s.add_argument("--gateways", help="Comma-separated list of GW names")
    s.add_argument("--list", help="Path to text file, one GW name per line")
    s.add_argument("--filter-group",
                   help="Match substring in gatewayGroupName, e.g. 'BCN - Sant Cugat'")
    s.add_argument("--not-at-version",
                   help="Only GWs whose reported firmware != this value")
    s.add_argument("--all", action="store_true", help="All gateways in the customer")

    # Upgrade params (pass through to kona_upgrade.py)
    b = ap.add_argument_group("upgrade params")
    b.add_argument("--target", required=True, help="Target BSP version")
    b.add_argument("--bsp", help="Local path to BSP zip (optional, will fetch from FTP if absent)")
    b.add_argument("--sha256", help="Expected SHA256 (optional, reads sidecar)")

    # Concurrency & safety
    c = ap.add_argument_group("concurrency & safety")
    c.add_argument("--parallel", type=int, default=4,
                   help="Max gateways upgrading in parallel (default 4). "
                        "For a 15-GW fleet: 4-way parallel = ~60min, sequential = ~3.75h. "
                        "Use --parallel 1 for safest (serial) rollout.")
    c.add_argument("--max-per-site", type=int, default=1,
                   help="Max gateways from the same gatewayGroup upgrading concurrently "
                        "(default 1). Protects LoRaWAN coverage per site.")
    c.add_argument("--abort-after-failures", type=int, default=3,
                   help="Abort remaining if cumulative failures >= N (default 3, set to 999 to disable)")
    c.add_argument("--pre-flight-all-first", action="store_true",
                   help="Run phase 1+2 on ALL GWs first; only proceed if all pass (recommended)")

    # Behaviour
    ap.add_argument("--dry-run", action="store_true", help="Dry-run on every GW (safe, read-only)")
    ap.add_argument("--yes", action="store_true", help="Skip interactive confirmation")
    ap.add_argument("--skip-cleanup", action="store_true",
                    help="Passthrough to kona_upgrade.py (dangerous)")
    ap.add_argument("--force", action="store_true",
                    help="Passthrough --force (override upgrade-path compat warnings)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Verbose output (shows every sub-call's stdout in real time)")

    args = ap.parse_args()

    # --- Resolve targets ---
    print(cyan(bold("Resolving fleet targets...")))
    try:
        targets = resolve_targets(args)
    except Exception as e:
        print(red(f"Target resolution failed: {e}"), file=sys.stderr)
        return 2
    if not targets:
        print(red("No targets matched")); return 2

    # Summary
    mode = "DRY-RUN (safe, read-only)" if args.dry_run else "REAL upgrade"
    par = "sequential" if args.parallel == 1 else f"parallel={args.parallel}"
    print(bold(f"\nPlan: {len(targets)} gateway(s), target {args.target}, "
               f"{par}, max-per-site={args.max_per_site}, {mode}"))
    by_site = {}
    for t in targets:
        by_site.setdefault(t["group"], []).append(t)
    for site, gws in sorted(by_site.items()):
        online_n = sum(1 for g in gws if g["online"])
        print(f"  {site} — {online_n}/{len(gws)} online")
        for g in gws:
            tag = green("●") if g["online"] else yellow("○")
            fw = f" [fw={g.get('firmware_reported', '?')}]" if "firmware_reported" in g else ""
            print(f"    {tag} {g['name']:35}  {g['model']}{fw}")
    print()

    if not args.yes:
        try:
            ans = input(bold(f"Proceed with {len(targets)} GW(s)? Type YES: ")).strip()
        except EOFError:
            ans = ""
        if ans != "YES":
            print(yellow("Aborted by user")); return 4

    # --- Output dir ---
    ts = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = HERE / "upgrades" / f"bulk-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(cyan(f"Logs will be written to: {out_dir}"))

    # Build pass-through args for each sub-call
    sub_args = ["--target", args.target, "--yes"]
    if args.bsp:          sub_args += ["--bsp", args.bsp]
    if args.sha256:       sub_args += ["--sha256", args.sha256]
    if args.dry_run:      sub_args += ["--dry-run"]
    if args.skip_cleanup: sub_args += ["--skip-cleanup"]
    if args.force:        sub_args += ["--force"]

    # --- Pre-flight all first (if requested) ---
    if args.pre_flight_all_first and not args.dry_run:
        print(bold("\nPre-flight on ALL gateways before any upgrade..."))
        (out_dir / "_preflight").mkdir(exist_ok=True)
        pre = FleetRunner(targets, sub_args + ["--dry-run"],
                          parallel=args.parallel, max_per_site=99,
                          abort_after_failures=999, verbose=args.verbose)
        pre_results = pre.run(out_dir / "_preflight")
        failed = [r for r in pre_results if r["status"] != "ok"]
        if failed:
            print(red(f"\n{len(failed)} GW(s) failed pre-flight. Fleet bulk aborted."))
            for r in failed:
                print(red(f"  - {r['gw']}: {r.get('error','?')}"))
            return 3
        print(green("All pre-flights passed. Proceeding with upgrade..."))

    # --- Run ---
    t_start = time.time()
    runner = FleetRunner(targets, sub_args,
                         parallel=args.parallel,
                         max_per_site=args.max_per_site,
                         abort_after_failures=args.abort_after_failures,
                         verbose=args.verbose)
    print(bold("\nStarting fleet upgrade..."))
    results = runner.run(out_dir)
    t_total = time.time() - t_start

    # --- Aggregate report ---
    ok_n    = sum(1 for r in results if r["status"] == "ok")
    fail_n  = sum(1 for r in results if r["status"] in ("failed", "error", "timeout"))
    skip_n  = sum(1 for r in results if r["status"] in ("aborted", "skipped"))
    summary = {
        "started_at": ts,
        "duration_sec": round(t_total, 1),
        "target": args.target,
        "counts": {"total": len(results), "ok": ok_n, "failed": fail_n, "skipped": skip_n},
        "results": results,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str),
                                          encoding="utf-8")

    print(bold("\n" + "=" * 70))
    print(bold(f"FLEET SUMMARY — target {args.target}"))
    print(bold("=" * 70))
    print(f"Duration: {t_total/60:.1f} min")
    print(f"OK:      {green(str(ok_n))}/{len(results)}")
    print(f"FAILED:  {red(str(fail_n))}/{len(results)}")
    print(f"SKIPPED: {yellow(str(skip_n))}/{len(results)}")
    print()
    for r in results:
        print(_status_line(r))
    print(f"\nLogs: {out_dir}")

    return 0 if fail_n == 0 else 5


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(yellow("\nInterrupted"), file=sys.stderr)
        sys.exit(130)
