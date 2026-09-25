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
