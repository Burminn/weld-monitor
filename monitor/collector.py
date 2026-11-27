"""Tracker: zamienia kolejne odczyty robota na log stanów, sztuki i zdarzenia.
Collector: wątek, który cyklicznie pyta źródło danych (symulator albo OPC UA)."""
import threading
import time
from collections import deque


class Tracker:
    def __init__(self, cfg, db):
        self.cfg, self.db = cfg, db
        self.cycle_max = cfg.get("cycle_max_s", 600)
        self.lock = threading.Lock()
        self.s = {r["id"]: dict(state=None, program=None, counter=None,
                                last_piece=None, since=None, alarm_since=None)
                  for r in cfg["robots"]}
        self.live = {}
        self.trace = {r["id"]: deque(maxlen=120) for r in cfg["robots"]}

    def startup(self, now):
        """Okres, gdy monitor był wyłączony, liczymy jako 'brak danych/offline'."""
        last_seen = self.db.get_meta("last_seen")
        if last_seen:
            ts = min(float(last_seen), now)
            for rid in self.s:
                row = self.db.last_state(rid)
                if row and row["state"] != "offline":
                    self.db.add_state(ts, rid, "offline", None)
            mins = (now - ts) / 60
            self.db.add_event(now, None, "warn", "monitor",
                              f"Start monitora (przerwa w zapisie ok. {mins:.0f} min)")
        else:
            self.db.add_event(now, None, "info", "monitor", "Pierwszy start monitora")

    def ingest(self, rid, ts, snap, live=True):
        st = self.s[rid]
        connected = bool(snap.get("connected"))
        state = snap.get("state", "unknown") if connected else "offline"
        if connected and snap.get("program"):
            program = snap["program"]
        else:
            program = st["program"]

        if state != st["state"]:
            self.db.add_state(ts, rid, state, program)
            prev = st["state"]
            if prev is not None:
                if state == "alarm":
                    self.db.add_event(ts, rid, "err", "alarm", f"Alarm: {snap.get('alarm') or 'bez opisu'}")
                    st["alarm_since"] = ts
                elif prev == "alarm" and st["alarm_since"]:
                    self.db.add_event(ts, rid, "info", "alarm",
                                      f"Alarm zakończony po {(ts - st['alarm_since']) / 60:.1f} min")
                if state == "offline":
                    self.db.add_event(ts, rid, "warn", "conn",
                                      f"Utrata połączenia z robotem ({snap.get('error', 'brak odpowiedzi')})")
                elif prev == "offline":
                    self.db.add_event(ts, rid, "info", "conn", "Połączenie z robotem przywrócone")
            elif state == "alarm":
                st["alarm_since"] = ts
            st["state"], st["since"] = state, ts

        if connected and program and program != st["program"]:
            if st["program"] is not None:
                self.db.add_event(ts, rid, "info", "program", f"Zmiana programu: {program}")
            st["program"] = program

        counter = snap.get("counter") if connected else None
        if counter is not None:
            prev = st["counter"]
            if prev is not None:
                delta = counter - prev
                if 0 < delta <= 50:
                    for i in range(delta):
                        lp = st["last_piece"]
                        cyc = ts - lp if (i == 0 and lp and ts - lp <= self.cycle_max) else None
                        self.db.add_piece(ts, rid, st["program"], cyc)
                    st["last_piece"] = ts
                elif delta < 0 or delta > 50:
                    self.db.add_event(ts, rid, "warn", "counter",
                                      f"Licznik robota zmienił się nieoczekiwanie ({prev} → {counter}), pominięto")
            st["counter"] = counter

        if live:
            with self.lock:
                self.live[rid] = {
                    "connected": connected, "state": state, "program": st["program"],
                    "since": st["since"], "updated": ts, "counter": counter,
                    "alarm": snap.get("alarm") if state == "alarm" else None,
                    "current_a": snap.get("current_a"), "voltage_v": snap.get("voltage_v"),
                    "error": snap.get("error"),
                }
                self.trace[rid].append((ts, snap.get("current_a"), snap.get("voltage_v")))

    def snapshot_live(self, rid):
        with self.lock:
            return dict(self.live.get(rid, {})), list(self.trace[rid])


class Collector(threading.Thread):
    def __init__(self, cfg, source, tracker, db):
        super().__init__(daemon=True, name="collector")
        self.cfg, self.source, self.tracker, self.db = cfg, source, tracker, db
        self.stop_ev = threading.Event()

    def run(self):
        poll = self.cfg.get("poll_interval_s", 1.0)
        next_hb = 0
        while not self.stop_ev.is_set():
            t0 = time.time()
            for r in self.cfg["robots"]:
                try:
                    snap = self.source.read(r)
                except Exception as e:  # źródło nie może zabić pętli
                    snap = {"connected": False, "error": str(e)}
                self.tracker.ingest(r["id"], time.time(), snap)
            if t0 >= next_hb:
                self.db.set_meta("last_seen", f"{t0:.3f}")
                next_hb = t0 + 15
            self.stop_ev.wait(max(0.05, poll - (time.time() - t0)))
