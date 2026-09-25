import html
import secrets
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import state as state_mod
from .jobs import parse

CSS = """
:root{--bg:#f6f5f2;--fg:#1d1d1f;--muted:#6b6b70;--card:#fff;--line:#e3e1dc;--up:#1f7a4d;--down:#b3261e;--accent:#3553c7}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececef;--muted:#9a9aa2;--card:#1d1d21;--line:#2c2c31;--up:#4cc38a;--down:#ff7b72;--accent:#8ea2ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 system-ui,sans-serif}
main{max-width:720px;margin:0 auto;padding:24px 16px}h1{font-size:1.5rem;margin:0 0 4px}p.lead{color:var(--muted);margin:0 0 20px}
.card{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px;margin:0 0 12px}
.card img{width:84px;height:126px;object-fit:cover;border-radius:8px;background:var(--line)}
.meta{flex:1;min-width:0}.meta h2{font-size:1.05rem;margin:0 0 4px}.meta small{color:var(--muted)}
form.inline{display:inline}button{font:inherit;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:8px;padding:6px 12px;cursor:pointer;margin:8px 6px 0 0}
button.on.up{border-color:var(--up);color:var(--up)}button.on.down{border-color:var(--down);color:var(--down)}
a{color:var(--accent)}input{font:inherit;padding:8px;border:1px solid var(--line);border-radius:8px;width:100%;margin:4px 0 12px;background:var(--card);color:var(--fg)}
.note{color:var(--muted);font-size:.9rem}
"""


def page(body):
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Trial Shows</title><style>{CSS}</style></head>'
            f'<body><main>{body}</main></body></html>')


def render_login(msg=""):
    err = f'<p class="note">{html.escape(msg)}</p>' if msg else ""
    return page('<h1>Trial Shows</h1><p class="lead">Sign in with your Jellyfin account to vote.</p>'
                f'{err}<form method="post" action="/login"><label>Username<input name="username" autocomplete="username"></label>'
                '<label>Password<input type="password" name="password" autocomplete="current-password"></label>'
                '<button type="submit">Sign in</button></form>')


def trials_view(cfg, c):
    st = state_mod.load(cfg.state_path)
    index = c.jellyfin.series_index()
    rows = [(rec, index.get(rec["path"].rstrip("/"))) for rec in st["shows"].values() if rec.get("status") == "active"]
    return st, rows


def _deadline(cfg, rec):
    if not rec.get("window_start"):
        return "episodes on the way - voting opens when they arrive"
    end = parse(rec["window_start"]) + timedelta(days=cfg.window_days)
    days = max(0, (end - datetime.now(timezone.utc)).days)
    return f"vote by {end.strftime('%a %d %b')} ({days} day{'s' if days != 1 else ''} left)"


def render_page(cfg, c, user):
    st, rows = trials_view(cfg, c)
    pub = cfg.jellyfin_public_url.rstrip("/")
    cards = []
    for rec, jf in rows:
        title = html.escape(rec["title"])
        if not jf:
            cards.append(f'<div class="card"><div class="meta"><h2>{title}</h2><small>{html.escape(_deadline(cfg, rec))}</small></div></div>')
            continue
        iid = html.escape(jf["Id"])
        mine = c.jellyfin.likes(jf["Id"], user["id"])
        def btn(value, label, on):
            return (f'<form class="inline" method="post" action="/vote"><input type="hidden" name="item" value="{iid}">'
                    f'<input type="hidden" name="value" value="{value}"><button class="{"on " if on else ""}{value}">{label}</button></form>')
        buttons = btn("up", "👍 Keep it", mine is True) + btn("down", "👎 Drop it", mine is False)
        if mine is not None:
            buttons += btn("clear", "Clear my vote", False)
        dry = f'<br><small>Current outcome: {html.escape(rec["dry_run"])}</small>' if rec.get("dry_run") else ""
        cards.append(f'<div class="card"><img src="{pub}/Items/{iid}/Images/Primary?maxHeight=252" alt="">'
                     f'<div class="meta"><h2><a href="{pub}/web/#/details?id={iid}">{title}</a></h2>'
                     f'<small>{html.escape(_deadline(cfg, rec))}</small>{dry}<div>{buttons}</div></div></div>')
    body = (f'<h1>Trial Shows</h1><p class="lead">Hi {html.escape(user["name"])}. These shows are on a {cfg.window_days}-day trial '
            f'with their first {cfg.trial_episodes} episodes. Vote to keep or drop them - the majority of people who tried a show decides. '
            'If you watch but don\'t vote, finishing all trial episodes counts as a keep.</p>')
    body += "".join(cards) or '<p class="note">No shows on trial right now.</p>'
    if user["admin"]:
        rejected = [r for r in st["shows"].values() if r.get("status") == "rejected"]
        if rejected:
            body += "<h2>Rejected</h2>" + "".join(
                f'<form method="post" action="/unreject"><input type="hidden" name="tvdb" value="{r["tvdb"]}">'
                f'{html.escape(r["title"])} <button>Allow again</button></form>' for r in rejected)
    body += '<form method="post" action="/logout"><button>Sign out</button></form>'
    return page(body)


def make_server(cfg, c, host="0.0.0.0", port=None):
    sessions, lock = {}, threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body="", ctype="text/html; charset=utf-8", headers=()):
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, headers=()):
            self._send(303, "", headers=[("Location", "/"), *headers])

        def _token(self):
            morsel = cookies.SimpleCookie(self.headers.get("Cookie", "")).get("trials_session")
            return morsel.value if morsel else None

        def _user(self):
            return sessions.get(self._token())

        def _form(self):
            n = int(self.headers.get("Content-Length") or 0)
            return {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(n).decode()).items()}

        def _handle_get(self):
            if self.path == "/healthz":
                return self._send(200, "ok", "text/plain")
            if self.path != "/":
                return self._send(404, "not found", "text/plain")
            user = self._user()
            self._send(200, render_page(cfg, c, user) if user else render_login())

        def _handle_post(self):
            form = self._form()
            if self.path == "/login":
                user = c.jellyfin.authenticate(form.get("username", ""), form.get("password", ""))
                if not user:
                    return self._send(200, render_login("Wrong username or password."))
                token = secrets.token_urlsafe(24)
                with lock:
                    sessions[token] = user
                return self._redirect([("Set-Cookie", f"trials_session={token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=2592000")])
            user = self._user()
            if not user:
                return self._redirect()
            if self.path == "/logout":
                with lock:
                    sessions.pop(self._token(), None)
                return self._redirect([("Set-Cookie", "trials_session=; Path=/; Max-Age=0")])
            if self.path == "/vote":
                value = {"up": True, "down": False, "clear": None}.get(form.get("value"), "bad")
                allowed = {jf["Id"] for _, jf in trials_view(cfg, c)[1] if jf}
                if value == "bad" or form.get("item") not in allowed:
                    return self._send(400, "not a trial show", "text/plain")
                c.jellyfin.set_like(form["item"], user["id"], value)
                return self._redirect()
            if self.path == "/unreject":
                if not user["admin"]:
                    return self._send(403, "admins only", "text/plain")
                tvdb = int(form.get("tvdb") or 0)
                with state_mod.locked(cfg.state_path) as st:
                    if tvdb in st["rejected"]:
                        st["rejected"].remove(tvdb)
                    st["shows"].pop(str(tvdb), None)
                return self._redirect()
            self._send(404, "not found", "text/plain")

        def do_GET(self):
            try:
                self._handle_get()
            except Exception as e:
                print(f"trials web error on {self.command} {self.path}: {e}", flush=True)
                self._send(503, page('<h1>Trial Shows</h1><p class="lead">Jellyfin or Sonarr is unreachable right now - try again in a minute.</p>'))

        def do_POST(self):
            try:
                self._handle_post()
            except Exception as e:
                print(f"trials web error on {self.command} {self.path}: {e}", flush=True)
                self._send(503, page('<h1>Trial Shows</h1><p class="lead">Jellyfin or Sonarr is unreachable right now - try again in a minute.</p>'))

    return ThreadingHTTPServer((host, cfg.port if port is None else port), Handler)
