from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .decide import UserView, decide
from .library import choose_destination, ep_key, restore_plan

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
