#!/usr/bin/env python3
"""cronexpr.py — a small 5-field cron evaluator, timezone-aware (stdlib only).

Agents (3.1 "Crew up") run on cron schedules written in the wall-clock time of a
zone (Europe/Zurich by default): "0 2 * * *" means 02:00 in Zurich, summer and
winter. Supported: `*`, numbers, lists `1,15`, ranges `1-5`, steps `*/10` `0-30/5`,
month and weekday names (`jan`, `mon`), weekday 0 or 7 = Sunday, and the usual
macros (@hourly @daily @weekly @monthly @yearly). Day-of-month and day-of-week
follow Vixie cron: when both are restricted, either one matching is enough.

DST: an occurrence that falls in the spring-forward gap does not exist and is
skipped; one in the repeated autumn hour runs once (the first time).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Europe/Zurich"

_MACROS = {
    "@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *", "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_MONTHS = {m: i + 1 for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split())}
_DOWS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}
# (min, max) per field: minute hour dom month dow
_BOUNDS = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


class CronError(ValueError):
    pass


@dataclass(frozen=True)
class Cron:
    minutes: frozenset
    hours: frozenset
    doms: frozenset
    months: frozenset
    dows: frozenset          # 0..6, Sunday = 0
    dom_star: bool
    dow_star: bool
    expr: str

    def _day_ok(self, d: datetime) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.doms
        dow_ok = (d.isoweekday() % 7) in self.dows
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok

    def next_after(self, after: float, tz: str = DEFAULT_TZ) -> float:
        """First occurrence strictly after the UTC epoch `after`, as a UTC epoch."""
        z = ZoneInfo(tz or DEFAULT_TZ)
        local = datetime.fromtimestamp(after, z).replace(tzinfo=None, second=0, microsecond=0)
        t = local + timedelta(minutes=1)
        limit = local + timedelta(days=366 * 5)
        while t <= limit:
            if not self._day_ok(t):
                t = (t + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if t.hour not in self.hours:
                t = (t + timedelta(hours=1)).replace(minute=0)
                continue
            if t.minute not in self.minutes:
                t += timedelta(minutes=1)
                continue
            aware = t.replace(tzinfo=z, fold=0)
            ts = aware.timestamp()
            # spring-forward gap: the wall-clock time does not exist → skip it
            back = datetime.fromtimestamp(ts, z).replace(tzinfo=None)
            if back == t and ts > after:
                return ts
            t += timedelta(minutes=1)
        raise CronError(f"no occurrence within 5 years: {self.expr!r}")

    def describe_next(self, n: int = 3, after: float | None = None,
                      tz: str = DEFAULT_TZ) -> list[str]:
        """The next `n` occurrences as ISO local times — for the UI preview."""
        out, cur = [], after if after is not None else datetime.now(timezone.utc).timestamp()
        z = ZoneInfo(tz or DEFAULT_TZ)
        for _ in range(n):
            cur = self.next_after(cur, tz)
            out.append(datetime.fromtimestamp(cur, z).isoformat(timespec="minutes"))
        return out


def _field(spec: str, idx: int) -> set[int]:
    lo, hi = _BOUNDS[idx]
    names = _MONTHS if idx == 3 else _DOWS if idx == 4 else {}
    out: set[int] = set()

    def val(s: str) -> int:
        s = s.strip().lower()
        if s in names:
            return names[s]
        if not s.isdigit():
            raise CronError(f"bad value {s!r}")
        return int(s)

    for part in spec.split(","):
        if not part:
            raise CronError("empty list item")
        step = 1
        if "/" in part:
            part, st = part.split("/", 1)
            if not st.isdigit() or int(st) == 0:
                raise CronError(f"bad step {st!r}")
            step = int(st)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = val(x), val(y)
        else:
            a = val(part)
            b = hi if step > 1 else a
        if a < lo or b > hi or a > b:
            raise CronError(f"value out of range {lo}-{hi}: {spec!r}")
        out.update(range(a, b + 1, step))
    return out


def parse(expr: str) -> Cron:
    raw = (expr or "").strip()
    e = _MACROS.get(raw.lower(), raw)
    parts = e.split()
    if len(parts) != 5:
        raise CronError("a cron expression has 5 fields: minute hour day-of-month month "
                        "day-of-week (e.g. '0 2 * * *' = every night at 02:00)")
    f = [_field(p, i) for i, p in enumerate(parts)]
    dows = frozenset(d % 7 for d in f[4])
    return Cron(frozenset(f[0]), frozenset(f[1]), frozenset(f[2]), frozenset(f[3]), dows,
                dom_star=parts[2].startswith("*"), dow_star=parts[4].startswith("*"), expr=raw)


def valid_tz(tz: str) -> bool:
    try:
        ZoneInfo(tz)
        return True
    except Exception:  # noqa: BLE001
        return False
