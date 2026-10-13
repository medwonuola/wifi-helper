# wifi-helper

Terminal utility and background offline auto-heal daemon for Huawei 5G CPE Max 6 (`192.168.118.1`) and Huawei WiFi AX3S (`192.168.3.1`).

## Setup

```bash
cd ~/Projects/wifi-helper
uv sync
```

Ensure `.env` contains your gateway credentials:
```ini
MODEM_URL=http://192.168.118.1
MODEM_USER=admin
MODEM_PASSWORD=your_modem_password

ROUTER_URL=http://192.168.3.1
ROUTER_USER=admin
ROUTER_PASSWORD=your_router_password

ALLOWED_GATEWAYS=["192.168.3.1","192.168.118.1"]
ALLOWED_SSIDS=["Free Internet","Free Internet 3.0"]
```

## CLI Commands

Run with `uv run wifi <command>` or via the venv binary `.venv/bin/wifi <command>`:

### 1. Status Overview
```bash
uv run wifi status
```
Displays link state, 5G carrier (Airtel), signal bars, download/upload rates, total data used, unread SMS count, WAN IPs, and connected devices.

### 2. SMS Management
```bash
# View inbox
uv run wifi sms list --count 10 --page 1

# Send an SMS
uv run wifi sms send <recipient_phone> "<message>"
```

### 3. USSD Codes
```bash
# Run USSD code (e.g. Airtel balance / plan check)
uv run wifi ussd "*312#"
```

### 4. Connection Restarts
```bash
# Soft fix: cycle cellular mobile data connection (fast, ~10s)
uv run wifi cycle

# Hard restart modem, router, or both
uv run wifi reboot --modem
uv run wifi reboot --router
uv run wifi reboot --all
```

### 5. One-Shot Auto-Diagnose & Auto-Fix
```bash
uv run wifi fix
```
Checks target network reachability and internet flow (HTTP 204). If offline on target network, escalates fixes automatically.

---

## Background Auto-Heal Daemon

The daemon monitors your connection in the background. If internet drops while connected to your home Wi-Fi:
1. **Tier 1**: Soft carrier cycle (toggles data switch).
2. **Tier 2**: Hard reboots 5G CPE modem.
3. **Tier 3**: Reboots Wi-Fi AX3S router.

> **Safety Guard**: The daemon **only** acts when connected to your target router or modem (`192.168.3.1`, `192.168.118.1`, `Free Internet`). If you are on a phone hotspot, coffee shop, or office network, it skips automatically without modifying anything.

### Run in Foreground:
```bash
uv run wifi daemon
```

### Install as macOS Background Service (runs automatically on login):
```bash
# Install and start LaunchAgent
uv run wifi daemon --install

# Check service status & recent logs
uv run wifi daemon --status

# Stop and uninstall service
uv run wifi daemon --uninstall
```
Logs are written to `~/Projects/wifi-helper/daemon.log`.
