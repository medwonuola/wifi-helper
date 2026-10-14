from dataclasses import asdict
import time
from typing import Callable
from rich.console import Console
from . import history, modem, net
from .config import Settings
from .diagnose import EXPLANATIONS, Action, IncidentState, Snapshot, Verdict, choose_action, classify, full_check, quick_check
from .router import RouterClient

console = Console(emoji=False)

RECOVERY_WAIT = {Action.CYCLE_DATA: 45, Action.REBOOT_MODEM: 180, Action.REBOOT_ROUTER: 150}
SLEEP_GAP = 30.0
WAKE_GRACE = 90.0


def log(msg: str) -> None:
    console.print(f"[dim]{time.strftime('%H:%M:%S')}[/dim] {msg}")


class Slept(Exception):
    pass


class SleepDetector:
    def __init__(self, wall: Callable[[], float] = time.time, awake: Callable[[], float] = time.monotonic):
        self.wall, self.awake = wall, awake
        self._wall, self._awake = wall(), awake()
        self.slept_at: float | None = None
        self.woke_at: float | None = None

    def slept(self) -> bool:
        wall, awake = self.wall(), self.awake()
        gap = (wall - self._wall) - (awake - self._awake)
        before, self._wall, self._awake = self._wall, wall, awake
        if gap > SLEEP_GAP:
            self.slept_at, self.woke_at = before, awake
            return True
        return False

    def settling(self) -> bool:
        return self.woke_at is not None and self.awake() - self.woke_at < WAKE_GRACE


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
        self.last_reboot_at = 0.0
        self.sleep = SleepDetector()

    def perform(self, action: Action) -> bool:
        now = time.time()
        if self.dry_run:
            log(f"[cyan]dry run:[/cyan] would {action.value}")
            return False
        try:
            match action:
                case Action.CYCLE_DATA:
                    with modem.session(self.settings) as c:
                        ok = modem.cycle_data(c)
                case Action.REBOOT_MODEM:
                    with modem.session(self.settings) as c:
                        ok = modem.reboot(c)
                case Action.REBOOT_ROUTER:
                    with RouterClient(self.settings) as r:
                        ok = r.reboot()
                case _:
                    return False
        except Exception as e:
            log(f"[red]{action.value} failed: {e}[/red]")
            return False
        if action in (Action.REBOOT_MODEM, Action.REBOOT_ROUTER):
            self.last_reboot_at = now
        return ok

    def heal(
        self,
        snap: Snapshot,
        incident: history.Incident,
        state: IncidentState,
        on_step: Callable[[Snapshot, Verdict], None] | None = None,
        started: float | None = None,
    ) -> bool:
        started = time.monotonic() if started is None else started
        verdict = classify(snap)
        if snap.modem and incident.modem is None:
            incident.modem = asdict(snap.modem)
        while True:
            if self.sleep.slept():
                raise Slept
            if not incident.verdicts or incident.verdicts[-1] != verdict.value:
                if incident.verdicts:
                    log(f"Now: {verdict.value}")
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
                separate_router=self.settings.separate_router,
                last_reboot_at=self.last_reboot_at,
                reboot_cooldown=self.settings.reboot_cooldown,
                now=time.time(),
                outage_seconds=time.monotonic() - started,
            )
            if action == Action.NONE:
                return False

            log(f"[yellow]{verdict.value}: trying {action.value}[/yellow]")
            state.record(action)
            incident.actions.append(action.value)
            recovered = self.perform(action) and wait_for_recovery(self.settings, RECOVERY_WAIT[action])
            if self.sleep.slept():
                raise Slept
            if recovered:
                incident.recovered_by = action.value
                log(f"[green]Back online after {action.value}.[/green]")
                return True
            snap = full_check(self.settings)
            verdict = classify(snap)

    def self_check(self) -> bool:
        home = net.check_home_network(self.settings)
        if not home.is_home:
            log(f"Not on home network ({home.reason}); idle until it's back.")
            return False
        modem_ok = net.tcp_reachable(self.settings.modem_host, 80)
        log(f"Home network ok (gateway {home.gateway}, mac {home.gateway_mac or 'unknown'}); "
            f"modem {'reachable' if modem_ok else '[red]NOT reachable[/red]'}")
        if not modem_ok:
            log("[red]Can't reach the modem from this process. On macOS, check System Settings > "
                "Privacy & Security > Local Network.[/red]")
        return True

    def run(self) -> None:
        s = self.settings
        log(f"[bold green]wifi-helper watchdog started[/bold green] (every {s.check_interval}s, home gateways: {', '.join(s.home_gateways)})")
        was_home = self.self_check()
        offline_streak = 0
        first_fail: tuple[float, str] | None = None
        outage: Outage | None = None

        while True:
            try:
                if self.sleep.slept():
                    raise Slept
                snap = quick_check(s)
                if snap.home.is_home != was_home:
                    was_home = snap.home.is_home
                    if was_home:
                        self.self_check()
                    else:
                        log(f"Left home network ({snap.home.reason}); idle until it's back.")
                if not snap.home.is_home or snap.http == "ok":
                    if outage:
                        outage.close("left home network" if not snap.home.is_home else None)
                        if snap.home.is_home:
                            log(f"[green]Online again after {outage.incident.duration_s:.0f}s.[/green]")
                    offline_streak, outage, first_fail = 0, None, None
                    time.sleep(s.check_interval)
                    continue

                if self.sleep.settling():
                    time.sleep(min(s.check_interval, 5))
                    continue
                offline_streak += 1
                first_fail = first_fail or (time.monotonic(), history.now_iso())
                if offline_streak < s.offline_checks_before_fix:
                    time.sleep(min(s.check_interval, 5))
                    continue

                snap = full_check(s, snap)
                verdict = classify(snap)
                if verdict in (Verdict.ONLINE, Verdict.NOT_HOME):
                    time.sleep(min(s.check_interval, 5))
                    continue
                if outage is None:
                    outage = Outage(verdict, *first_fail)
                    log(f"[bold red]Offline: {verdict.value}[/bold red] {EXPLANATIONS[verdict]}")
                elif outage.state.exhausted_for(s.reboot_cooldown):
                    outage.state = IncidentState()
                if self.heal(snap, outage.incident, outage.state, started=outage.t0):
                    outage.close(outage.incident.recovered_by)
                    offline_streak, outage, first_fail = 0, None, None
            except Slept:
                if outage:
                    outage.close("computer slept", ended=self.sleep.slept_at)
                offline_streak, outage, first_fail = 0, None, None
                log("Computer was asleep; letting Wi-Fi and VPN reconnect before judging the network.")
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

    def close(self, recovered_by: str | None, ended: float | None = None) -> None:
        self.incident.ended = history.now_iso(ended)
        self.incident.duration_s = round(time.monotonic() - self.t0, 1)
        self.incident.recovered_by = recovered_by or self.incident.recovered_by or "by itself"
        history.append(self.incident)
