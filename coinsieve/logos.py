"""Round token avatars for the card: real logos (Emerging/Established, Jupiter `icon`) or letter badges.

Logo URLs are untrusted: https only, size-capped download, must decode as a raster image (SVG and
anything else -> letter badge), pixel-count capped, cached as a normalised PNG.
"""
import hashlib
import io
import logging
from pathlib import Path

import requests
from matplotlib import font_manager
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

SIZE = 128
MAX_BYTES = 2_000_000
Image.MAX_IMAGE_PIXELS = 25_000_000  # refuse decompression bombs


def _circle(img):
    img = img.convert("RGBA")
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2)).resize((SIZE, SIZE))
    # white base so logos with transparent backgrounds still read as round coins
    img = Image.alpha_composite(Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255)), img)
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, SIZE - 1, SIZE - 1), fill=255)
    out = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def letter_badge(symbol, color):
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((0, 0, SIZE - 1, SIZE - 1), fill=color)
    letter = next((c for c in (symbol or "?").upper() if c.isalnum()), "?")
    font = ImageFont.truetype(font_manager.findfont("DejaVu Sans:bold"), 64)
    d.text((SIZE / 2, SIZE / 2), letter, font=font, fill="#ffffff", anchor="mm")
    return img


def fetch_logo(url, cache_dir, timeout=10):
    """Circular logo from a URL, or None. Cached by URL hash."""
    if not url or not url.startswith("https://"):
        return None
    cache = Path(cache_dir) / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.png"
    if cache.exists():
        return Image.open(cache).convert("RGBA")
    try:
        with requests.get(url, timeout=timeout, stream=True) as r:
            r.raise_for_status()
            data = r.raw.read(MAX_BYTES + 1, decode_content=True)
        if len(data) > MAX_BYTES:
            raise ValueError("logo too large")
        img = Image.open(io.BytesIO(data))
        img.load()  # full decode (verifies it is a real raster image)
        img = _circle(img)
    except Exception as e:
        log.info("logo %s unusable: %s", url[:80], e)
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    img.save(cache)
    return img


def avatar(m, tier, color, cache_dir, use_logo):
    """Logo when allowed and usable, else a letter badge in the tier colour."""
    img = fetch_logo(m.get("icon"), cache_dir) if use_logo else None
    return img if img is not None else letter_badge(m.get("symbol"), color)
