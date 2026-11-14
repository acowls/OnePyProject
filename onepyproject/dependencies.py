"""Parser and representation for task dependencies and their lag units."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re


@dataclass(frozen=True)
class Dependency:
    uid: int
    kind: str = "FS"
    lag: float = 0.0
    unit: str = "d"


_PATTERN = re.compile(
    r"^\s*(\d+)\s*(FS|SS|FF|SF)?\s*(?:([+-])\s*((?:\d+(?:\.\d+)?|\.\d+)(?:e[+-]?\d+)?)\s*(emo|ed|eh|em|ew|mo|d|h|m|w|%)?)?\s*$",
    re.IGNORECASE,
)
WORK_MINUTES = {"m": 1, "h": 60, "d": 480, "w": 2400, "mo": 9600}
ELAPSED_MINUTES = {"em": 1, "eh": 60, "ed": 1440, "ew": 10080, "emo": 43200}


def parse_dependency(value: int | str) -> Dependency:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Predecessors must be task IDs or dependency expressions")
    match = _PATTERN.fullmatch(str(value))
    if not match or int(match[1]) <= 0:
        raise ValueError(f"Invalid predecessor {value!r}; use e.g. 5, 5SS+3d or 5FF-1d")
    lag = float(match[4]) if match[4] else 0.0
    if match[3] == "-":
        lag = -lag
    if not math.isfinite(lag):
        raise ValueError("Predecessor lag must be finite")
    return Dependency(int(match[1]), (match[2] or "FS").upper(), lag, (match[5] or "d").lower())


def format_dependency(link: Dependency) -> int | str:
    if link.kind == "FS" and not link.lag:
        return link.uid
    suffix = f"{link.lag:+.15g}{link.unit}" if link.lag else ""
    return f"{link.uid}{link.kind}{suffix}"


def parse_predecessors(text: str) -> list[int | str]:
    if not text.strip():
        return []
    result = [format_dependency(parse_dependency(part)) for part in text.split(",")]
    ids = [parse_dependency(item).uid for item in result]
    if len(ids) != len(set(ids)):
        raise ValueError("A task cannot have the same predecessor more than once")
    return result


def task_numbers(tasks) -> dict[int, int]:
    """Map stable task identifiers to their current one-based display numbers."""
    return {task.uid: number for number, task in enumerate(tasks, 1)}


def remap_predecessors(values: list[int | str], mapping: dict[int, int]) -> list[int | str]:
    """Replace only IDs, retaining link type, lag, precision, and stored text."""
    result = []
    for value in values:
        link = parse_dependency(value)
        if link.uid not in mapping:
            raise ValueError(f"Task ID {link.uid} does not exist")
        replacement = mapping[link.uid]
        if isinstance(value, int):
            result.append(replacement)
        else:
            result.append(re.sub(r"^(\s*)\d+", lambda match: f"{match.group(1)}{replacement}", value, count=1))
    return result


def parse_display_predecessors(text: str, tasks) -> list[int | str]:
    numbers = task_numbers(tasks)
    return remap_predecessors(parse_predecessors(text), {number: uid for uid, number in numbers.items()})


def lag_minutes(link: Dependency, predecessor_duration_days: float) -> tuple[float, bool]:
    if link.unit == "%":
        minutes = predecessor_duration_days * 480 * link.lag / 100
        elapsed = False
    elif link.unit in ELAPSED_MINUTES:
        minutes = link.lag * ELAPSED_MINUTES[link.unit]
        elapsed = True
    else:
        minutes = link.lag * WORK_MINUTES[link.unit]
        elapsed = False
    if not math.isfinite(minutes) or abs(minutes) > 480 * 100000:
        raise ValueError("Predecessor lag is too large")
    return minutes, elapsed
