#!/usr/bin/env python3
"""
Tektelic NS device onboarding.

Implements the canonical 3-step flow from the internal onboarding training
(Device Model → Application → Device) against the Tektelic NS API.

Endpoints (verified 2026-04-19 against lorawan-ns-eu.tektelic.com):
  POST /api/auth/login
  GET  /api/auth/user
  GET  /api/customer/{cid}/devices?limit=N&page=N
  GET  /api/device/{uuid}
  GET  /api/deviceModel/{uuid}
  GET  /api/application/{uuid}
  POST /api/deviceModel       (create device model)
  POST /api/application       (create application)
  POST /api/device            (register device)
  POST /api/converter         (create data converter — schema TBD)

Usage:
  # Inventory / discovery
  python device_onboarding.py --list-devices [--app NAME] [--model NAME]
  python device_onboarding.py --list-applications
  python device_onboarding.py --list-models
  python device_onboarding.py --find-eui 24E124600C234196

  # Create (idempotent — skips if already exists by name)
  python device_onboarding.py --create-model \
      --model-name Milesight-UC100-868M --manufacturer Milesight \
      --model-type "Modbus Gateway" --device-class CLASS_A

  # Register one device (uses existing model + application by name)
  python device_onboarding.py --register \
      --device-name "Milesight UC100 BCN QA" \
      --dev-eui 24E124128B1234 --app-eui 24E124C0002A0001 \
      --app-key 0123456789ABCDEF0123456789ABCDEF \
      --app SVAN-SHOWCASE-BCN-QA --model Milesight-UC100-868M

  # Bulk from YAML
  python device_onboarding.py --register-yaml devices/my_batch.yaml

Env (loaded from .env next to this script):
  TEKTELIC_NS_EU_URL   (default https://lorawan-ns-eu.tektelic.com)
  TEKTELIC_NS_USER
  TEKTELIC_NS_PASS
  TEKTELIC_CUSTOMER_ID (optional, auto-discovered from /api/auth/user)
"""

__version__ = "0.1.0"

import argparse
import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def load_env(env_path):
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


for _candidate in [SCRIPT_DIR / ".env", Path.cwd() / ".env", SCRIPT_DIR.parents[2] / ".env"]:
    if _candidate.exists():
        load_env(_candidate)
        break


log = logging.getLogger(__name__)


# ---- ANSI colours ----
_USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if _USE_COLOR else s


def green(s):  return _c("32", s)
def red(s):    return _c("31", s)
def yellow(s): return _c("33", s)
def cyan(s):   return _c("36", s)
def bold(s):   return _c("1", s)


# ============================================================================
# NS client
# ============================================================================

NS_URL = os.environ.get("TEKTELIC_NS_EU_URL", "https://lorawan-ns-eu.tektelic.com")


class NS:
    """Minimal Tektelic NS client."""

    def __init__(self, url=None, user=None, password=None):
        self.url = url or NS_URL
        self.user = user or os.environ.get("TEKTELIC_NS_USER")
        self.password = password or os.environ.get("TEKTELIC_NS_PASS")
        self.cid = os.environ.get("TEKTELIC_CUSTOMER_ID")
        self.token = None
        self.ctx = ssl.create_default_context()
        if not self.user or not self.password:
            raise RuntimeError("NS needs TEKTELIC_NS_USER + TEKTELIC_NS_PASS in .env")

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h["X-Authorization"] = "Bearer " + self.token
        return h

    def _request(self, method, path, body=None, timeout=20):
        url = self.url + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
            raw = r.read()
        if not raw:
            return None
        return json.loads(raw)

    def login(self):
        self.token = None  # don't send stale token
        body = {"username": self.user, "password": self.password}
        resp = self._request("POST", "/api/auth/login", body=body)
        self.token = resp["token"]
        if not self.cid:
            profile = self._request("GET", "/api/auth/user")
            self.cid = profile.get("customerId", {}).get("id")
            if not self.cid:
                raise RuntimeError("Could not auto-discover customer_id from /api/auth/user")
            log.debug(f"Auto-discovered customer_id: {self.cid}")
        return self

    # ---- read ----

    def list_devices(self, limit=100, max_pages=10):
        out = []
        for page in range(max_pages):
            data = self._request("GET", f"/api/customer/{self.cid}/devices?limit={limit}&page={page}")
            out.extend(data.get("data", []))
            if not data.get("hasNext"):
                break
        return out

    def list_gateways(self, limit=100, max_pages=10):
        out = []
        for page in range(max_pages):
            data = self._request("GET", f"/api/customer/{self.cid}/gateways?limit={limit}&page={page}")
            out.extend(data.get("data", []) if isinstance(data, dict) else data)
            if isinstance(data, dict) and not data.get("hasNext"):
                break
            elif isinstance(data, list):
                break
        return out

    def get_device(self, uuid):
        return self._request("GET", f"/api/device/{uuid}")

    def get_model(self, uuid):
        return self._request("GET", f"/api/deviceModel/{uuid}")

    def get_application(self, uuid):
        return self._request("GET", f"/api/application/{uuid}")

    # ---- derived lookups ----

    def find_device_by_eui(self, dev_eui):
        dev_eui = dev_eui.upper()
        for d in self.list_devices():
            if (d.get("deviceEUI") or "").upper() == dev_eui:
                return d
        return None

    def find_application_by_name(self, name):
        # Applications aren't listed via a direct endpoint — we derive them from
        # the devices list (every device carries applicationId + applicationName)
        for d in self.list_devices():
            if d.get("applicationName") == name:
                return {
                    "id": d["applicationId"]["id"],
                    "name": name,
                }
        return None

    def find_model_by_name(self, name):
        # Same approach — scrape from devices list
        for d in self.list_devices():
            if d.get("deviceModelName") == name:
                return {
                    "id": d["deviceModelId"]["id"],
                    "name": name,
                }
        return None

    # ---- create ----

    def create_model(self, name, manufacturer, type_, device_class="CLASS_A", additional_info=""):
        existing = self.find_model_by_name(name)
        if existing:
            log.info(f"  [skip] device model '{name}' already exists (id={existing['id'][:8]}...)")
            return existing
        body = {
            "name": name,
            "manufacturer": manufacturer,
            "type": type_,
            "deviceClass": device_class,
            "additionalInfo": additional_info,
        }
        log.info(f"  POST /api/deviceModel  name={name}")
        resp = self._request("POST", "/api/deviceModel", body=body)
        return {"id": resp["id"]["id"], "name": name, "raw": resp}

    def create_application(self, name, additional_info=None, sub_customer_id=None):
        existing = self.find_application_by_name(name)
        if existing:
            log.info(f"  [skip] application '{name}' already exists (id={existing['id'][:8]}...)")
            return existing
        body = {
            "name": name,
            "additionalInfo": additional_info,
            "abp": False,
        }
        if sub_customer_id:
            body["subCustomerId"] = {"entityType": "SUB_CUSTOMER", "id": sub_customer_id}
        log.info(f"  POST /api/application  name={name}")
        resp = self._request("POST", "/api/application", body=body)
        return {"id": resp["id"]["id"], "name": name, "raw": resp}

    def create_device(self, name, dev_eui, app_eui, app_key,
                      application_id, device_model_id,
                      device_class="CLASS_A", inactivity_timeout=3600,
                      use_app_network_settings=True):
        # Idempotence: if DevEUI already exists, return that
        existing = self.find_device_by_eui(dev_eui)
        if existing:
            log.info(f"  [skip] device with DevEUI {dev_eui} already exists (name='{existing['name']}')")
            return {"id": existing["id"]["id"], "name": existing["name"], "existed": True}
        body = {
            "name": name,
            "deviceEUI": dev_eui.upper(),
            "appEUI": app_eui.upper(),
            "appKey": app_key.upper(),
            "deviceClass": device_class,
            "inactivityTimeout": inactivity_timeout,
            "useAppNetworkSettings": use_app_network_settings,
            "abp": False,
            "applicationId": {"entityType": "APPLICATION", "id": application_id},
            "deviceModelId": {"entityType": "DEVICE_MODEL", "id": device_model_id},
        }
        log.info(f"  POST /api/device  name={name} eui={dev_eui}")
        resp = self._request("POST", "/api/device", body=body)
        return {"id": resp["id"]["id"], "name": name, "raw": resp}


# ============================================================================
# CLI
# ============================================================================

def cmd_list_devices(ns, args):
    devs = ns.list_devices()
    filtered = devs
    if args.app:
        filtered = [d for d in filtered if d.get("applicationName") == args.app]
    if args.model:
        filtered = [d for d in filtered if d.get("deviceModelName") == args.model]
    print(f"Total: {len(filtered)} devices (of {len(devs)} total in tenant)")
    for d in filtered:
        eui = d.get("deviceEUI", "-")
        name = d.get("name", "-")
        app = d.get("applicationName", "-")
        model = d.get("deviceModelName", "-")
        print(f"  {eui:20s}  {name:40s}  app={app:30s}  model={model}")


def cmd_list_applications(ns, args):
    # Aggregate from devices (no direct list endpoint)
    apps = {}
    for d in ns.list_devices():
        a = d.get("applicationName", "?")
        apps[a] = apps.get(a, 0) + 1
    print(f"Total: {len(apps)} applications (by device count)")
    for a, n in sorted(apps.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {a}")


def cmd_list_models(ns, args):
    models = {}
    for d in ns.list_devices():
        m = d.get("deviceModelName", "?")
        models[m] = models.get(m, 0) + 1
    print(f"Total: {len(models)} device models (by device count)")
    for m, n in sorted(models.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {m}")


def cmd_find_eui(ns, args):
    d = ns.find_device_by_eui(args.find_eui)
    if not d:
        print(red(f"DevEUI {args.find_eui} not found in tenant"))
        return 1
    print(json.dumps(d, indent=2))
    return 0


def cmd_create_model(ns, args):
    result = ns.create_model(
        name=args.model_name,
        manufacturer=args.manufacturer,
        type_=args.model_type,
        device_class=args.device_class,
        additional_info=args.model_desc or "",
    )
    print(json.dumps({"id": result["id"], "name": result["name"]}, indent=2))


def cmd_register(ns, args):
    # Resolve application and model by name
    app = ns.find_application_by_name(args.app)
    if not app:
        print(red(f"Application '{args.app}' not found. Create it first or pick an existing one."))
        return 2
    model = ns.find_model_by_name(args.model)
    if not model:
        print(red(f"Device model '{args.model}' not found. Create it with --create-model first."))
        return 2
    result = ns.create_device(
        name=args.device_name,
        dev_eui=args.dev_eui, app_eui=args.app_eui, app_key=args.app_key,
        application_id=app["id"], device_model_id=model["id"],
        device_class=args.device_class,
        inactivity_timeout=args.inactivity_timeout,
    )
    print(json.dumps({"id": result.get("id"), "name": result.get("name"),
                      "existed": result.get("existed", False)}, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)

    actions = ap.add_argument_group("actions (pick one)")
    actions.add_argument("--list-devices", action="store_true")
    actions.add_argument("--list-applications", action="store_true")
    actions.add_argument("--list-models", action="store_true")
    actions.add_argument("--find-eui", metavar="EUI")
    actions.add_argument("--create-model", action="store_true")
    actions.add_argument("--register", action="store_true")

    ap.add_argument("--app", help="Application name (filter for list-devices or for --register)")
    ap.add_argument("--model", help="Device model name (filter for list-devices or for --register)")

    mg = ap.add_argument_group("create-model")
    mg.add_argument("--model-name", help="Name of device model to create, e.g. Milesight-UC100-868M")
    mg.add_argument("--manufacturer", help="Manufacturer, e.g. Milesight")
    mg.add_argument("--model-type", help="Free-form type, e.g. 'Modbus Gateway'")
    mg.add_argument("--device-class", default="CLASS_A", choices=["CLASS_A", "CLASS_B", "CLASS_C"])
    mg.add_argument("--model-desc", help="additionalInfo description")

    rg = ap.add_argument_group("register")
    rg.add_argument("--device-name", help="Name to register, e.g. 'Milesight UC100 BCN QA'")
    rg.add_argument("--dev-eui", help="Device EUI (16 hex chars)")
    rg.add_argument("--app-eui", help="Application EUI / JoinEUI (16 hex chars)")
    rg.add_argument("--app-key", help="Application Key (32 hex chars)")
    rg.add_argument("--inactivity-timeout", type=int, default=3600, help="seconds (default 3600)")

    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        ns = NS().login()
    except Exception as e:
        print(red(f"Login failed: {e}"), file=sys.stderr)
        return 2

    if args.list_devices:
        return cmd_list_devices(ns, args) or 0
    if args.list_applications:
        return cmd_list_applications(ns, args) or 0
    if args.list_models:
        return cmd_list_models(ns, args) or 0
    if args.find_eui:
        return cmd_find_eui(ns, args)
    if args.create_model:
        if not (args.model_name and args.manufacturer and args.model_type):
            print(red("--create-model requires --model-name --manufacturer --model-type"))
            return 2
        return cmd_create_model(ns, args) or 0
    if args.register:
        if not all([args.device_name, args.dev_eui, args.app_eui, args.app_key, args.app, args.model]):
            print(red("--register requires --device-name --dev-eui --app-eui --app-key --app --model"))
            return 2
        return cmd_register(ns, args) or 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
