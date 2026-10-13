from dataclasses import dataclass
from enum import Enum
import time
from rich.console import Console
from .config import settings
from .modem import ModemClient
from .net import has_internet, is_target_network
from .router import RouterClient

console = Console()


class FixAction(str, Enum):
    NONE = "none"
    CYCLE_DATA = "cycle_data"
    REBOOT_MODEM = "reboot_modem"
    REBOOT_ROUTER = "reboot_router"


@dataclass
class DiagnosticResult:
    is_target_network: bool
    has_internet: bool
    router_reachable: bool
    modem_reachable: bool
    modem_status: str | None = None
    recommended_action: FixAction = FixAction.NONE
    action_taken: FixAction = FixAction.NONE
    success: bool = False


class Watchdog:
    def __init__(self):
        self.last_reboot_time: float = 0.0

    def diagnose(self) -> DiagnosticResult:
        if not is_target_network():
            return DiagnosticResult(
                is_target_network=False,
                has_internet=False,
                router_reachable=False,
                modem_reachable=False,
            )

        online = has_internet()
        if online:
            return DiagnosticResult(
                is_target_network=True,
                has_internet=True,
                router_reachable=True,
                modem_reachable=True,
            )

        router_ok = False
        try:
            with RouterClient(timeout=3.0) as r:
                router_ok = r.login()
        except Exception:
            pass

        modem_ok = False
        modem_status_str = None
        try:
            with ModemClient(timeout=5.0) as m:
                modem_ok = m.login()
                if modem_ok:
                    st = m.status()
                    modem_status_str = f"{st.operator_name} {st.network_type} ({st.connection_status})"
        except Exception:
            pass

        action = FixAction.CYCLE_DATA
        if not modem_ok and router_ok:
            action = FixAction.REBOOT_MODEM
        elif not router_ok:
            action = FixAction.REBOOT_ROUTER

        return DiagnosticResult(
            is_target_network=True,
            has_internet=False,
            router_reachable=router_ok,
            modem_reachable=modem_ok,
            modem_status=modem_status_str,
            recommended_action=action,
        )

    def autofix(self, diag: DiagnosticResult) -> DiagnosticResult:
        if diag.has_internet or not diag.is_target_network:
            return diag

        now = time.time()
        in_cooldown = (now - self.last_reboot_time) < settings.reboot_cooldown

        if diag.recommended_action == FixAction.CYCLE_DATA:
            console.print("[yellow]Autofix Tier 1: Soft-cycling modem mobile data...[/yellow]")
            try:
                with ModemClient() as m:
                    if m.cycle_data():
                        diag.action_taken = FixAction.CYCLE_DATA
                        time.sleep(8.0)
                        if has_internet():
                            console.print("[green]Internet recovered after data cycle![/green]")
                            diag.success = True
                            return diag
            except Exception as e:
                console.print(f"[red]Soft fix failed: {e}[/red]")

            if not in_cooldown:
                diag.recommended_action = FixAction.REBOOT_MODEM

        if diag.recommended_action == FixAction.REBOOT_MODEM and not in_cooldown:
            console.print("[red]Autofix Tier 2: Hard rebooting 5G Modem...[/red]")
            try:
                with ModemClient() as m:
                    if m.reboot():
                        self.last_reboot_time = now
                        diag.action_taken = FixAction.REBOOT_MODEM
                        console.print("[yellow]Waiting 45s for modem to restart...[/yellow]")
                        time.sleep(45.0)
                        if has_internet():
                            console.print("[green]Internet recovered after modem reboot![/green]")
                            diag.success = True
                            return diag
            except Exception as e:
                console.print(f"[red]Modem reboot failed: {e}[/red]")

            if not in_cooldown:
                diag.recommended_action = FixAction.REBOOT_ROUTER

        if diag.recommended_action == FixAction.REBOOT_ROUTER and not in_cooldown:
            console.print("[red]Autofix Tier 3: Rebooting Wi-Fi Router AX3...[/red]")
            try:
                with RouterClient() as r:
                    if r.reboot():
                        self.last_reboot_time = now
                        diag.action_taken = FixAction.REBOOT_ROUTER
                        console.print("[yellow]Waiting 45s for router to restart...[/yellow]")
                        time.sleep(45.0)
                        if has_internet():
                            console.print("[green]Internet recovered after router reboot![/green]")
                            diag.success = True
                            return diag
            except Exception as e:
                console.print(f"[red]Router reboot failed: {e}[/red]")

        diag.success = has_internet()
        return diag

    def run_daemon(self):
        console.print("[bold green]WiFi Helper Watchdog started.[/bold green]")
        console.print(f"Monitoring network (interval: {settings.check_interval}s, allowed gateways: {settings.allowed_gateways})...")

        while True:
            try:
                diag = self.diagnose()
                if not diag.is_target_network:
                    time.sleep(settings.check_interval)
                    continue

                if not diag.has_internet:
                    console.print("[bold red]Connection drop detected on target network! Running autofix...[/bold red]")
                    self.autofix(diag)
            except Exception as e:
                console.print(f"[red]Watchdog iteration error: {e}[/red]")

            time.sleep(settings.check_interval)
