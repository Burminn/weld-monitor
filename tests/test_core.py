import datetime as dt
from zoneinfo import ZoneInfo

from monitor.reports import Calendar, build_report, state_seconds

WAW = ZoneInfo("Europe/Warsaw")


def ts(y, m, d, h=0, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=WAW).timestamp()


def snap(state="work", counter=None, program="P1", **kw):
    return {"connected": True, "state": state, "program": program, "counter": counter, **kw}


# ----------------------------------------------------------------- kalendarz

def test_production_day_starts_with_first_shift(cal):
    assert cal.prod_day(ts(2026, 3, 10, 5, 59)) == dt.date(2026, 3, 9)
    assert cal.prod_day(ts(2026, 3, 10, 6, 0)) == dt.date(2026, 3, 10)


def test_current_shift_boundaries(cal):
    assert cal.current_shift(ts(2026, 3, 10, 13, 59))["name"] == "I"
    assert cal.current_shift(ts(2026, 3, 10, 14, 0))["name"] == "II"
    assert cal.current_shift(ts(2026, 3, 10, 23, 0))["name"] == "III"
    assert cal.current_shift(ts(2026, 3, 11, 2, 0))["name"] == "III"


def test_dst_days_have_23_and_25_hours(cal):
    s, e = cal.day_bounds(dt.date(2026, 3, 28))  # 29.03.2026 zmiana na czas letni
    assert (e - s) / 3600 == 23
    s, e = cal.day_bounds(dt.date(2026, 10, 24))  # 25.10.2026 powrót na zimowy
    assert (e - s) / 3600 == 25


# ------------------------------------------------------------ tracker i sztuki

def test_counter_increment_creates_pieces_and_cycle(tracker, db):
    t0 = ts(2026, 3, 10, 8)
    tracker.ingest("R1", t0, snap(counter=100))
    tracker.ingest("R1", t0 + 60, snap(counter=101))
    tracker.ingest("R1", t0 + 120, snap(counter=102))
    rows = db.query("SELECT cycle_s FROM pieces ORDER BY ts")
    assert [r["cycle_s"] for r in rows] == [None, 60.0]


def test_unexpected_counter_jump_is_skipped_and_logged(tracker, db):
    t0 = ts(2026, 3, 10, 8)
    tracker.ingest("R1", t0, snap(counter=100))
    tracker.ingest("R1", t0 + 1, snap(counter=5000))   # skok
    tracker.ingest("R1", t0 + 2, snap(counter=10))     # reset licznika
    assert db.query("SELECT COUNT(*) n FROM pieces")[0]["n"] == 0
    assert db.query("SELECT COUNT(*) n FROM events WHERE kind='counter'")[0]["n"] == 2


def test_alarm_and_connection_events(tracker, db):
    t0 = ts(2026, 3, 10, 8)
    tracker.ingest("R1", t0, snap("work"))
    tracker.ingest("R1", t0 + 10, snap("alarm", alarm="E-001"))
    tracker.ingest("R1", t0 + 70, snap("work"))
    tracker.ingest("R1", t0 + 80, {"connected": False, "error": "timeout"})
    tracker.ingest("R1", t0 + 90, snap("work"))
    kinds = [(r["kind"], r["level"]) for r in db.query("SELECT kind, level FROM events ORDER BY ts, id")]
    assert ("alarm", "err") in kinds and ("alarm", "info") in kinds
    assert ("conn", "warn") in kinds and ("conn", "info") in kinds


def test_restart_gap_marked_as_offline(cfg, db):
    from monitor.collector import Tracker
    t0 = ts(2026, 3, 10, 8)
    first = Tracker(cfg, db)
    first.startup(t0)
    first.ingest("R1", t0, snap("work"))
    db.set_meta("last_seen", f"{t0 + 10:.3f}")
    second = Tracker(cfg, db)
    second.startup(t0 + 3600)
    assert db.last_state("R1")["state"] == "offline"
    ev = db.query("SELECT text FROM events WHERE kind='monitor' ORDER BY id DESC LIMIT 1")[0]["text"]
    assert "przerwa" in ev


# ------------------------------------------------------------------ rozliczenie

def test_state_seconds_sum_to_period(tracker, db):
    t0 = ts(2026, 3, 10, 6)
    tracker.ingest("R1", t0, snap("work"))
    tracker.ingest("R1", t0 + 600, snap("idle"))
    tracker.ingest("R1", t0 + 900, snap("alarm", alarm="x"))
    tracker.ingest("R1", t0 + 1200, snap("work"))
    sec = state_seconds(db, "R1", t0, t0 + 1800, t0 + 1800)
    assert sec == {"work": 1200.0, "idle": 300.0, "alarm": 300.0, "offline": 0.0}


def test_state_seconds_does_not_count_future(tracker, db):
    t0 = ts(2026, 3, 10, 6)
    tracker.ingest("R1", t0, snap("work"))
    sec = state_seconds(db, "R1", t0, t0 + 3600, t0 + 600)
    assert sum(sec.values()) == 600


def test_report_rows_per_shift(cfg, cal, tracker, db):
    t0 = ts(2026, 3, 10, 6)
    tracker.ingest("R1", t0, snap("work", counter=1))
    tracker.ingest("R1", t0 + 100, snap("work", counter=2))
    rows = build_report(db, cal, cfg, dt.date(2026, 3, 10), dt.date(2026, 3, 10), "shift", t0 + 7200)
    r1 = [r for r in rows if r["robot"] == "R1"]
    assert [r["label"].split()[-1] for r in r1] == ["I", "II", "III"]
    assert r1[0]["pieces"] == 1
