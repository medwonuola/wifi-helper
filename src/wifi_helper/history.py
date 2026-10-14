"""Append-only outage log, so you can see how often and when the link dies."""

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
from .config import state_dir


def incidents_path() -> Path:
    return state_dir() / "incidents.jsonl"


@dataclass
class Incident:
    started: str
    verdict: str
    ended: str | None = None
    duration_s: float | None = None
    recovered_by: str | None = None
    actions: list[str] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    modem: dict | None = None


def now_iso(ts: float | None = None) -> str:
    return (datetime.fromtimestamp(ts) if ts is not None else datetime.now()).astimezone().isoformat(timespec="seconds")


def append(incident: Incident) -> None:
    path = incidents_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(asdict(incident)) + "\n")
    except OSError:
        pass


def load(limit: int | None = None) -> list[Incident]:
    path = incidents_path()
    if not path.exists():
        return []
    out: list[Incident] = []
    for line in path.read_text().splitlines():
        try:
            out.append(Incident(**json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue
    return out[-limit:] if limit else out


@dataclass(frozen=True)
class Summary:
    count: int
    total_down_s: float
    median_down_s: float
    by_verdict: Counter
    by_fix: Counter
    by_hour: Counter


def summarize(incidents: list[Incident]) -> Summary:
    durations = sorted(i.duration_s for i in incidents if i.duration_s is not None)
    median = durations[len(durations) // 2] if durations else 0.0
    by_hour: Counter = Counter()
    for i in incidents:
        try:
            by_hour[datetime.fromisoformat(i.started).hour] += 1
        except ValueError:
            pass
    return Summary(
        count=len(incidents),
        total_down_s=sum(durations),
        median_down_s=median,
        by_verdict=Counter(i.verdict for i in incidents),
        by_fix=Counter(i.recovered_by or "not recovered" for i in incidents),
        by_hour=by_hour,
    )
