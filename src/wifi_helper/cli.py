from collections.abc import Iterator
from contextlib import contextmanager
from ipaddress import ip_address
import sys
from typing import Annotated, NoReturn
from huawei_lte_api.Client import Client
from huawei_lte_api.enums.sms import StatusEnum
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import typer
from . import history, modem, net, service
from .config import ENV_FILE, env_candidates, settings
from .diagnose import EXPLANATIONS, Action, IncidentState, Snapshot, Verdict, choose_action, classify, full_check
from .modem import AuthError, ModemError
from .router import RouterClient, RouterError
from .watchdog import Outage, Slept, Watchdog

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


def bars(n: int) -> str:
    return "█" * n + "░" * max(0, 5 - n)


def fail(msg: str) -> NoReturn:
    console.print(f"[red]{msg}[/red]")
    raise typer.Exit(1)


def require_home() -> None:
    home = net.check_home_network(settings)
    if not home.is_home:
        fail(f"Not on your home network ({home.reason}). Refusing to touch devices. See `wifi config`.")


@contextmanager
def modem_session() -> Iterator[Client]:
    require_home()
    with modem.session(settings) as c:
        yield c


def kv_table(title: str, rows: dict[str, str]) -> Table:
    t = Table(title=title, show_header=False)
    t.add_column(style="dim")
    t.add_column()
    for k, v in rows.items():
        t.add_row(k, v)
    return t


def print_snapshot(snap: Snapshot) -> None:
    t = Table(title="Path check", header_style="bold cyan")
    for col, style in (("Hop", None), ("Result", None), ("Detail", "dim")):
        t.add_column(col, style=style)
    h = snap.home
    t.add_row("Home network", ok(h.is_home), f"gateway {h.gateway or '-'}  mac {h.gateway_mac or '-'}  wifi {h.ssid or 'hidden by OS'}")
    if not h.is_home:
        console.print(t)
        return
    if snap.router_reachable is not None:
        t.add_row("Router", ok(snap.router_reachable, "reachable", "unreachable"), settings.router_host)
    t.add_row("Modem", ok(snap.modem_reachable, "reachable", "unreachable"), settings.modem_host)
    if m := snap.modem:
        t.add_row("Modem says", ok(m.connected, m.connection_label, m.connection_label), f"{bars(m.signal_bars)} {m.network_type}  sim {ok(m.sim_ok, 'ok', 'problem')}")
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
        verdict, IncidentState(), separate_router=settings.separate_router, last_reboot_at=0, reboot_cooldown=0, now=0
    )
    color = {Verdict.ONLINE: "green", Verdict.NOT_HOME: "yellow"}.get(verdict, "red")
    body = EXPLANATIONS[verdict]
    if action != Action.NONE:
        body += f"\n\n`wifi fix` would start with: [bold]{action.value}[/bold]"
    console.print(Panel(body, title=f"[{color}]{verdict.value}[/{color}]", border_style=color))
    if snap.home.is_home and snap.home.gateway_mac and not settings.home_gateway_mac:
        console.print(f"[dim]Tip: pin your network by adding HOME_GATEWAY_MAC={snap.home.gateway_mac} to your .env[/dim]")


@app.command(help="Check every hop from this computer to the internet and explain what's wrong. Changes nothing.")
def doctor():
    snap = full_check(settings)
    print_snapshot(snap)
    print_verdict(snap, classify(snap))


@app.command(help="Diagnose and apply the smallest fix that works (cycle data, then reboot).")
def fix(dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be done without doing it")] = False):
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

    try:
        recovered = Watchdog(settings, dry_run=dry_run).heal(snap, outage.incident, outage.state, on_step, started=outage.t0)
    except Slept:
        fail("The computer slept during the fix. Run `wifi fix` again.")
    if not dry_run:
        outage.close(outage.incident.recovered_by if recovered else "not recovered")
    if recovered:
        console.print("[bold green]Internet is back.[/bold green]")
    elif seen and seen[-1] == Verdict.MODEM_CONNECTING:
        fail("The modem is still connecting. Give it a minute or two, then run `wifi fix` again if it's still down.")
    elif seen and seen[-1] == Verdict.MODEM_UNREACHABLE:
        fail("The modem isn't answering; it is usually rebooting. Wait 5 minutes, then try `wifi reboot --router` if it's still down.")
    else:
        fail("Out of safe automatic fixes. See the explanation above.")


def carrier_nat(ip: str) -> bool:
    try:
        return not ip_address(ip).is_global
    except ValueError:
        return False


@app.command(help="Signal, network type, data usage, SMS count and connected devices.")
def status():
    with modem_session() as c:
        st = modem.status(c)
        unread, inbox, max_m = modem.sms_counts(c)
    mon = st.monitoring
    console.print(kv_table(f"Modem ({settings.modem_host})", {
        "Carrier": st.operator_name,
        "Network": mon.network_type + (" with 5G NR" if st.nr_active and "5G" not in mon.network_type else ""),
        "Signal": f"{bars(mon.signal_bars)} ({mon.signal_bars}/5)",
        "Connection": mon.connection_label,
        "Cellular IP": st.wan_ip + (" (carrier NAT)" if carrier_nat(st.wan_ip) else ""),
        "Carrier DNS": ", ".join(mon.carrier_dns) or "-",
        "Speed now": f"↓ {st.download_bps / 1e6:.1f} Mbps  ↑ {st.upload_bps / 1e6:.1f} Mbps",
        "Data since reset": f"↓ {st.total_download_bytes / 1e9:.2f} GB  ↑ {st.total_upload_bytes / 1e9:.2f} GB",
        "SMS": f"{inbox}/{max_m} ({unread} unread)",
    }))
    if not settings.separate_router:
        return
    with RouterClient(settings) as r:
        rst = r.status()
    console.print(kv_table(f"Router ({settings.router_host})", {
        "Model": rst.device_name,
        "Firmware": rst.firmware,
        "WAN": f"{rst.wan_ip} via {rst.wan_gateway} ({rst.wan_status})",
        "Active devices": str(rst.active_hosts),
    }))


@app.command(help="Turn mobile data off and on (fast fix, ~10-30s, Wi-Fi stays up).")
def cycle():
    with modem_session() as c:
        if not modem.cycle_data(c):
            fail("Modem didn't accept the data switch.")
    console.print("[green]Data connection restarted.[/green]")


@app.command(help="Reboot the modem and/or router.")
def reboot(
    router: Annotated[bool, typer.Option("--router", "-r", help="Reboot the Wi-Fi router instead of the modem")] = False,
    all_devs: Annotated[bool, typer.Option("--all", "-a", help="Reboot both")] = False,
):
    if (all_devs or router) and not settings.separate_router:
        fail("ROUTER_URL is not set.")
    require_home()
    if all_devs or not router:
        with modem.session(settings) as c:
            if not modem.reboot(c):
                fail("Modem refused the reboot.")
        console.print("[green]Modem is rebooting.[/green]")
    if all_devs or router:
        with RouterClient(settings) as r:
            if not r.reboot():
                fail("Router refused the reboot.")
        console.print("[green]Router is rebooting.[/green]")


@sms_app.command("list", help="Show the SMS inbox (data balance alerts, bundle expiry, ...).")
def sms_list(
    count: Annotated[int, typer.Option("--count", "-c", min=1, max=modem.SMS_PAGE_MAX)] = 10,
    page: Annotated[int, typer.Option("--page", "-p", min=1)] = 1,
    all_messages: Annotated[bool, typer.Option("--all", "-a", help="Every message in the inbox")] = False,
):
    with modem_session() as c:
        unread, inbox, max_m = modem.sms_counts(c)
        messages = modem.sms_all(c) if all_messages else modem.sms_list(c, page, count)
    console.print(f"[bold]Inbox:[/bold] {inbox}/{max_m} ({unread} unread)\n")
    if not messages:
        console.print("[yellow]No messages.[/yellow]")
        return
    t = Table(header_style="bold green")
    t.add_column("Date", style="dim", no_wrap=True)
    t.add_column("From", style="cyan", no_wrap=True)
    t.add_column("")
    t.add_column("Message")
    for msg in messages:
        new = "[bold red]new[/bold red]" if msg.status == StatusEnum.NEW else ""
        t.add_row(f"{msg.date_time:%Y-%m-%d %H:%M}", msg.phone, new, msg.content.strip())
    console.print(t)


@sms_app.command("send", help="Send an SMS.")
def sms_send(
    phone: Annotated[str, typer.Argument(help="Recipient number")],
    message: Annotated[str, typer.Argument(help="Message text")],
):
    with modem_session() as c:
        if c.sms.send_sms([phone], message) != "OK":
            fail("Modem didn't accept the SMS.")
    console.print("[green]Sent.[/green]")


@app.command(help="Run a USSD code through the modem's SIM, e.g. to check your data balance.")
def ussd(
    code: Annotated[str, typer.Argument(help="USSD code, e.g. your carrier's data balance code")],
    timeout: Annotated[int, typer.Option("--timeout", "-t")] = 25,
):
    with modem_session() as c:
        res = modem.ussd(c, code, timeout)
    console.print(Panel(res, title=code, border_style="cyan"))


@app.command(help="Watch the connection and heal it automatically.")
def daemon(
    install: Annotated[bool, typer.Option("--install", "-i", help="Install as a background service that starts at login")] = False,
    uninstall: Annotated[bool, typer.Option("--uninstall", "-u", help="Remove the background service")] = False,
    service_status: Annotated[bool, typer.Option("--status", "-s", help="Show whether the service is running")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Watch and log, but never touch devices")] = False,
):
    if install and not ENV_FILE:
        fail("No .env found. Create one first (see `wifi config`).")
    if install or uninstall:
        res = service.install() if install else service.uninstall()
        console.print(f"[green]{res.message}[/green]" if res.ok else f"[red]{res.message}[/red]")
        if res.ok and install:
            console.print(f"[dim]Log: {service.log_path()}[/dim]")
        raise typer.Exit(0 if res.ok else 1)
    if service_status:
        res = service.status()
        console.print(f"[green]running[/green] {res.message}" if res.ok else f"[yellow]{res.message}[/yellow]")
        if (log := service.log_path()).exists():
            console.print(f"[dim]{log}:[/dim]")
            console.print("\n".join(log.read_text().splitlines()[-8:]))
        return
    Watchdog(settings, dry_run=dry_run).run()


@app.command("history", help="Outages the watchdog has seen: how often, how long, what fixed them, what time of day.")
def history_cmd(limit: Annotated[int, typer.Option("--limit", "-n", help="Recent outages to list")] = 15):
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
    peak = max(s.by_hour.values())
    console.print("\nOutages by hour of day:")
    for hour, n in sorted(s.by_hour.items()):
        console.print(f"  {hour:02d}:00 {'█' * max(1, round(20 * n / peak))} {n}")


@app.command(help="Show which settings file is in use and what's configured (passwords hidden).")
def config():
    console.print(f"Settings file: [bold]{ENV_FILE or 'none found'}[/bold]")
    if not ENV_FILE:
        console.print("Looked in: " + ", ".join(str(p) for p in env_candidates()))
    s = settings
    rows = {
        "MODEM_URL": s.modem_url,
        "MODEM_USER / PASSWORD": f"{s.modem_user} / {'set' if s.modem_password else '[red]missing[/red]'}",
        "ROUTER_URL": s.router_url or "[dim]none (modem is the Wi-Fi)[/dim]",
    }
    if s.separate_router:
        rows["ROUTER_USER / PASSWORD"] = f"{s.router_user} / {'set' if s.router_password else '[red]missing[/red]'}"
    rows |= {
        "Home gateways": ", ".join(s.home_gateways),
        "HOME_GATEWAY_MAC": ", ".join(s.home_gateway_mac) or "[yellow]not pinned[/yellow]",
        "ALLOWED_SSIDS": ", ".join(s.allowed_ssids) or "[dim]any[/dim]",
        "Check every": f"{s.check_interval}s, act after {s.offline_checks_before_fix} failed checks",
        "Reboot cooldown": f"{s.reboot_cooldown}s",
    }
    console.print(kv_table("", rows))


def main():
    try:
        app()
    except AuthError as e:
        console.print(f"[red]Login failed: {e}[/red]")
        sys.exit(1)
    except (ModemError, RouterError) as e:
        console.print(f"[red]{e}[/red]")
        sys.exit(1)
