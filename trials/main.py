import argparse
import threading
from datetime import datetime, timedelta, timezone

from . import state as state_mod
from .clients import Jellyfin, Ntfy, Radarr, Seerr, Sonarr
from .config import Config
from .jobs import Clients, daily_decide, iso, parse, weekly_add, weekly_add_movies


def build_clients(cfg):
    return Clients(Sonarr(cfg.sonarr_url, cfg.sonarr_key), Jellyfin(cfg.jellyfin_url, cfg.jellyfin_key, roots=(cfg.trials_root, cfg.trials_movies_root)),
                   Seerr(cfg.seerr_url, cfg.seerr_key), Ntfy(cfg.ntfy_url, cfg.ntfy_topic),
                   Radarr(cfg.radarr_url, cfg.radarr_key) if cfg.radarr_url and cfg.radarr_key else None)


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
    lines = []
    # ponytail: one lock for the whole job (incl. API calls) - an admin /unreject waits for a running job (minutes at most); split into read/act/write phases if that ever matters
    with state_mod.locked(cfg.state_path) as st:
        try:
            if name == "add":
                weekly_add(cfg, c, st, now, lines)
                try:   # movies are extra: a Radarr/Seerr movie problem must not cost the week's shows a retry
                    weekly_add_movies(cfg, c, st, now, lines)
                except Exception as e:
                    lines.append(f"movie add failed: {type(e).__name__}: {str(e)[:120]}")
                st["last_add_week"] = now.strftime("%G-W%V")
            else:
                daily_decide(cfg, c, st, now, lines)
                st["last_decide_date"] = now.date().isoformat()
            st.pop(f"retry_after_{name}", None)
        except Exception as e:  # fail closed: stop this run, keep what really happened, retry in an hour
            st[f"retry_after_{name}"] = iso(now + timedelta(hours=1))
            lines.append(f"{name} run aborted, retrying in 1h: {type(e).__name__}: {str(e)[:120]}")
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
        try:
            now = datetime.now(timezone.utc)
            for name in due(now, state_mod.load(cfg.state_path)):
                run_job(name, cfg, c, now)
        except Exception as e:
            print(f"trials scheduler error: {type(e).__name__}: {e}", flush=True)
        stop.wait(600)


def probe(cfg, c):
    if not cfg.ntfy_topic:
        print("WARNING: NTFY_TOPIC is empty - SAFETY STOP alerts will not be sent")
    print("sonarr series:", len(c.sonarr.series()))
    print("sonarr quality profile id:", c.sonarr.quality_profile_id(cfg.quality_profile))
    print(f"free at {cfg.trials_root}: {c.sonarr.free_bytes(cfg.trials_root) / 1e12:.2f} TB")
    print("jellyfin users:", [u["Name"] for u in c.jellyfin.users()])
    print("jellyfin series indexed:", len(c.jellyfin.series_index()))
    if c.radarr:
        print("radarr movies:", len(c.radarr.movies()), "| trials movies root:", cfg.trials_movies_root)
        print("radarr quality profile id:", c.radarr.quality_profile_id(cfg.quality_profile))
        print(f"free at {cfg.trials_movies_root} (radarr): {c.radarr.free_bytes(cfg.trials_movies_root) / 1e12:.2f} TB")
    else:
        print("radarr: not configured - movie trials disabled")
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
    def override_now():   # an admin overruled a vote: apply it now, not at tomorrow's 11:00 run
        threading.Thread(target=run_job, args=("decide", cfg, c, datetime.now(timezone.utc)), daemon=True).start()
    make_server(cfg, c, on_override=override_now).serve_forever()
