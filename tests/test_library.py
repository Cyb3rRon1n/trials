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
