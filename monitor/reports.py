"""Kalendarz zmian i rozliczenie produkcji (sztuki + czas w stanach)."""
from datetime import date, datetime, time, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

STATES = ("work", "idle", "alarm", "offline")


def get_tz(name):
    try:
        return ZoneInfo(name)
    except Exception:
        return datetime.now().astimezone().tzinfo


class Calendar:
    """Doba produkcyjna zaczyna się od startu pierwszej zmiany."""

    def __init__(self, cfg):
        self.tz = get_tz(cfg.get("timezone", "Europe/Warsaw"))
        self.shifts = []
        for s in cfg["shifts"]:
            h, m = map(int, s["start"].split(":"))
            self.shifts.append((s["name"], time(h, m)))
        self.shifts.sort(key=lambda x: x[1])

    def _at(self, d, t):
        return datetime.combine(d, t, tzinfo=self.tz)

    def day_bounds(self, d):
        s = self._at(d, self.shifts[0][1])
        e = self._at(d + timedelta(days=1), self.shifts[0][1])
        return s.timestamp(), e.timestamp()

    def shifts_of_day(self, d):
        starts = [self._at(d, t) for _, t in self.shifts]
        ends = starts[1:] + [self._at(d + timedelta(days=1), self.shifts[0][1])]
        return [(n, s.timestamp(), e.timestamp()) for (n, _), s, e in zip(self.shifts, starts, ends)]

    def prod_day(self, ts):
        dt = datetime.fromtimestamp(ts, self.tz)
        return dt.date() if dt.time() >= self.shifts[0][1] else dt.date() - timedelta(days=1)

    def current_shift(self, ts):
        for n, s, e in self.shifts_of_day(self.prod_day(ts)):
            if s <= ts < e:
                return {"name": n, "start": s, "end": e}
        return None


def state_seconds(db, rid, start, end, now):
    out = dict.fromkeys(STATES, 0.0)
    end = min(end, now)
    if end <= start:
        return out
    cur = db.state_at(rid, start)
    cursor = start
    for row in db.states_between(rid, start, end) + [{"ts": end, "state": None}]:
        if cur is not None:
            bucket = cur if cur in out else "offline"  # 'unknown' liczymy jako brak danych
            out[bucket] += max(0.0, row["ts"] - cursor)
        cursor, cur = row["ts"], row["state"] if row["state"] else cur
    return out


def build_report(db, cal, cfg, d_from, d_to, group, now):
    periods = []
    d = d_from
    while d <= d_to:
        if group == "day":
            s, e = cal.day_bounds(d)
            periods.append((d.isoformat(), s, e))
        else:
            for n, s, e in cal.shifts_of_day(d):
                periods.append((f"{d.isoformat()} zm. {n}", s, e))
        d += timedelta(days=1)
    rows = []
    for label, s, e in periods:
        stats = db.piece_stats(s, e)
        for r in cfg["robots"]:
            sec = state_seconds(db, r["id"], s, e, now)
            tot = sum(sec.values())
            p = stats.get(r["id"], {})
            rows.append({
                "label": label, "start": s, "end": e, "robot": r["id"], "robot_name": r["name"],
                "pieces": p.get("n", 0),
                "avg_cycle_s": round(p["avg_cycle"], 1) if p.get("avg_cycle") else None,
                "work_s": round(sec["work"]), "idle_s": round(sec["idle"]),
                "alarm_s": round(sec["alarm"]), "offline_s": round(sec["offline"]),
                "util": round(sec["work"] / tot, 3) if tot > 0 else None,
                "future": s > now,
            })
    return rows
