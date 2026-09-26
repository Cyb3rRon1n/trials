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
    if not rec.get("completed"):
        c.sonarr.monitor_all_and_search(rec["sonarr_id"])
        rec["completed"] = True
    if not rec.get("untagged"):
        c.sonarr.remove_tag(rec["sonarr_id"], tag)
        rec["untagged"] = True


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
    snap = rec.get("played") or {}
    needed = {k for eps in snap.values() for k in eps}
    age = now - parse(rec["moved_at"])
    jf = index.get(rec["new_path"].rstrip("/"))
    if not jf:
        if age < GIVE_UP:
            return []
        rec.update(status="kept", played=None)
        return [f"{rec['title']}: kept, but Jellyfin never showed its new location after 7 days - watched marks NOT restored"]
    if users:
        new_items = {ep_key(1, e["IndexNumber"]): e["Id"]
                     for e in c.jellyfin.season1_episodes(jf["Id"], users[0]["Id"]) if e.get("IndexNumber")}
        if needed - set(new_items) and age < GIVE_UP:
            return []
        for user_id, item_id, played, ticks in restore_plan(snap, new_items):
            if played:
                c.jellyfin.mark_played(item_id, user_id)
            else:
                c.jellyfin.set_position(item_id, user_id, ticks)
        missing = sorted(needed - set(new_items))
    else:
        new_items = {}
        missing = sorted(needed)
    rec.update(status="kept", played=None)
    if missing:
        return [f"{rec['title']}: kept, watched marks restored except {missing} (not found in Jellyfin after 7 days)"]
    return [f"{rec['title']}: now in its permanent library, watched marks restored"]


def apply_keep(cfg, c, rec, s, tag, index, users, now, why):
    jf = index.get(s["path"].rstrip("/"))
    if jf:
        _, rec["played"] = user_views(c, jf["Id"], users, cfg.trial_episodes)
        if rec.get("noted") and users:
            try:  # cosmetic; the move normally gives Jellyfin a fresh item anyway
                c.jellyfin.set_trial_note(jf["Id"], users[0]["Id"], None)
                rec["noted"] = False
            except Exception:
                pass
    old = s["path"]
    new = c.sonarr.move_series(s["id"], rec["dest"])
    rec.update(status="moving", new_path=new, moved_at=iso(now), completed=False, untagged=False)
    _finish_keep_steps(c, rec, tag)
    msg = f"KEPT {rec['title']} ({why}) -> {new}; downloading the rest"
    msg += _notify_jellyfin(c, created=[new], deleted=[old])
    return msg


def apply_drop(c, st, rec, s, why, now):
    unavailable = why == "unavailable"
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


def daily_decide(cfg, c, st, now, lines=None):
    lines = [] if lines is None else lines
    n = cfg.trial_episodes
    series = {s["id"]: s for s in c.sonarr.series()}
    tag = c.sonarr.tag_id(TAG)
    users = c.jellyfin.users()
    index = c.jellyfin.series_index()
    keeps, drops = [], []
    for rec in st["shows"].values():
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
        eps = c.sonarr.episodes(s["id"])
        just_searched = False
        if not rec.get("setup_done"):
            just_searched = setup_trial(c, rec, eps, n)
            eps = c.sonarr.episodes(s["id"])
        if c.seerr.requested_since(rec["tmdb"], rec["added_at"]) or user_extended(rec, eps, n):
            keeps.append((rec, s, "requested by a user"))
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
        lines.append(apply_keep(cfg, c, rec, s, tag, index, users, now, why))
    for rec, s, why in drops:
        lines.append(apply_drop(c, st, rec, s, why, now))
    return lines
