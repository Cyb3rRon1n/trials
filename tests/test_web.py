import http.client
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
import pytest
from trials import state
from trials.jobs import Clients, iso
from trials.web import make_server
from fakes import FakeJellyfin, FakeNtfy, FakeSeerr, FakeSonarr, make_cfg


@pytest.fixture
def app(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    st = state.empty()
    st["shows"]["1005"] = {"tvdb": 1005, "tmdb": 5, "title": "Plain <Show>", "sonarr_id": 100,
                           "path": "/data/media/trials/Plain Show", "status": "active", "dry_run": None,
                           "window_start": iso(datetime.now(timezone.utc) - timedelta(days=1))}
    st["shows"]["2002"] = {"tvdb": 2002, "title": "Bad Show", "status": "rejected"}
    st["rejected"] = [2002]
    state.save(cfg.state_path, st)
    c.jellyfin.index["/data/media/trials/Plain Show"] = {"Id": "jf1"}
    srv = make_server(cfg, c, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield cfg, c, srv.server_address[1]
    srv.shutdown()


def req(port, method, path, form=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, urllib.parse.urlencode(form or {}), headers)
    r = conn.getresponse()
    return r.status, r.getheader("Set-Cookie"), r.read().decode()


def login(port, user):
    status, cookie, _ = req(port, "POST", "/login", {"username": user, "password": "pw"})
    assert status == 303
    return cookie.split(";")[0]


def test_healthz_and_login_required(app):
    _, _, port = app
    assert req(port, "GET", "/healthz")[0] == 200
    status, _, body = req(port, "GET", "/")
    assert status == 200 and 'name="password"' in body


def test_bad_login(app):
    _, _, port = app
    status, cookie, body = req(port, "POST", "/login", {"username": "bobby", "password": "nope"})
    assert status == 200 and cookie is None and "Wrong username or password" in body


def test_list_escapes_and_vote_up(app):
    cfg, c, port = app
    cookie = login(port, "bobby")
    body = req(port, "GET", "/", cookie=cookie)[2]
    assert "Plain &lt;Show&gt;" in body and "vote by" in body.lower()
    assert req(port, "POST", "/vote", {"item": "jf1", "value": "up"}, cookie)[0] == 303
    assert c.jellyfin.like[("jf1", "u2")] is True


def test_vote_rejects_non_trial_item(app):
    cfg, c, port = app
    cookie = login(port, "bobby")
    assert req(port, "POST", "/vote", {"item": "some-other-show", "value": "down"}, cookie)[0] == 400
    assert c.jellyfin.calls == []


def test_unreject_admin_only(app):
    cfg, c, port = app
    assert req(port, "POST", "/unreject", {"tvdb": "2002"}, login(port, "bobby"))[0] == 403
    assert req(port, "POST", "/unreject", {"tvdb": "2002"}, login(port, "adriel"))[0] == 303
    st = state.load(cfg.state_path)
    assert st["rejected"] == [] and "2002" not in st["shows"]


def test_jellyfin_outage_returns_503(app):
    cfg, c, port = app
    cookie = login(port, "bobby")
    c.jellyfin.series_index = lambda: (_ for _ in ()).throw(RuntimeError("connection refused"))
    status, _, body = req(port, "GET", "/", cookie=cookie)
    assert status == 503 and "unreachable" in body.lower()
    assert req(port, "GET", "/healthz")[0] == 200


def test_logout_invalidates_session(app):
    cfg, c, port = app
    cookie = login(port, "bobby")
    assert req(port, "POST", "/logout", cookie=cookie)[0] == 303
    status, _, body = req(port, "GET", "/", cookie=cookie)
    assert status == 200 and 'name="password"' in body
