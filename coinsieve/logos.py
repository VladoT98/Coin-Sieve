"""Round token avatars: real logos (Jupiter `icon`) or letter badges. Used by the card and the dashboard.

Logo URLs are untrusted: https only, size-capped download, must decode as a raster image (SVG and
anything else -> letter badge), pixel-count capped, cached as a normalised PNG.
"""
import hashlib
import io
import logging
import re
from pathlib import Path

import requests
from matplotlib import font_manager
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

SIZE = 128
MAX_BYTES = 2_000_000
Image.MAX_IMAGE_PIXELS = 25_000_000  # refuse decompression bombs
IPFS_GATEWAYS = ["https://gateway.pinata.cloud", "https://4everland.io"]  # tried in order, then the original URL
IPFS_PATH = re.compile(r"^https://[^/]+(?P<rest>/ipfs/[A-Za-z0-9]{46,}(?:/[^?#]*)?)$")
IPFS_SUBDOMAIN = re.compile(r"^https://(?P<cid>[a-z0-9]{46,})\.ipfs\.[^/]+(?P<path>/[^?#]*)?$")


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


def _cache_path(url, cache_dir):
    return Path(cache_dir) / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.png"


def _candidates(url):
    """The URL itself, or for IPFS links the same CID on several gateways (ipfs.io / dweb.link / w3s.link
    share one rate limit and answered 429 on 2026-10-09; pinata and 4everland served the same file)."""
    if m := IPFS_PATH.match(url):
        rest = m.group("rest")
    elif m := IPFS_SUBDOMAIN.match(url):
        rest = f"/ipfs/{m.group('cid')}{m.group('path') or ''}"
    else:
        return [url]
    alts = [g + rest for g in IPFS_GATEWAYS]
    return list(dict.fromkeys(alts + [url]))


def _download(url, timeout):
    with requests.get(url, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        data = r.raw.read(MAX_BYTES + 1, decode_content=True)
    if len(data) > MAX_BYTES:
        raise ValueError("logo too large")
    img = Image.open(io.BytesIO(data))
    img.load()  # full decode (verifies it is a real raster image)
    return _circle(img)


def fetch_logo(url, cache_dir, timeout=10):
    """Circular logo from a URL, or None. Cached by URL hash (of the original URL)."""
    if not url or not url.startswith("https://"):
        return None
    cache = _cache_path(url, cache_dir)
    if cache.exists():
        return Image.open(cache).convert("RGBA")
    img = None
    for u in _candidates(url):
        try:
            img = _download(u, timeout)
            break
        except Exception as e:
            log.info("logo %s unusable: %s", u[:80], e)
    if img is None:
        return None
    cache.parent.mkdir(parents=True, exist_ok=True)
    img.save(cache)
    return img


def logo_png(url, cache_dir):
    """Bytes of the vetted, cached PNG for a logo URL, or None (dashboard serves only these)."""
    return _cache_path(url, cache_dir).read_bytes() if fetch_logo(url, cache_dir) is not None else None


def avatar(m, tier, color, cache_dir, use_logo):
    """Logo when allowed and usable, else a letter badge in the tier colour."""
    img = fetch_logo(m.get("icon"), cache_dir) if use_logo else None
    return img if img is not None else letter_badge(m.get("symbol"), color)
