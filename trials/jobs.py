import glob
import hashlib
import mimetypes
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .decide import UserView, decide, user_verdict
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


def trial_key(rec):
    return f"movie:{rec['tmdb']}" if is_movie(rec) else str(rec["tvdb"])


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


def half_season(count):
    """trial length: the first half of season 1, rounded up"""
    return max(1, -(-count // 2))


def trial_n(cfg, rec):
    """trials set up before half-season trials keep their fixed episode count"""
    return rec.get("trial_n", cfg.trial_episodes)


def recent(date, now, days):
    """`date` (a Seerr/TMDb date or timestamp) falls within the last `days` days and isn't in the future"""
    return bool(date) and now - timedelta(days=days) <= _day(date) <= now


def _day(date):
    """a Seerr/TMDb date ("2026-09-25") or timestamp, as an aware datetime"""
    d = parse(date)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def brand_new(details, now, days):
    """weekly picks are brand-new shows: one season that has started airing (specials aside; an
    announced season 2 is fine), premiered in the last `days` days, and at least one episode out"""
    seasons = [x for x in details.get("seasons") or [] if (x.get("seasonNumber") or 0) >= 1
               and x.get("airDate") and _day(x["airDate"]) <= now]
    last = details.get("lastEpisodeToAir") or {}
    return (len(seasons) == 1 and recent(details.get("firstAirDate"), now, days)
            and last.get("seasonNumber") in (0, 1) and (last.get("episodeNumber") or 0) >= 1)   # 0: a special aired last


def is_season(rec):
    """whole-season trial (weekly picks since whole-season trials); older trials have a fixed E01-0n"""
    return rec.get("trial_mode") == "season"


def aired(e, now):
    return bool(e.get("airDateUtc")) and parse(e["airDateUtc"]) <= now


def trial_eps(episodes, n):
    return [e for e in episodes if e["seasonNumber"] == 1 and 1 <= e["episodeNumber"] <= n]


def setup_trial(c, rec, episodes):
    n = half_season(sum(1 for e in episodes if e["seasonNumber"] == 1))
    trial = [e["id"] for e in trial_eps(episodes, n)]
    if not trial:
        rec["setup_done"] = False
        return False
    rec["trial_n"] = n
    sid = rec["sonarr_id"]
    c.sonarr.set_monitored([e["id"] for e in episodes if e["id"] not in trial], False)
    c.sonarr.set_monitored(trial, True)
    c.sonarr.search_episodes(trial)
    rec["known_episode_ids"] = sorted(e["id"] for e in episodes)
    rec["setup_done"] = True
    return True


def setup_season(c, rec, episodes, now):
    """the whole of season 1 is on trial: all of it monitored (so episodes airing during the trial
    download too), later seasons left off, and the aired ones searched now"""
    s1 = [e for e in episodes if e["seasonNumber"] == 1]
    if not s1:
        rec["setup_done"] = False
        return False
    sid, ids = rec["sonarr_id"], {e["id"] for e in s1}
    c.sonarr.set_monitored([e["id"] for e in episodes if e["id"] not in ids], False)
    c.sonarr.monitor_season(sid, 1)   # season-level, so S1 episodes listed later get monitored too
    c.sonarr.set_monitored(sorted(ids), True)
    c.sonarr.search_episodes([e["id"] for e in s1 if aired(e, now)])
    rec["known_episode_ids"] = sorted(e["id"] for e in episodes)
    rec["setup_done"] = True
    return True


def _setup(c, rec, episodes, now):
    return setup_season(c, rec, episodes, now) if is_season(rec) else setup_trial(c, rec, episodes)


def _notify_jellyfin(c, created=(), deleted=()):
    try:
        c.jellyfin.notify_paths(created=created, deleted=deleted)
        return ""
    except Exception as e:  # Jellyfin's own scan will catch up; never un-record a real change for this
        return f" (Jellyfin not notified: {e})"


def _skip_set(st, now, movies=False):
    """tvdb ids (or, for movies, tmdb ids) never to add again: rejected or already trialled"""
    skip = set(st.get("rejected_movies", []) if movies else st["rejected"])
    for key, rec in st["shows"].items():
        if is_movie(rec) != movies:
            continue
        dropped = rec.get("dropped_at")
        if rec.get("status") == "unavailable" and dropped and now - parse(dropped) > UNAVAILABLE_COOLDOWN:
            continue
        skip.add(int(key.split(":")[-1]))
    return skip


GENRE_ALIASES = {"sci-fi": "science fiction", "scifi": "science fiction"}


def genres_of(names):
    out = set()
    for n in names:
        n = n.lower().strip()
        n = GENRE_ALIASES.get(n, n)
        out |= {GENRE_ALIASES.get(p.strip(), p.strip()) for p in n.replace("&", ",").split(",") if p.strip()}
    return out


def taste_profile(c, users):
    """genre -> weight from what everyone watched, 👍/👎 and ♥'d (movies count half)"""
    prof = {}
    for u in users:
        for it in c.jellyfin.taste_items(u["Id"]):
            ud = it.get("UserData") or {}
            w = (3 if ud.get("IsFavorite") else 0) + {True: 2, False: -2}.get(ud.get("Likes"), 0) \
                + (1 if ud.get("Played") or (ud.get("PlayedPercentage") or 0) > 0 or (ud.get("PlaybackPositionTicks") or 0) > 0 else 0)
            if not w:
                continue
            w = w / 2 if it.get("Type") == "Movie" else w
            for g in genres_of(it.get("Genres") or []):
                prof[g] = prof.get(g, 0) + w
    return prof


def affinity(genre_names, prof):
    gs = genres_of(genre_names)
    return sum(prof.get(g, 0) for g in gs) / len(gs) if gs else 0.0


def _added_this_week(st, now, movies):
    """weekly picks already made this week (requests the admins sent in don't use up a slot)"""
    week = now.strftime("%G-W%V")
    return sum(1 for rec in st["shows"].values() if is_movie(rec) == movies and not is_request(rec)
               and rec.get("added_at") and parse(rec["added_at"]).strftime("%G-W%V") == week)


def _pool(items):
    """trending + popular, first appearance wins (so the order is the trending position)"""
    pool, seen = [], set()
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"]); pool.append(item)
    return pool


def _rank(ranked, pool_size):
    """(affinity, trend position, ...) tuples, best first: half taste match, half trending position -
    so it stays "what's new" and not just "what's big"; with no history yet it's plain trending order"""
    top = max((abs(r[0]) for r in ranked), default=0) or 1
    return sorted(ranked, key=lambda r: -(0.5 * r[0] / top + 0.5 * (1 - r[1] / max(pool_size, 1))))


def _low_space(cfg, free, what, lines):
    if free < cfg.min_free_tb * 1e12:
        lines.append(f"skipped weekly {what}: only {free / 1e12:.2f} TB free (< {cfg.min_free_tb} TB)")
        return True
    return False


def weekly_add(cfg, c, st, now, lines=None):
    lines = [] if lines is None else lines
    if _low_space(cfg, c.sonarr.free_bytes(cfg.trials_root), "add", lines):
        return lines
    skip = _skip_set(st, now) | {s["tvdbId"] for s in c.sonarr.series()}
    tag = c.sonarr.tag_id(TAG)
    profile = c.sonarr.quality_profile_id(cfg.quality_profile)
    target = max(0, cfg.trials_per_week - _added_this_week(st, now, movies=False))
    added = 0
    # candidates: trending + popular, ranked by how well they match what users watch, like and ♥
    pool = _pool(c.seerr.trending_tv() + c.seerr.popular_tv())
    prof = taste_profile(c, c.jellyfin.users()) if target else {}
    ranked = []
    for rank, item in enumerate(pool):
        det = c.seerr.tv(item["id"])
        tvdb = (det.get("externalIds") or {}).get("tvdbId")
        if not tvdb or tvdb in skip or not brand_new(det, now, cfg.new_days):
            continue
        ranked.append((affinity([g["name"] for g in det.get("genres") or []], prof), rank, item, det, tvdb))
    for _, _, item, det, tvdb in _rank(ranked, len(pool)):
        if added >= target:
            break
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
            "played": None, "setup_done": False, "dry_run": None, "trial_mode": "season"}
        skip.add(tvdb)
        setup_season(c, rec, c.sonarr.episodes(s["id"]), now)
        lines.append(f"trial added: {s['title']} (season 1) -> Trials library")
        added += 1
    return lines


def movie_release(details):
    """when a movie became downloadable: its digital release (TMDb type 4) - physical (5) if it has
    no digital one - earliest in any country. None if TMDb lists neither."""
    dates = {4: [], 5: []}
    for country in (details.get("releases") or {}).get("results") or []:
        for d in country.get("release_dates") or []:
            if d.get("type") in dates and d.get("release_date"):
                dates[d["type"]].append(d["release_date"])
    found = dates[4] or dates[5]
    return min(found, key=parse) if found else None


def weekly_add_movies(cfg, c, st, now, lines=None):
    """weekly new-movie trials: released digitally in the last new_days, picked like the shows"""
    lines = [] if lines is None else lines
    if c.radarr is None:
        return lines
    if _low_space(cfg, c.radarr.free_bytes(cfg.trials_movies_root), "movie add", lines):
        return lines
    target = max(0, cfg.movies_per_week - _added_this_week(st, now, movies=True))
    if not target:
        return lines
    skip = _skip_set(st, now, movies=True) | {m["tmdbId"] for m in c.radarr.movies()}
    pool = _pool(c.seerr.trending_movies() + c.seerr.popular_movies())
    prof = taste_profile(c, c.jellyfin.users())
    ranked = []
    for rank, item in enumerate(pool):
        if item["id"] in skip:
            continue
        try:
            det, lookup = c.seerr.movie(item["id"]), None
            released = movie_release(det)
            if not released:   # TMDb has no digital/physical date: Radarr may (it reads other sources too)
                lookup = c.radarr.lookup_tmdb(item["id"]) or {}
                released = lookup.get("digitalRelease") or lookup.get("physicalRelease")
        except Exception:   # one bad candidate (Seerr/Radarr hiccup) just isn't picked this week
            continue
        if recent(released, now, cfg.new_days):
            ranked.append((affinity([g["name"] for g in det.get("genres") or []], prof), rank, item, lookup))
    added, tag, profile = 0, None, None
    for _, _, item, lookup in _rank(ranked, len(pool)):
        if added >= target:
            break
        try:
            lookup = lookup or c.radarr.lookup_tmdb(item["id"])
        except Exception:
            continue
        if not lookup:
            continue
        added += 1
        if not cfg.enforce:   # dry run: nothing added to Radarr or state
            lines.append(f"[dry-run] would add movie trial: {lookup['title']} ({lookup.get('year')})")
            continue
        if tag is None:
            tag, profile = c.radarr.tag_id(TAG), c.radarr.quality_profile_id(cfg.quality_profile)
        m = c.radarr.add_movie(lookup, profile, cfg.trials_movies_root, tag)
        title = f"{m['title']} ({m.get('year')})"
        st["shows"][f"movie:{m['tmdbId']}"] = {
            "tmdb": m["tmdbId"], "title": title, "radarr_id": m["id"], "path": m["path"], "added_at": iso(now),
            "window_start": None, "status": "active", "dest": cfg.movies_root, "played": None, "dry_run": None,
            "kind": "auto", "media": "movie"}
        lines.append(f"movie trial added: {title} -> Trials library")
    return lines


def _art_dir(cfg):
    return os.path.join(os.path.dirname(cfg.state_path) or ".", "art")


def _copies(folder, item, kind):
    """saved originals of this item's artwork (not a half-written .tmp)"""
    return [p for p in glob.glob(os.path.join(folder, f"{item}-{kind}.*")) if not p.endswith(".tmp")]


def _save_orig(path, data):
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)


def _qr_badge(cfg, c, rec, jf_id, lines):
    """Best-effort: a "scan to vote" QR on the artwork TV apps show full-screen (the backdrop, else
    the poster) - Roku / Android TV have no 👍/👎. The original is kept on disk until the trial ends.
    Once badged, one fetch per run checks it's still ours: a metadata refresh can replace it."""
    qr = rec.get("qr")
    if not cfg.qr_art or not cfg.trials_public_url or not rec.get("window_start") or (qr and not qr.get("type")):
        return
    try:
        from . import art   # Pillow is only loaded when this is used
        if qr:
            kind, current = qr["type"], c.jellyfin.get_image(jf_id, qr["type"])
            if not current:   # removed by hand, or a half-failed upload: put the badge back
                path = _orig_file(qr)
                if not path:
                    lines.append(f"{rec['title']}: artwork gone from Jellyfin and original artwork file missing: {qr['orig']}")
                    return
                with open(path, "rb") as f:
                    orig = f.read()
                lines.append(f"{rec['title']}: its artwork was gone from Jellyfin - vote QR re-added")
            elif hashlib.sha256(current).hexdigest() == qr.get("sha"):
                return
            elif art.has_badge(current):   # still ours, just re-encoded somewhere: not new artwork
                qr["sha"] = hashlib.sha256(current).hexdigest()
                return
            else:   # Jellyfin replaced it (metadata refresh): that's the original now
                orig, path = current, os.path.join(_art_dir(cfg), f"{jf_id}-{kind}.{art.ext_of(current)}")
                _save_orig(path, orig)
                qr["orig"] = path   # before badging/uploading, which can fail; an older copy is just left behind
        else:
            os.makedirs(_art_dir(cfg), exist_ok=True)
            for kind in ("Backdrop", "Primary"):
                # an original saved by an earlier, interrupted run wins: Jellyfin may already show the badge
                saved = _copies(_art_dir(cfg), jf_id, kind)
                if saved:
                    path = saved[0]
                    with open(path, "rb") as f:
                        orig = f.read()
                    break
                orig = c.jellyfin.get_image(jf_id, kind)
                if orig:
                    path = os.path.join(_art_dir(cfg), f"{jf_id}-{kind}.{art.ext_of(orig)}")
                    _save_orig(path, orig)
                    break
            else:
                rec["qr"] = {"item": jf_id, "type": None, "orig": None}
                lines.append(f"{rec['title']}: no artwork in Jellyfin to put the vote QR on")
                return
        data = art.badge(orig, f"{cfg.trials_public_url.rstrip('/')}/?t={trial_key(rec)}")
        c.jellyfin.set_image(jf_id, kind, data)
        rec["qr"] = {"item": jf_id, "type": kind, "orig": path, "sha": hashlib.sha256(data).hexdigest()}
    except Exception as e:
        lines.append(f"{rec['title']}: couldn't add the vote QR to its artwork: {e}")


def _orig_file(qr):
    """the saved original: the recorded file, else the newest <item>-<type>.* copy next to it"""
    if os.path.exists(qr["orig"]):
        return qr["orig"]
    copies = _copies(os.path.dirname(qr["orig"]), qr["item"], qr["type"])
    return max(copies, key=os.path.getmtime) if copies else None


def _qr_restore(c, rec, item_id):
    """Put the original artwork back on `item_id` (None: the item is gone) and delete the saved copies.
    Returns a problem to report, or "" - a failure keeps the files so nothing is lost."""
    qr = rec.get("qr")
    if not qr:
        return ""
    if qr.get("orig"):
        path = _orig_file(qr)
        if not path:
            return f" (original artwork file missing: {qr['orig']})"
        try:
            if item_id:
                with open(path, "rb") as f:
                    c.jellyfin.set_image(item_id, qr["type"], f.read(), mimetypes.guess_type(path)[0] or "image/jpeg")
            for old in _copies(os.path.dirname(path), qr["item"], qr["type"]):
                os.remove(old)
        except Exception as e:
            return f" (original artwork not put back, it's in {path}: {e})"
    rec["qr"] = None
    return ""


def _tally(cfg, c, rec, jf_id, users, now):
    """Best-effort running count while the vote is open, worked out exactly like the final verdict.
    Counts only - no names - for the announcer, which reads rec["tally"] from state.json."""
    try:
        if is_movie(rec):
            (views, _), n = movie_views(c, jf_id, users), 1
        elif is_request(rec) or is_season(rec):
            views, _, n = request_views(c, jf_id, users, cfg.trial_episodes, is_season(rec))
        else:
            n = trial_n(cfg, rec)
            views, _ = user_views(c, jf_id, users, n)
        _, keep, drop = decide(views, n)
        rec["tally"] = {"keep": keep, "drop": drop, "voters": sum(user_verdict(v, n) is not None for v in views),
                        "at": iso(now)}
    except Exception:   # Jellyfin hiccup: keep yesterday's numbers, never abort a run
        pass


def _mark_on_trial(cfg, c, rec, jf_id, users, lines):
    """Best-effort: tell Jellyfin viewers the show is on trial and where to vote. Never aborts a run."""
    _qr_badge(cfg, c, rec, jf_id, lines)
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
    """someone monitored more than the trial in Sonarr (for a whole-season trial: a later season)"""
    season1 = [e for e in episodes if e["seasonNumber"] == 1]
    trial = {e["id"] for e in (season1 if is_season(rec) else trial_eps(episodes, n))}
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


def like_threshold(n, available):
    """no vote: finishing this many episodes counts as a like (n, or all there is if fewer)"""
    return max(1, min(n, available))


def request_views(c, jf_id, users, n, season1=False):
    """whole request / whole season 1: any episode counts; no vote + finished like_threshold() episodes = keep"""
    views, snapshot, total = [], {}, 0
    fetch = c.jellyfin.season1_episodes if season1 else c.jellyfin.all_episodes
    for u in users:
        eps = [e for e in fetch(jf_id, u["Id"]) if e.get("IndexNumber") is not None]
        total = max(total, len(eps))
        data = [(e, e.get("UserData") or {}) for e in eps]
        watched = sum(1 for _, d in data if d.get("Played") or (d.get("PlaybackPositionTicks") or 0) > 0)
        finished = sum(1 for _, d in data if d.get("Played"))
        views.append(UserView(c.jellyfin.likes(jf_id, u["Id"]), watched, finished, c.jellyfin.favorite(jf_id, u["Id"])))
        snapshot[u["Id"]] = {ep_key(e.get("ParentIndexNumber") or 0, e["IndexNumber"]): _hist(d) for e, d in data}
    return views, snapshot, like_threshold(n, total)


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
    art_note = _qr_restore(c, rec, jf["Id"])
    rec.update(status="kept", played=None, taste=None)
    if missing:
        return [f"{rec['title']}: kept, watched marks restored except {missing} (not found in Jellyfin after 7 days){art_note}"]
    return [f"{rec['title']}: now in its permanent library, watched marks restored{art_note}"]


def apply_keep(cfg, c, rec, s, tag, index, users, now, why):
    jf = index.get(s["path"].rstrip("/"))
    if jf:
        if is_movie(rec):
            views, rec["played"] = movie_views(c, jf["Id"], users)
        elif is_request(rec) or is_season(rec):
            views, rec["played"], _ = request_views(c, jf["Id"], users, cfg.trial_episodes, is_season(rec))
        else:
            views, rec["played"] = user_views(c, jf["Id"], users, trial_n(cfg, rec))
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
    return _apply_drop(c, st, rec, s, why, now) + _qr_restore(c, rec, None)   # the item is gone


def _apply_drop(c, st, rec, s, why, now):
    unavailable = why == "unavailable"
    if is_movie(rec):
        c.radarr.delete_movie(s["id"], exclude=not unavailable)
        if unavailable:   # not judged, just never came: may be picked again after the cooldown
            rec.update(status="unavailable", dropped_at=iso(now))
            msg = f"DROPPED {rec['title']}: never arrived - deleted"
        else:
            rec["status"] = "rejected"
            st.setdefault("rejected_movies", []).append(rec["tmdb"])
            msg = f"REJECTED {rec['title']} ({why}) - deleted"
        return msg + _notify_jellyfin(c, deleted=[s["path"]])
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


def _arrival(cfg, c, rec, s, eps, index, users, now, lines, just_searched=False):
    """a whole request / whole season 1 opens for voting once every aired episode is in - or after
    arrival_days with whatever arrived. Returns True when that's nothing at all: a weekly pick is then
    dropped; a request never is for being slow (it was asked for)."""
    if is_season(rec):
        out = [e for e in eps if e["seasonNumber"] == 1 and aired(e, now)]
    else:
        out = [e for e in eps if e.get("monitored") and e["seasonNumber"] > 0 and aired(e, now)]
    have = [e for e in out if e["hasFile"]]
    overdue = now - parse(rec["added_at"]) > timedelta(days=cfg.arrival_days)
    jf = index.get(s["path"].rstrip("/"))
    if out and len(have) == len(out) or overdue and have:
        if not jf:
            lines.append(f"{rec['title']}: files present but Jellyfin hasn't indexed {s['path']} yet - waiting")
        elif len(have) == len(out):
            _open_window(cfg, c, rec, jf["Id"], users, now, lines, "trial episodes arrived" if is_season(rec) else "request arrived")
        else:
            _open_window(cfg, c, rec, jf["Id"], users, now, lines, f"{len(have)}/{len(out)} episodes arrived")
        return False
    if overdue and is_season(rec):
        return True
    if have != out and not just_searched:
        # aired episodes never come back via RSS, so retry the missing ones daily
        c.sonarr.search_episodes([e["id"] for e in out if not e["hasFile"]])
    return False


def _decide_movie(cfg, c, rec, movies, movie_index, mtag, users, now, keeps, drops, lines):
    m = movies.get(rec["radarr_id"])
    if rec["status"] == "moving":
        if not m or m.get("tmdbId") != rec["tmdb"]:
            rec["status"] = "released"
            lines.append(f"{rec['title']}: removed from Radarr during move" + _qr_restore(c, rec, None))
            return
        _finish_keep_steps(c, rec, TAG)
        lines.extend(finish_move(c, rec, {}, users, now))
        return
    if rec["status"] != "active":
        return
    if not m or mtag not in m.get("tags", []) or m.get("tmdbId") != rec["tmdb"]:
        rec["status"] = "released"
        lines.append(f"{rec['title']}: no longer a trial in Radarr (tag removed or movie deleted) - left alone"
                     + _qr_restore(c, rec, (rec.get("qr") or {}).get("item") if m and m.get("tmdbId") == rec["tmdb"] else None))
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
        elif rec.get("kind") == "auto" and now - parse(rec["added_at"]) > timedelta(days=cfg.arrival_days):
            drops.append((rec, m, "unavailable"))   # a weekly pick that never came; a request just waits
        return
    if now < parse(rec["window_start"]) + timedelta(days=cfg.window_days):
        if jf:
            _mark_on_trial(cfg, c, rec, jf["Id"], users, lines)
            _tally(cfg, c, rec, jf["Id"], users, now)
        return
    if not jf:
        lines.append(f"{rec['title']}: not found in Jellyfin - decision postponed")
        return
    views, rec["played"] = movie_views(c, jf["Id"], users)
    verdict, likes, dislikes = decide(views, 1)
    (keeps if verdict == "keep" else drops).append((rec, m, f"{likes} like / {dislikes} dislike"))


def daily_decide(cfg, c, st, now, lines=None):
    lines = [] if lines is None else lines
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
                lines.append(f"{rec['title']}: removed from Sonarr during move" + _qr_restore(c, rec, None))
                continue
            _finish_keep_steps(c, rec, tag)
            lines += finish_move(c, rec, index, users, now)
            continue
        if rec["status"] != "active":
            continue
        s = series.get(rec["sonarr_id"])
        if not s or tag not in s.get("tags", []) or s.get("tvdbId") != rec["tvdb"]:
            rec["status"] = "released"
            lines.append(f"{rec['title']}: no longer a trial in Sonarr (tag removed or series deleted) - left alone"
                         + _qr_restore(c, rec, (rec.get("qr") or {}).get("item") if s and s.get("tvdbId") == rec["tvdb"] else None))
            continue
        if rec.get("override"):   # an admin (adriel / bobby) overruled the vote
            (keeps if rec["override"]["verdict"] == "keep" else drops).append((rec, s, f"overruled by {rec['override']['by']}"))
            continue
        eps = c.sonarr.episodes(s["id"])
        just_searched = False
        if not rec.get("setup_done"):
            just_searched = _setup(c, rec, eps, now)
            eps = c.sonarr.episodes(s["id"])
        n = cfg.trial_episodes if is_request(rec) else trial_n(cfg, rec)
        if not is_request(rec) and (c.seerr.requested_since(rec["tmdb"], rec["added_at"]) or user_extended(rec, eps, n)):
            keeps.append((rec, s, "requested by a user"))
            continue
        if rec["window_start"] is None and (is_request(rec) or is_season(rec)):
            if _arrival(cfg, c, rec, s, eps, index, users, now, lines, just_searched):
                drops.append((rec, s, "unavailable"))
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
                _tally(cfg, c, rec, index[s["path"].rstrip("/")]["Id"], users, now)
            continue
        jf = index.get(s["path"].rstrip("/"))
        if not jf:
            lines.append(f"{rec['title']}: not found in Jellyfin - decision postponed")
            continue
        if is_request(rec) or is_season(rec):
            views, snap, n_eff = request_views(c, jf["Id"], users, n, is_season(rec))
            if is_season(rec) and not any(snap.values()):
                lines.append(f"{rec['title']}: Jellyfin doesn't show any season 1 episodes yet - decision postponed")
                continue
            rec["played"] = snap
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
