# Trial Shows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A small service on cyberpac that adds 3 trending TV shows a week as 3-episode trials in a separate Jellyfin "Trials" library, lets users vote 👍/👎, and after 21 days keeps (moves and completes) or deletes each show.

**Architecture:** One stdlib-only Python package, `trials`, run as a single container. It has pure logic modules (`decide`, `library`), thin API clients (`clients`), orchestration jobs (`jobs`), a server-rendered voting page (`web`), and an in-process scheduler (`main`). State lives in Sonarr (the `trial` tag), in Jellyfin (votes in `UserData.Likes`), and in one small JSON file.

**Tech Stack:** Python ≥3.11, stdlib only at runtime (`urllib`, `http.server`, `json`, `threading`). pytest for tests. Docker (`python:3.12-slim`). Sonarr v4 API v3, Jellyfin 12 API, Seerr API v1, ntfy.

**Spec:** `docs/superpowers/specs/2026-09-25-trial-shows-design.md` (read it first).

## Global Constraints

- **Only shows this app added are ever judged or changed.** Every change needs **both** the Sonarr `trial` tag **and** a state-file record with `status == "active"` (or `"moving"` for finishing a move). Existing and user-requested shows are never touched.
- Zero runtime dependencies. Only `pytest` in the `dev` extra. Build backend is `hatchling` (matching sibling repos). `requires-python = ">=3.11"`.
- Defaults (all overridable by env): `TRIALS_PER_WEEK=3`, `TRIAL_EPISODES=3`, `WINDOW_DAYS=21`, `ARRIVAL_DAYS=14`, `MIN_FREE_TB=1`, `MAX_DELETES_PER_RUN=3`, `TRIALS_ENFORCE=0`.
- Dry-run by default. With `TRIALS_ENFORCE` off, the app never moves or deletes a series. It only reports. Weekly adds still happen.
- Fail closed. Any API error aborts the rest of that run. Missing data is never read as "nobody watched". State is saved after every run, including an aborted one, because it only ever records actions that really happened.
- Deletion ceiling. If more than `MAX_DELETES_PER_RUN` deletions are due in one run, delete **nothing** and send an alert.
- Tie keeps the show. Nobody engaged means reject.
- Paths as seen by the Sonarr and Jellyfin containers: trials `/data/media/trials`, tv `/data/media/tv`, anime `/data/media/anime`, drama `/data/media/drama`. On the host: `/mnt/media/media/...`.
- Jellyfin API auth is the header `Authorization: MediaBrowser Token="<key>"`. On Jellyfin 12, `X-Emby-Token` and `?api_key=` return 401.
- Workspace rules: no `Co-Authored-By` or AI-attribution trailers in commits. Confirm with the user before any `git push`.
- Tests: `python -m pytest tests/` from the repo root.

## File Structure

```
trials/
  pyproject.toml            package + pytest config
  README.md                 what it is, env vars, deploy notes
  Dockerfile
  .github/workflows/ci.yml
  trials/
    __init__.py
    __main__.py             -> main.main()
    config.py               Config dataclass from env
    state.py                load / save (atomic) / locked()
    decide.py               UserView, user_verdict(), decide()   [pure]
    library.py              choose_destination(), ep_key(), restore_plan()   [pure]
    clients.py              ApiError, Http, Sonarr, Jellyfin, Seerr, Ntfy
    jobs.py                 Clients, weekly_add(), daily_decide() + helpers
    web.py                  make_server(): login, list, vote, unreject
    main.py                 due(), run_job(), scheduler, CLI (serve/add/decide/probe)
  tests/
    fakes.py                FakeSonarr, FakeJellyfin, FakeSeerr, FakeNtfy, make_cfg()
    test_config.py  test_state.py  test_decide.py  test_library.py
    test_clients.py test_jobs_add.py test_jobs_decide.py test_web.py test_main.py
  deploy/
    compose-snippet.yml     service stanza for cyberpac's docker-compose.override.yml
    homepage-snippet.yaml   Homepage tile + ntfy widget
```

---

### Task 1: Repo scaffold + config

**Files:**
- Create: `pyproject.toml`, `README.md`, `.gitignore`, `trials/__init__.py`, `trials/__main__.py`, `trials/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config` (frozen dataclass; fields listed below) and `Config.from_env(env: Mapping | None = None) -> Config`, which raises `ValueError("missing required env: ...")`.

- [ ] **Step 1: Write scaffold files**

`pyproject.toml`:
```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "trials"
version = "0.1.0"
description = "Trial Shows for Jellyfin: weekly trending-show trials, votes, keep-or-delete."
readme = "README.md"
requires-python = ">=3.11"

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.hatch.build.targets.wheel]
packages = ["trials"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.gitignore`:
```
__pycache__/
*.egg-info/
.venv/
.pytest_cache/
state.json
```

`trials/__init__.py`:
```python
"""Trial Shows for Jellyfin."""
```

`trials/__main__.py`:
```python
from .main import main

main()
```

`README.md`:
```markdown
# trials

Weekly "trial shows" for a Jellyfin + Sonarr + Seerr stack. Every Monday it adds the first
3 episodes of 3 TMDb-trending shows to a separate **Trials** library. Users vote 👍/👎 on a
small page. After 21 days, shows most people liked are moved to their permanent library
and completed; the rest are deleted. It only ever touches shows it added itself.

Design: `docs/superpowers/specs/2026-09-25-trial-shows-design.md`.

## Run
    pip install -e ".[dev]"
    python -m pytest tests/
    python -m trials probe      # read-only API check
    python -m trials serve      # web page + scheduler

Configuration is by environment variable (see `trials/config.py`). `TRIALS_ENFORCE=0`
(the default) means decisions are only reported, never carried out.
```

- [ ] **Step 2: Write the failing test**

`tests/test_config.py`:
```python
import pytest
from trials.config import Config

BASE = {"SEERR_URL": "http://seerr:5055", "SEERR_KEY": "sk", "SONARR_URL": "http://sonarr:8989",
        "SONARR_KEY": "k", "JELLYFIN_URL": "http://jellyfin:8096", "JELLYFIN_KEY": "jk",
        "JELLYFIN_PUBLIC_URL": "https://jellyfin.example"}


def test_defaults():
    c = Config.from_env(BASE)
    assert (c.trials_per_week, c.trial_episodes, c.window_days, c.arrival_days) == (3, 3, 21, 14)
    assert c.min_free_tb == 1.0 and c.max_deletes_per_run == 3 and c.enforce is False
    assert c.trials_root == "/data/media/trials" and c.port == 8080


def test_overrides_and_types():
    c = Config.from_env(dict(BASE, TRIALS_PER_WEEK="5", MIN_FREE_TB="0.5", TRIALS_ENFORCE="true"))
    assert c.trials_per_week == 5 and c.min_free_tb == 0.5 and c.enforce is True


def test_enforce_false_values():
    for v in ("0", "false", "no", "off"):
        assert Config.from_env(dict(BASE, TRIALS_ENFORCE=v)).enforce is False


def test_missing_required():
    with pytest.raises(ValueError, match="SONARR_KEY"):
        Config.from_env({k: v for k, v in BASE.items() if k != "SONARR_KEY"})
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]" && python -m pytest tests/test_config.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'trials.config'`.

- [ ] **Step 4: Implement `trials/config.py`**

```python
import os
from dataclasses import dataclass, fields

REQUIRED = ("SEERR_URL", "SEERR_KEY", "SONARR_URL", "SONARR_KEY",
            "JELLYFIN_URL", "JELLYFIN_KEY", "JELLYFIN_PUBLIC_URL")
ENV_NAMES = {"enforce": "TRIALS_ENFORCE"}


@dataclass(frozen=True)
class Config:
    seerr_url: str
    seerr_key: str
    sonarr_url: str
    sonarr_key: str
    jellyfin_url: str
    jellyfin_key: str
    jellyfin_public_url: str
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    quality_profile: str = "HD-1080p"
    trials_per_week: int = 3
    trial_episodes: int = 3
    window_days: int = 21
    arrival_days: int = 14
    min_free_tb: float = 1.0
    max_deletes_per_run: int = 3
    enforce: bool = False
    trials_root: str = "/data/media/trials"
    tv_root: str = "/data/media/tv"
    anime_root: str = "/data/media/anime"
    drama_root: str = "/data/media/drama"
    state_path: str = "/data/state.json"
    port: int = 8080

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        missing = [k for k in REQUIRED if not env.get(k)]
        if missing:
            raise ValueError("missing required env: " + ", ".join(missing))
        kw = {}
        for f in fields(cls):
            raw = env.get(ENV_NAMES.get(f.name, f.name.upper()))
            if raw is None or raw == "":
                continue
            if f.type is bool:
                kw[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            else:
                kw[f.name] = f.type(raw)
        return cls(**kw)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: scaffold trials package and env config"
```

---

### Task 2: State file

**Files:**
- Create: `trials/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Produces: `empty() -> dict`, `load(path) -> dict`, `save(path, st)`, and `locked(path)`, a context manager that yields the state dict and **always** saves on exit (including on exception) under a process-wide lock.
- State shape: `{"shows": {str(tvdb): record}, "rejected": [int tvdb], "last_add_week": str|None, "last_decide_date": str|None}`. Record keys are defined in Task 6.

- [ ] **Step 1: Write the failing test**

`tests/test_state.py`:
```python
import json
import pytest
from trials import state


def test_load_missing_returns_empty(tmp_path):
    assert state.load(str(tmp_path / "s.json")) == state.empty()


def test_save_load_roundtrip_and_defaults_merged(tmp_path):
    p = str(tmp_path / "s.json")
    state.save(p, {"shows": {"1": {"title": "X"}}})
    st = state.load(p)
    assert st["shows"]["1"]["title"] == "X" and st["rejected"] == [] and st["last_add_week"] is None


def test_save_is_atomic_no_tmp_left(tmp_path):
    p = str(tmp_path / "s.json")
    state.save(p, state.empty())
    assert sorted(x.name for x in tmp_path.iterdir()) == ["s.json"]


def test_locked_saves_even_on_error(tmp_path):
    p = str(tmp_path / "s.json")
    with pytest.raises(RuntimeError):
        with state.locked(p) as st:
            st["rejected"].append(42)
            raise RuntimeError("boom")
    assert json.load(open(p))["rejected"] == [42]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_state.py -v`
Expected: FAIL, `ImportError: cannot import name 'state'`.

- [ ] **Step 3: Implement `trials/state.py`**

```python
import contextlib
import json
import os
import threading

_LOCK = threading.Lock()


def empty():
    return {"shows": {}, "rejected": [], "last_add_week": None, "last_decide_date": None}


def load(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty()
    st = empty()
    st.update(data)
    return st


def save(path, st):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


@contextlib.contextmanager
def locked(path):
    with _LOCK:
        st = load(path)
        try:
            yield st
        finally:
            save(path, st)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_state.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: atomic JSON state with lock"
```

---

### Task 3: Vote tally and decision (pure)

**Files:**
- Create: `trials/decide.py`
- Test: `tests/test_decide.py`

**Interfaces:**
- Produces: `UserView(likes: bool | None, watched: int, finished: int)` (frozen dataclass). `watched` = trial episodes played or with position > 0. `finished` = trial episodes marked played.
- `user_verdict(v: UserView, trial_episodes: int) -> "like" | "dislike" | None`
- `decide(views: list[UserView], trial_episodes: int) -> tuple[str, int, int]`, returning `("keep"|"reject", likes, dislikes)`

- [ ] **Step 1: Write the failing test**

`tests/test_decide.py`:
```python
from trials.decide import UserView, decide, user_verdict

N = 3


def test_explicit_vote_wins_over_viewing():
    assert user_verdict(UserView(likes=False, watched=3, finished=3), N) == "dislike"
    assert user_verdict(UserView(likes=True, watched=0, finished=0), N) == "like"


def test_viewing_fallback():
    assert user_verdict(UserView(None, 3, 3), N) == "like"      # finished all trial eps
    assert user_verdict(UserView(None, 2, 1), N) == "dislike"   # stopped early
    assert user_verdict(UserView(None, 1, 0), N) == "dislike"   # started ep 1, quit


def test_never_watched_not_counted():
    assert user_verdict(UserView(None, 0, 0), N) is None


def test_majority_of_engaged():
    views = [UserView(True, 0, 0), UserView(None, 3, 3), UserView(False, 1, 0), UserView(None, 0, 0)]
    assert decide(views, N) == ("keep", 2, 1)


def test_tie_keeps():
    assert decide([UserView(True, 0, 0), UserView(False, 0, 0)], N) == ("keep", 1, 1)


def test_more_dislikes_rejects():
    assert decide([UserView(False, 0, 0), UserView(None, 1, 0), UserView(True, 0, 0)], N) == ("reject", 1, 2)


def test_nobody_engaged_rejects():
    assert decide([UserView(None, 0, 0)] * 5, N) == ("reject", 0, 0)
    assert decide([], N) == ("reject", 0, 0)


def test_single_engaged_like_keeps():
    assert decide([UserView(None, 3, 3)] + [UserView(None, 0, 0)] * 8, N) == ("keep", 1, 0)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_decide.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'trials.decide'`.

- [ ] **Step 3: Implement `trials/decide.py`**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class UserView:
    likes: bool | None
    watched: int
    finished: int


def user_verdict(v, trial_episodes):
    if v.likes is not None:
        return "like" if v.likes else "dislike"
    if v.watched == 0:
        return None
    return "like" if v.finished >= trial_episodes else "dislike"


def decide(views, trial_episodes):
    verdicts = [user_verdict(v, trial_episodes) for v in views]
    likes, dislikes = verdicts.count("like"), verdicts.count("dislike")
    if likes + dislikes == 0:
        return "reject", 0, 0
    return ("keep" if likes >= dislikes else "reject"), likes, dislikes
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_decide.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: vote tally and keep/reject decision"
```

---

### Task 4: Destination library and watched-state mapping (pure)

**Files:**
- Create: `trials/library.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Produces:
  - `choose_destination(genres: list[str], origin: list[str], tv_root: str, anime_root: str, drama_root: str) -> str`
  - `ep_key(season: int, episode: int) -> str` (for example `"S01E03"`)
  - `restore_plan(snapshot: dict[user_id, dict[ep_key, {"played": bool, "ticks": int}]], new_items: dict[ep_key, item_id]) -> list[tuple[user_id, item_id, played, ticks]]`. The result is sorted, and entries with neither played nor ticks are skipped, as are episodes missing from `new_items`.

- [ ] **Step 1: Write the failing test**

`tests/test_library.py`:
```python
from trials.library import choose_destination, ep_key, restore_plan

R = ("/data/media/tv", "/data/media/anime", "/data/media/drama")


def test_japanese_animation_goes_to_anime():
    assert choose_destination(["Animation", "Action & Adventure"], ["JP"], *R) == "/data/media/anime"


def test_asian_live_action_goes_to_drama():
    for c in ("KR", "CN", "TW", "JP"):
        assert choose_destination(["Drama"], [c], *R) == "/data/media/drama"


def test_western_animation_and_default_go_to_tv():
    assert choose_destination(["Animation", "Comedy"], ["US"], *R) == "/data/media/tv"
    assert choose_destination(["Crime"], ["GB"], *R) == "/data/media/tv"
    assert choose_destination([], [], *R) == "/data/media/tv"


def test_ep_key():
    assert ep_key(1, 3) == "S01E03" and ep_key(12, 104) == "S12E104"


def test_restore_plan_maps_by_episode_and_skips_untouched():
    snap = {"u2": {"S01E01": {"played": True, "ticks": 0}, "S01E02": {"played": False, "ticks": 555}},
            "u1": {"S01E01": {"played": False, "ticks": 0}, "S01E03": {"played": True, "ticks": 0}}}
    new = {"S01E01": "n1", "S01E02": "n2"}          # S01E03 not scanned yet
    assert restore_plan(snap, new) == [("u2", "n1", True, 0), ("u2", "n2", False, 555)]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_library.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'trials.library'`.

- [ ] **Step 3: Implement `trials/library.py`**

```python
DRAMA_COUNTRIES = {"KR", "CN", "TW", "JP"}


def choose_destination(genres, origin, tv_root, anime_root, drama_root):
    origin = set(origin)
    animated = "Animation" in genres
    if animated and "JP" in origin:
        return anime_root
    if not animated and origin & DRAMA_COUNTRIES:
        return drama_root
    return tv_root


def ep_key(season, episode):
    return f"S{season:02d}E{episode:02d}"


def restore_plan(snapshot, new_items):
    plan = []
    for user_id, eps in sorted(snapshot.items()):
        for key, st in sorted(eps.items()):
            item_id = new_items.get(key)
            played, ticks = bool(st.get("played")), int(st.get("ticks") or 0)
            if item_id and (played or ticks > 0):
                plan.append((user_id, item_id, played, ticks))
    return plan
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_library.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: destination library choice and watched-state mapping"
```

---

### Task 5: API clients

**Files:**
- Create: `trials/clients.py`
- Test: `tests/test_clients.py`

**Interfaces:**
- Produces:
  - `ApiError(Exception)`.
  - `urllib_transport(method, url, headers, data) -> (status:int, body:bytes)`. It raises `ApiError` on connection failure.
  - `Http(base, headers, transport)` with `.call(method, path, params=None, body=None, ok=(200,201,202,204)) -> parsed JSON | None`. It raises `ApiError` for any status not in `ok`.
- `Sonarr(url, key, transport=urllib_transport)`:
  - `tag_id(label) -> int` (creates the tag if missing)
  - `quality_profile_id(name) -> int` (`ApiError` if absent)
  - `series() -> list[dict]`
  - `get_series(sid) -> dict`
  - `lookup_tvdb(tvdb) -> dict | None`
  - `add_series(lookup, profile_id, root, tag_id) -> dict`
  - `episodes(sid) -> list[dict]`
  - `set_monitored(ids, monitored)`
  - `set_season_monitored(sid, season, monitored)`
  - `search_episodes(ids)`
  - `free_bytes(path) -> int`
  - `move_series(sid, root) -> new_path`
  - `monitor_all_and_search(sid)`
  - `remove_tag(sid, tag_id)`
  - `delete_series(sid, exclude: bool)`
- `Jellyfin(url, key, transport=urllib_transport)`:
  - `users() -> list[{"Id","Name"}]`
  - `series_index() -> dict[path_without_trailing_slash, item]`
  - `season1_episodes(series_id, user_id) -> list[item]`
  - `likes(item_id, user_id) -> bool | None`
  - `set_like(item_id, user_id, likes: bool | None)`
  - `mark_played(item_id, user_id)`
  - `set_position(item_id, user_id, ticks)`
  - `notify_paths(created=(), deleted=())`
  - `authenticate(username, password) -> {"id","name","admin"} | None`
- `Seerr(url, key, transport=urllib_transport)`:
  - `trending_tv(pages=3) -> list[dict]`
  - `tv(tmdb) -> dict`
  - `requested_since(tmdb, since_iso) -> bool`
- `Ntfy(url, topic, transport=urllib_transport)`:
  - `send(title, message)` (a no-op when `topic` is empty)

- [ ] **Step 1: Write the failing test**

`tests/test_clients.py`:
```python
import json
import pytest
from trials.clients import ApiError, Http, Jellyfin, Ntfy, Seerr, Sonarr


class FakeTransport:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, data):
        body = json.loads(data) if data and data[:1] in (b"{", b"[") else data
        self.calls.append((method, url, headers, body))
        for (m, prefix), (status, body) in self.routes.items():
            if m == method and url.startswith(prefix):
                return status, (json.dumps(body).encode() if body is not None else b"")
        return 404, b"{}"


def test_http_raises_on_error_status():
    t = FakeTransport({("GET", "http://x/boom"): (500, {"e": 1})})
    with pytest.raises(ApiError, match="HTTP 500"):
        Http("http://x", {}, t).call("GET", "/boom")


def test_sonarr_sends_api_key_and_free_bytes_longest_prefix():
    t = FakeTransport({("GET", "http://s/api/v3/diskspace"): (200, [
        {"path": "/", "freeSpace": 1}, {"path": "/data", "freeSpace": 5}, {"path": "/data/media/", "freeSpace": 9}])})
    s = Sonarr("http://s", "KEY", t)
    assert s.free_bytes("/data/media/trials") == 9
    assert t.calls[0][2]["X-Api-Key"] == "KEY"


def test_sonarr_move_series_sets_path_and_movefiles():
    t = FakeTransport({("GET", "http://s/api/v3/series/5"): (200, {"id": 5, "path": "/data/media/trials/Show A", "rootFolderPath": "/data/media/trials"}),
                       ("PUT", "http://s/api/v3/series/5"): (202, {})})
    new = Sonarr("http://s", "k", t).move_series(5, "/data/media/tv")
    method, url, _, body = t.calls[-1]
    assert new == "/data/media/tv/Show A" and method == "PUT" and "moveFiles=true" in url
    assert body["path"] == "/data/media/tv/Show A" and body["rootFolderPath"] == "/data/media/tv"


def test_sonarr_tag_id_creates_when_missing():
    t = FakeTransport({("GET", "http://s/api/v3/tag"): (200, [{"id": 1, "label": "anime"}]),
                       ("POST", "http://s/api/v3/tag"): (201, {"id": 4, "label": "trial"})})
    assert Sonarr("http://s", "k", t).tag_id("trial") == 4


def test_jellyfin_auth_header_and_authenticate():
    ok = FakeTransport({("POST", "http://j/Users/AuthenticateByName"): (200, {"User": {"Id": "u1", "Name": "adriel", "Policy": {"IsAdministrator": True}}})})
    assert Jellyfin("http://j", "K", ok).authenticate("adriel", "pw") == {"id": "u1", "name": "adriel", "admin": True}
    assert 'Client="trials"' in ok.calls[0][2]["Authorization"]
    bad = FakeTransport({("POST", "http://j/Users/AuthenticateByName"): (401, None)})
    assert Jellyfin("http://j", "K", bad).authenticate("x", "y") is None


def test_jellyfin_set_like_and_clear():
    t = FakeTransport({("POST", "http://j/UserItems/i1/Rating"): (200, {}), ("DELETE", "http://j/UserItems/i1/Rating"): (200, {})})
    j = Jellyfin("http://j", "K", t)
    j.set_like("i1", "u1", False)
    j.set_like("i1", "u1", None)
    assert t.calls[0][0] == "POST" and "likes=false" in t.calls[0][1] and "userId=u1" in t.calls[0][1]
    assert t.calls[1][0] == "DELETE"
    assert t.calls[0][2]["Authorization"] == 'MediaBrowser Token="K"'


def test_seerr_trending_filters_tv():
    t = FakeTransport({("GET", "http://e/api/v1/discover/trending"): (200, {"results": [
        {"id": 1, "mediaType": "movie"}, {"id": 2, "mediaType": "tv"}]})})
    assert [r["id"] for r in Seerr("http://e", "k", t).trending_tv(pages=1)] == [2]


def test_seerr_requested_since():
    t = FakeTransport({("GET", "http://e/api/v1/request"): (200, {"results": [
        {"type": "tv", "createdAt": "2026-10-02T10:00:00.000Z", "media": {"tmdbId": 77}}]})})
    s = Seerr("http://e", "k", t)
    assert s.requested_since(77, "2026-10-01T00:00:00Z") is True
    assert s.requested_since(77, "2026-10-03T00:00:00Z") is False
    assert s.requested_since(78, "2026-10-01T00:00:00Z") is False


def test_ntfy_noop_without_topic_and_posts_with_topic():
    t = FakeTransport({("POST", "https://n/topic"): (200, None)})
    Ntfy("https://n", "", t).send("T", "m")
    assert t.calls == []
    Ntfy("https://n", "topic", t).send("T", "m")
    assert t.calls[0][1] == "https://n/topic" and t.calls[0][2]["Title"] == "T"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_clients.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'trials.clients'`.

- [ ] **Step 3: Implement `trials/clients.py`**

```python
import json
import urllib.error
import urllib.parse
import urllib.request

AUTH_CLIENT = 'MediaBrowser Client="trials", Device="trials-web", DeviceId="trials-web", Version="0.1.0"'


class ApiError(Exception):
    pass


def urllib_transport(method, url, headers, data):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ApiError(f"{method} {url}: {e}") from e


class Http:
    def __init__(self, base, headers, transport=urllib_transport):
        self.base, self.headers, self.transport = base.rstrip("/"), headers, transport

    def call(self, method, path, params=None, body=None, ok=(200, 201, 202, 204)):
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers, data = dict(self.headers), None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, raw = self.transport(method, url, headers, data)
        if status not in ok:
            raise ApiError(f"{method} {path} -> HTTP {status}: {raw[:200]!r}")
        return json.loads(raw) if raw else None


def _covers(root, path):
    root = root.rstrip("/")
    return root == "" or path == root or path.startswith(root + "/")


class Sonarr:
    def __init__(self, url, key, transport=urllib_transport):
        self.http = Http(url.rstrip("/") + "/api/v3", {"X-Api-Key": key}, transport)

    def tag_id(self, label):
        for t in self.http.call("GET", "/tag"):
            if t["label"] == label:
                return t["id"]
        return self.http.call("POST", "/tag", body={"label": label})["id"]

    def quality_profile_id(self, name):
        for p in self.http.call("GET", "/qualityprofile"):
            if p["name"] == name:
                return p["id"]
        raise ApiError(f"Sonarr quality profile {name!r} not found")

    def series(self):
        return self.http.call("GET", "/series")

    def get_series(self, sid):
        return self.http.call("GET", f"/series/{sid}")

    def lookup_tvdb(self, tvdb):
        found = self.http.call("GET", "/series/lookup", {"term": f"tvdb:{tvdb}"})
        return found[0] if found else None

    def add_series(self, lookup, profile_id, root, tag_id):
        body = dict(lookup, qualityProfileId=profile_id, rootFolderPath=root, tags=[tag_id],
                    monitored=True, seasonFolder=True,
                    addOptions={"monitor": "none", "searchForMissingEpisodes": False,
                                "searchForCutoffUnmetEpisodes": False})
        return self.http.call("POST", "/series", body=body)

    def episodes(self, sid):
        return self.http.call("GET", "/episode", {"seriesId": sid})

    def set_monitored(self, ids, monitored):
        if ids:
            self.http.call("PUT", "/episode/monitor", body={"episodeIds": list(ids), "monitored": monitored})

    def set_season_monitored(self, sid, season, monitored):
        s = self.get_series(sid)
        for x in s["seasons"]:
            if x["seasonNumber"] == season:
                x["monitored"] = monitored
        self.http.call("PUT", f"/series/{sid}", body=s)

    def search_episodes(self, ids):
        if ids:
            self.http.call("POST", "/command", body={"name": "EpisodeSearch", "episodeIds": list(ids)})

    def free_bytes(self, path):
        best = None
        for d in self.http.call("GET", "/diskspace"):
            if _covers(d["path"], path) and (best is None or len(d["path"].rstrip("/")) > len(best["path"].rstrip("/"))):
                best = d
        if best is None:
            raise ApiError(f"no Sonarr diskspace entry covers {path}")
        return int(best["freeSpace"])

    def move_series(self, sid, root):
        s = self.get_series(sid)
        s["rootFolderPath"] = root
        s["path"] = root.rstrip("/") + "/" + s["path"].rstrip("/").rsplit("/", 1)[-1]
        self.http.call("PUT", f"/series/{sid}", {"moveFiles": "true"}, body=s)
        return s["path"]

    def monitor_all_and_search(self, sid):
        s = self.get_series(sid)
        s["monitored"] = True
        for x in s["seasons"]:
            if x["seasonNumber"] > 0:
                x["monitored"] = True
        self.http.call("PUT", f"/series/{sid}", body=s)
        self.set_monitored([e["id"] for e in self.episodes(sid) if e["seasonNumber"] > 0], True)
        self.http.call("POST", "/command", body={"name": "SeriesSearch", "seriesId": sid})

    def remove_tag(self, sid, tag_id):
        s = self.get_series(sid)
        s["tags"] = [t for t in s["tags"] if t != tag_id]
        self.http.call("PUT", f"/series/{sid}", body=s)

    def delete_series(self, sid, exclude):
        self.http.call("DELETE", f"/series/{sid}",
                       {"deleteFiles": "true", "addImportListExclusion": "true" if exclude else "false"})


class Jellyfin:
    def __init__(self, url, key, transport=urllib_transport):
        self.http = Http(url, {"Authorization": f'MediaBrowser Token="{key}"'}, transport)
        self.anon = Http(url, {"Authorization": AUTH_CLIENT}, transport)

    def users(self):
        return [{"Id": u["Id"], "Name": u["Name"]} for u in self.http.call("GET", "/Users")]

    def series_index(self):
        r = self.http.call("GET", "/Items", {"Recursive": "true", "IncludeItemTypes": "Series", "Fields": "Path"})
        return {i["Path"].rstrip("/"): i for i in r["Items"] if i.get("Path")}

    def season1_episodes(self, series_id, user_id):
        return self.http.call("GET", f"/Shows/{series_id}/Episodes", {"userId": user_id, "season": 1})["Items"]

    def likes(self, item_id, user_id):
        return (self.http.call("GET", f"/Items/{item_id}", {"userId": user_id}).get("UserData") or {}).get("Likes")

    def set_like(self, item_id, user_id, likes):
        if likes is None:
            self.http.call("DELETE", f"/UserItems/{item_id}/Rating", {"userId": user_id})
        else:
            self.http.call("POST", f"/UserItems/{item_id}/Rating",
                           {"userId": user_id, "likes": "true" if likes else "false"})

    def mark_played(self, item_id, user_id):
        self.http.call("POST", f"/UserPlayedItems/{item_id}", {"userId": user_id})

    def set_position(self, item_id, user_id, ticks):
        self.http.call("POST", f"/UserItems/{item_id}/UserData", {"userId": user_id},
                       body={"PlaybackPositionTicks": ticks})

    def notify_paths(self, created=(), deleted=()):
        ups = [{"Path": p, "UpdateType": "Created"} for p in created] + \
              [{"Path": p, "UpdateType": "Deleted"} for p in deleted]
        if ups:
            self.http.call("POST", "/Library/Media/Updated", body={"Updates": ups})

    def authenticate(self, username, password):
        r = self.anon.call("POST", "/Users/AuthenticateByName",
                           body={"Username": username, "Pw": password}, ok=(200, 401))
        if not r or "User" not in r:
            return None
        u = r["User"]
        return {"id": u["Id"], "name": u["Name"], "admin": bool((u.get("Policy") or {}).get("IsAdministrator"))}


class Seerr:
    def __init__(self, url, key, transport=urllib_transport):
        self.http = Http(url.rstrip("/") + "/api/v1", {"X-Api-Key": key}, transport)

    def trending_tv(self, pages=3):
        out = []
        for page in range(1, pages + 1):
            out += [r for r in self.http.call("GET", "/discover/trending", {"page": page})["results"]
                    if r.get("mediaType") == "tv"]
        return out

    def tv(self, tmdb):
        return self.http.call("GET", f"/tv/{tmdb}")

    def requested_since(self, tmdb, since_iso):
        r = self.http.call("GET", "/request", {"take": 100, "skip": 0, "sort": "added", "filter": "all"})
        return any(x.get("type") == "tv" and (x.get("media") or {}).get("tmdbId") == tmdb
                   and x.get("createdAt", "")[:19] > since_iso[:19] for x in r["results"])


class Ntfy:
    def __init__(self, url, topic, transport=urllib_transport):
        self.url, self.topic, self.transport = url.rstrip("/"), topic, transport

    def send(self, title, message):
        if not self.topic:
            return
        status, raw = self.transport("POST", f"{self.url}/{self.topic}", {"Title": title}, message.encode())
        if status >= 300:
            raise ApiError(f"ntfy -> HTTP {status}: {raw[:200]!r}")
```

Note: `requested_since` compares only the first 19 characters, `YYYY-MM-DDTHH:MM:SS`. Seerr returns `...:00.000Z` and this app stores `...:00Z`. Comparing the common prefix keeps the string comparison correct.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_clients.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: Sonarr, Jellyfin, Seerr, ntfy API clients"
```

---

### Task 6: Test fakes + weekly add job

**Files:**
- Create: `tests/fakes.py`, `trials/jobs.py` (first half: shared helpers + `weekly_add`)
- Test: `tests/test_jobs_add.py`

**Interfaces:**
- Consumes: `Config` (Task 1), `choose_destination` (Task 4), client method names (Task 5).
- Produces (in `trials/jobs.py`):
  - `TAG = "trial"`, `Clients(sonarr, jellyfin, seerr, ntfy)` (dataclass), `iso(dt) -> "YYYY-MM-DDTHH:MM:SSZ"`, `parse(s) -> aware datetime`.
  - `aired_enough(details, n) -> bool`, `trial_eps(episodes, n) -> list`.
  - `setup_trial(c, rec, episodes, n) -> bool`. It monitors S01E01..En, unmonitors the rest of season 1, starts a search, sets `rec["setup_done"]`, and returns whether setup happened (False if Sonarr has no episodes yet).
  - `weekly_add(cfg, c, st, now) -> list[str]`.
- **State record** created per trial (the keys every later task uses): `{"tvdb": int, "tmdb": int, "title": str, "sonarr_id": int, "path": str, "added_at": iso, "window_start": iso|None, "status": "active", "dest": str, "played": None, "reported": False, "setup_done": bool, "dry_run": None}`. Later statuses: `"moving"` (with `new_path`, `moved_at`), `"kept"`, `"rejected"`, `"unavailable"` (with `dropped_at`), `"released"`.

- [ ] **Step 1: Write the fakes**

`tests/fakes.py`:
```python
from datetime import datetime, timezone
from trials.config import Config


def make_cfg(tmp_path, **kw):
    base = dict(seerr_url="http://seerr", seerr_key="k", sonarr_url="http://sonarr", sonarr_key="k",
                jellyfin_url="http://jf", jellyfin_key="k", jellyfin_public_url="https://jf.example",
                state_path=str(tmp_path / "state.json"), enforce=True)
    base.update(kw)
    return Config(**base)


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)   # a Monday


class FakeSonarr:
    def __init__(self, free=5e12):
        self.free, self.tags, self.next_id = free, {"trial": 1}, 100
        self.series_db, self.eps, self.lookups, self.calls = {}, {}, {}, []

    def tag_id(self, label):
        return self.tags.setdefault(label, len(self.tags) + 1)

    def quality_profile_id(self, name):
        return 7

    def series(self):
        return [dict(s) for s in self.series_db.values()]

    def lookup_tvdb(self, tvdb):
        return self.lookups.get(tvdb)

    def add_series(self, lookup, profile_id, root, tag_id):
        sid, self.next_id = self.next_id, self.next_id + 1
        s = dict(lookup, id=sid, path=f"{root}/{lookup['title']}", tags=[tag_id], rootFolderPath=root)
        self.series_db[sid] = s
        self.eps[sid] = [{"id": sid * 100 + i, "seasonNumber": 1, "episodeNumber": i,
                          "monitored": False, "hasFile": False} for i in range(1, 11)]
        self.calls.append(("add", sid))
        return dict(s)

    def add_existing(self, sid, tvdb, title, tags=(), path=None):
        self.series_db[sid] = {"id": sid, "tvdbId": tvdb, "title": title, "tags": list(tags),
                               "path": path or f"/data/media/tv/{title}"}
        self.eps[sid] = []

    def episodes(self, sid):
        return [dict(e) for e in self.eps.get(sid, [])]

    def set_monitored(self, ids, monitored):
        for eps in self.eps.values():
            for e in eps:
                if e["id"] in ids:
                    e["monitored"] = monitored

    def set_season_monitored(self, sid, season, monitored):
        self.calls.append(("season", sid, season, monitored))

    def search_episodes(self, ids):
        self.calls.append(("search", tuple(ids)))

    def free_bytes(self, path):
        return self.free

    def move_series(self, sid, root):
        s = self.series_db[sid]
        s["path"] = root + "/" + s["path"].rsplit("/", 1)[-1]
        self.calls.append(("move", sid, root))
        return s["path"]

    def monitor_all_and_search(self, sid):
        self.calls.append(("monitor_all", sid))

    def remove_tag(self, sid, tag_id):
        self.series_db[sid]["tags"].remove(tag_id)
        self.calls.append(("untag", sid))

    def delete_series(self, sid, exclude):
        self.series_db.pop(sid)
        self.calls.append(("delete", sid, exclude))


class FakeJellyfin:
    def __init__(self):
        self.users_list = [{"Id": "u1", "Name": "adriel"}, {"Id": "u2", "Name": "bobby"}]
        self.index, self.eps, self.like, self.calls = {}, {}, {}, []

    def users(self):
        return self.users_list

    def series_index(self):
        return dict(self.index)

    def season1_episodes(self, series_id, user_id):
        return self.eps.get((series_id, user_id), [])

    def likes(self, item_id, user_id):
        return self.like.get((item_id, user_id))

    def set_like(self, item_id, user_id, likes):
        self.like[(item_id, user_id)] = likes
        self.calls.append(("like", item_id, user_id, likes))

    def mark_played(self, item_id, user_id):
        self.calls.append(("played", item_id, user_id))

    def set_position(self, item_id, user_id, ticks):
        self.calls.append(("pos", item_id, user_id, ticks))

    def notify_paths(self, created=(), deleted=()):
        self.calls.append(("notify", tuple(created), tuple(deleted)))

    def authenticate(self, username, password):
        users = {("adriel", "pw"): {"id": "u1", "name": "adriel", "admin": True},
                 ("bobby", "pw"): {"id": "u2", "name": "bobby", "admin": False}}
        return users.get((username, password))


def ep(item_id, n, played=False, ticks=0):
    return {"Id": item_id, "ParentIndexNumber": 1, "IndexNumber": n,
            "UserData": {"Played": played, "PlaybackPositionTicks": ticks}}


class FakeSeerr:
    def __init__(self):
        self.trending, self.details, self.requested = [], {}, set()

    def add_show(self, tmdb, tvdb, name, last=(1, 8), genres=("Drama",), origin=("US",)):
        self.trending.append({"id": tmdb, "mediaType": "tv", "name": name})
        self.details[tmdb] = {"id": tmdb, "name": name, "externalIds": {"tvdbId": tvdb},
                              "lastEpisodeToAir": {"seasonNumber": last[0], "episodeNumber": last[1]},
                              "genres": [{"name": g} for g in genres], "originCountry": list(origin)}

    def trending_tv(self, pages=3):
        return list(self.trending)

    def tv(self, tmdb):
        return self.details[tmdb]

    def requested_since(self, tmdb, since_iso):
        return tmdb in self.requested


class FakeNtfy:
    def __init__(self):
        self.sent = []

    def send(self, title, message):
        self.sent.append((title, message))
```

- [ ] **Step 2: Write the failing test**

`tests/test_jobs_add.py`:
```python
from trials import state
from trials.jobs import Clients, weekly_add
from fakes import FakeJellyfin, FakeNtfy, FakeSeerr, FakeSonarr, NOW, make_cfg


def setup(tmp_path, **kw):
    cfg = make_cfg(tmp_path, **kw)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    return cfg, c, state.empty()


def test_adds_top_n_eligible_and_skips_the_rest(tmp_path):
    cfg, c, st = setup(tmp_path, trials_per_week=2)
    c.seerr.add_show(1, 1001, "Already Have")          # already in Sonarr
    c.seerr.add_show(2, 1002, "Was Rejected")
    c.seerr.add_show(3, 1003, "Too New", last=(1, 2))   # only 2 aired
    c.seerr.add_show(4, 1004, "Anime One", genres=("Animation",), origin=("JP",))
    c.seerr.add_show(5, 1005, "Plain Show")
    c.seerr.add_show(6, 1006, "Third Eligible")
    for tvdb, t in [(1004, "Anime One"), (1005, "Plain Show"), (1006, "Third Eligible")]:
        c.sonarr.lookups[tvdb] = {"title": t, "tvdbId": tvdb}
    c.sonarr.add_existing(1, 1001, "Already Have")
    st["rejected"] = [1002]

    lines = weekly_add(cfg, c, st, NOW)

    assert sorted(st["shows"]) == ["1004", "1005"]
    rec = st["shows"]["1004"]
    assert rec["status"] == "active" and rec["dest"] == "/data/media/anime" and rec["window_start"] is None
    assert rec["path"] == "/data/media/trials/Anime One" and rec["setup_done"] is True
    assert st["shows"]["1005"]["dest"] == "/data/media/tv"
    sid = rec["sonarr_id"]
    mon = {e["episodeNumber"]: e["monitored"] for e in c.sonarr.eps[sid]}
    assert [n for n, m in mon.items() if m] == [1, 2, 3]
    assert ("search", (sid * 100 + 1, sid * 100 + 2, sid * 100 + 3)) in c.sonarr.calls
    assert len(lines) == 2 and "Anime One" in lines[0]


def test_skips_shows_already_in_state(tmp_path):
    cfg, c, st = setup(tmp_path)
    c.seerr.add_show(5, 1005, "Old Trial")
    c.sonarr.lookups[1005] = {"title": "Old Trial", "tvdbId": 1005}
    st["shows"]["1005"] = {"status": "kept"}
    assert weekly_add(cfg, c, st, NOW) == []


def test_low_disk_space_adds_nothing(tmp_path):
    cfg, c, st = setup(tmp_path)
    c.sonarr.free = 0.5e12
    c.seerr.add_show(5, 1005, "Plain Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    lines = weekly_add(cfg, c, st, NOW)
    assert st["shows"] == {} and "skipped" in lines[0]


def test_setup_deferred_when_sonarr_has_no_episodes_yet(tmp_path):
    cfg, c, st = setup(tmp_path)
    c.seerr.add_show(5, 1005, "Plain Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    real_add = c.sonarr.add_series

    def add_without_eps(*a):
        s = real_add(*a)
        c.sonarr.eps[s["id"]] = []
        return s
    c.sonarr.add_series = add_without_eps
    weekly_add(cfg, c, st, NOW)
    assert st["shows"]["1005"]["setup_done"] is False
```

Also create `tests/conftest.py` so `from fakes import ...` works:
```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
```

- [ ] **Step 3: Run it to verify it fails**

Run: `python -m pytest tests/test_jobs_add.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'trials.jobs'`.

- [ ] **Step 4: Implement the first half of `trials/jobs.py`**

```python
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .library import choose_destination

TAG = "trial"
UNAVAILABLE_COOLDOWN = timedelta(days=30)


@dataclass
class Clients:
    sonarr: object
    jellyfin: object
    seerr: object
    ntfy: object


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def aired_enough(details, n):
    last = details.get("lastEpisodeToAir") or {}
    season, episode = last.get("seasonNumber") or 0, last.get("episodeNumber") or 0
    return season > 1 or (season == 1 and episode >= n)


def trial_eps(episodes, n):
    return [e for e in episodes if e["seasonNumber"] == 1 and 1 <= e["episodeNumber"] <= n]


def setup_trial(c, rec, episodes, n):
    trial = [e["id"] for e in trial_eps(episodes, n)]
    if not trial:
        rec["setup_done"] = False
        return False
    sid = rec["sonarr_id"]
    c.sonarr.set_season_monitored(sid, 1, True)
    c.sonarr.set_monitored([e["id"] for e in episodes if e["id"] not in trial], False)
    c.sonarr.set_monitored(trial, True)
    c.sonarr.search_episodes(trial)
    rec["setup_done"] = True
    return True


def _skip_set(st, now):
    skip = set(st["rejected"])
    for key, rec in st["shows"].items():
        dropped = rec.get("dropped_at")
        if rec.get("status") == "unavailable" and dropped and now - parse(dropped) > UNAVAILABLE_COOLDOWN:
            continue
        skip.add(int(key))
    return skip


def weekly_add(cfg, c, st, now):
    free = c.sonarr.free_bytes(cfg.trials_root)
    if free < cfg.min_free_tb * 1e12:
        return [f"skipped weekly add: only {free / 1e12:.2f} TB free (< {cfg.min_free_tb} TB)"]
    skip = _skip_set(st, now) | {s["tvdbId"] for s in c.sonarr.series()}
    tag = c.sonarr.tag_id(TAG)
    profile = c.sonarr.quality_profile_id(cfg.quality_profile)
    lines = []
    for item in c.seerr.trending_tv():
        if len(lines) >= cfg.trials_per_week:
            break
        det = c.seerr.tv(item["id"])
        tvdb = (det.get("externalIds") or {}).get("tvdbId")
        if not tvdb or tvdb in skip or not aired_enough(det, cfg.trial_episodes):
            continue
        lookup = c.sonarr.lookup_tvdb(tvdb)
        if not lookup:
            continue
        s = c.sonarr.add_series(lookup, profile, cfg.trials_root, tag)
        rec = st["shows"][str(tvdb)] = {
            "tvdb": tvdb, "tmdb": item["id"], "title": s["title"], "sonarr_id": s["id"], "path": s["path"],
            "added_at": iso(now), "window_start": None, "status": "active",
            "dest": choose_destination([g["name"] for g in det.get("genres") or []], det.get("originCountry") or [],
                                       cfg.tv_root, cfg.anime_root, cfg.drama_root),
            "played": None, "reported": False, "setup_done": False, "dry_run": None}
        skip.add(tvdb)
        setup_trial(c, rec, c.sonarr.episodes(s["id"]), cfg.trial_episodes)
        lines.append(f"trial added: {s['title']} (S01E01-E{cfg.trial_episodes:02d}) -> Trials library")
    return lines
```

The state record is written **before** `setup_trial`. If a later call fails, the show is still tracked, so the next daily run finishes setup (Task 7) instead of leaving an untracked `trial`-tagged series.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_jobs_add.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "feat: weekly trending-trial add job with fakes"
```

---

### Task 7: Daily decide job

**Files:**
- Modify: `trials/jobs.py` (append the second half)
- Test: `tests/test_jobs_decide.py`

**Interfaces:**
- Consumes: `UserView, decide` (Task 3), `ep_key, restore_plan` (Task 4), and everything from Task 6.
- Produces:
  - `user_views(c, jf_id, users, n) -> (list[UserView], snapshot)`
  - `daily_decide(cfg, c, st, now) -> list[str]`
  - `finish_move(c, rec, index, users, now) -> list[str]`
  - `apply_keep(cfg, c, rec, s, tag, index, users, now, why) -> str`
  - `apply_drop(c, st, rec, s, why, now) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_jobs_decide.py`:
```python
from datetime import timedelta
from trials import state
from trials.jobs import Clients, daily_decide, iso, weekly_add
from fakes import FakeJellyfin, FakeNtfy, FakeSeerr, FakeSonarr, NOW, ep, make_cfg


def world(tmp_path, added_days_ago=30, **kw):
    cfg = make_cfg(tmp_path, **kw)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    st = state.empty()
    c.seerr.add_show(5, 1005, "Plain Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    weekly_add(cfg, c, st, NOW - timedelta(days=added_days_ago))
    rec = st["shows"]["1005"]
    return cfg, c, st, rec, rec["sonarr_id"]


def arrive(c, sid):
    for e in c.sonarr.eps[sid][:3]:
        e["hasFile"] = True


def open_window_ended(c, st, rec, sid):
    arrive(c, sid)
    rec["window_start"] = iso(NOW - timedelta(days=22))
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}


def test_window_starts_when_all_trial_eps_arrive(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path, added_days_ago=1)
    daily_decide(cfg, c, st, NOW)
    assert rec["window_start"] is None
    arrive(c, sid)
    lines = daily_decide(cfg, c, st, NOW)
    assert rec["window_start"] == iso(NOW) and "voting open" in lines[0]


def test_unavailable_after_arrival_days_is_deleted_without_exclusion(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    daily_decide(cfg, c, st, NOW)          # added 30 days ago, never arrived
    assert ("delete", sid, False) in c.sonarr.calls
    assert rec["status"] == "unavailable" and 1005 not in st["rejected"]


def test_majority_like_keeps_moves_then_restores_watched(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1, True), ep("e2", 2, True), ep("e3", 3, True)]
    c.jellyfin.eps[("jf1", "u2")] = [ep("e1", 1, False, 900)]
    c.jellyfin.like[("jf1", "u2")] = True           # watched a bit, voted 👍
    lines = daily_decide(cfg, c, st, NOW)
    assert ("move", sid, "/data/media/tv") in c.sonarr.calls and ("monitor_all", sid) in c.sonarr.calls
    assert ("untag", sid) in c.sonarr.calls and rec["status"] == "moving"
    assert any("KEPT" in l for l in lines)
    # next day Jellyfin has scanned the new location
    c.jellyfin.index = {"/data/media/tv/Plain Show": {"Id": "jf2"}}
    c.jellyfin.eps[("jf2", "u1")] = [ep("n1", 1), ep("n2", 2), ep("n3", 3)]
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    played = sorted(x for x in c.jellyfin.calls if x[0] in ("played", "pos"))
    assert played == [("played", "n1", "u1"), ("played", "n2", "u1"), ("played", "n3", "u1"), ("pos", "n1", "u2", 900)]
    assert rec["status"] == "kept" and rec["played"] is None


def test_majority_dislike_rejects_with_exclusion(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1, True)]      # quit after 1 -> dislike
    lines = daily_decide(cfg, c, st, NOW)
    assert ("delete", sid, True) in c.sonarr.calls and rec["status"] == "rejected"
    assert st["rejected"] == [1005] and any("REJECTED" in l for l in lines)


def test_nobody_engaged_rejects(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "rejected"


def test_dry_run_reports_once_and_changes_nothing(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path, enforce=False)
    open_window_ended(c, st, rec, sid)
    first = daily_decide(cfg, c, st, NOW)
    second = daily_decide(cfg, c, st, NOW)
    assert any("[dry-run] would DELETE" in l for l in first) and second == []
    assert not [x for x in c.sonarr.calls if x[0] in ("delete", "move")]
    assert rec["status"] == "active" and rec["dry_run"].startswith("would DELETE")


def test_tag_removed_by_user_releases_without_touching(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    open_window_ended(c, st, rec, sid)
    c.sonarr.series_db[sid]["tags"] = []
    daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "released"
    assert not [x for x in c.sonarr.calls if x[0] in ("delete", "move")]


def test_seerr_request_mid_trial_keeps_immediately(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    arrive(c, sid)
    rec["window_start"] = iso(NOW)               # window far from over
    c.seerr.requested.add(5)
    lines = daily_decide(cfg, c, st, NOW)
    assert ("move", sid, "/data/media/tv") in c.sonarr.calls and "requested" in lines[0]


def test_user_monitoring_more_episodes_keeps(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    arrive(c, sid)
    rec["window_start"] = iso(NOW)
    c.sonarr.eps[sid][5]["monitored"] = True      # someone monitored E06 in Sonarr
    daily_decide(cfg, c, st, NOW)
    assert rec["status"] == "moving"


def test_trial_tagged_series_not_in_state_is_ignored(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path)
    c.sonarr.add_existing(999, 4242, "Hand Tagged", tags=[1])
    open_window_ended(c, st, rec, sid)
    daily_decide(cfg, c, st, NOW)
    assert 999 in c.sonarr.series_db and not [x for x in c.sonarr.calls if x[1:2] == (999,)]


def test_deletion_ceiling_deletes_nothing(tmp_path):
    cfg, c, st, rec, sid = world(tmp_path, max_deletes_per_run=0)
    open_window_ended(c, st, rec, sid)
    lines = daily_decide(cfg, c, st, NOW)
    assert "SAFETY STOP" in lines[0] and sid in c.sonarr.series_db and rec["status"] == "active"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_jobs_decide.py -v`
Expected: FAIL, `ImportError: cannot import name 'daily_decide'`.

- [ ] **Step 3: Append to `trials/jobs.py`**

Add to the imports at the top:
```python
from .decide import UserView, decide
from .library import choose_destination, ep_key, restore_plan
```
(This replaces the previous `from .library import choose_destination` line.)

Append:
```python
def user_extended(episodes, n):
    trial = {e["id"] for e in trial_eps(episodes, n)}
    return any(e.get("monitored") and e["id"] not in trial for e in episodes)


def user_views(c, jf_id, users, n):
    views, snapshot = [], {}
    for u in users:
        eps = [e for e in c.jellyfin.season1_episodes(jf_id, u["Id"]) if 1 <= (e.get("IndexNumber") or 0) <= n]
        data = [(e, e.get("UserData") or {}) for e in eps]
        watched = sum(1 for _, d in data if d.get("Played") or (d.get("PlaybackPositionTicks") or 0) > 0)
        finished = sum(1 for _, d in data if d.get("Played"))
        views.append(UserView(c.jellyfin.likes(jf_id, u["Id"]), watched, finished))
        snapshot[u["Id"]] = {ep_key(1, e["IndexNumber"]): {"played": bool(d.get("Played")),
                                                            "ticks": int(d.get("PlaybackPositionTicks") or 0)}
                             for e, d in data}
    return views, snapshot


def finish_move(c, rec, index, users, now):
    jf = index.get(rec["new_path"].rstrip("/"))
    if not jf:
        if now - parse(rec["moved_at"]) > timedelta(days=2):
            return [f"{rec['title']}: moved, but Jellyfin hasn't picked it up after 2 days - watched marks not restored yet"]
        return []
    if users and rec.get("played"):
        new_items = {ep_key(1, e["IndexNumber"]): e["Id"]
                     for e in c.jellyfin.season1_episodes(jf["Id"], users[0]["Id"]) if e.get("IndexNumber")}
        for user_id, item_id, played, ticks in restore_plan(rec["played"], new_items):
            if played:
                c.jellyfin.mark_played(item_id, user_id)
            else:
                c.jellyfin.set_position(item_id, user_id, ticks)
    rec.update(status="kept", played=None)
    return [f"{rec['title']}: now in its permanent library, watched marks restored"]


def apply_keep(cfg, c, rec, s, tag, index, users, now, why):
    jf = index.get(s["path"].rstrip("/"))
    if jf and rec.get("played") is None:
        _, rec["played"] = user_views(c, jf["Id"], users, cfg.trial_episodes)
    old = s["path"]
    new = c.sonarr.move_series(s["id"], rec["dest"])
    rec.update(status="moving", new_path=new, moved_at=iso(now))
    c.sonarr.monitor_all_and_search(s["id"])
    c.sonarr.remove_tag(s["id"], tag)
    c.jellyfin.notify_paths(created=[new], deleted=[old])
    return f"KEPT {rec['title']} ({why}) -> {new}; downloading the rest"


def apply_drop(c, st, rec, s, why, now):
    unavailable = why == "unavailable"
    c.sonarr.delete_series(s["id"], exclude=not unavailable)
    c.jellyfin.notify_paths(deleted=[s["path"]])
    if unavailable:
        rec.update(status="unavailable", dropped_at=iso(now))
        return f"DROPPED {rec['title']}: trial episodes never arrived"
    rec["status"] = "rejected"
    st["rejected"].append(rec["tvdb"])
    return f"REJECTED {rec['title']} ({why}) - deleted"


def daily_decide(cfg, c, st, now):
    n = cfg.trial_episodes
    series = {s["id"]: s for s in c.sonarr.series()}
    tag = c.sonarr.tag_id(TAG)
    users = c.jellyfin.users()
    index = c.jellyfin.series_index()
    lines, keeps, drops = [], [], []
    for rec in st["shows"].values():
        if rec["status"] == "moving":
            lines += finish_move(c, rec, index, users, now)
            continue
        if rec["status"] != "active":
            continue
        s = series.get(rec["sonarr_id"])
        if not s or tag not in s.get("tags", []):
            rec["status"] = "released"
            lines.append(f"{rec['title']}: no longer a trial in Sonarr (tag removed or series deleted) - left alone")
            continue
        eps = c.sonarr.episodes(s["id"])
        if not rec.get("setup_done"):
            setup_trial(c, rec, eps, n)
            eps = c.sonarr.episodes(s["id"])
        if c.seerr.requested_since(rec["tmdb"], rec["added_at"]) or user_extended(eps, n):
            keeps.append((rec, s, "requested by a user"))
            continue
        if rec["window_start"] is None:
            t = trial_eps(eps, n)
            if len(t) == n and all(e["hasFile"] for e in t):
                rec["window_start"] = iso(now)
                lines.append(f"{rec['title']}: trial episodes arrived - voting open for {cfg.window_days} days")
            elif now - parse(rec["added_at"]) > timedelta(days=cfg.arrival_days):
                drops.append((rec, s, "unavailable"))
            continue
        if now < parse(rec["window_start"]) + timedelta(days=cfg.window_days):
            continue
        jf = index.get(s["path"].rstrip("/"))
        if not jf:
            lines.append(f"{rec['title']}: not found in Jellyfin - decision postponed")
            continue
        views, rec["played"] = user_views(c, jf["Id"], users, n)
        verdict, likes, dislikes = decide(views, n)
        (keeps if verdict == "keep" else drops).append((rec, s, f"{likes} like / {dislikes} dislike"))

    if not cfg.enforce:
        for label, group in (("KEEP", keeps), ("DELETE", drops)):
            for rec, _, why in group:
                rec["dry_run"] = f"would {label} ({why})"
                if not rec.get("reported"):
                    rec["reported"] = True
                    lines.append(f"[dry-run] would {label} {rec['title']} ({why})")
        return lines

    if len(drops) > cfg.max_deletes_per_run:
        lines.insert(0, f"SAFETY STOP: {len(drops)} deletions due (> {cfg.max_deletes_per_run}); nothing deleted - check the trials app")
        drops = []
    for rec, s, why in keeps:
        lines.append(apply_keep(cfg, c, rec, s, tag, index, users, now, why))
    for rec, s, why in drops:
        lines.append(apply_drop(c, st, rec, s, why, now))
    return lines
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest tests/ -v`
Expected: all pass (Tasks 1–7).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: daily decide job - tally, keep/move, reject, safety ceiling, dry-run"
```

---

### Task 8: Voting page

**Files:**
- Create: `trials/web.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `state.load`, `state.locked` (Task 2), `parse` (Task 6), `Jellyfin` methods (`series_index`, `likes`, `set_like`, `authenticate`).
- Produces:
  - `make_server(cfg, c, host="0.0.0.0", port=None) -> ThreadingHTTPServer`.
  - Routes: `GET /healthz`, `GET /`, `POST /login`, `POST /logout`, `POST /vote` (`item`, `value` ∈ up|down|clear), and `POST /unreject` (`tvdb`, admins only).

- [ ] **Step 1: Write the failing test**

`tests/test_web.py`:
```python
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
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_web.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'trials.web'`.

- [ ] **Step 3: Implement `trials/web.py`**

```python
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

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, "ok", "text/plain")
            if self.path != "/":
                return self._send(404, "not found", "text/plain")
            user = self._user()
            self._send(200, render_page(cfg, c, user) if user else render_login())

        def do_POST(self):
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

    return ThreadingHTTPServer((host, cfg.port if port is None else port), Handler)
```

Sessions live in memory, so a container restart signs everyone out. That's acceptable: logging in again is one form. `# ponytail:` note: move sessions to the state file if restarts become frequent.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_web.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: voting page with Jellyfin login"
```

---

### Task 9: Scheduler + CLI

**Files:**
- Create: `trials/main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `Config.from_env`, `state`, all clients, `Clients`, `weekly_add`, `daily_decide`, `iso`, `parse`, `make_server`.
- Produces:
  - `due(now, st) -> list["add"|"decide"]`. `add` runs Mondays from 10:00 UTC, once per ISO week. `decide` runs daily from 11:00 UTC, once per date. Both respect `st["retry_after_<job>"]`.
  - `run_job(name, cfg, c, now) -> list[str]`.
  - `main(argv=None)` with subcommands `serve | add | decide | probe`.

- [ ] **Step 1: Write the failing test**

`tests/test_main.py`:
```python
from datetime import datetime, timedelta, timezone
from trials import state
from trials.jobs import Clients, iso
from trials.main import due, run_job
from fakes import FakeJellyfin, FakeNtfy, FakeSeerr, FakeSonarr, make_cfg

MON_0930 = datetime(2026, 10, 5, 9, 30, tzinfo=timezone.utc)
MON_1130 = datetime(2026, 10, 5, 11, 30, tzinfo=timezone.utc)
TUE_1130 = MON_1130 + timedelta(days=1)


def test_due_schedule():
    st = state.empty()
    assert due(MON_0930, st) == []
    assert due(MON_1130, st) == ["add", "decide"]
    st.update(last_add_week="2026-W41", last_decide_date="2026-10-05")
    assert due(MON_1130, st) == []
    assert due(TUE_1130, st) == ["decide"]


def test_due_respects_retry_after():
    st = state.empty()
    st["retry_after_decide"] = iso(MON_1130 + timedelta(minutes=30))
    assert due(MON_1130, st) == ["add"]


def test_run_job_success_marks_run_and_notifies(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())
    c.seerr.add_show(5, 1005, "Plain Show")
    c.sonarr.lookups[1005] = {"title": "Plain Show", "tvdbId": 1005}
    lines = run_job("add", cfg, c, MON_1130)
    st = state.load(cfg.state_path)
    assert st["last_add_week"] == "2026-W41" and "1005" in st["shows"]
    assert c.ntfy.sent and "Plain Show" in c.ntfy.sent[0][1] and lines


def test_run_job_failure_sets_retry_and_keeps_partial_state(tmp_path):
    cfg = make_cfg(tmp_path)
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy())

    def boom():
        raise RuntimeError("jellyfin down")
    c.jellyfin.users = boom
    lines = run_job("decide", cfg, c, MON_1130)
    st = state.load(cfg.state_path)
    assert st["last_decide_date"] is None and st["retry_after_decide"] == iso(MON_1130 + timedelta(hours=1))
    assert "aborted" in lines[0]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_main.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'trials.main'`.

- [ ] **Step 3: Implement `trials/main.py`**

```python
import argparse
import threading
from datetime import datetime, timedelta, timezone

from . import state as state_mod
from .clients import Jellyfin, Ntfy, Seerr, Sonarr
from .config import Config
from .jobs import Clients, daily_decide, iso, parse, weekly_add


def build_clients(cfg):
    return Clients(Sonarr(cfg.sonarr_url, cfg.sonarr_key), Jellyfin(cfg.jellyfin_url, cfg.jellyfin_key),
                   Seerr(cfg.seerr_url, cfg.seerr_key), Ntfy(cfg.ntfy_url, cfg.ntfy_topic))


def _blocked(st, name, now):
    retry = st.get(f"retry_after_{name}")
    return bool(retry) and now < parse(retry)


def due(now, st):
    jobs = []
    if now.weekday() == 0 and now.hour >= 10 and st.get("last_add_week") != now.strftime("%G-W%V") and not _blocked(st, "add", now):
        jobs.append("add")
    if now.hour >= 11 and st.get("last_decide_date") != now.date().isoformat() and not _blocked(st, "decide", now):
        jobs.append("decide")
    return jobs


def run_job(name, cfg, c, now):
    with state_mod.locked(cfg.state_path) as st:
        try:
            if name == "add":
                lines = weekly_add(cfg, c, st, now)
                st["last_add_week"] = now.strftime("%G-W%V")
            else:
                lines = daily_decide(cfg, c, st, now)
                st["last_decide_date"] = now.date().isoformat()
            st.pop(f"retry_after_{name}", None)
        except Exception as e:  # fail closed: stop this run, keep what really happened, retry in an hour
            st[f"retry_after_{name}"] = iso(now + timedelta(hours=1))
            lines = [f"{name} run aborted, retrying in 1h: {e}"]
    for line in lines:
        print(line, flush=True)
    if lines:
        try:
            c.ntfy.send(f"Trial Shows: {name}", "\n".join(lines))
        except Exception as e:
            print(f"ntfy failed: {e}", flush=True)
    return lines


def scheduler(cfg, c, stop):
    while not stop.is_set():
        now = datetime.now(timezone.utc)
        for name in due(now, state_mod.load(cfg.state_path)):
            run_job(name, cfg, c, now)
        stop.wait(600)


def probe(cfg, c):
    print("sonarr series:", len(c.sonarr.series()))
    print("sonarr quality profile id:", c.sonarr.quality_profile_id(cfg.quality_profile))
    print(f"free at {cfg.trials_root}: {c.sonarr.free_bytes(cfg.trials_root) / 1e12:.2f} TB")
    print("jellyfin users:", [u["Name"] for u in c.jellyfin.users()])
    print("jellyfin series indexed:", len(c.jellyfin.series_index()))
    trending = c.seerr.trending_tv(pages=1)[:5]
    for t in trending:
        d = c.seerr.tv(t["id"])
        print("trending:", t.get("name"), "| tvdb", (d.get("externalIds") or {}).get("tvdbId"),
              "| last aired", d.get("lastEpisodeToAir", {}) and (d["lastEpisodeToAir"].get("seasonNumber"), d["lastEpisodeToAir"].get("episodeNumber")))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="trials")
    ap.add_argument("command", choices=["serve", "add", "decide", "probe"])
    args = ap.parse_args(argv)
    cfg = Config.from_env()
    c = build_clients(cfg)
    if args.command == "probe":
        return probe(cfg, c)
    if args.command in ("add", "decide"):
        run_job(args.command, cfg, c, datetime.now(timezone.utc))
        return
    from .web import make_server
    stop = threading.Event()
    threading.Thread(target=scheduler, args=(cfg, c, stop), daemon=True).start()
    print(f"trials serving on :{cfg.port} (enforce={cfg.enforce})", flush=True)
    make_server(cfg, c).serve_forever()
```

- [ ] **Step 4: Run the whole suite**

Run: `python -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: scheduler and CLI (serve/add/decide/probe)"
```

---

### Task 10: Container, CI, deploy snippets, GitHub repo

**Files:**
- Create: `Dockerfile`, `.github/workflows/ci.yml`, `deploy/compose-snippet.yml`, `deploy/homepage-snippet.yaml`

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY trials ./trials
RUN pip install --no-cache-dir .
USER 1000:1000
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"
CMD ["python", "-m", "trials", "serve"]
```

- [ ] **Step 2: Write `.github/workflows/ci.yml`**

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[dev]"
      - run: python -m pytest tests/ -v
      - run: docker build -t trials:ci .
```

- [ ] **Step 3: Write `deploy/compose-snippet.yml`**

This is the service stanza for cyberpac's `~/vulcan/stack/docker-compose.override.yml`. The `labels:` block is filled in Task 11 Step 5 by copying Seerr's Traefik labels and renaming the router.

```yaml
  trials:
    build: /home/sentinel/trials
    image: trials:local
    container_name: trials
    restart: unless-stopped
    user: "1000:1000"
    env_file: /home/sentinel/trials/.env
    volumes:
      - ./config/trials:/data
    networks:
      - default
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    mem_limit: 256m
```

- [ ] **Step 4: Write `deploy/homepage-snippet.yaml`**

```yaml
- Trial Shows:
    href: https://trials.totallylegitmedia.us
    icon: mdi-thumbs-up-down
    description: Vote to keep or drop this week's trial shows (Trials library in Jellyfin)
    siteMonitor: http://trials:8080/healthz
- Trial Shows alerts:
    href: https://ntfy.sh/NTFY_TOPIC_FROM_ENV
    icon: ntfy.png
    description: Keep/drop decisions and safety alerts from Trial Shows
    widget:
      type: ntfy
      url: https://ntfy.sh
      topic: NTFY_TOPIC_FROM_ENV
```
(`NTFY_TOPIC_FROM_ENV` is literally replaced with the generated topic from Task 11 Step 4 when this is pasted into Homepage's `services.yaml`.)

- [ ] **Step 5: Verify the image builds and the suite passes**

Run: `docker build -t trials:local . && python -m pytest tests/ -v`
Expected: image builds, all tests pass.

- [ ] **Step 6: Commit, create the GitHub repo, push (confirm first)**

```bash
git add -A && git commit -m "build: Dockerfile, CI, deploy snippets"
```
**Ask the user:** public or private repo, and OK to push? Then:
```bash
gh repo create Cyb3rRon1n/trials --<public|private> --source . --remote origin --push
```

---

### Task 11: Deploy to cyberpac (dry-run) and verify live

All commands run from the workstation over `ssh 192.168.10.157` unless noted. **The first real weekly add creates real Sonarr series and downloads. Ask the user before Step 8.**

- [ ] **Step 1: Get the code onto cyberpac**

Public repo: `ssh 192.168.10.157 'git clone https://github.com/Cyb3rRon1n/trials ~/trials'`.
Private repo: `rsync -a --exclude .venv --exclude .git ~/projects/github/repos/trials/ 192.168.10.157:trials/`.

- [ ] **Step 2: Trials folder and Sonarr root folder**

```bash
ssh 192.168.10.157 'mkdir -p /mnt/media/media/trials && ls -ld /mnt/media/media/trials'
ssh 192.168.10.157 python3 - <<'EOF'
import json, re, os, urllib.request
K = re.search(r"<ApiKey>([^<]+)", open(os.path.expanduser("~/vulcan/stack/config/sonarr/config.xml")).read()).group(1)
r = urllib.request.Request("http://localhost:8989/api/v3/rootfolder", json.dumps({"path": "/data/media/trials"}).encode(),
                           {"X-Api-Key": K, "Content-Type": "application/json"}, method="POST")
print(json.load(urllib.request.urlopen(r))["path"])
EOF
```
Expected: `drwxr-xr-x sentinel sentinel ... trials`, then `/data/media/trials`.

- [ ] **Step 3: Jellyfin Trials library + user access + API key**

```bash
ssh 192.168.10.157 'K=$(grep -A6 "type: jellyfin" ~/vulcan/stack/config/homepage/services.yaml | grep -oE "key: *[a-f0-9]{32}" | head -1 | awk "{print \$2}") python3 -' <<'EOF'
import json, os, urllib.request, urllib.parse
H = {"Authorization": f'MediaBrowser Token="{os.environ["K"]}"', "Content-Type": "application/json"}
def call(m, p, body=None):
    r = urllib.request.urlopen(urllib.request.Request("http://localhost:8096" + p, json.dumps(body).encode() if body is not None else None, H, method=m))
    raw = r.read(); return json.loads(raw) if raw else None
libs = call("GET", "/Library/VirtualFolders")
if not any(l["Name"] == "Trials" for l in libs):
    opts = next(l for l in libs if l["Name"] == "Shows")["LibraryOptions"]
    opts["PathInfos"] = [{"Path": "/data/media/trials"}]
    call("POST", "/Library/VirtualFolders?" + urllib.parse.urlencode({"name": "Trials", "collectionType": "tvshows", "refreshLibrary": "false"}), {"LibraryOptions": opts})
trials_id = next(l["ItemId"] for l in call("GET", "/Library/VirtualFolders") if l["Name"] == "Trials")
for u in call("GET", "/Users"):
    p = u["Policy"]
    if not p.get("EnableAllFolders") and trials_id not in p.get("EnabledFolders", []):
        p["EnabledFolders"] = p.get("EnabledFolders", []) + [trials_id]
        call("POST", f"/Users/{u['Id']}/Policy", p); print("granted Trials to", u["Name"])
call("POST", "/Auth/Keys?App=Trials")
print("trials key:", next(k["AccessToken"] for k in call("GET", "/Auth/Keys")["Items"] if k["AppName"] == "Trials"))
EOF
```
Expected: `trials key: <32 hex>`. The Trials library appears in Jellyfin with the same metadata settings as Shows.

- [ ] **Step 4: Write `~/trials/.env` on cyberpac**

```bash
ssh 192.168.10.157 'cd ~/trials && S=$(grep -o "<ApiKey>[^<]*" ~/vulcan/stack/config/sonarr/config.xml | cut -c9-) && E=$(python3 -c "import json;print(json.load(open(\"$HOME/vulcan/stack/config/seerr/settings.json\"))[\"main\"][\"apiKey\"])") && T=cyberpac-trials-$(openssl rand -hex 6) && cat > .env <<EOF
SEERR_URL=http://seerr:5055
SEERR_KEY=$E
SONARR_URL=http://sonarr:8989
SONARR_KEY=$S
JELLYFIN_URL=http://jellyfin:8096
JELLYFIN_KEY=<paste the trials key from Step 3>
JELLYFIN_PUBLIC_URL=https://jellyfin.totallylegitmedia.us
NTFY_TOPIC=$T
TRIALS_ENFORCE=0
EOF
chmod 600 .env && echo topic=$T'
```
Then replace the `JELLYFIN_KEY=` line with the key printed in Step 3:
`ssh 192.168.10.157 "sed -i 's|^JELLYFIN_KEY=.*|JELLYFIN_KEY=<key>|' ~/trials/.env"`.
Also check Sonarr's quality profile names: if `HD-1080p` doesn't exist, add `QUALITY_PROFILE=<exact name>`. List them with:
`ssh 192.168.10.157 'curl -s -H "X-Api-Key: $(grep -o "<ApiKey>[^<]*" ~/vulcan/stack/config/sonarr/config.xml | cut -c9-)" localhost:8989/api/v3/qualityprofile | python3 -c "import json,sys;print([p[\"name\"] for p in json.load(sys.stdin)])"'`

- [ ] **Step 5: Add the service to the override with Traefik + Authelia labels**

Print Seerr's labels as the template:
`ssh 192.168.10.157 "docker inspect seerr --format '{{json .Config.Labels}}' | python3 -m json.tool | grep traefik"`
Append `deploy/compose-snippet.yml` under `services:` in `~/vulcan/stack/docker-compose.override.yml`. Add a `labels:` list that copies every `traefik.*` label from Seerr, with `seerr` replaced by `trials` in router and service names, the `Host(...)` rule set to `trials.totallylegitmedia.us`, and `loadbalancer.server.port=8080`. **Include** the Authelia middleware label: if Seerr's labels exclude Authelia (Seerr was deliberately excluded), take the `middlewares` label from `docker inspect sonarr` instead.

- [ ] **Step 6: Start it and run the read-only probe**

```bash
ssh 192.168.10.157 'cd ~/vulcan/stack && docker compose up -d --build trials && sleep 10 && docker ps --filter name=trials --format "{{.Status}}" && docker exec trials python -m trials probe'
```
Expected: container `Up (healthy)`. The probe prints the Sonarr series count, the quality profile id, free TB (~12), 9 Jellyfin users, the Jellyfin series count, and 5 trending shows each with a tvdb id.

- [ ] **Step 7: Verify the web page**

`curl -s -o /dev/null -w "%{http_code}\n" https://trials.totallylegitmedia.us` should give `302` (to Authelia). Then in a browser: sign in through Authelia, then Jellyfin login, and confirm "No shows on trial right now."
Add `deploy/homepage-snippet.yaml` to `~/vulcan/stack/config/homepage/services.yaml`, with the topic from Step 4, and check the tile renders.

- [ ] **Step 8: First weekly add (ask the user first)**

`ssh 192.168.10.157 'docker exec trials python -m trials add'`
Expected: an ntfy message and 3 `trial added:` lines. In Sonarr: 3 new series under `/data/media/trials` tagged `trial`, with only S01E01–E03 monitored and searching. After download they appear in Jellyfin's **Trials** library and on the voting page with "vote by <date>".

- [ ] **Step 9: Dry-run decide check**

`ssh 192.168.10.157 'docker exec trials python -m trials decide && cat ~/vulcan/stack/config/trials/state.json | python3 -m json.tool | head -40'`
Expected: once episodes land, a `voting open for 21 days` line. No move or delete calls, since `TRIALS_ENFORCE=0`.

- [ ] **Step 10: Record and hand over**

Update the `trial-shows-project` memory: deployed, dry-run, the ntfy topic, the first cycle's decision date, and that `TRIALS_ENFORCE=1` needs the user's go-ahead after they've seen one dry-run decision. Tell the user when the first decisions will be reported (window start + 21 days).
