"""Warstwa bazy SQLite: log stanów, sztuki, zdarzenia, checklista uruchomienia."""
import contextlib
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_log(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, robot TEXT NOT NULL,
  state TEXT NOT NULL, program TEXT);
CREATE INDEX IF NOT EXISTS ix_state ON state_log(robot, ts);
CREATE TABLE IF NOT EXISTS pieces(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, robot TEXT NOT NULL,
  program TEXT, cycle_s REAL);
CREATE INDEX IF NOT EXISTS ix_pieces ON pieces(robot, ts);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, robot TEXT,
  level TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ix_events ON events(ts);
CREATE TABLE IF NOT EXISTS checklist(
  robot TEXT NOT NULL, step TEXT NOT NULL, done INTEGER NOT NULL,
  PRIMARY KEY(robot, step));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.defer = False
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    @contextlib.contextmanager
    def bulk(self):
        """Wiele zapisów w jednej transakcji (np. historia demo)."""
        with self.lock:
            self.defer = True
            try:
                yield
            finally:
                self.defer = False
                self.conn.commit()

    def _exec(self, sql, args=()):
        with self.lock:
            self.conn.execute(sql, args)
            if not self.defer:
                self.conn.commit()

    def query(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    # --- zapisy
    def add_state(self, ts, robot, state, program):
        self._exec("INSERT INTO state_log(ts,robot,state,program) VALUES(?,?,?,?)",
                   (ts, robot, state, program))

    def add_piece(self, ts, robot, program, cycle_s):
        self._exec("INSERT INTO pieces(ts,robot,program,cycle_s) VALUES(?,?,?,?)",
                   (ts, robot, program, cycle_s))

    def add_event(self, ts, robot, level, kind, text):
        self._exec("INSERT INTO events(ts,robot,level,kind,text) VALUES(?,?,?,?,?)",
                   (ts, robot, level, kind, text))

    def set_meta(self, key, value):
        self._exec("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)", (key, str(value)))

    def set_check(self, robot, step, done):
        self._exec("INSERT OR REPLACE INTO checklist(robot,step,done) VALUES(?,?,?)",
                   (robot, step, 1 if done else 0))

    # --- odczyty
    def get_meta(self, key):
        r = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return r[0]["value"] if r else None

    def count_states(self):
        return self.query("SELECT COUNT(*) AS n FROM state_log")[0]["n"]

    def last_state(self, robot):
        r = self.query("SELECT * FROM state_log WHERE robot=? ORDER BY ts DESC, id DESC LIMIT 1", (robot,))
        return r[0] if r else None

    def state_at(self, robot, ts):
        r = self.query("SELECT state FROM state_log WHERE robot=? AND ts<=? "
                       "ORDER BY ts DESC, id DESC LIMIT 1", (robot, ts))
        return r[0]["state"] if r else None

    def states_between(self, robot, start, end):
        return self.query("SELECT ts,state FROM state_log WHERE robot=? AND ts>? AND ts<? "
                          "ORDER BY ts, id", (robot, start, end))

    def piece_stats(self, start, end):
        rows = self.query("SELECT robot, COUNT(*) AS n, AVG(cycle_s) AS avg_cycle FROM pieces "
                          "WHERE ts>=? AND ts<? GROUP BY robot", (start, end))
        return {r["robot"]: r for r in rows}

    def events(self, limit=100, robot=None):
        if robot:
            return self.query("SELECT * FROM events WHERE robot=? ORDER BY ts DESC, id DESC LIMIT ?",
                              (robot, limit))
        return self.query("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,))

    @staticmethod
    def _event_where(robot=None, ts_from=None, ts_to=None, level=None, kind=None, q=None):
        w, a = [], []
        if robot: w.append("robot=?"); a.append(robot)
        if ts_from is not None: w.append("ts>=?"); a.append(ts_from)
        if ts_to is not None: w.append("ts<?"); a.append(ts_to)
        if level: w.append("level=?"); a.append(level)
        if kind: w.append("kind=?"); a.append(kind)
        if q:
            esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            w.append("text LIKE ? ESCAPE '\\'"); a.append(f"%{esc}%")
        return (" WHERE " + " AND ".join(w)) if w else "", a

    def events_filtered(self, limit=100, offset=0, **flt):
        where, a = self._event_where(**flt)
        total = self.query("SELECT COUNT(*) AS n FROM events" + where, a)[0]["n"]
        rows = self.query("SELECT * FROM events" + where + " ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                          a + [limit, offset])
        return rows, total

    def event_kinds(self):
        return [r["kind"] for r in self.query("SELECT DISTINCT kind FROM events ORDER BY kind")]

    def bounds(self):
        r = self.query("SELECT MIN(ts) AS first, MAX(ts) AS last, COUNT(*) AS n FROM events")[0]
        return r

    def export_iter(self, table, robot=None, ts_from=None, ts_to=None, **flt):
        """Strumieniowy odczyt całej historii (osobne połączenie tylko do odczytu)."""
        assert table in ("events", "state_log", "pieces")
        if table == "events":
            where, a = self._event_where(robot, ts_from, ts_to, **flt)
        else:
            w, a = [], []
            if robot: w.append("robot=?"); a.append(robot)
            if ts_from is not None: w.append("ts>=?"); a.append(ts_from)
            if ts_to is not None: w.append("ts<?"); a.append(ts_to)
            where = (" WHERE " + " AND ".join(w)) if w else ""
        con = sqlite3.connect(self.path, check_same_thread=False)
        try:
            cur = con.execute(f"SELECT * FROM {table}{where} ORDER BY ts, id", a)
            cols = [c[0] for c in cur.description]
            yield cols
            while True:
                chunk = cur.fetchmany(2000)
                if not chunk:
                    break
                for row in chunk:
                    yield row
        finally:
            con.close()

    def checklist(self):
        out = {}
        for r in self.query("SELECT robot, step, done FROM checklist"):
            out.setdefault(r["robot"], {})[r["step"]] = bool(r["done"])
        return out
