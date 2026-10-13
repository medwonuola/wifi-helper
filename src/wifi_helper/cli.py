import sys
from typing import Annotated, NoReturn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import typer
from . import history, net, service
from .config import ENV_FILE, env_candidates, settings
from .diagnose import EXPLANATIONS, Action, IncidentState, Snapshot, Verdict, choose_action, classify, full_check
from .modem import AuthError, ModemClient, ModemError
from .router import RouterClient
from .watchdog import Outage, Watchdog

app = typer.Typer(
    help="Offline helper for Huawei HiLink 4G/5G home internet (Airtel, MTN, Glo, ...): diagnose, fix, watch.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
sms_app = typer.Typer(help="Read and send SMS through the modem's SIM.", no_args_is_help=True)
app.add_typer(sms_app, name="sms")
console = Console(emoji=False)


def ok(v: bool | None, yes: str = "yes", no: str = "no") -> str:
    if v is None:
        return "[dim]not checked[/dim]"
    return f"[green]{yes}[/green]" if v else f"[red]{no}[/red]"


def fail(msg: str) -> NoReturn:
    console.print(f"[red]{msg}[/red]")
    raise typer.Exit(1)


def require_home() -> None:
    home = net.check_home_network(settings)
    if not home.is_home:
        fail(f"Not on your home network ({home.reason}). Refusing to touch devices. See `wifi config`.")


@app.command()
def doctor():
    """Check every hop from this computer to the internet and explain what's wrong. Changes nothing."""
    snap = full_check(settings)
    verdict = classify(snap)
    print_snapshot(snap)
    print_verdict(snap, verdict)


def print_snapshot(snap: Snapshot) -> None:
    t = Table(title="Path check", show_header=True, header_style="bold cyan")
    t.add_column("Hop")
    t.add_column("Result")
    t.add_column("Detail", style="dim")
    h = snap.home
    t.add_row("Home network", ok(h.is_home), f"gateway {h.gateway or '-'}  mac {h.gateway_mac or '-'}  wifi {h.ssid or 'hidden by OS'}")
    if not h.is_home:
        console.print(t)
        return
    if snap.router_reachable is not None:
        t.add_row("Router", ok(snap.router_reachable, "reachable", "unreachable"), settings.router_host)
    t.add_row("Modem", ok(snap.modem_reachable, "reachable", "unreachable"), settings.modem_host)
    if snap.modem:
        m = snap.modem
        bars = "█" * m.signal_bars + "░" * max(0, 5 - m.signal_bars)
        t.add_row("Modem says", ok(m.connected, m.connection_label, m.connection_label), f"{bars} {m.network_type}  sim {ok(m.sim_ok, 'ok', 'problem')}")
        t.add_row(
            "Carrier link",
            ok(snap.bearer_ok, "carrier answers", "carrier silent"),
            f"DNS probe to {', '.join(m.carrier_dns) or '-'} (proves the cellular link carries traffic)",
        )
    elif snap.modem_error:
        t.add_row("Modem says", "[yellow]unknown[/yellow]", snap.modem_error)
    t.add_row("Internet (IP)", ok(snap.ip_ok, "reachable", "unreachable"), "TCP to 1.1.1.1 / 8.8.8.8 / 9.9.9.9")
    t.add_row("DNS (this device)", ok(snap.dns_ok, "resolving", "failing"), net.DNS_PROBE_NAME)
    web = {"ok": "[green]ok (204)[/green]", "redirect": "[yellow]redirected[/yellow]", "fail": "[red]failing[/red]"}[snap.http]
    t.add_row("Web check", web, "generate_204")
    if snap.vpn_iface:
        t.add_row("Traffic leaves via", f"[yellow]{snap.vpn_iface} (VPN)[/yellow]" if snap.vpn_active else snap.vpn_iface, "")
    console.print(t)


def print_verdict(snap: Snapshot, verdict: Verdict) -> None:
    action = choose_action(
        verdict, IncidentState(), router_configured=settings.router_configured, last_reboot_at=0, reboot_cooldown=0, now=0
    )
    color = "green" if verdict == Verdict.ONLINE else "yellow" if verdict == Verdict.NOT_HOME else "red"
    body = EXPLANATIONS[verdict]
    if action != Action.NONE:
        body += f"\n\n`wifi fix` would start with: [bold]{action.value}[/bold]"
    console.print(Panel(body, title=f"[{color}]{verdict.value}[/{color}]", border_style=color))
    if snap.home.is_home and snap.home.gateway_mac and not settings.home_gateway_mac:
        console.print(f"[dim]Tip: pin your network by adding HOME_GATEWAY_MAC={snap.home.gateway_mac} to your .env[/dim]")


@app.command()
def fix(dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be done without doing it")] = False):
    """Diagnose and apply the smallest fix that works (cycle data, then reboot)."""
    wd = Watchdog(settings, dry_run=dry_run)
    snap = full_check(settings)
    verdict = classify(snap)
    if verdict in (Verdict.ONLINE, Verdict.NOT_HOME):
        print_verdict(snap, verdict)
        return
    print_snapshot(snap)
    outage = Outage(verdict)
    seen: list[Verdict] = []

    def on_step(_snap: Snapshot, v: Verdict) -> None:
        if not seen or seen[-1] != v:
            seen.append(v)
            console.print(f"[bold]{v.value}[/bold]: {EXPLANATIONS[v]}")

    recovered = wd.heal(snap, outage.incident, outage.state, on_step)
    if not dry_run:
        outage.close(outage.incident.recovered_by if recovered else "not recovered")
    if recovered:
        console.print("[bold green]Internet is back.[/bold green]")
    else:
        fail("Out of safe automatic fixes. See the explanation above.")


@app.command()
def status():
    """Signal, network type, data usage, SMS count and connected devices."""
    require_home()
    try:
        with ModemClient() as m:
            st = m.status()
            unread, inbox, max_m = m.sms_counts()
        mon = st.monitoring
        t = Table(title=f"Modem ({settings.modem_host})", show_header=False)
        t.add_column(style="dim")
        t.add_column()
        bars = "█" * mon.signal_bars + "░" * max(0, 5 - mon.signal_bars)
        t.add_row("Carrier", st.operator_name)
        t.add_row("Network", mon.network_type + (" + 5G NR" if st.nr_active and "5G" not in mon.network_type else ""))
        t.add_row("Signal", f"{bars} ({mon.signal_bars}/5)")
        t.add_row("Connection", mon.connection_label)
        t.add_row("Cellular IP", st.wan_ip + (" (carrier NAT)" if is_cgnat(st.wan_ip) else ""))
        t.add_row("Carrier DNS", ", ".join(mon.carrier_dns) or "-")
        t.add_row("Speed now", f"↓ {st.current_download_rate * 8 / 1e6:.1f} Mbps  ↑ {st.current_upload_rate * 8 / 1e6:.1f} Mbps")
        t.add_row("Data since reset", f"↓ {st.total_download_bytes / 1e9:.2f} GB  ↑ {st.total_upload_bytes / 1e9:.2f} GB")
        t.add_row("SMS", f"{inbox}/{max_m} ({unread} unread)")
        console.print(t)
    except Exception as e:
        console.print(f"[red]Modem: {e}[/red]")

    if not settings.router_configured or settings.router_host == settings.modem_host:
        return
    try:
        with RouterClient() as r:
            rst = r.status()
        t = Table(title=f"Router ({settings.router_host})", show_header=False)
        t.add_column(style="dim")
        t.add_column()
        t.add_row("Model", rst.device_name)
        t.add_row("Firmware", rst.harmony_version)
        t.add_row("WAN", f"{rst.wan_ip} via {rst.wan_gateway} ({rst.wan_status})")
        t.add_row("Active devices", str(rst.active_hosts_count))
        console.print(t)
    except Exception as e:
        console.print(f"[red]Router: {e}[/red]")


def is_cgnat(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return False
    a, b = int(parts[0]), int(parts[1])
    return a == 10 or (a == 100 and 64 <= b <= 127) or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


@app.command()
def cycle():
    """Turn mobile data off and on (fast fix, ~10-30s, Wi-Fi stays up)."""
    require_home()
    try:
        with ModemClient() as m:
            if not m.cycle_data():
                fail("Modem didn't accept the data switch.")
        console.print("[green]Data connection restarted.[/green]")
    except ModemError as e:
        fail(str(e))


@app.command()
def reboot(
    modem: Annotated[bool, typer.Option("--modem", "-m", help="Reboot the modem (default)")] = False,
    router: Annotated[bool, typer.Option("--router", "-r", help="Reboot the Wi-Fi router")] = False,
    all_devs: Annotated[bool, typer.Option("--all", "-a", help="Reboot both")] = False,
):
    """Reboot the modem and/or router."""
    require_home()
    if not (modem or router or all_devs):
        modem = True
    failed = False
    if modem or all_devs:
        try:
            with ModemClient() as m:
                accepted = m.reboot()
            console.print("[green]Modem is rebooting.[/green]" if accepted else "[red]Modem refused the reboot.[/red]")
            failed |= not accepted
        except Exception as e:
            console.print(f"[red]Modem: {e}[/red]")
            failed = True
    if router or all_devs:
        if not settings.router_configured:
            fail("ROUTER_URL is not set.")
        try:
            with RouterClient() as r:
                accepted = r.reboot()
            console.print("[green]Router is rebooting.[/green]" if accepted else "[red]Router refused the reboot.[/red]")
            failed |= not accepted
        except Exception as e:
            console.print(f"[red]Router: {e}[/red]")
            failed = True
    if failed:
        raise typer.Exit(1)


@sms_app.command("list")
def sms_list(
    count: Annotated[int, typer.Option("--count", "-c")] = 10,
    page: Annotated[int, typer.Option("--page", "-p")] = 1,
):
    """Show the SMS inbox (data balance alerts, bundle expiry, ...)."""
    require_home()
    try:
        with ModemClient() as m:
            unread, inbox, max_m = m.sms_counts()
            messages = m.sms_list(page=page, count=count)
    except ModemError as e:
        fail(str(e))
    console.print(f"[bold]Inbox:[/bold] {inbox}/{max_m} ({unread} unread)\n")
    if not messages:
        console.print("[yellow]No messages.[/yellow]")
        return
    t = Table(show_header=True, header_style="bold green")
    t.add_column("Date", style="dim", no_wrap=True)
    t.add_column("From", style="cyan", no_wrap=True)
    t.add_column("")
    t.add_column("Message")
    for msg in messages:
        t.add_row(msg.date, msg.phone, "[bold red]new[/bold red]" if msg.is_unread else "", msg.content.strip())
    console.print(t)


@sms_app.command("send")
def sms_send(
    phone: Annotated[str, typer.Argument(help="Recipient number")],
    message: Annotated[str, typer.Argument(help="Message text")],
):
    """Send an SMS."""
    require_home()
    try:
        with ModemClient() as m:
            if not m.sms_send(phone, message):
                fail("Modem didn't accept the SMS.")
    except ModemError as e:
        fail(str(e))
    console.print("[green]Sent.[/green]")


@app.command()
def ussd(
    code: Annotated[str, typer.Argument(help="USSD code, e.g. your carrier's data balance code")],
    timeout: Annotated[int, typer.Option("--timeout", "-t")] = 25,
):
    """Run a USSD code through the modem's SIM, e.g. to check your data balance."""
    require_home()
    try:
        with ModemClient() as m:
            res = m.ussd_send(code, timeout_seconds=timeout)
    except ModemError as e:
        fail(str(e))
    console.print(Panel(res, title=code, border_style="cyan"))


@app.command()
def daemon(
    install: Annotated[bool, typer.Option("--install", "-i", help="Install as a background service that starts at login")] = False,
    uninstall: Annotated[bool, typer.Option("--uninstall", "-u", help="Remove the background service")] = False,
    service_status: Annotated[bool, typer.Option("--status", "-s", help="Show whether the service is running")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Watch and log, but never touch devices")] = False,
):
    """Watch the connection and heal it automatically."""
    if install:
        if not ENV_FILE:
            fail("No .env found. Create one first (see `wifi config`).")
        res = service.install()
        console.print(f"[green]Installed:[/green] {res.message}\n[dim]Log: {service.log_path()}[/dim]" if res.ok else f"[red]{res.message}[/red]")
        raise typer.Exit(0 if res.ok else 1)
    if uninstall:
        res = service.uninstall()
        console.print(res.message)
        raise typer.Exit(0 if res.ok else 1)
    if service_status:
        res = service.status()
        console.print(f"[green]running[/green] {res.message}" if res.ok else f"[yellow]{res.message}[/yellow]")
        log = service.log_path()
        if log.exists():
            console.print(f"[dim]{log}:[/dim]")
            console.print("\n".join(log.read_text().splitlines()[-8:]))
        return
    Watchdog(settings, dry_run=dry_run).run()


@app.command("history")
def history_cmd(limit: Annotated[int, typer.Option("--limit", "-n", help="Recent outages to list")] = 15):
    """Outages the watchdog has seen: how often, how long, what fixed them, what time of day."""
    incidents = history.load()
    if not incidents:
        console.print(f"No outages recorded yet ({history.incidents_path()}).")
        return
    t = Table(title="Recent outages", header_style="bold")
    for col in ("Started", "Down", "Cause", "Tried", "Fixed by"):
        t.add_column(col)
    for i in incidents[-limit:]:
        t.add_row(i.started.replace("T", " ")[:16], f"{i.duration_s or 0:.0f}s", i.verdict, ", ".join(i.actions) or "-", i.recovered_by or "-")
    console.print(t)

    s = history.summarize(incidents)
    console.print(
        f"\n[bold]{s.count}[/bold] outages, [bold]{s.total_down_s / 60:.0f} min[/bold] total downtime, "
        f"median [bold]{s.median_down_s:.0f}s[/bold]"
    )
    console.print("Causes:   " + ", ".join(f"{k} {v}" for k, v in s.by_verdict.most_common()))
    console.print("Fixed by: " + ", ".join(f"{k} {v}" for k, v in s.by_fix.most_common()))
    if s.by_hour:
        peak = max(s.by_hour.values())
        console.print("\nOutages by hour of day:")
        for hour in range(24):
            n = s.by_hour.get(hour, 0)
            if n:
                console.print(f"  {hour:02d}:00 {'█' * max(1, round(20 * n / peak))} {n}")


@app.command()
def config():
    """Show which settings file is in use and what's configured (passwords hidden)."""
    console.print(f"Settings file: [bold]{ENV_FILE or 'none found'}[/bold]")
    if not ENV_FILE:
        console.print("Looked in: " + ", ".join(str(p) for p in env_candidates()))
    t = Table(show_header=False)
    t.add_column(style="dim")
    t.add_column()
    t.add_row("MODEM_URL", settings.modem_url)
    t.add_row("MODEM_USER / PASSWORD", f"{settings.modem_user} / {'set' if settings.modem_password else '[red]missing[/red]'}")
    t.add_row("ROUTER_URL", settings.router_url or "[dim]none (modem is the Wi-Fi)[/dim]")
    if settings.router_configured:
        t.add_row("ROUTER_USER / PASSWORD", f"{settings.router_user} / {'set' if settings.router_password else '[red]missing[/red]'}")
    t.add_row("Home gateways", ", ".join(settings.home_gateways))
    t.add_row("HOME_GATEWAY_MAC", settings.home_gateway_mac or "[yellow]not pinned[/yellow]")
    t.add_row("ALLOWED_SSIDS", ", ".join(settings.allowed_ssids) or "[dim]any[/dim]")
    t.add_row("Check every", f"{settings.check_interval}s, act after {settings.offline_checks_before_fix} failed checks")
    t.add_row("Reboot cooldown", f"{settings.reboot_cooldown}s")
    console.print(t)


def main():
    try:
        app()
    except AuthError as e:
        console.print(f"[red]Login failed: {e}[/red]")
        sys.exit(1)
