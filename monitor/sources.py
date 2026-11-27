"""Źródła danych. Każde ma metodę read(robot_cfg) -> dict:
  connected, state (work|idle|alarm|unknown), program, counter, alarm,
  current_a, voltage_v, error
Symulator służy do rozwijania panelu bez robota. OpcUaSource jest
NIEPRZETESTOWANY na prawdziwym robocie (brak dostępu do sprzętu i do listy węzłów)."""
import math
import os
import random
import threading
import time

from .settings import robot_password

PROGRAMS = ["PR-101 Rama A", "PR-102 Wspornik", "PR-205 Płyta bazowa"]
ALARMS = ["E-014 Błąd zajarzenia łuku", "E-021 Brak drutu",
          "E-033 Kolizja palnika", "E-007 Brak gazu osłonowego"]


class SimModel:
    """Prosty automat: praca (cykl) -> sztuka +1 -> praca / postój / alarm."""

    def __init__(self, seed, speed=1.0):
        self.r = random.Random(seed)
        self.speed = speed
        self.state = "work"
        self.program = self.r.choice(PROGRAMS)
        self.counter = self.r.randint(1000, 9000)
        self.alarm = None
        self.t_next = None
        self.offline = False

    def _dur(self, lo, hi):
        return self.r.uniform(lo, hi) / self.speed

    def step(self, now):
        if self.t_next is None:
            self.state, self.t_next = "work", now + self._dur(45, 70)
        while now >= self.t_next:
            t = self.t_next
            if self.state == "work":
                self.counter += 1
                x = self.r.random()
                if x < 0.02:
                    self._alarm(t)
                elif x < 0.14:
                    self.state, self.t_next = "idle", t + self._dur(20, 90)
                else:
                    self.t_next = t + self._dur(45, 70)
            else:
                self.alarm = None
                if self.r.random() < 0.3:
                    self.program = self.r.choice(PROGRAMS)
                self.state, self.t_next = "work", t + self._dur(45, 70)

    def _alarm(self, t):
        self.state, self.alarm = "alarm", self.r.choice(ALARMS)
        self.t_next = t + self._dur(60, 240)

    def inject_fault(self, now):
        self._alarm(now)

    def snapshot(self, now):
        self.step(now)
        if self.offline:
            return {"connected": False, "error": "symulacja: odcięto połączenie"}
        arc = self.state == "work" and self.r.random() < 0.85
        return {
            "connected": True, "state": self.state, "program": self.program,
            "counter": self.counter, "alarm": self.alarm,
            "current_a": round(175 + 12 * math.sin(now * 0.9) + self.r.gauss(0, 2), 1) if arc else 0.0,
            "voltage_v": round(23.5 + 0.6 * math.sin(now * 0.7) + self.r.gauss(0, 0.15), 1) if arc else 0.0,
        }


class SimSource:
    def __init__(self, models):
        self.models = models
        self.lock = threading.Lock()

    def read(self, robot):
        with self.lock:
            return self.models[robot["id"]].snapshot(time.time())

    def fault(self, rid):
        with self.lock:
            self.models[rid].inject_fault(time.time())

    def toggle_offline(self, rid):
        with self.lock:
            m = self.models[rid]
            m.offline = not m.offline
            return m.offline


def make_models(cfg, speed):
    return {r["id"]: SimModel(seed=sum(map(ord, r["id"])), speed=speed) for r in cfg["robots"]}


def seed_history(tracker, cfg, models, days, step_s=2.0):
    """Generuje historię demo (tempo 1x), żeby raporty nie były puste."""
    now = time.time()
    t = now - days * 86400
    with tracker.db.bulk():
        while t < now:
            for r in cfg["robots"]:
                tracker.ingest(r["id"], t, models[r["id"]].snapshot(t), live=False)
            t += step_s
    tracker.db.set_meta("last_seen", f"{now:.3f}")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OpcUaSource:
    """Klient OPC UA (asyncua) w osobnym wątku; read() zwraca ostatni odczyt z cache."""

    def __init__(self, cfg, nodes):
        import asyncio
        self.cfg, self.nodes = cfg, nodes.get("metrics", {})
        self.cache = {r["id"]: {"connected": False, "error": "jeszcze nie połączono"}
                      for r in cfg["robots"]}
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True, name="opcua").start()

    def read(self, robot):
        return dict(self.cache[robot["id"]])

    def _run(self):
        import asyncio
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(asyncio.gather(*[self._robot(r) for r in self.cfg["robots"]]))

    async def _robot(self, r):
        import asyncio
        from asyncua import Client
        sec = self.cfg.get("opcua", {})
        poll = self.cfg.get("poll_interval_s", 1.0)
        while True:
            try:
                client = Client(url=f"opc.tcp://{r['ip']}:{r.get('port', 4840)}", timeout=5)
                if r.get("username"):
                    client.set_user(r["username"])
                    client.set_password(robot_password(r))
                if sec.get("security_policy") and sec["security_policy"] != "None":
                    ab = lambda p: p if os.path.isabs(p) else os.path.join(ROOT, p)
                    s = (f"{sec['security_policy']},{sec.get('security_mode', 'SignAndEncrypt')},"
                         f"{ab(sec['client_cert'])},{ab(sec['client_key'])}")
                    if r.get("server_cert"):
                        s += f",{ab(r['server_cert'])}"
                    await client.set_security_string(s)
                async with client:
                    while True:
                        vals = {}
                        for key, m in self.nodes.items():
                            if m.get("node_id"):
                                vals[key] = await client.get_node(m["node_id"]).read_value()
                        self.cache[r["id"]] = self._map(vals)
                        await asyncio.sleep(poll)
            except Exception as e:
                self.cache[r["id"]] = {"connected": False, "error": f"{type(e).__name__}: {e}"}
                await asyncio.sleep(5)

    def _map(self, v):
        out = {"connected": True, "state": "unknown"}
        if "state" in v:
            out["state"] = self.nodes["state"].get("state_map", {}).get(str(v["state"]), "unknown")
        if "program" in v:
            out["program"] = str(v["program"])
        if "piece_counter" in v:
            out["counter"] = int(v["piece_counter"])
        if "alarm_text" in v and v["alarm_text"]:
            out["alarm"] = str(v["alarm_text"])
        if "weld_current" in v:
            out["current_a"] = float(v["weld_current"])
        if "weld_voltage" in v:
            out["voltage_v"] = float(v["weld_voltage"])
        return out
