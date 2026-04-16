# Draft message to internal support (re: 7.1.12.1 → 7.1.16.3 upgrade failure)

> Private draft — edit before sending. Fill in `{{recipient}}` and `{{gateway}}` placeholders.

---

**Asunto:** Root cause + fix para el fallo 7.1.12.1 → 7.1.16.3 en {{gateway}}

Hey {{recipient}}, reproduje el fallo del ticket del 9 de abril en un Kona Micro de lab y te paso lo que encontré — son dos issues encadenados:

1. **UBI eraseblocks exhaustion.** El auto-backup del upgrade se monta en `/backup/000/` (UBI partition `ubi1:backup`, 248 MB). Si hay un `/backup/000/bak.*` de un upgrade anterior > 100 MB, el nuevo no cabe y el tool aborta con `ubimkvol: error!: UBI device does not have free logical eraseblocks`.

2. **`tektelic-add-users` preinst falla.** Si el usuario `admin` existe de instalaciones pre-7.1.5.1, `useradd` falla, opkg aborta la transacción, y el auto-rollback te devuelve a la versión anterior.

**Fix antes del retry en {{gateway}} (SSH root):**

```bash
rm -rf /backup/000/bak.* && sync
userdel admin 2>/dev/null; rm -rf /home/admin
rm -rf /lib/firmware/* /var/lib/opkg/lists/*
```

Luego `tektelic-dist-upgrade -Du` como siempre.

Aparte, construí un script Python que hace todo el flujo con auto-detección de estos dos failure modes + otros (`/lib/firmware/` leftovers, NTP desync, disk free post-cleanup, GPG feed validation, 11-check post-verify incluyendo `pkt_fwd.log` + `gwbridge.log` + MQTT-bridge 2min stability). Lo tengo alineado 1:1 al onboarding training interno §Gateway Onboarding (slides 44-53) y al change request interno. Si te interesa lo corro contra tu GW cuando tengas ventana.

Un aviso aparte: el plan de reversión del CHG (`system-backup -r 000`) **no funciona** contra backups hechos por `tektelic-dist-upgrade` — formato incompatible entre las dos herramientas Tektelic. Lo tengo validado en vivo. Si queréis puedo abrir una issue para que actualicen el CHG o revisar opciones de rollback reales.

— Xim

---

## Notes before sending

- **Tono:** informal, casual pero técnico.
- **Privacidad:** reemplazar los `{{recipient}}` y `{{gateway}}` con los reales antes de enviar. No mencionar Civitech, home GW, ni IPs personales.
- **El último párrafo (CHG)** es políticamente sensible: implica que un procedimiento aprobado en ServiceNow es defectuoso. Si crees que levantar eso desde primer mensaje es demasiado, bórralo y mencionarlo en una reunión 1:1 con el owner del CHG.
- **Offer:** "Si te interesa lo corro contra tu GW" — cambia a lo que tengas capacidad. Alternativa: "te paso el repo con el toolkit + README para que lo corras tú".
- Adjuntar idealmente `UPGRADE_PROCEDURE.md` al mensaje (contiene el mapping al onboarding training + CHG + failure modes documentados).
