"""QR "scan to vote" badge for a trial's artwork - TV apps (Roku, Android TV) have no 👍/👎 buttons,
but they show the backdrop full-screen, and everyone has a phone."""
import io

import qrcode
from PIL import Image, ImageDraw, ImageFont

CAPTION = "Scan to vote"   # the bundled font has no emoji, and slim images ship no emoji font
EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif", "BMP": "bmp"}


def ext_of(data):
    return EXT.get(Image.open(io.BytesIO(data)).format, "img")


def _font(size):
    try:
        return ImageFont.load_default(size=size)   # bundled scalable font (needs FreeType)
    except Exception:
        return ImageFont.load_default()            # tiny bitmap font, still readable


def _layout(w, h, caption):
    """where the card goes: (font, qr side, padding, card box, caption width, caption top offset)"""
    side = max(48, round(h * 0.22))
    pad = max(4, side // 12)
    right, bottom = max(6, round(w * 0.06)), max(6, round(h * 0.06))   # clear of TV overscan
    font = _font(max(10, side // 8))
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    tw = draw.textlength(caption, font=font)
    _, t, _, b = draw.textbbox((0, 0), caption, font=font)
    card_w, card_h = max(side, round(tw)) + 2 * pad, side + (b - t) + 3 * pad
    x0, y0 = w - right - card_w, h - bottom - card_h
    return font, side, pad, (x0, y0, x0 + card_w, y0 + card_h), tw, t


def badge(image, url, caption=CAPTION):
    """image bytes -> JPEG bytes with a QR code for `url` on a white card in the bottom-right corner,
    sized at ~22% of the image height"""
    img = Image.open(io.BytesIO(image)).convert("RGB")
    font, side, pad, (x0, y0, x1, y1), tw, t = _layout(*img.size, caption)
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
    qr.add_data(url)
    code = qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
    code = code.resize((side, side), Image.NEAREST)
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=pad * 2, fill="white")
    img.paste(code, (x0 + (x1 - x0 - side) // 2, y0 + pad))
    draw.text((x0 + (x1 - x0 - tw) / 2, y0 + 2 * pad + side - t), caption, fill="black", font=font)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()


def has_badge(image, caption=CAPTION):
    """our card is on this image: the padding above the QR is still white (survives re-encoding,
    unlike a byte-for-byte hash)"""
    img = Image.open(io.BytesIO(image)).convert("L")
    _, _, pad, (x0, y0, x1, _), _, _ = _layout(*img.size, caption)
    lo, _ = img.crop((x0 + 2 * pad, y0 + pad // 4, x1 - 2 * pad, y0 + max(1, pad * 3 // 4))).getextrema()
    return lo >= 200
