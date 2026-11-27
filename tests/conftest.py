import copy
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from monitor.collector import Tracker  # noqa: E402
from monitor.db import DB  # noqa: E402
from monitor.reports import Calendar  # noqa: E402
from monitor.server import create_app  # noqa: E402


@pytest.fixture()
def cfg():
    with open(os.path.join(ROOT, "config.example.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture()
def db(tmp_path):
    return DB(str(tmp_path / "t.db"))


@pytest.fixture()
def cal(cfg):
    return Calendar(cfg)


@pytest.fixture()
def tracker(cfg, db):
    return Tracker(cfg, db)


@pytest.fixture()
def make_client(cfg, db, tracker, cal, monkeypatch):
    def _make(env=None):
        for k in ("MONITOR_USER", "MONITOR_PASSWORD"):
            monkeypatch.delenv(k, raising=False)
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        c = copy.deepcopy(cfg)
        app = create_app(c, db, tracker, cal, None, {"metrics": {}})
        app.config["TESTING"] = True
        return app.test_client()
    return _make
