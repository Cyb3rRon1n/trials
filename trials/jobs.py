from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .decide import UserView, decide
from .library import choose_destination, ep_key, restore_plan

TAG = "trial"
UNAVAILABLE_COOLDOWN = timedelta(days=30)
GIVE_UP = timedelta(days=7)


@dataclass
class Clients:
    sonarr: object
    jellyfin: object
    seerr: object
    ntfy: object
    radarr: object = None      # movie request trials; None = shows only


def _in(root, path):
    root = root.rstrip("/")
    return (path or "").rstrip("/").startswith(root + "/")


def is_request(rec):
    return rec.get("kind") == "request"


def is_movie(rec):
    return rec.get("media") == "movie"


def adopt_requests(cfg, c, st, now, lines=None):
    """A request an admin approved into the Trials folder becomes a trial of the WHOLE request.
    Only things sitting in the trials roots are adopted - nothing else is ever judged."""
    lines = [] if lines is None else lines
    live = ("active", "moving")
    tag = c.sonarr.tag_id(TAG)
    for s in c.sonarr.series():
        key = str(s.get("tvdbId"))
        if not _in(cfg.trials_root, s.get("path")) or st["shows"].get(key, {}).get("status") in live:
            continue
        det = c.seerr.tv(s["tmdbId"]) if s.get("tmdbId") else {}
        dest = choose_destination([g["name"] for g in det.get("genres") or []], det.get("originCountry") or [],
                                  cfg.tv_root, cfg.anime_root, cfg.drama_root)
        if tag not in s.get("tags", []):
            c.sonarr.add_tag(s["id"], tag)
        st["shows"][key] = {"tvdb": s["tvdbId"], "tmdb": s.get("tmdbId"), "title": s["title"], "sonarr_id": s["id"],
                            "path": s["path"], "added_at": iso(now), "window_start": None, "status": "active",
                            "dest": dest, "played": None, "setup_done": True, "dry_run": None,
                            "kind": "request", "media": "tv"}
        lines.append(f"request trial: {s['title']} (whole request) -> Trials library")
    if c.radarr is None:
        return lines
    mtag = c.radarr.tag_id(TAG)
    for m in c.radarr.movies():
        key = f"movie:{m.get('tmdbId')}"
        if not _in(cfg.trials_movies_root, m.get("path")) or st["shows"].get(key, {}).get("status") in live:
            continue
        if mtag not in m.get("tags", []):
            c.radarr.add_tag(m["id"], mtag)
        st["shows"][key] = {"tmdb": m.get("tmdbId"), "title": f"{m['title']} ({m.get('year')})", "radarr_id": m["id"],
                            "path": m["path"], "added_at": iso(now), "window_start": None, "status": "active",
                            "dest": cfg.movies_root, "played": None, "dry_run": None, "kind": "request", "media": "movie"}
        lines.append(f"request trial: {m['title']} (movie) -> Trials movies library")
    return lines


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
    c.sonarr.set_monitored([e["id"] for e in episodes if e["id"] not in trial], False)
    c.sonarr.set_monitored(trial, True)
    c.sonarr.search_episodes(trial)
    rec["known_episode_ids"] = sorted(e["id"] for e in episodes)
    rec["setup_done"] = True
    return True


def _notify_jellyfin(c, created=(), deleted=()):
    try:
        c.jellyfin.notify_paths(created=created, deleted=deleted)
        return ""
    except Exception as e:  # Jellyfin's own scan will catch up; never un-record a real change for this
        return f" (Jellyfin not notified: {e})"


def _skip_set(st, now):
    skip = set(st["rejected"])
    for key, rec in st["shows"].items():
        dropped = rec.get("dropped_at")
        if rec.get("status") == "unavailable" and dropped and now - parse(dropped) > UNAVAILABLE_COOLDOWN:
            continue
        skip.add(int(key))
    return skip


def weekly_add(cfg, c, st, now, lines=None):
    lines = [] if lines is None else lines
    free = c.sonarr.free_bytes(cfg.trials_root)
    if free < cfg.min_free_tb * 1e12:
        lines.append(f"skipped weekly add: only {free / 1e12:.2f} TB free (< {cfg.min_free_tb} TB)")
        return lines
    skip = _skip_set(st, now) | {s["tvdbId"] for s in c.sonarr.series()}
    tag = c.sonarr.tag_id(TAG)
    profile = c.sonarr.quality_profile_id(cfg.quality_profile)
    week = now.strftime("%G-W%V")
    already = sum(1 for rec in st["shows"].values()
                  if rec.get("added_at") and parse(rec["added_at"]).strftime("%G-W%V") == week)
    target = max(0, cfg.trials_per_week - already)
    added = 0
    for item in c.seerr.trending_tv():
        if added >= target:
            break
        det = c.seerr.tv(item["id"])
        tvdb = (det.get("externalIds") or {}).get("tvdbId")
        if not tvdb or tvdb in skip or not aired_enough(det, cfg.trial_episodes):
            continue
        lookup = c.sonarr.lookup_tvdb(tvdb)
        if not lookup:
            continue
        dest = choose_destination([g["name"] for g in det.get("genres") or []], det.get("originCountry") or [],
                                  cfg.tv_root, cfg.anime_root, cfg.drama_root)
        # anime releases use absolute numbering; as "standard" Sonarr matches other arcs' "E01"
        s = c.sonarr.add_series(lookup, profile, cfg.trials_root, tag,
                                series_type="anime" if dest == cfg.anime_root else "standard")
        rec = st["shows"][str(tvdb)] = {
            "tvdb": tvdb, "tmdb": item["id"], "title": s["title"], "sonarr_id": s["id"], "path": s["path"],
            "added_at": iso(now), "window_start": None, "status": "active", "dest": dest,
            "played": None, "setup_done": False, "dry_run": None}
        skip.add(tvdb)
        setup_trial(c, rec, c.sonarr.episodes(s["id"]), cfg.trial_episodes)
        lines.append(f"trial added: {s['title']} (S01E01-E{cfg.trial_episodes:02d}) -> Trials library")
        added += 1
    return lines


def _mark_on_trial(cfg, c, rec, jf_id, users, lines):
    """Best-effort: tell Jellyfin viewers the show is on trial and where to vote. Never aborts a run."""
    if not cfg.trials_public_url or rec.get("noted") or not users or not rec.get("window_start"):
        return
    end = parse(rec["window_start"]) + timedelta(days=cfg.window_days)
    note = f"🗳 ON TRIAL until {end.strftime('%a %d %b')} – vote to keep or drop it at {cfg.trials_public_url}"
    try:
        c.jellyfin.set_trial_note(jf_id, users[0]["Id"], note)
        rec["noted"] = True
    except Exception as e:
        lines.append(f"{rec['title']}: couldn't add the on-trial note in Jellyfin: {e}")


def user_extended(rec, episodes, n):
    trial = {e["id"] for e in trial_eps(episodes, n)}
    known = set(rec.get("known_episode_ids") or [])
    return any(e.get("monitored") and e["id"] not in trial and e["id"] in known for e in episodes)


def _finish_keep_steps(c, rec, tag):
    if is_movie(rec):
        if not rec.get("untagged"):
            c.radarr.remove_tag(rec["radarr_id"], c.radarr.tag_id(TAG))
            rec["untagged"] = True
        return
    if not rec.get("completed"):
        if not is_request(rec):   # a request already monitors exactly what was asked for
            c.sonarr.monitor_all_and_search(rec["sonarr_id"])
        rec["completed"] = True
    if not rec.get("untagged"):
        c.sonarr.remove_tag(rec["sonarr_id"], tag)
        rec["untagged"] = True


def _hist(d):
    return {"played": bool(d.get("Played")), "ticks": int(d.get("PlaybackPositionTicks") or 0),
            "count": int(d.get("PlayCount") or 0), "date": d.get("LastPlayedDate")}


def _taste(users, views):
    """each user's 👍/👎 and ♥ on the show/movie itself, so a kept title keeps them"""
    return {u["Id"]: {"likes": v.likes, "fav": v.favorite} for u, v in zip(users, views)
            if v.likes is not None or v.favorite}


def request_views(c, jf_id, users, n):
    """whole-request show: any episode counts; no vote + finished min(n, available) episodes = keep"""
    views, snapshot, total = [], {}, 0
    for u in users:
        eps = [e for e in c.jellyfin.all_episodes(jf_id, u["Id"]) if e.get("IndexNumber") is not None]
        total = max(total, len(eps))
        data = [(e, e.get("UserData") or {}) for e in eps]
        watched = sum(1 for _, d in data if d.get("Played") or (d.get("PlaybackPositionTicks") or 0) > 0)
        finished = sum(1 for _, d in data if d.get("Played"))
        views.append(UserView(c.jellyfin.likes(jf_id, u["Id"]), watched, finished, c.jellyfin.favorite(jf_id, u["Id"])))
        snapshot[u["Id"]] = {ep_key(e.get("ParentIndexNumber") or 0, e["IndexNumber"]): _hist(d) for e, d in data}
    return views, snapshot, max(1, min(n, total))


def movie_views(c, jf_id, users):
    """no vote: watched to the end = keep, started but abandoned = drop"""
    views, snapshot = [], {}
    for u in users:
        d = c.jellyfin.user_data(jf_id, u["Id"])
        played, ticks = bool(d.get("Played")), int(d.get("PlaybackPositionTicks") or 0)
        views.append(UserView(c.jellyfin.likes(jf_id, u["Id"]), int(played or ticks > 0), int(played), bool(d.get("IsFavorite"))))
        snapshot[u["Id"]] = {"movie": _hist(d)}
    return views, snapshot


def user_views(c, jf_id, users, n):
    views, snapshot = [], {}
    for u in users:
        eps = [e for e in c.jellyfin.season1_episodes(jf_id, u["Id"]) if 1 <= (e.get("IndexNumber") or 0) <= n]
        data = [(e, e.get("UserData") or {}) for e in eps]
        watched = sum(1 for _, d in data if d.get("Played") or (d.get("PlaybackPositionTicks") or 0) > 0)
        finished = sum(1 for _, d in data if d.get("Played"))
        views.append(UserView(c.jellyfin.likes(jf_id, u["Id"]), watched, finished, c.jellyfin.favorite(jf_id, u["Id"])))
        snapshot[u["Id"]] = {ep_key(1, e["IndexNumber"]): _hist(d) for e, d in data}
    return views, snapshot


def finish_move(c, rec, index, users, now):
    snap = rec.get("played") or {}
    needed = {k for eps in snap.values() for k in eps}
    age = now - parse(rec["moved_at"])
    jf = (c.jellyfin.movie_index() if is_movie(rec) else index).get(rec["new_path"].rstrip("/"))
    if not jf:
        if age < GIVE_UP:
            return []
        rec.update(status="kept", played=None)
        return [f"{rec['title']}: kept, but Jellyfin never showed its new location after 7 days - watched marks NOT restored"]
    if users:
        if is_movie(rec):
            new_items = {"movie": jf["Id"]}
        else:
            new_items = {ep_key(e.get("ParentIndexNumber") or 0, e["IndexNumber"]): e["Id"]
                         for e in c.jellyfin.all_episodes(jf["Id"], users[0]["Id"]) if e.get("IndexNumber")}
        if needed - set(new_items) and age < GIVE_UP:
            return []
        for user_id, item_id, played, ticks, count, date in restore_plan(snap, new_items):
            c.jellyfin.restore_played(item_id, user_id, played, ticks, count, date)
        for user_id, t in (rec.get("taste") or {}).items():
            if t.get("likes") is not None:
                c.jellyfin.set_like(jf["Id"], user_id, t["likes"])
            if t.get("fav"):
                c.jellyfin.set_favorite(jf["Id"], user_id, True)
        missing = sorted(needed - set(new_items))
    else:
        new_items = {}
        missing = sorted(needed)
    rec.update(status="kept", played=None, taste=None)
    if missing:
        return [f"{rec['title']}: kept, watched marks restored except {missing} (not found in Jellyfin after 7 days)"]
    return [f"{rec['title']}: now in its permanent library, watched marks restored"]


def apply_keep(cfg, c, rec, s, tag, index, users, now, why):
    jf = index.get(s["path"].rstrip("/"))
    if jf:
        if is_movie(rec):
            views, rec["played"] = movie_views(c, jf["Id"], users)
        elif is_request(rec):
            views, rec["played"], _ = request_views(c, jf["Id"], users, cfg.trial_episodes)
        else:
            views, rec["played"] = user_views(c, jf["Id"], users, cfg.trial_episodes)
        rec["taste"] = _taste(users, views)
        if rec.get("noted") and users:
            try:  # cosmetic; the move normally gives Jellyfin a fresh item anyway
                c.jellyfin.set_trial_note(jf["Id"], users[0]["Id"], None)
                rec["noted"] = False
            except Exception:
                pass
    old = s["path"]
    new = c.radarr.move_movie(s["id"], rec["dest"]) if is_movie(rec) else c.sonarr.move_series(s["id"], rec["dest"])
    rec.update(status="moving", new_path=new, moved_at=iso(now), completed=False, untagged=False)
    _finish_keep_steps(c, rec, tag)
    msg = f"KEPT {rec['title']} ({why}) -> {new}" + ("" if is_movie(rec) or is_request(rec) else "; downloading the rest")
    msg += _notify_jellyfin(c, created=[new], deleted=[old])
    return msg


def apply_drop(c, st, rec, s, why, now):
    unavailable = why == "unavailable"
    if is_movie(rec):
        c.radarr.delete_movie(s["id"], exclude=True)
        rec["status"] = "rejected"
        st.setdefault("rejected_movies", []).append(rec["tmdb"])
        return f"REJECTED {rec['title']} ({why}) - deleted" + _notify_jellyfin(c, deleted=[s["path"]])
    c.sonarr.delete_series(s["id"], exclude=not unavailable)
    if unavailable:
        rec.update(status="unavailable", dropped_at=iso(now))
        msg = f"DROPPED {rec['title']}: trial episodes never arrived"
    else:
        rec["status"] = "rejected"
        st["rejected"].append(rec["tvdb"])
        msg = f"REJECTED {rec['title']} ({why}) - deleted"
    msg += _notify_jellyfin(c, deleted=[s["path"]])
    return msg


def _open_window(cfg, c, rec, jf_id, users, now, lines, why="arrived"):
    rec["window_start"] = iso(now)
    lines.append(f"{rec['title']}: {why} - voting open for {cfg.window_days} days")
    _mark_on_trial(cfg, c, rec, jf_id, users, lines)


def _request_arrival(cfg, c, rec, s, eps, index, users, now, lines):
    """a whole request opens for voting once every aired, monitored episode is in - or after
    arrival_days with whatever arrived. Never dropped for being slow: it was asked for."""
    aired = [e for e in eps if e.get("monitored") and e["seasonNumber"] > 0
             and e.get("airDateUtc") and parse(e["airDateUtc"]) <= now]
    have = [e for e in aired if e["hasFile"]]
    late = bool(have) and now - parse(rec["added_at"]) > timedelta(days=cfg.arrival_days)
    jf = index.get(s["path"].rstrip("/"))
    if aired and len(have) == len(aired) or late:
        if jf:
            _open_window(cfg, c, rec, jf["Id"], users, now, lines,
                         "request arrived" if len(have) == len(aired) else f"{len(have)}/{len(aired)} episodes arrived")
        else:
            lines.append(f"{rec['title']}: files present but Jellyfin hasn't indexed {s['path']} yet - waiting")
    elif have != aired:
        c.sonarr.search_episodes([e["id"] for e in aired if not e["hasFile"]])


def _decide_movie(cfg, c, rec, movies, movie_index, mtag, users, now, keeps, drops, lines):
    m = movies.get(rec["radarr_id"])
    if rec["status"] == "moving":
        if not m or m.get("tmdbId") != rec["tmdb"]:
            rec["status"] = "released"
            lines.append(f"{rec['title']}: removed from Radarr during move")
            return
        _finish_keep_steps(c, rec, TAG)
        lines.extend(finish_move(c, rec, {}, users, now))
        return
    if rec["status"] != "active":
        return
    if not m or mtag not in m.get("tags", []) or m.get("tmdbId") != rec["tmdb"]:
        rec["status"] = "released"
        lines.append(f"{rec['title']}: no longer a trial in Radarr (tag removed or movie deleted) - left alone")
        return
    if rec.get("override"):
        (keeps if rec["override"]["verdict"] == "keep" else drops).append((rec, m, f"overruled by {rec['override']['by']}"))
        return
    jf = movie_index.get(m["path"].rstrip("/"))
    if rec["window_start"] is None:
        if m.get("hasFile") and jf:
            _open_window(cfg, c, rec, jf["Id"], users, now, lines)
        elif m.get("hasFile"):
            lines.append(f"{rec['title']}: file present but Jellyfin hasn't indexed it yet - waiting")
        return
    if now < parse(rec["window_start"]) + timedelta(days=cfg.window_days):
        if jf:
            _mark_on_trial(cfg, c, rec, jf["Id"], users, lines)
        return
    if not jf:
        lines.append(f"{rec['title']}: not found in Jellyfin - decision postponed")
        return
    views, rec["played"] = movie_views(c, jf["Id"], users)
    verdict, likes, dislikes = decide(views, 1)
    (keeps if verdict == "keep" else drops).append((rec, m, f"{likes} like / {dislikes} dislike"))


def daily_decide(cfg, c, st, now, lines=None):
    lines = [] if lines is None else lines
    n = cfg.trial_episodes
    adopt_requests(cfg, c, st, now, lines)
    series = {s["id"]: s for s in c.sonarr.series()}
    tag = c.sonarr.tag_id(TAG)
    users = c.jellyfin.users()
    index = c.jellyfin.series_index()
    keeps, drops = [], []
    has_movies = c.radarr is not None and any(is_movie(r) and r["status"] in ("active", "moving") for r in st["shows"].values())
    movies = {m["id"]: m for m in c.radarr.movies()} if has_movies else {}
    movie_index = c.jellyfin.movie_index() if has_movies else {}
    mtag = c.radarr.tag_id(TAG) if has_movies else None
    for rec in st["shows"].values():
        if is_movie(rec):
            if has_movies:
                _decide_movie(cfg, c, rec, movies, movie_index, mtag, users, now, keeps, drops, lines)
            continue
        if rec["status"] == "moving":
            s = series.get(rec["sonarr_id"])
            if not s or s.get("tvdbId") != rec["tvdb"]:
                rec["status"] = "released"
                lines.append(f"{rec['title']}: removed from Sonarr during move")
                continue
            _finish_keep_steps(c, rec, tag)
            lines += finish_move(c, rec, index, users, now)
            continue
        if rec["status"] != "active":
            continue
        s = series.get(rec["sonarr_id"])
        if not s or tag not in s.get("tags", []) or s.get("tvdbId") != rec["tvdb"]:
            rec["status"] = "released"
            lines.append(f"{rec['title']}: no longer a trial in Sonarr (tag removed or series deleted) - left alone")
            continue
        if rec.get("override"):   # an admin (adriel / bobby) overruled the vote
            (keeps if rec["override"]["verdict"] == "keep" else drops).append((rec, s, f"overruled by {rec['override']['by']}"))
            continue
        eps = c.sonarr.episodes(s["id"])
        just_searched = False
        if not rec.get("setup_done"):
            just_searched = setup_trial(c, rec, eps, n)
            eps = c.sonarr.episodes(s["id"])
        if not is_request(rec) and (c.seerr.requested_since(rec["tmdb"], rec["added_at"]) or user_extended(rec, eps, n)):
            keeps.append((rec, s, "requested by a user"))
            continue
        if rec["window_start"] is None and is_request(rec):
            _request_arrival(cfg, c, rec, s, eps, index, users, now, lines)
            continue
        if rec["window_start"] is None:
            t = trial_eps(eps, n)
            all_have_files = len(t) == n and all(e["hasFile"] for e in t)
            if all_have_files and s["path"].rstrip("/") in index:
                rec["window_start"] = iso(now)
                lines.append(f"{rec['title']}: trial episodes arrived - voting open for {cfg.window_days} days")
                _mark_on_trial(cfg, c, rec, index[s["path"].rstrip("/")]["Id"], users, lines)
            elif all_have_files:
                lines.append(f"{rec['title']}: files present but Jellyfin hasn't indexed {s['path']} yet - waiting")
            elif now - parse(rec["added_at"]) > timedelta(days=cfg.arrival_days):
                drops.append((rec, s, "unavailable"))
            elif not just_searched:
                # the setup search is one-shot and aired episodes never come back via RSS,
                # so retry the missing ones daily until they arrive or arrival_days runs out
                c.sonarr.search_episodes([e["id"] for e in t if not e["hasFile"]])
            continue
        if now < parse(rec["window_start"]) + timedelta(days=cfg.window_days):
            if s["path"].rstrip("/") in index:
                _mark_on_trial(cfg, c, rec, index[s["path"].rstrip("/")]["Id"], users, lines)
            continue
        jf = index.get(s["path"].rstrip("/"))
        if not jf:
            lines.append(f"{rec['title']}: not found in Jellyfin - decision postponed")
            continue
        if is_request(rec):
            views, rec["played"], n_eff = request_views(c, jf["Id"], users, n)
            verdict, likes, dislikes = decide(views, n_eff)
            (keeps if verdict == "keep" else drops).append((rec, s, f"{likes} like / {dislikes} dislike"))
            continue
        views, snap = user_views(c, jf["Id"], users, n)
        if not any(len(v) == n for v in snap.values()):
            lines.append(f"{rec['title']}: Jellyfin doesn't show all {n} trial episodes yet - decision postponed")
            continue
        rec["played"] = snap
        verdict, likes, dislikes = decide(views, n)
        (keeps if verdict == "keep" else drops).append((rec, s, f"{likes} like / {dislikes} dislike"))

    if not cfg.enforce:
        if len(drops) > cfg.max_deletes_per_run:
            lines.append(f"[dry-run] would hit SAFETY STOP: {len(drops)} deletions due (> {cfg.max_deletes_per_run})")
        for label, group in (("KEEP", keeps), ("DELETE", drops)):
            for rec, _, why in group:
                rec["dry_run"] = f"would {label} ({why})"
                old_label = rec.get("dry_run_label")
                if old_label != label:
                    rec["dry_run_label"] = label
                    lines.append(f"[dry-run] would {label} {rec['title']} ({why})")
        return lines

    if len(drops) > cfg.max_deletes_per_run:
        lines.insert(0, f"SAFETY STOP: {len(drops)} deletions due (> {cfg.max_deletes_per_run}); nothing deleted - check the trials app")
        drops = []
    for rec, s, why in keeps:
        lines.append(apply_keep(cfg, c, rec, s, tag, movie_index if is_movie(rec) else index, users, now, why))
    for rec, s, why in drops:
        lines.append(apply_drop(c, st, rec, s, why, now))
    return lines
