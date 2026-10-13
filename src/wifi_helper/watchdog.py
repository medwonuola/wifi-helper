"""Watch the connection and heal it with the smallest fix that works."""

from dataclasses import asdict
import time
from typing import Callable
from rich.console import Console
from . import history
from .config import Settings
from .diagnose import EXPLANATIONS, Action, IncidentState, Snapshot, Verdict, choose_action, classify, full_check, quick_check
from .modem import AuthError, ModemClient
from .router import RouterClient

console = Console()

# How long to wait for the internet to come back after each action.
RECOVERY_WAIT = {Action.CYCLE_DATA: 45, Action.REBOOT_MODEM: 180, Action.REBOOT_ROUTER: 150}


def log(msg: str) -> None:
    console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] {msg}")


class AuthBackoff:
    """Stop retrying logins after a failure. Repeated bad logins lock HiLink devices."""

    def __init__(self, base: float = 300.0, cap: float = 3600.0):
        self.base, self.cap = base, cap
        self.failures = 0
        self.until = 0.0
        self.last_error = ""

    def allowed(self, now: float) -> bool:
        return now >= self.until

    def failed(self, err: AuthError, now: float) -> None:
        self.failures += 1
        self.last_error = str(err)
        self.until = now + min(self.cap, self.base * 2 ** (self.failures - 1))

    def succeeded(self) -> None:
        self.failures, self.until, self.last_error = 0, 0.0, ""


def wait_for_recovery(settings: Settings, seconds: int, poll: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(poll)
        if quick_check(settings).http == "ok":
            return True
    return False


class Watchdog:
    def __init__(self, settings: Settings, dry_run: bool = False):
        self.settings = settings
        self.dry_run = dry_run
        self.auth = AuthBackoff()
        self.last_reboot_at = 0.0

    def perform(self, action: Action) -> bool:
        """Run one action. Returns True if the device accepted it."""
        now = time.time()
        if self.dry_run:
            log(f"[cyan]dry run:[/cyan] would {action.value}")
            return False
        if action in (Action.CYCLE_DATA, Action.REBOOT_MODEM) and not self.auth.allowed(now):
            log(f"[yellow]Skipping {action.value}: modem login paused after failure ({self.auth.last_error}).[/yellow]")
            return False
        try:
            if action == Action.CYCLE_DATA:
                with ModemClient() as m:
                    ok = m.cycle_data()
            elif action == Action.REBOOT_MODEM:
                with ModemClient() as m:
                    ok = m.reboot()
            elif action == Action.REBOOT_ROUTER:
                with RouterClient() as r:
                    ok = r.reboot()
            else:
                return False
        except AuthError as e:
            if action != Action.REBOOT_ROUTER:
                self.auth.failed(e, now)
            log(f"[red]{action.value} failed: {e}[/red]")
            return False
        except Exception as e:
            log(f"[red]{action.value} failed: {e}[/red]")
            return False
        if action != Action.REBOOT_ROUTER:
            self.auth.succeeded()
        if action in (Action.REBOOT_MODEM, Action.REBOOT_ROUTER):
            self.last_reboot_at = now
        return ok

    def heal(
        self,
        snap: Snapshot,
        incident: history.Incident,
        state: IncidentState,
        on_step: Callable[[Snapshot, Verdict], None] | None = None,
    ) -> bool:
        """Diagnose and fix until online, out of options, or we leave the home network."""
        verdict = classify(snap)
        if snap.modem and incident.modem is None:
            incident.modem = asdict(snap.modem)
        while True:
            if not incident.verdicts or incident.verdicts[-1] != verdict.value:
                incident.verdicts.append(verdict.value)
            if on_step:
                on_step(snap, verdict)
            if verdict == Verdict.ONLINE:
                return True
            if verdict == Verdict.NOT_HOME:
                return False
            action = choose_action(
                verdict,
                state,
                router_configured=self.settings.router_configured,
                last_reboot_at=self.last_reboot_at,
                reboot_cooldown=self.settings.reboot_cooldown,
                now=time.time(),
            )
            if action == Action.NONE:
                return False

            log(f"[yellow]{verdict.value}: trying {action.value}[/yellow]")
            state.record(action)
            incident.actions.append(action.value)
            if self.perform(action) and wait_for_recovery(self.settings, RECOVERY_WAIT[action]):
                incident.recovered_by = action.value
                log(f"[green]Back online after {action.value}.[/green]")
                return True
            snap = full_check(self.settings)
            verdict = classify(snap)

    def run(self) -> None:
        s = self.settings
        log(f"[bold green]wifi-helper watchdog started[/bold green] (every {s.check_interval}s, home gateways: {', '.join(s.home_gateways)})")
        offline_streak = 0
        first_fail: tuple[float, str] | None = None  # when the current outage really began
        outage: Outage | None = None

        while True:
            try:
                snap = quick_check(s)
                if not snap.home.is_home or snap.http == "ok":
                    if outage:
                        outage.close("left home network" if not snap.home.is_home else None)
                        if snap.home.is_home:
                            log(f"[green]Online again after {outage.incident.duration_s:.0f}s.[/green]")
                    offline_streak, outage, first_fail = 0, None, None
                    time.sleep(s.check_interval)
                    continue

                offline_streak += 1
                first_fail = first_fail or (time.monotonic(), history.now_iso())
                if offline_streak < s.offline_checks_before_fix:
                    time.sleep(min(s.check_interval, 5))
                    continue

                snap = full_check(s, snap)
                verdict = classify(snap)
                if verdict in (Verdict.ONLINE, Verdict.NOT_HOME):
                    continue
                if outage is None:
                    outage = Outage(verdict, *first_fail)
                    log(f"[bold red]Offline: {verdict.value}[/bold red] {EXPLANATIONS[verdict]}")
                elif outage.state.exhausted_for(s.reboot_cooldown):
                    outage.state = IncidentState()  # long outage: allow the ladder again
                if self.heal(snap, outage.incident, outage.state):
                    outage.close(outage.incident.recovered_by)
                    offline_streak, outage, first_fail = 0, None, None
            except KeyboardInterrupt:
                if outage:
                    outage.close("watchdog stopped")
                raise
            except Exception as e:
                log(f"[red]watchdog error: {e}[/red]")
            time.sleep(s.check_interval)


class Outage:
    def __init__(self, verdict: Verdict, t0: float | None = None, started: str | None = None):
        self.t0 = time.monotonic() if t0 is None else t0
        self.incident = history.Incident(started=started or history.now_iso(), verdict=verdict.value)
        self.state = IncidentState()

    def close(self, recovered_by: str | None) -> None:
        self.incident.ended = history.now_iso()
        self.incident.duration_s = round(time.monotonic() - self.t0, 1)
        self.incident.recovered_by = recovered_by or self.incident.recovered_by or "by itself"
        history.append(self.incident)
