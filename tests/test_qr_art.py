"""a 'scan to vote' QR on trial artwork, for TV apps that have no 👍/👎 buttons"""
import hashlib
import io
import os
from datetime import timedelta
from PIL import Image
from trials import state
from trials.jobs import Clients, daily_decide, iso, weekly_add, weekly_add_movies
from fakes import FakeJellyfin, FakeNtfy, FakeRadarr, FakeSeerr, FakeSonarr, NOW, ep, make_cfg

PUB = "https://trials.example"


def jpeg(color=(10, 20, 30), size=(640, 360)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def world(tmp_path, **kw):
    cfg = make_cfg(tmp_path, **{"trials_public_url": PUB, **kw})
    c = Clients(FakeSonarr(), FakeJellyfin(), FakeSeerr(), FakeNtfy(), FakeRadarr())
    st = state.empty()
    c.seerr.add_show(5, 1005, "New Show")
    c.sonarr.lookups[1005] = {"title": "New Show", "tvdbId": 1005}
    weekly_add(cfg, c, st, NOW)
    rec = st["shows"]["1005"]
    for e in c.sonarr.eps[rec["sonarr_id"]][:4]:
        e["hasFile"] = True
    c.jellyfin.index[rec["path"]] = {"Id": "jf1"}
    c.jellyfin.images = {("jf1", "Backdrop"): jpeg(), ("jf1", "Primary"): jpeg((200, 0, 0), (300, 450))}
    return cfg, c, st, rec


def art_dir(cfg):
    return os.path.join(os.path.dirname(cfg.state_path), "art")


def test_backdrop_badged_when_voting_opens_original_saved(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    orig = c.jellyfin.images[("jf1", "Backdrop")]
    daily_decide(cfg, c, st, NOW)
    saved = os.path.join(art_dir(cfg), "jf1-Backdrop.jpg")
    assert rec["qr"] == {"item": "jf1", "type": "Backdrop", "orig": saved,
                         "sha": hashlib.sha256(c.jellyfin.images[("jf1", "Backdrop")]).hexdigest()}
    assert open(saved, "rb").read() == orig
    badged = c.jellyfin.images[("jf1", "Backdrop")]
    assert badged != orig and Image.open(io.BytesIO(badged)).size == (640, 360)
    assert c.jellyfin.images[("jf1", "Primary")] == jpeg((200, 0, 0), (300, 450))     # poster untouched
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert len([x for x in c.jellyfin.calls if x[0] == "image"]) == 1                  # once


def test_qr_links_to_that_title_on_the_vote_page(tmp_path, monkeypatch):
    import trials.art
    seen = []
    monkeypatch.setattr(trials.art, "badge", lambda image, url: seen.append(url) or image)
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)
    assert seen == [f"{PUB}/?t=1005"]


def test_no_backdrop_uses_the_poster(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    del c.jellyfin.images[("jf1", "Backdrop")]
    daily_decide(cfg, c, st, NOW)
    assert rec["qr"]["type"] == "Primary" and rec["qr"]["orig"].endswith("jf1-Primary.jpg")


def test_saved_original_is_reused_not_redownloaded(tmp_path):
    # a run that uploaded the badge but died before saving state must not save the badge as "original"
    cfg, c, st, rec = world(tmp_path)
    os.makedirs(art_dir(cfg))
    with open(os.path.join(art_dir(cfg), "jf1-Backdrop.jpg"), "wb") as f:
        f.write(jpeg((1, 2, 3)))
    c.jellyfin.images[("jf1", "Backdrop")] = b"already badged"
    daily_decide(cfg, c, st, NOW)
    assert open(rec["qr"]["orig"], "rb").read() == jpeg((1, 2, 3))


def test_failure_is_reported_and_never_aborts(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    c.jellyfin.image_fails = True
    lines = daily_decide(cfg, c, st, NOW)
    assert rec["window_start"] == iso(NOW) and "qr" not in rec
    assert any("vote QR" in l for l in lines)
    c.jellyfin.image_fails = False
    daily_decide(cfg, c, st, NOW + timedelta(days=1))                                  # retried next run
    assert rec["qr"]["type"] == "Backdrop"


def test_off_switch_and_no_public_url(tmp_path):
    for kw in ({"qr_art": False}, {"trials_public_url": ""}):
        cfg, c, st, rec = world(tmp_path / str(len(kw)), **kw)
        daily_decide(cfg, c, st, NOW)
        assert "qr" not in rec and not [x for x in c.jellyfin.calls if x[0] == "image"]


def test_backfills_windows_already_open(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    rec["window_start"] = iso(NOW - timedelta(days=3))
    daily_decide(cfg, c, st, NOW)
    assert rec["qr"]["type"] == "Backdrop"


def test_keep_restores_the_original_on_the_moved_item(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    orig = c.jellyfin.images[("jf1", "Backdrop")]
    daily_decide(cfg, c, st, NOW)
    saved = rec["qr"]["orig"]
    c.jellyfin.like[("jf1", "u1")] = True
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1)]
    daily_decide(cfg, c, st, NOW + timedelta(days=22))
    assert rec["status"] == "moving" and os.path.exists(saved)
    c.jellyfin.index = {"/data/media/tv/New Show": {"Id": "jf2"}}
    c.jellyfin.eps[("jf2", "u1")] = [ep("n1", 1)]
    daily_decide(cfg, c, st, NOW + timedelta(days=23))
    assert rec["status"] == "kept" and c.jellyfin.images[("jf2", "Backdrop")] == orig
    assert not os.path.exists(saved) and rec["qr"] is None


def test_drop_deletes_the_saved_original(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)
    saved = rec["qr"]["orig"]
    c.jellyfin.like[("jf1", "u1")] = False
    c.jellyfin.eps[("jf1", "u1")] = [ep("e1", 1)]
    daily_decide(cfg, c, st, NOW + timedelta(days=22))
    assert rec["status"] == "rejected" and not os.path.exists(saved) and rec["qr"] is None


def test_released_early_puts_the_original_back(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    orig = c.jellyfin.images[("jf1", "Backdrop")]
    daily_decide(cfg, c, st, NOW)
    c.sonarr.series_db[rec["sonarr_id"]]["tags"] = []                                # handed back to the admins
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert rec["status"] == "released" and c.jellyfin.images[("jf1", "Backdrop")] == orig and rec["qr"] is None


def test_movies_get_the_badge_too(tmp_path):
    cfg, c, st, _ = world(tmp_path)
    c.seerr.add_film(1, "Fresh")
    weekly_add_movies(cfg, c, st, NOW)
    mrec = st["shows"]["movie:1"]
    c.radarr.movies_db[mrec["radarr_id"]]["hasFile"] = True
    c.jellyfin.movies[mrec["path"]] = {"Id": "m1"}
    c.jellyfin.images[("m1", "Backdrop")] = jpeg()
    daily_decide(cfg, c, st, NOW)
    assert mrec["qr"]["item"] == "m1" and mrec["qr"]["type"] == "Backdrop"
    c.radarr.movies_db[mrec["radarr_id"]]["tags"] = []
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert mrec["status"] == "released" and c.jellyfin.images[("m1", "Backdrop")] == jpeg() and mrec["qr"] is None


def sha(data):
    return hashlib.sha256(data).hexdigest()


def test_unchanged_badge_costs_one_fetch_and_no_upload(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)
    assert rec["qr"]["sha"] == sha(c.jellyfin.images[("jf1", "Backdrop")])
    c.jellyfin.calls.clear()
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert [x for x in c.jellyfin.calls if x[0] in ("get_image", "image")] == [("get_image", "jf1", "Backdrop")]


def test_artwork_replaced_by_a_metadata_refresh_is_re_badged(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)
    fresh = jpeg((90, 10, 10))
    c.jellyfin.images[("jf1", "Backdrop")] = fresh                                    # Jellyfin refreshed the art
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    now_shown = c.jellyfin.images[("jf1", "Backdrop")]
    assert open(rec["qr"]["orig"], "rb").read() == fresh                                # new original kept
    assert now_shown != fresh and rec["qr"]["sha"] == sha(now_shown)                   # badged again


def test_our_badge_re_encoded_is_not_mistaken_for_new_art(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    orig = c.jellyfin.images[("jf1", "Backdrop")]
    daily_decide(cfg, c, st, NOW)
    worse = io.BytesIO()
    Image.open(io.BytesIO(c.jellyfin.images[("jf1", "Backdrop")])).save(worse, "JPEG", quality=60)
    c.jellyfin.images[("jf1", "Backdrop")] = worse.getvalue()
    c.jellyfin.calls.clear()
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert open(rec["qr"]["orig"], "rb").read() == orig and not [x for x in c.jellyfin.calls if x[0] == "image"]
    assert rec["qr"]["sha"] == sha(worse.getvalue())


def test_refresh_check_failure_is_tolerated(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)
    before = dict(rec["qr"])
    c.jellyfin.image_fails = True
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert rec["qr"] == before and rec["status"] == "active" and any("vote QR" in l for l in lines)


def png(color=(5, 120, 5)):
    buf = io.BytesIO()
    Image.new("RGB", (640, 360), color).save(buf, "PNG")
    return buf.getvalue()


def release(c, rec):
    c.sonarr.series_db[rec["sonarr_id"]]["tags"] = []


def test_upload_failure_mid_re_badge_keeps_the_newest_original(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)
    first = rec["qr"]["orig"]
    c.jellyfin.images[("jf1", "Backdrop")] = png()                                   # refreshed, now a PNG
    c.jellyfin.upload_fails = True
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert any("vote QR" in l for l in lines)
    assert rec["qr"]["orig"].endswith("jf1-Backdrop.png") and open(rec["qr"]["orig"], "rb").read() == png()
    assert os.path.exists(first)                                                      # old one never deleted here
    c.jellyfin.upload_fails = False
    c.jellyfin.images[("jf1", "Backdrop")] = b"something else entirely"
    release(c, rec)
    daily_decide(cfg, c, st, NOW + timedelta(days=2))
    assert c.jellyfin.images[("jf1", "Backdrop")] == png() and rec["qr"] is None
    assert os.listdir(art_dir(cfg)) == []                                             # stale copies cleaned up too


def test_restore_falls_back_to_any_saved_copy(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)
    os.rename(rec["qr"]["orig"], rec["qr"]["orig"][:-4] + ".webp")
    release(c, rec)
    daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert c.jellyfin.images[("jf1", "Backdrop")] == jpeg() and rec["qr"] is None


def test_restore_reports_a_missing_original(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)
    os.remove(rec["qr"]["orig"])
    release(c, rec)
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=1))
    assert any("original artwork file missing: " + os.path.join(art_dir(cfg), "jf1-Backdrop.jpg") in l for l in lines)


def test_badge_gone_from_jellyfin_is_put_back(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    daily_decide(cfg, c, st, NOW)
    del c.jellyfin.images[("jf1", "Backdrop")]                                       # removed by hand, or a half-failed upload
    lines = daily_decide(cfg, c, st, NOW + timedelta(days=1))
    now_shown = c.jellyfin.images[("jf1", "Backdrop")]
    assert rec["qr"]["sha"] == sha(now_shown) and any("re-added" in l for l in lines)
    assert open(rec["qr"]["orig"], "rb").read() == jpeg()


def test_a_half_written_copy_is_never_taken_for_the_original(tmp_path):
    cfg, c, st, rec = world(tmp_path)
    os.makedirs(art_dir(cfg))
    open(os.path.join(art_dir(cfg), "jf1-Backdrop.jpg.tmp"), "wb").write(b"half")
    daily_decide(cfg, c, st, NOW)
    assert open(rec["qr"]["orig"], "rb").read() == jpeg()
