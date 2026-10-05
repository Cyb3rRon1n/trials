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


def badge(image, url, caption=CAPTION):
    """image bytes -> JPEG bytes with a QR code for `url` on a white card in the bottom-right corner,
    sized at ~22% of the image height"""
    img = Image.open(io.BytesIO(image)).convert("RGB")
    w, h = img.size
    side = max(48, round(h * 0.22))
    pad, margin = max(4, side // 12), max(6, round(h * 0.03))
    font = _font(max(10, side // 8))
    draw = ImageDraw.Draw(img)
    tw = draw.textlength(caption, font=font)
    l, t, r, b = draw.textbbox((0, 0), caption, font=font)
    text_h = b - t
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=2)
    qr.add_data(url)
    code = qr.make_image(fill_color="black", back_color="white").get_image().convert("RGB")
    code = code.resize((side, side), Image.NEAREST)
    card_w, card_h = max(side, round(tw)) + 2 * pad, side + text_h + 3 * pad
    x0, y0 = w - margin - card_w, h - margin - card_h
    draw.rounded_rectangle((x0, y0, x0 + card_w, y0 + card_h), radius=pad * 2, fill="white")
    img.paste(code, (x0 + (card_w - side) // 2, y0 + pad))
    draw.text((x0 + (card_w - tw) / 2, y0 + 2 * pad + side - t), caption, fill="black", font=font)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=90)
    return out.getvalue()
