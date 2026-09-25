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
