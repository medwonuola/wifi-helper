from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
from statistics import median
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(asdict(incident)) + "\n")


def load() -> list[Incident]:
    path = incidents_path()
    return [Incident(**json.loads(line)) for line in path.read_text().splitlines()] if path.exists() else []


@dataclass(frozen=True)
class Summary:
    count: int
    total_down_s: float
    median_down_s: float
    by_verdict: Counter
    by_fix: Counter
    by_hour: Counter


def summarize(incidents: list[Incident]) -> Summary:
    durations = [i.duration_s for i in incidents if i.duration_s is not None]
    return Summary(
        count=len(incidents),
        total_down_s=sum(durations),
        median_down_s=median(durations) if durations else 0.0,
        by_verdict=Counter(i.verdict for i in incidents),
        by_fix=Counter(i.recovered_by or "not recovered" for i in incidents),
        by_hour=Counter(datetime.fromisoformat(i.started).hour for i in incidents),
    )
