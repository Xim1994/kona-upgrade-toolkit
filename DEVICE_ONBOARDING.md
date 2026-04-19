# Device Onboarding — Tektelic NS

Canonical flow from internal onboarding training (slides 59-71) + validated against live QA NS (2026-04-19).

## 1. Onboarding steps (canonical order)

Per the training material, four steps per new device:

1. **Device Model** — `POST /api/deviceModel` (reuse if exists)
2. **Application** — `POST /api/application` (reuse if exists)
3. **Device (sensor)** — `POST /api/device` with DevEUI + AppEUI + AppKey + refs to the model and application
4. **Configuration (downlink)** — optional; for sensors that accept downlink for config (UC100 does NOT — its Modbus config is loaded via Milesight ToolBox USB, not via LoRaWAN downlink)

Data converter (JS decoder) is step 5 — optional, required only when the customer needs JSON instead of raw payload.

## 2. API endpoints — verified

All against `https://lorawan-ns-eu.tektelic.com`. Auth: `POST /api/auth/login` returns JWT, use as `X-Authorization: Bearer <token>`.

| Method | Path | Purpose | Verified |
|---|---|---|---|
| POST | `/api/auth/login` | Login, returns `{token}` | ✓ |
| GET | `/api/auth/user` | Get current user + customerId | ✓ |
| GET | `/api/customer/{cid}/devices?limit=N&page=N` | List devices (paginated) | ✓ (100 devices in QA) |
| GET | `/api/customer/{cid}/gateways?limit=N&page=N` | List gateways (paginated) | ✓ |
| GET | `/api/device/{uuid}` | Get device full record | ✓ |
| GET | `/api/deviceModel/{uuid}` | Get device model | ✓ |
| GET | `/api/application/{uuid}` | Get application | ✓ |
| POST | `/api/deviceModel` | Create device model | 405 on GET → POST expected |
| POST | `/api/application` | Create application | 405 on GET → POST expected |
| POST | `/api/device` | Create device | 405 on GET → POST expected |
| POST | `/api/converter` | Create data converter | 405 on GET → POST expected |

## 3. Schemas (observed from live data)

### Device Model
```json
{
  "name": "Milesight-UC100-868M",            // <Manufacturer>-<Model>[-<variant>]
  "manufacturer": "Milesight",
  "type": "Modbus Gateway",                   // free-form description
  "deviceClass": "CLASS_A",                   // CLASS_A | CLASS_B | CLASS_C
  "additionalInfo": "Modbus RTU to LoRaWAN EU868 bridge"
}
```

### Application
```json
{
  "name": "SVAN-SHOWCASE-BCN-QA",             // <SITE>-<PURPOSE>-<ENV>
  "additionalInfo": null,
  "subCustomerId": { "entityType": "SUB_CUSTOMER", "id": "<uuid>" },  // optional
  "alarmRules": null,
  "abp": false
}
```

### Device
```json
{
  "name": "IOT<epoch-ms>" or custom,          // convention: IOT<timestamp>
  "deviceEUI": "24E124128B1234",              // 16-char hex, from device label
  "appEUI": "24E124C0002A0001",               // 16-char hex (Milesight default JoinEUI)
  "appKey": "0123456789ABCDEF0123456789ABCDEF", // 32-char hex, from ToolBox
  "deviceClass": "CLASS_A",
  "inactivityTimeout": 3600,                  // seconds before NS marks offline
  "applicationId": { "entityType": "APPLICATION", "id": "<uuid>" },
  "deviceModelId": { "entityType": "DEVICE_MODEL", "id": "<uuid>" },
  "useAppNetworkSettings": true,
  "abp": false
}
```

## 4. Naming conventions (observed live)

| Entity | Pattern | Live examples |
|---|---|---|
| Application | `<SITE>-<PURPOSE>-<ENV>` | `SVAN-SHOWCASE-BCN-QA`, `BIB-PCM-MILESIGHT`, `RDG-PCM-MILESIGHT-QA`, `VIE-ASYSTOM-DEV`, `ING-Dezem` |
| Device Model (prod) | `<Manufacturer>-<ID>-<Name>` | `Tektelic-T0006940-TUNDRA`, `Milesight-Field-Tester`, `ELSYS-ELSYS Elsys ERS CO2` |
| Device Model (dev) | `dev-dev-<vendor>-<model>` or `<env>-<vendor>-<model>` | `dev-dev-milesight-ws523`, `qa-milesight-ws523` |
| Device (auto) | `IOT<epoch-ms>` | `IOT177313853867` |
| Device (custom) | free-form | `Milesight Field Tester BIB`, `Test TS302 device` |

## 5. QA NS landscape (2026-04-19 snapshot)

- **Customer:** Joaquim Bravo_Jordana (CUSTOMER_ADMIN)
- **Customer ID:** `0f9649d0-262f-11ee-af4f-eb9db0a8e27e`
- **100 devices** across **15 applications**
- **Top applications:** `SVAN-DEV-ACCOUNT5` (26), `SVAN-QA-EU-OL` (17), `RDG-PCM-MILESIGHT-QA` (11), `PCM-DEV-TRAINING` (7), `SVAN-QA-EU-IOT` (6), `SVAN-DEV-PILOT-ENERGYMONITORING` (6), `SVAN-SHOWCASE-BCN-QA` (4)
- **Top models:** `dev-dev-milesight-ws523` (20), variants of Milesight hashed models, `dev-dev-jri-lora-spy-t0`, `deZem GmbH HARVY2`

## 6. Excel inventory cross-check (from `onboard devices.xlsx`)

| Excel name | Entorno | DevEUI | NS status (QA tenant) | Notes |
|---|---|---|---|---|
| Tektelic Tundra T0006779 | QA | — | ambiguous (3 Tundras in BCN-QA) | need DevEUI to disambiguate |
| Tektelic Tundra T0006779 B2 | None | 647FDA000001B2AC | ✓ `IOT177313853867` in `SVAN-SHOWCASE-BCN-QA`, model `Tektelic-T0006940-TUNDRA` | mismatch: excel says no device_created but NS has it |
| Tektelic Tundra Display | None | — | unknown | no EUI to look up |
| Tektelic Elsys.se ERS2 Sound | Prod | A81758FFFE0A13E8 | ✗ not in QA | likely in Prod tenant (SSO) |
| Tektelic Elsys.se ERSCO2 | QA | A81758FFFE045EEC | ✓ `IOT177261049167` in `SVAN-SHOWCASE-BCN-QA`, model `ELSYS-ELSYS Elsys ERS CO2` | all good |
| Milesight VS121-868M | Prod | 24E124600C234196 | ✗ not in QA | Prod tenant |
| Milesight WS303-868M | None | 24E124993D529043 | ✗ not anywhere yet | pending onboarding |

**Verdict:** Excel is roughly accurate; the few "None" entries under Entorno for devices that ARE in QA (like Tundra B2) should probably be updated to "QA". Prod devices confirmed separate tenant.

## 7. Plan for Milesight UC100 868M onboarding

When Xim has the device physically:

1. **Physical setup (local, not NS)**
   - Connect UC100 to Windows PC via USB (USB-RS232 cable to the device's debug port, or Ethernet)
   - Install Milesight ToolBox
   - Read DevEUI / AppEUI / AppKey from ToolBox
   - Configure Modbus master settings (baud, slave ID, registers to poll) — this step is ONLY possible via ToolBox, no remote equivalent
   - Save config to UC100

2. **NS onboarding (automated via script)**
   - Check/create Device Model `Milesight-UC100-868M` (class A, manufacturer Milesight, type "Modbus Gateway")
   - Reuse Application `SVAN-SHOWCASE-BCN-QA`
   - Create Device with the DevEUI/AppEUI/AppKey read from ToolBox
   - Name: `Milesight UC100 BCN QA` (custom, matches `Milesight Field Tester BIB` pattern)
   - inactivityTimeout: 3600 (standard)

3. **Data Converter (later, optional)**
   - Official decoder: github.com/Milesight-IoT/IoT-Payload-Decoder/blob/main/es/uc100.js
   - Upload via `POST /api/converter` (schema to be confirmed from first manual upload)

4. **Validation**
   - Wait for UC100 to OTAA-join (visible in `/api/device/{uuid}` as `devAddress` populated)
   - Query `/api/device/{uuid}/latest-telemetry` or similar for first uplink
   - Confirm GW that received it is BCNNIOTGW04_Maintenance (or the target)

## 8. Open questions

- **Data Converter upload** — need to verify POST /api/converter schema. Plan: upload a test converter via UI, capture XHR body, replicate.
- **"Normalizer" column in Excel** — what exactly is it? PDF mentions "Data Converter" but not "Normalizer". Likely a BI-specific term for the same concept.
- **"PCM & SVAN" in Excel "Device created" column** — PCM = ? SVAN = ? These look like 2 internal platforms that consume the NS data. For now: do nothing, just make sure the NS side is clean.
- **AWS** — Xim mentioned "aunque esten ya en aws sabes nos e" — some decoders may already live in AWS (probably Lambda functions for post-processing). Worth documenting when we have time: which decoders live in NS Data Converters vs AWS.

## 9. References

- Internal onboarding training — §Sensor Onboarding (slides 59-68) and §Data Converter (slides 69-71)
- Live NS API discovery: `references/homelab/lorawan-qa/kona_upgrade.py` → `ns_resolve_gw_ip` uses the same auth flow
- Device inventory: `references/homelab/lorawan-qa/devices/inventory.yaml`
