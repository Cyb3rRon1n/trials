import io
import pytest
from PIL import Image
from trials.art import badge, ext_of

URL = "https://trials.example/?t=movie:555"


def picture(w=1280, h=720, fmt="JPEG", color=(30, 60, 90)):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, fmt)
    return buf.getvalue()


def test_badge_keeps_size_and_only_touches_the_bottom_right():
    out = Image.open(io.BytesIO(badge(picture(), URL)))
    assert out.format == "JPEG" and out.size == (1280, 720)
    px = out.convert("RGB")
    assert px.getpixel((40, 40)) == pytest.approx((30, 60, 90), abs=4)          # top-left untouched
    corner = px.crop((1280 - 200, 720 - 200, 1280 - 20, 720 - 20))
    assert all(hi >= 240 for _, hi in corner.getextrema())                       # white card / QR there


def test_badge_qr_decodes_to_the_vote_url():
    zxingcpp = pytest.importorskip("zxingcpp")
    out = Image.open(io.BytesIO(badge(picture(), URL)))
    assert [r.text for r in zxingcpp.read_barcodes(out)] == [URL]


def test_badge_handles_png_with_alpha_and_tiny_images():
    buf = io.BytesIO()
    Image.new("RGBA", (300, 450), (200, 0, 0, 128)).save(buf, "PNG")
    out = Image.open(io.BytesIO(badge(buf.getvalue(), URL)))
    assert out.format == "JPEG" and out.size == (300, 450)


def test_ext_of():
    assert ext_of(picture()) == "jpg" and ext_of(picture(fmt="PNG")) == "png" and ext_of(picture(fmt="WEBP")) == "webp"


def test_card_clears_tv_overscan_six_percent_from_bottom_and_right():
    out = Image.open(io.BytesIO(badge(picture(), URL))).convert("RGB")
    w, h = out.size
    right = out.crop((w - int(w * 0.06) + 8, 0, w, h))        # +8: a JPEG block of ringing at the card's edge
    bottom = out.crop((0, h - int(h * 0.06) + 8, w, h))
    for strip in (right, bottom):
        assert all(hi <= 100 for _, hi in strip.getextrema())                     # still the dark background


def test_has_badge_recognises_our_card_even_re_encoded():
    from trials.art import has_badge
    badged = badge(picture(), URL)
    worse = io.BytesIO()
    Image.open(io.BytesIO(badged)).save(worse, "JPEG", quality=60)                # Jellyfin/a proxy re-encoding it
    assert has_badge(badged) and has_badge(worse.getvalue())
    assert not has_badge(picture()) and not has_badge(picture(color=(128, 128, 128)))
