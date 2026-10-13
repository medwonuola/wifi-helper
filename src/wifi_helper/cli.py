from pathlib import Path
import subprocess
import sys
from typing import Annotated, Optional
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import typer
from .modem import ModemClient
from .net import get_current_ssid, get_default_gateway, has_internet, is_target_network
from .router import RouterClient
from .watchdog import Watchdog

app = typer.Typer(help="CLI tool and auto-heal daemon for your Huawei Wi-Fi network.")
sms_app = typer.Typer(help="SMS inbox, reading, and sending commands.")
app.add_typer(sms_app, name="sms")
console = Console()


@app.command()
def status():
    table = Table(title="Local Link Status", show_header=True, header_style="bold cyan")
    table.add_column("Property", style="dim")
    table.add_column("Value")

    gw = get_default_gateway() or "Unknown"
    ssid = get_current_ssid() or "Unknown"
    target = is_target_network()
    online = has_internet()

    table.add_row("Default Gateway", gw)
    table.add_row("Wi-Fi SSID", ssid)
    table.add_row("Target Network", "[green]Yes[/green]" if target else "[yellow]No[/yellow]")
    table.add_row("Internet Flow", "[green]Online[/green]" if online else "[bold red]Offline / Degraded[/bold red]")
    console.print(table)
    console.print()

    if not target:
        console.print("[yellow]Not connected to target Wi-Fi network.[/yellow]")
        return

    try:
        with ModemClient() as m:
            st = m.status()
            unread, inbox, max_m = m.sms_counts()

            m_table = Table(title="5G Modem (192.168.118.1)", show_header=True, header_style="bold magenta")
            m_table.add_column("Property", style="dim")
            m_table.add_column("Value")

            bars = "█" * st.signal_bars + "░" * (5 - st.signal_bars)
            m_table.add_row("Carrier", st.operator_name)
            m_table.add_row("Network Type", st.network_type)
            m_table.add_row("Signal Strength", f"{bars} ({st.signal_bars}/5 bars)")
            m_table.add_row("Connection Status", st.connection_status)
            m_table.add_row("Cellular WAN IP", st.wan_ip)
            m_table.add_row("Current Rates", f"↓ {st.current_download_rate // 1024} KB/s  ↑ {st.current_upload_rate // 1024} KB/s")
            m_table.add_row("Total Data Used", f"↓ {st.total_download_bytes / (1024**3):.2f} GB  ↑ {st.total_upload_bytes / (1024**3):.2f} GB")
            m_table.add_row("SMS Storage", f"{inbox}/{max_m} ({unread} unread)")

            console.print(m_table)
            console.print()
    except Exception as e:
        console.print(f"[red]Failed to query modem: {e}[/red]\n")

    try:
        with RouterClient() as r:
            rst = r.status()

            r_table = Table(title="Wi-Fi Router (192.168.3.1)", show_header=True, header_style="bold blue")
            r_table.add_column("Property", style="dim")
            r_table.add_column("Value")

            r_table.add_row("Device Model", rst.device_name)
            r_table.add_row("HarmonyOS", rst.harmony_version)
            r_table.add_row("WAN IP", rst.wan_ip)
            r_table.add_row("WAN Gateway", rst.wan_gateway)
            r_table.add_row("WAN Status", rst.wan_status)
            r_table.add_row("Active Clients", str(rst.active_hosts_count))

            console.print(r_table)
    except Exception as e:
        console.print(f"[red]Failed to query router: {e}[/red]\n")


@sms_app.command("list")
def sms_list(
    count: Annotated[int, typer.Option("--count", "-c", help="Number of messages to display")] = 10,
    page: Annotated[int, typer.Option("--page", "-p", help="Page number")] = 1,
):
    try:
        with ModemClient() as m:
            unread, inbox, max_m = m.sms_counts()
            console.print(f"[bold]Inbox:[/bold] {inbox}/{max_m} total messages ({unread} unread)\n")

            messages = m.sms_list(page=page, count=count)
            if not messages:
                console.print("[yellow]No messages found.[/yellow]")
                return

            table = Table(show_header=True, header_style="bold green")
            table.add_column("Date", style="dim", no_wrap=True)
            table.add_column("Sender", style="cyan", no_wrap=True)
            table.add_column("Status", no_wrap=True)
            table.add_column("Message")

            for msg in messages:
                status_str = "[bold red]UNREAD[/bold red]" if msg.is_unread else "[dim]READ[/dim]"
                table.add_row(msg.date, msg.phone, status_str, msg.content.strip())

            console.print(table)
    except Exception as e:
        console.print(f"[red]Failed to fetch SMS: {e}[/red]")
        sys.exit(1)


@sms_app.command("send")
def sms_send(
    phone: Annotated[str, typer.Argument(help="Recipient phone number")],
    message: Annotated[str, typer.Argument(help="SMS text message content")],
):
    try:
        with ModemClient() as m:
            console.print(f"Sending SMS to [cyan]{phone}[/cyan]...")
            if m.sms_send(phone, message):
                console.print("[green]SMS sent successfully![/green]")
            else:
                console.print("[red]Failed to send SMS.[/red]")
                sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error sending SMS: {e}[/red]")
        sys.exit(1)


@app.command()
def ussd(
    code: Annotated[str, typer.Argument(help="USSD code string, e.g. *312#")],
    timeout: Annotated[int, typer.Option("--timeout", "-t", help="Timeout in seconds")] = 25,
):
    console.print(f"Running USSD code [cyan]{code}[/cyan] (timeout {timeout}s)...")
    try:
        with ModemClient() as m:
            res = m.ussd_send(code, timeout_seconds=timeout)
            console.print(Panel(res, title=f"USSD Response ({code})", border_style="cyan"))
    except Exception as e:
        console.print(f"[red]USSD execution failed: {e}[/red]")
        sys.exit(1)


@app.command()
def cycle():
    console.print("Cycling mobile cellular data switch (soft fix)...")
    try:
        with ModemClient() as m:
            if m.cycle_data():
                console.print("[green]Data connection cycled successfully.[/green]")
            else:
                console.print("[red]Failed to cycle mobile data.[/red]")
                sys.exit(1)
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        sys.exit(1)


@app.command()
def reboot(
    modem: Annotated[bool, typer.Option("--modem", "-m", help="Reboot only the 5G modem")] = False,
    router: Annotated[bool, typer.Option("--router", "-r", help="Reboot only the Wi-Fi router")] = False,
    all_devs: Annotated[bool, typer.Option("--all", "-a", help="Reboot both devices")] = False,
):
    if not modem and not router and not all_devs:
        modem = True

    if modem or all_devs:
        console.print("Rebooting 5G Modem (192.168.118.1)...")
        try:
            with ModemClient() as m:
                if m.reboot():
                    console.print("[green]5G Modem reboot command accepted.[/green]")
                else:
                    console.print("[red]Failed to reboot modem.[/red]")
        except Exception as e:
            console.print(f"[red]Error rebooting modem: {e}[/red]")

    if router or all_devs:
        console.print("Rebooting Wi-Fi Router (192.168.3.1)...")
        try:
            with RouterClient() as r:
                if r.reboot():
                    console.print("[green]Wi-Fi Router reboot command accepted.[/green]")
                else:
                    console.print("[red]Failed to reboot router.[/red]")
        except Exception as e:
            console.print(f"[red]Error rebooting router: {e}[/red]")


@app.command()
def fix():
    console.print("[bold]Running offline auto-diagnose and auto-fix...[/bold]")
    wd = Watchdog()
    diag = wd.diagnose()

    if not diag.is_target_network:
        console.print("[yellow]Not currently connected to target Wi-Fi network. Skipping.[/yellow]")
        return

    if diag.has_internet:
        console.print("[green]Connection is healthy! Internet is flowing normally.[/green]")
        return

    console.print(f"[bold red]Connection issue detected! Recommended action: {diag.recommended_action.value}[/bold red]")
    result = wd.autofix(diag)
    if result.success:
        console.print("[bold green]Network connection successfully restored![/bold green]")
    else:
        console.print("[bold red]Autofix sequence completed but connection has not yet recovered.[/bold red]")


@app.command()
def daemon(
    install: Annotated[bool, typer.Option("--install", "-i", help="Install macOS background LaunchAgent service")] = False,
    uninstall: Annotated[bool, typer.Option("--uninstall", "-u", help="Uninstall macOS background service")] = False,
    service_status: Annotated[bool, typer.Option("--status", "-s", help="Check background service status")] = False,
):
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.user.wifi-helper.plist"
    repo_dir = Path(__file__).resolve().parent.parent.parent
    venv_bin = repo_dir / ".venv" / "bin" / "wifi"
    log_path = repo_dir / "daemon.log"

    if install:
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.user.wifi-helper</string>
    <key>ProgramArguments</key>
    <array>
        <string>{venv_bin}</string>
        <string>daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{repo_dir}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist_content)
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        res = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
        if res.returncode == 0:
            console.print(f"[green]Successfully installed and loaded service:[/green] {plist_path}")
            console.print(f"[dim]Logs: {log_path}[/dim]")
        else:
            console.print(f"[red]Failed to load launchd service: {res.stderr}[/red]")
        return

    if uninstall:
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            plist_path.unlink()
            console.print(f"[green]Uninstalled service:[/green] {plist_path}")
        else:
            console.print("[yellow]Service plist does not exist.[/yellow]")
        return

    if service_status:
        res = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
        matching = [line for line in res.stdout.splitlines() if "com.user.wifi-helper" in line]
        if matching:
            console.print(f"[green]Service is active:[/green] {matching[0]}")
            if log_path.exists():
                console.print(f"[dim]Recent log output ({log_path}):[/dim]")
                tail = subprocess.run(["tail", "-n", "5", str(log_path)], capture_output=True, text=True)
                console.print(tail.stdout)
        else:
            console.print("[yellow]Service is not currently running in launchctl.[/yellow]")
        return

    wd = Watchdog()
    wd.run_daemon()


def main():
    app()


if __name__ == "__main__":
    main()
