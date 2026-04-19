# ChirpStack v4 — Homelab LoRaWAN Network Server

Self-hosted NS for Civitech + LoRaWAN QA. Docker Compose in LXC on Proxmox.

## Deployment

- **CT ID:** 115 (node `xim`)
- **Name:** `chirpstack`
- **OS:** Debian 12 unprivileged
- **Resources:** 2 GB RAM, 2 cores, 10 GB disk
- **IP:** `192.168.1.173/24` static
- **Features:** nesting enabled (Docker in LXC)
- **Root password:** store in Vaultwarden entry `homelab/chirpstack-ct115-root`

## First-time bootstrap

In Proxmox web UI → Datacenter → xim → CT115 → `>_ Console`:

```bash
curl -fsSL https://raw.githubusercontent.com/Xim1994/kona-upgrade-toolkit/main/chirpstack/bootstrap.sh | bash
```

Installs Docker + ChirpStack v4 + starts services.

## Access

- Web UI: http://192.168.1.173:8080
- Default: admin / admin  → CHANGE IMMEDIATELY
- MQTT: tcp://192.168.1.173:1883
- UDP GW port: 192.168.1.173:1700

## Flow in UI

1. Network Server → Regions → enable `eu868`
2. Tenant (default `ChirpStack` exists)
3. Application → Add: `BCN-HOMELAB-QA`
4. Device Profile → Add: `Milesight-UC100-868M`, region `eu868`, MAC `1.0.3`, OTAA
5. Device → Add: DevEUI from ToolBox
6. Device keys (OTAA): AppKey from ToolBox

## Home kona reconfiguration

Currently → Tektelic NS. To redirect to ChirpStack:

1. https://192.168.1.134 → Setup wizards → ChirpStack
2. Host: `192.168.1.173`, port `1700` UDP
3. Apply + reboot

Verify: ChirpStack UI → Gateways → kona online after ~30s.

## MQTT topics

```
application/<app-uuid>/device/<deveui>/event/up
application/<app-uuid>/device/<deveui>/event/join
application/<app-uuid>/device/<deveui>/event/status
application/<app-uuid>/device/<deveui>/command/down
```

```bash
mosquitto_sub -h 192.168.1.173 -t 'application/+/device/+/event/up' -v
```

## Relation to BI QA NS

Independent. Same DevEUI/AppKey can be registered in both. GW's NS configuration routes the device. Home GW → ChirpStack, BCN GW → BI Tektelic. UC100 attaches to whichever it hears.

Strategy: validate home end-to-end, pre-register in BCN QA, roam transparently on trip.

## References

- https://www.chirpstack.io/docs/
- https://github.com/chirpstack/chirpstack-docker
