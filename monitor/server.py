"""Serwer WWW: API JSON + panel w przeglądarce. Uruchomienie: python -m monitor"""
import argparse
import csv
import hmac
import io
import json
import os
import socket
import sys
import time
from datetime import date, datetime, timedelta

from flask import Flask, Response, abort, jsonify, request, send_from_directory

from . import settings, sources
from .collector import Collector, Tracker
from .db import DB
from .reports import Calendar, build_report

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Kroki uruchomienia komunikacji OPC UA z robotem G4 — wg instrukcji producenta „Podłączenie robota serii G4 do sieci LAN” v1.1 (nie dołączonej do repo)
CHECKLIST = [
    ("kabel", "Kabel LAN wpięty w górne gniazdo RJ45 w sterowniku robota", "str. 3"),
    ("licencja", "Dodana funkcja „OPC UA Server Function” (Set → Management Settings → System → Add(option)). Brak opcji = kontakt z dostawcą robota", "str. 4"),
    ("extcomm", "Dodana funkcja „External Comm.” (dodaje się razem z OPC UA; w razie braku Add(standard))", "str. 4"),
    ("cert_klienta", "Certyfikat klienta utworzony (DTPS: Receive from Robot → OPC UA Client certificate → Create; może też IWNB / Production Management) i zapisany na SD/USB", "str. 5"),
    ("ext_edit", "External comm. → Edit: adres IP, maska, brama i unikalny numer robota", "str. 6"),
    ("backup", "Backup comm. → zaznaczone „Valid”", "str. 6"),
    ("tawers", "Opcjonalnie: ustawienia „TAWERS weld data recording communication” (jeśli robot ma to oprogramowanie)", "str. 6"),
    ("opc_valid", "OPC UA Server Function → Edit → Function Settings → „Valid”", "str. 7"),
    ("security", "Security Policy ustawione (logowanie włączone/wyłączone); zmień fabryczne hasło", "str. 7"),
    ("srv_export", "Manage Certificates → Server certificates → Export (certyfikat serwera na SD/USB)", "str. 8"),
    ("trusted", "Trusted Certificates → Import certyfikatu klienta; po dodaniu widoczny na liście", "str. 8"),
    ("auto", "Robot w trybie automatycznym (komunikacja załącza się w tle tylko wtedy)", "str. 8"),
    ("dtps_register", "W DTPS: OPC UA Server certificate → Register (IP robota, plik certyfikatu serwera, te same tryby zabezpieczeń, login/hasło); test = widać pliki z robota", "str. 9–10"),
]


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return sorted(ips) or ["127.0.0.1"]


def create_app(cfg, db, tracker, cal, source, nodes):
    app = Flask(__name__, static_folder=os.path.join(os.path.dirname(__file__), "static"), static_url_path="/static")
    robots = {r["id"]: r for r in cfg["robots"]}
    is_sim = cfg["mode"] == "sim" and cfg.get("sim", {}).get("controls", False)
    creds = settings.panel_credentials()
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

    @app.before_request
    def guard():
        """Opcjonalne logowanie (HTTP Basic) i ochrona zapisów przed żądaniami z obcych stron."""
        if creds and request.endpoint != "healthz":
            a = request.authorization
            ok = (a is not None and a.type == "basic"
                  and hmac.compare_digest((a.username or "").encode(), creds[0].encode())
                  & hmac.compare_digest((a.password or "").encode(), creds[1].encode()))
            if not ok:
                return Response("Wymagane logowanie", 401, {"WWW-Authenticate": 'Basic realm="monitor"'})
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] != request.host:
                abort(403)
            if not request.is_json:
                abort(415)

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/state")
    def api_state():
        now = time.time()
        shift = cal.current_shift(now)
        day_s, day_e = cal.day_bounds(cal.prod_day(now))
        sp = db.piece_stats(shift["start"], shift["end"]) if shift else {}
        dp = db.piece_stats(day_s, day_e)
        out = []
        for r in cfg["robots"]:
            live, trace = tracker.snapshot_live(r["id"])
            live.setdefault("state", "unknown")
            out.append({
                "id": r["id"], "name": r["name"], "controller": r["controller"],
                "robot_no": r["robot_no"], "ip": r["ip"], **live,
                "pieces_shift": sp.get(r["id"], {}).get("n", 0),
                "pieces_day": dp.get(r["id"], {}).get("n", 0),
                "avg_cycle_shift": sp.get(r["id"], {}).get("avg_cycle"),
                "trace": [[round(t, 1), a, v] for t, a, v in trace[-90:]],
            })
        return jsonify({"now": now, "shift": shift, "robots": out})

    def day_ts(s, end=False):
        """Początek (lub koniec, wyłącznie) doby kalendarzowej w strefie z konfiguracji."""
        if not s:
            return None
        try:
            d = date.fromisoformat(s)
        except ValueError:
            abort(400, "Zła data (format RRRR-MM-DD)")
        if end:
            d += timedelta(days=1)
        return datetime.combine(d, datetime.min.time(), tzinfo=cal.tz).timestamp()

    def event_filter():
        a = request.args
        robot = a.get("robot") or None
        if robot and robot not in robots:
            abort(400, "Nieznane stanowisko")
        level = a.get("level") or None
        if level and level not in ("err", "warn", "info"):
            abort(400, "Zły poziom")
        return dict(robot=robot, ts_from=day_ts(a.get("from")), ts_to=day_ts(a.get("to"), True),
                    level=level, kind=a.get("kind") or None, q=(a.get("q") or "").strip()[:100] or None)

    @app.get("/api/events")
    def api_events():
        try:
            limit = max(1, min(int(request.args.get("limit", 100)), 1000))
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            abort(400, "Zły limit/offset")
        rows, total = db.events_filtered(limit, offset, **event_filter())
        resp = jsonify(rows)
        resp.headers["X-Total-Count"] = str(total)
        return resp

    @app.get("/api/events/meta")
    def api_events_meta():
        return jsonify({"kinds": db.event_kinds(), **db.bounds()})

    LEVELS = {"err": "Błąd", "warn": "Uwaga", "info": "Info"}
    STATES = {"work": "Praca", "idle": "Postój", "alarm": "Alarm", "offline": "Brak połączenia", "unknown": "Brak połączenia"}

    @app.get("/api/export/<table>.csv")
    def api_export(table):
        if table not in ("events", "state_log", "pieces"):
            abort(404)
        flt = event_filter() if table == "events" else dict(
            robot=request.args.get("robot") or None,
            ts_from=day_ts(request.args.get("from")), ts_to=day_ts(request.args.get("to"), True))
        if flt.get("robot") and flt["robot"] not in robots:
            abort(400, "Nieznane stanowisko")
        tz = cal.tz
        lt = lambda ts: datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M:%S")
        pl = lambda x: "" if x is None else str(round(x, 1)).replace(".", ",")
        rname = lambda r: robots[r]["name"] if r in robots else ("system" if not r else r)
        heads = {"events": ["Czas", "Stanowisko", "Poziom", "Rodzaj", "Zdarzenie"],
                 "state_log": ["Czas", "Stanowisko", "Stan", "Program"],
                 "pieces": ["Czas", "Stanowisko", "Program", "Cykl [s]"]}

        def gen():
            it = db.export_iter(table, **flt)
            next(it)  # nazwy kolumn
            buf = io.StringIO()
            w = csv.writer(buf, delimiter=";")
            w.writerow(heads[table])
            yield "﻿" + buf.getvalue()
            for row in it:
                if table == "events":
                    _, ts, rb, lv, kd, tx = row
                    out = [lt(ts), rname(rb), LEVELS.get(lv, lv), kd, tx]
                elif table == "state_log":
                    _, ts, rb, st, pr = row
                    out = [lt(ts), rname(rb), STATES.get(st, st), pr or ""]
                else:
                    _, ts, rb, pr, cy = row
                    out = [lt(ts), rname(rb), pr or "", pl(cy)]
                buf.seek(0); buf.truncate()
                w.writerow([("'" + c if isinstance(c, str) and c and c[0] in "=+-@" else c) for c in out])
                yield buf.getvalue()

        stamp = datetime.now(tz).strftime("%Y%m%d")
        return Response(gen(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={table}_{stamp}.csv"})

    def parse_range():
        today = cal.prod_day(time.time())
        try:
            d_to = date.fromisoformat(request.args["to"]) if request.args.get("to") else today
            d_from = date.fromisoformat(request.args["from"]) if request.args.get("from") else d_to - timedelta(days=2)
        except ValueError:
            abort(400, "Zła data (format RRRR-MM-DD)")
        if d_from > d_to or (d_to - d_from).days > 62:
            abort(400, "Zakres dat: od ≤ do, maksymalnie 63 dni")
        group = "day" if request.args.get("group") == "day" else "shift"
        return d_from, d_to, group

    @app.get("/api/report")
    def api_report():
        d_from, d_to, group = parse_range()
        rows = build_report(db, cal, cfg, d_from, d_to, group, time.time())
        return jsonify({"from": d_from.isoformat(), "to": d_to.isoformat(), "group": group, "rows": rows})

    @app.get("/api/report.csv")
    def api_report_csv():
        d_from, d_to, group = parse_range()
        rows = build_report(db, cal, cfg, d_from, d_to, group, time.time())
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        w.writerow(["Okres", "Stanowisko", "Sztuki", "Sr. cykl [s]", "Praca [min]", "Postoj [min]",
                    "Alarm [min]", "Brak danych [min]", "Wykorzystanie [%]"])
        pl = lambda x: "" if x is None else str(x).replace(".", ",")  # przecinek dziesiętny dla polskiego Excela
        for r in rows:
            w.writerow([r["label"], r["robot_name"], r["pieces"], pl(r["avg_cycle_s"]),
                        pl(round(r["work_s"] / 60, 1)), pl(round(r["idle_s"] / 60, 1)),
                        pl(round(r["alarm_s"] / 60, 1)), pl(round(r["offline_s"] / 60, 1)),
                        pl(None if r["util"] is None else round(r["util"] * 100, 1))])
        data = "﻿" + buf.getvalue()  # BOM: Excel poprawnie czyta polskie znaki
        return Response(data, mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename=produkcja_{d_from}_{d_to}.csv"})

    @app.get("/api/config")
    def api_config():
        hidden = ("password", "password_env", "username", "server_cert")
        safe = [{k: v for k, v in r.items() if k not in hidden} for r in cfg["robots"]]
        sec = cfg.get("opcua", {})
        return jsonify({"mode": cfg["mode"], "robots": safe, "shifts": cfg["shifts"],
                        "opcua": {k: sec.get(k) for k in ("security_policy", "security_mode")}, "poll_interval_s": cfg.get("poll_interval_s"),
                        "checklist": [{"id": i, "text": t, "page": p} for i, t, p in CHECKLIST],
                        "checklist_state": db.checklist(),
                        "nodes": {k: {"node_id": m.get("node_id"), "opis": m.get("opis")}
                                  for k, m in nodes.get("metrics", {}).items()}})

    @app.post("/api/checklist/<rid>")
    def api_checklist(rid):
        body = request.get_json(silent=True) or {}
        if rid not in robots or body.get("step") not in {c[0] for c in CHECKLIST}:
            abort(400)
        db.set_check(rid, body["step"], bool(body.get("done")))
        return jsonify({"ok": True})

    if is_sim:
        @app.post("/api/sim/<rid>/fault")
        def sim_fault(rid):
            if rid not in robots:
                abort(404)
            source.fault(rid)
            return jsonify({"ok": True})

        @app.post("/api/sim/<rid>/offline")
        def sim_offline(rid):
            if rid not in robots:
                abort(404)
            return jsonify({"offline": source.toggle_offline(rid)})

    return app


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def default_config():
    """config.json (lokalny, poza repozytorium) albo przykładowy config.example.json."""
    own = os.path.join(ROOT, "config.json")
    return own if os.path.exists(own) else os.path.join(ROOT, "config.example.json")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Monitor stanowisk spawalniczych")
    ap.add_argument("--config", default=default_config())
    ap.add_argument("--nodes", default=os.path.join(ROOT, "nodes.json"))
    ap.add_argument("--mode", choices=["sim", "opcua"], help="nadpisuje config.json")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--speed", type=float, help="przyspieszenie symulatora (tylko tryb sim)")
    a = ap.parse_args(argv)

    cfg = load_json(a.config)
    nodes = load_json(a.nodes) if os.path.exists(a.nodes) else {}
    if a.mode: cfg["mode"] = a.mode
    if a.host: cfg["host"] = a.host
    if a.port: cfg["port"] = a.port
    if a.speed: cfg.setdefault("sim", {})["speed"] = a.speed

    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    db = DB(os.path.join(ROOT, "data", f"{cfg['mode']}.db"))  # osobna baza dla symulacji i prawdziwych robotów
    cal = Calendar(cfg)
    tracker = Tracker(cfg, db)
    tracker.startup(time.time())

    if cfg["mode"] == "sim":
        simcfg = cfg.get("sim", {})
        models = sources.make_models(cfg, 1.0)
        days = simcfg.get("seed_history_days", 3)
        if days and db.count_states() == 0:
            print("Przygotowuję bazę danych…")
            sources.seed_history(tracker, cfg, models, days)
        for m in models.values():
            m.speed, m.t_next = simcfg.get("speed", 4), None
        source = sources.SimSource(models)
    else:
        source = sources.OpcUaSource(cfg, nodes)

    Collector(cfg, source, tracker, db).start()
    app = create_app(cfg, db, tracker, cal, source, nodes)

    host, port = cfg.get("host", "127.0.0.1"), cfg.get("port", 8080)
    if not settings.is_loopback(host) and not settings.panel_credentials():
        print("\n!!! UWAGA: panel jest dostępny w sieci i nie ma logowania. Ustaw MONITOR_USER i "
              "MONITOR_PASSWORD albo host 127.0.0.1.", file=sys.stderr)
    if cfg["mode"] == "opcua":
        for r in cfg["robots"]:
            if settings.is_weak(settings.robot_password(r)):
                print(f"!!! UWAGA: robot {r['id']} ma puste albo fabryczne hasło OPC UA.", file=sys.stderr)
    print("\nMonitor stanowisk spawalniczych działa. Panel dostępny pod adresem:")
    print(f"  http://localhost:{port}")
    if host in ("0.0.0.0", "::"):
        for ip in local_ips():
            print(f"  http://{ip}:{port}   (z innych komputerów w sieci)")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
