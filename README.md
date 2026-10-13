# wifi-helper

A command-line doctor and auto-heal watchdog for 4G/5G home internet on Huawei HiLink modems, the 5G CPE boxes that Airtel, MTN and others ship. It runs on your laptop, talks to the modem over your LAN, and needs no internet or cloud account to work.

It exists for one failure in particular: **the modem shows full bars and "Connected", but nothing loads until someone restarts it.**

Example during an outage:

```
$ wifi doctor
                                   Path check
┏━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Hop                ┃ Result          ┃ Detail                                ┃
┡━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Home network       │ yes             │ gateway 192.168.3.1  mac aa:bb:cc:…   │
│ Router             │ reachable       │ 192.168.3.1                           │
│ Modem              │ reachable       │ 192.168.8.1                           │
│ Modem says         │ connected       │ █████ 4G LTE  sim ok                  │
│ Carrier link       │ carrier silent  │ DNS probe to 10.x.x.x, …              │
│ Internet (IP)      │ unreachable     │ TCP to 1.1.1.1 / 8.8.8.8 / 9.9.9.9    │
│ DNS (this device)  │ failing         │ connectivitycheck.gstatic.com         │
│ Web check          │ failing         │ generate_204                          │
└────────────────────┴─────────────────┴───────────────────────────────────────┘
╭──────────────────────────────── dead_bearer ─────────────────────────────────╮
│ The modem says 'connected' but nothing crosses the cellular link (the        │
│ carrier's own DNS is silent). The data session is stale.                     │
│                                                                              │
│ `wifi fix` would start with: cycle_data                                      │
╰──────────────────────────────────────────────────────────────────────────────╯
```

## How it works

Your traffic crosses several hops, and each one fails differently:

```
this computer ──Wi-Fi──> [router] ──Ethernet──> modem ──radio──> carrier core ──> internet
```

Restarting everything is the slow, blunt fix. wifi-helper finds *which* hop is broken and applies the smallest fix that can repair it:

| Probe | Question it answers |
|---|---|
| Default gateway + its MAC | Am I on my home network? (Nothing is touched otherwise.) |
| TCP to router / modem | Is the LAN side alive? |
| Modem status API (no login needed) | Does the modem think it's connected? Signal, SIM, network type. |
| **DNS query to the carrier's own resolver** | Do packets actually cross the cellular link and come back? |
| TCP to 1.1.1.1 / 8.8.8.8 / 9.9.9.9 | Is the public internet reachable by IP? |
| System DNS lookup | Do name lookups work on this device? |
| HTTP `generate_204` | Does the web work, or is something redirecting it? |

The carrier DNS probe is the key one. The modem reports which DNS servers the carrier assigned. Those addresses live inside the carrier's network, so a query to them must cross the radio link, but never touches the public internet or your VPN. Any reply, even an error, proves the cellular session carries traffic. The probe asks for the DNS root (`.`), so no website is ever looked up. That separates three problems that look identical from a browser:

- **carrier silent while modem says connected** → the data session is stale → restart mobile data
- **carrier answers, internet doesn't, VPN active** → your VPN is broken; rebooting the modem won't help
- **carrier answers, internet doesn't, no VPN** → carrier-side outage; one fresh session is worth a try, rebooting isn't

### What it does about each problem

| Verdict | Meaning | Automatic action |
|---|---|---|
| `dead_bearer` | Modem "connected", nothing flows | Restart mobile data (twice max), then reboot modem once |
| `modem_disconnected` | Signal, but no data session | Same as above |
| `upstream_down` | Carrier reachable, internet not | Restart mobile data once |
| `modem_connecting` | Modem still connecting (normal after power-up or a data restart) | Wait 3 minutes, then treat as disconnected |
| `modem_unreachable` | Router fine, modem not answering | Wait 5 minutes (it's usually the modem rebooting), then reboot router once |
| `no_signal` | No signal or SIM problem | None; a reboot can't fix this |
| `redirected` | Web redirected (often: data bundle exhausted) | None; check your balance (`wifi ussd`) |
| `vpn_problem` | Cellular fine, VPN not | None |
| `dns_broken` | IP works, name lookups don't | None; change DNS on your device |
| `lan_down` | Can't reach your own gateway | None; check this device's Wi-Fi |

Restarting mobile data (`cycle`) takes seconds and keeps Wi-Fi up. Reboots take minutes, are rate-limited (`REBOOT_COOLDOWN`), and happen at most once per outage.

## Install

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/). Works on macOS and Linux.

```bash
uv tool install git+https://github.com/medwonuola/wifi-helper
```

or from a clone:

```bash
git clone https://github.com/medwonuola/wifi-helper && cd wifi-helper
uv sync
uv run wifi --help
```

## Configure

Copy `.env.example` to `.env` (next to where you run it) or to `~/.config/wifi-helper/env`, then fill in your modem's address and admin password (the one for its web page). Then:

```bash
wifi config   # which settings file is loaded, what's missing (passwords never shown)
wifi doctor   # full path check; also prints your gateway MAC to pin in .env
```

Pin `HOME_GATEWAY_MAC`. Huawei routers all default to the same addresses, so without it, a friend's network that uses the same gateway IP would look like home.

## Commands

| Command | What it does |
|---|---|
| `wifi doctor` | Check every hop and explain what's wrong. Changes nothing. |
| `wifi fix [--dry-run]` | Diagnose and apply the smallest working fix. |
| `wifi status` | Carrier, 4G/5G, signal, speed, data used, SMS count, router clients. |
| `wifi cycle` | Restart mobile data. |
| `wifi reboot [--modem] [--router] [--all]` | Reboot devices. |
| `wifi sms list` / `wifi sms send NUMBER TEXT` | Read data-bundle alerts, send SMS. |
| `wifi ussd CODE` | Run a USSD code (e.g. your carrier's data balance code). |
| `wifi daemon [--dry-run]` | Watch and heal in the foreground. |
| `wifi daemon --install / --uninstall / --status` | Run at login (launchd on macOS, systemd user service on Linux). |
| `wifi history` | Every outage: when, how long, cause, what fixed it, and a by-hour chart. |

### The watchdog

Every `CHECK_INTERVAL` seconds it makes one cheap web check. Only after `OFFLINE_CHECKS_BEFORE_FIX` failures in a row does it run the full diagnosis and act, so a single dropped packet never triggers a reboot. Each outage is logged to `incidents.jsonl` (`~/Library/Logs/wifi-helper/` on macOS, `~/.local/state/wifi-helper/` on Linux). `wifi history` turns that into evidence: how often it dies, for how long, and at what time of day. That's useful when you call your carrier.

Safety rules:

- Acts only when the default gateway (and pinned MAC, if set) matches your home network.
- After a failed login it stops trying for 5 minutes, doubling up to an hour. Repeated bad logins make HiLink devices lock you out.
- Reboots respect `REBOOT_COOLDOWN` and happen once per outage at most.
- `--dry-run` everywhere you'd want it.

## Supported hardware

| Device | Status |
|---|---|
| Huawei 5G CPE (HiLink, SCRAM login) on Airtel Nigeria | Tested (author's setup) |
| Huawei WiFi AX3S as the router in front of it | Tested |
| Other Huawei HiLink CPEs, routers and dongles (B-series, E-series, 5G CPE Pro/Win) | Should work; same API |
| Older HiLink firmware with SHA-256 login | Implemented, untested on real hardware |
| ZTE, MediaTek and other non-Huawei routers | `doctor` checks still work (minus the modem API); no automatic fixes |

If you try it on another device, please open an issue with the output of `wifi doctor` and your model.

## Field notes

What's been confirmed on a real Airtel Nigeria line, and what is still theory:

- **Observed (author):** the modem keeps reporting `ConnectionStatus 901` ("connected") and full bars while no traffic flows, and restarting mobile data usually brings it back without a reboot. `901` reflects the modem's data session, not end-to-end reachability, so you have to test real traffic.
- **Measured:** the carrier-assigned DNS servers are private `10.x` addresses inside Airtel's network, and the line sits behind carrier-grade NAT.
- **Measured once, off-peak:** the full 1500-byte MTU passed across the cellular link into Airtel's core; Airtel's DNS answered 20 of 20 queries. Peak-hour behaviour is untested; `wifi history` will show if outages cluster in the evening.
- **Plausible, unproven:** stale sessions after tower power changeovers; aggressive idle timeouts in carrier NAT; heat-related drops on indoor CPEs. The history log is designed to help test these.

Cheap physical fixes that are reasonable regardless: a DC mini-UPS for the modem (so grid/generator switchovers don't power-cycle it), keeping the CPE out of direct sun with its vents clear, and setting a public DNS on your devices if you see `dns_broken`.

## Development

```bash
uv sync
uv run pytest
```

The decision logic (`classify` and `choose_action` in `src/wifi_helper/diagnose.py`) is pure and fully unit-tested; probes live in `net.py`, device APIs in `modem.py` and `router.py`.

## License

MIT
