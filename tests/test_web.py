import datetime as dt
import time

import pytest


def test_state_endpoint_and_headers(make_client):
    c = make_client()
    r = c.get("/api/state")
    assert r.status_code == 200 and len(r.get_json()["robots"]) == 2
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    assert c.get("/healthz").get_json() == {"status": "ok"}


def test_auth_when_credentials_set(make_client):
    c = make_client({"MONITOR_USER": "op", "MONITOR_PASSWORD": "dluga-tajna-fraza"})
    assert c.get("/api/state").status_code == 401
    assert c.get("/healthz").status_code == 200
    import base64
    good = {"Authorization": "Basic " + base64.b64encode(b"op:dluga-tajna-fraza").decode()}
    bad = {"Authorization": "Basic " + base64.b64encode(b"op:zle").decode()}
    assert c.get("/api/state", headers=good).status_code == 200
    assert c.get("/api/state", headers=bad).status_code == 401


def test_config_endpoint_hides_secrets(make_client, monkeypatch):
    monkeypatch.setenv("ROBOT_R1_PASSWORD", "super-tajne-haslo")
    body = make_client().get("/api/config").get_data(as_text=True)
    assert "super-tajne-haslo" not in body and "password" not in body and "client_key" not in body


def test_checklist_post_requires_json_and_same_origin(make_client):
    c = make_client()
    step = c.get("/api/config").get_json()["checklist"][0]["id"]
    ok = c.post("/api/checklist/R1", json={"step": step, "done": True})
    assert ok.status_code == 200
    assert c.post("/api/checklist/R1", data="step=x", content_type="application/x-www-form-urlencoded").status_code == 415
    assert c.post("/api/checklist/R1", json={"step": step, "done": True},
                  headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/checklist/NOPE", json={"step": step}).status_code == 400


def test_event_filters_validate_input(make_client):
    c = make_client()
    assert c.get("/api/events?robot=ZZ").status_code == 400
    assert c.get("/api/events?level=hack").status_code == 400
    assert c.get("/api/events?from=2026-13-40").status_code == 400
    assert c.get("/api/events?limit=abc").status_code == 400
    assert c.get("/api/events?limit=999999").status_code == 200


def test_like_search_is_escaped(make_client, db):
    db.add_event(time.time(), "R1", "info", "x", "100% gotowe")
    db.add_event(time.time(), "R1", "info", "x", "inny wpis")
    rows = make_client().get("/api/events?q=%25").get_json()
    assert [r["text"] for r in rows] == ["100% gotowe"]


def test_csv_export_neutralizes_formulas(make_client, db):
    db.add_event(time.time(), "R1", "err", "alarm", "=HYPERLINK(\"http://evil\")")
    body = make_client().get("/api/export/events.csv").get_data(as_text=True)
    assert "'=HYPERLINK" in body and ";=HYPERLINK" not in body


def test_report_range_limits(make_client):
    c = make_client()
    today = dt.date.today()
    assert c.get(f"/api/report?from={today}&to={today}").status_code == 200
    assert c.get("/api/report?from=2025-01-01&to=2026-01-01").status_code == 400
    assert c.get("/api/report?from=2026-02-02&to=2026-02-01").status_code == 400
    assert c.get("/api/report.csv").status_code == 200


def test_export_unknown_table(make_client):
    assert make_client().get("/api/export/users.csv").status_code == 404


def test_sim_models_are_deterministic():
    from monitor import sources
    a = sources.SimModel(seed=1).snapshot(1000.0)
    b = sources.SimModel(seed=1).snapshot(1000.0)
    assert a["counter"] == b["counter"] and a["program"] == b["program"]


def test_robot_password_from_env(monkeypatch):
    from monitor.settings import is_weak, robot_password
    monkeypatch.setenv("ROBOT_R9_PASSWORD", "z-env")
    assert robot_password({"id": "R9", "password": "z-pliku"}) == "z-env"
    monkeypatch.delenv("ROBOT_R9_PASSWORD")
    assert robot_password({"id": "R9", "password": "z-pliku"}) == "z-pliku"
    assert is_weak("AAAA") and not is_weak("losowe-silne-haslo-42")
