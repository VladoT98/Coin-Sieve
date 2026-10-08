"""Card images (PNG) for Telegram: daily screen and weekly report. One dark visual style.

Text is drawn with parse_math=False ("$" must not trigger matplotlib math mode). Fonts lack emoji/CJK,
so names pass through printable() first. Emojis live in the Telegram caption, not the image.
"""
import re
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch  # noqa: E402

BG, PANEL, PANEL2, TEXT, MUTED, TRACK = "#0f1218", "#171b24", "#1d2230", "#e8ebf1", "#8b93a5", "#2a3040"
UP, DOWN = "#3ecf8e", "#f06a6a"
TIER_STYLE = {
    "new_launches": ("New Launches", "6-48h old · passed every filter", "#5b8def"),
    "emerging": ("Emerging", "7-180 days · $1M-50M market cap", "#b07cf0"),
    "established": ("Established", "1+ year · $200M+ market cap", "#e8b04b"),
}
W = 1080
_NOT_RENDERABLE = re.compile(r"[^ -ɏͰ-ϿЀ-ӿ‐-‧←-↓]")


def compact_usd(v):
    if v is None:
        return "n/a"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(v) >= div:
            return f"${v / div:,.1f}{suffix}"
    return f"${v:,.0f}"


def printable(text, fallback="?"):
    s = " ".join(_NOT_RENDERABLE.sub("", text or "").split())
    return s or fallback


def series_change(series):
    """% change first -> last close, or None."""
    if not series or not series[0][1]:
        return None
    return (series[-1][1] - series[0][1]) / series[0][1] * 100


class Canvas:
    """Pixel-coordinate drawing on a matplotlib figure (y measured from the top)."""

    def __init__(self, height):
        self.h = height
        self.fig = plt.figure(figsize=(W / 100, height / 100), dpi=100)
        self.fig.patch.set_facecolor(BG)

    def fx(self, px):
        return px / W

    def fy(self, px):
        return 1 - px / self.h

    def text(self, x, y, s, **kw):
        kw.setdefault("va", "center")
        self.fig.text(self.fx(x), self.fy(y), s, parse_math=False, **kw)

    def box(self, x, y, w, h, color, radius=14, z=1):
        self.fig.patches.append(FancyBboxPatch(
            (self.fx(x), self.fy(y + h)), self.fx(w), h / self.h, transform=self.fig.transFigure,
            boxstyle=f"round,pad=0,rounding_size={radius / W}", mutation_aspect=W / self.h,
            color=color, zorder=z))

    def image(self, img, x, y, size, ring=None, z=4):
        ax = self.fig.add_axes([self.fx(x), self.fy(y + size), self.fx(size), size / self.h])
        ax.set_zorder(z)
        ax.set_axis_off()
        ax.imshow(np.asarray(img), interpolation="lanczos")
        if ring:
            ax.add_patch(Circle((img.size[0] / 2 - 0.5, img.size[1] / 2 - 0.5), img.size[0] / 2 + 3, fill=False,
                                edgecolor=ring, linewidth=3, clip_on=False))

    def axes(self, x, y, w, h, z=3):
        ax = self.fig.add_axes([self.fx(x), self.fy(y + h), self.fx(w), h / self.h])
        ax.set_zorder(z)
        ax.patch.set_alpha(0)
        ax.set_axis_off()
        return ax

    def header(self, title, subtitle, pill):
        self.text(56, 58, title, color=TEXT, fontsize=27, fontweight="bold")
        self.text(56, 98, subtitle, color=MUTED, fontsize=14)
        pw = 36 + 11.5 * len(pill)
        self.box(W - 56 - pw, 40, pw, 40, PANEL2, radius=20)
        self.text(W - 56 - pw / 2, 60, pill, color=TEXT, fontsize=14, fontweight="bold", ha="center")

    def footer(self, disclaimer):
        self.text(56, self.h - 46, disclaimer, color=MUTED, fontsize=11)
        self.text(W - 56, self.h - 46, "Data: DexScreener · Jupiter · RugCheck · GeckoTerminal", color=MUTED,
                  fontsize=10, ha="right")

    def save(self, path):
        self.fig.savefig(path, facecolor=BG, dpi=100)
        plt.close(self.fig)
        return path


def _logo_strip(c, sections, y):
    """Overlapping round avatars of every featured token, centred (the eye-catching header)."""
    avatars = [(it["avatar"], TIER_STYLE[t][2]) for t, items in sections for it in items if it.get("avatar")]
    if not avatars:
        return
    size, step = 92, 74
    x = (W - (step * (len(avatars) - 1) + size)) / 2
    for i, (img, color) in enumerate(avatars):
        c.image(img, x + i * step, y, size, ring=BG, z=4 + i)


def _funnel_panel(c, funnels, y, height):
    col_w = (W - 112 - 2 * 16) / 3
    for i, (tier, f) in enumerate(funnels):
        title, _, color = TIER_STYLE[tier]
        x = 56 + i * (col_w + 16)
        c.box(x, y, col_w, height, PANEL)
        c.text(x + 20, y + 30, title, color=color, fontsize=15, fontweight="bold")
        if not f:
            c.text(x + 20, y + 70, "no data yet", color=MUTED, fontsize=12)
            continue
        top = max(f["stages"][0][1], 1)
        for j, (lab, n) in enumerate(f["stages"]):
            sy = y + 62 + j * 40
            c.text(x + 20, sy, f"{n:,}", color=TEXT, fontsize=13.5, fontweight="bold")
            c.text(x + 20 + 11 * len(f"{n:,}") + 10, sy, lab, color=MUTED, fontsize=11.5)
            bar_w = col_w - 40
            c.box(x + 20, sy + 12, bar_w, 6, TRACK, radius=3, z=2)
            c.box(x + 20, sy + 12, max(bar_w * n / top, 6), 6, color, radius=3, z=3)
        ry = y + 62 + len(f["stages"]) * 40 + 4
        c.text(x + 20, ry, "Main reasons tokens were dropped", color=MUTED, fontsize=10.5)
        for k, (lab, n) in enumerate(f["reasons"][:3]):
            c.text(x + 20, ry + 24 + k * 21, f"{lab}", color=TEXT, fontsize=11)
            c.text(x + col_w - 20, ry + 24 + k * 21, f"{n:,}", color=TEXT, fontsize=11, ha="right")


def render_daily(sections, funnels, now, path, disclaimer):
    """sections: [(tier, [item])], item = {symbol, name, stats: [2 lines], series, avatar (PIL RGBA)}.
    funnels: [(tier, {"stages": [(label, n)], "reasons": [(label, n)]} | None)]."""
    sections = [(t, items) for t, items in sections if items]
    row_h, gap, head_h = 112, 12, 66
    strip_y, funnel_y, funnel_h = 132, 252, 330
    top = funnel_y + funnel_h + 36
    height = top + sum(head_h + len(items) * (row_h + gap) + 24 for _, items in sections) + 96
    c = Canvas(height)
    c.header("COIN SIEVE", "Daily Solana screen", datetime.fromtimestamp(now, timezone.utc).strftime("%d %b %Y"))
    _logo_strip(c, sections, strip_y)
    _funnel_panel(c, funnels, funnel_y, funnel_h)

    y = top
    for tier, items in sections:
        title, subtitle, color = TIER_STYLE[tier]
        c.box(56, y + 12, 6, 30, color, radius=3, z=2)
        c.text(76, y + 27, title, color=TEXT, fontsize=19, fontweight="bold")
        c.text(W - 56, y + 28, subtitle, color=MUTED, fontsize=12, ha="right")
        y += head_h
        for it in items:
            c.box(56, y, W - 112, row_h, PANEL)
            if it.get("avatar"):
                c.image(it["avatar"], 76, y + 26, 60)
            c.text(152, y + 42, printable(it["symbol"])[:12], color=TEXT, fontsize=17, fontweight="bold")
            name = printable(it.get("name"), "")
            if name and name.lower() != (it["symbol"] or "").lower():
                c.text(152, y + 74, name[:20], color=MUTED, fontsize=11)
            series, change = it.get("series"), series_change(it.get("series"))
            ax = c.axes(370, y + 22, 250, row_h - 44)
            if series:
                prices = [p for _, p in series]
                col = UP if (change or 0) >= 0 else DOWN
                xs = range(len(prices))
                ax.plot(xs, prices, color=col, linewidth=2.3, solid_capstyle="round")
                ax.fill_between(xs, prices, min(prices), color=col, alpha=0.14)
                ax.set_xlim(0, len(prices) - 1)
                c.text(650, y + 28, f"{change:+.1f}%", color=col, fontsize=15, fontweight="bold")
                c.text(W - 80, y + 29, f"price, last {len(prices)}h", color=MUTED, fontsize=11, ha="right")
            else:
                ax.text(0.5, 0.5, "chart unavailable", color=MUTED, fontsize=11, ha="center", va="center",
                        transform=ax.transAxes)
                c.text(650, y + 28, "price n/a", color=MUTED, fontsize=13)
            for j, line in enumerate(it["stats"][:2]):
                c.text(650, y + 58 + j * 26, line, color=TEXT if j == 0 else MUTED, fontsize=12.5)
            y += row_h + gap
        y += 24
    c.footer(disclaimer)
    return c.save(path)


def render_weekly(tiles, blocks, start, now, path, disclaimer):
    """tiles: [(big number, label)] (4); blocks: [(tier or None, title, [lines])]."""
    tile_y, tile_h = 140, 130
    line_h = 28
    blocks_h = sum(50 + max(len(lines), 1) * line_h + 20 + 20 for _, _, lines in blocks)  # = bh + gap below
    height = tile_y + tile_h + 40 + blocks_h + 96
    c = Canvas(height)
    rng = (f"{datetime.fromtimestamp(start, timezone.utc):%d %b} - "
           f"{datetime.fromtimestamp(now, timezone.utc):%d %b %Y}")
    c.header("COIN SIEVE", "Weekly report · Solana", rng)
    tw = (W - 112 - 3 * 16) / 4
    for i, (big, lab) in enumerate(tiles):
        x = 56 + i * (tw + 16)
        c.box(x, tile_y, tw, tile_h, PANEL)
        c.text(x + tw / 2, tile_y + 52, str(big), color=TEXT, fontsize=28, fontweight="bold", ha="center")
        c.text(x + tw / 2, tile_y + 98, lab, color=MUTED, fontsize=11.5, ha="center")
    y = tile_y + tile_h + 40
    for tier, title, lines in blocks:
        color = TIER_STYLE[tier][2] if tier else MUTED
        bh = 50 + max(len(lines), 1) * line_h + 20
        c.box(56, y, W - 112, bh, PANEL)
        c.box(56, y + 18, 6, 26, color, radius=3, z=2)
        c.text(80, y + 31, title, color=TEXT, fontsize=16, fontweight="bold")
        for j, line in enumerate(lines or ["none"]):
            c.text(80, y + 68 + j * line_h, printable(line, "-"), color=TEXT if lines else MUTED, fontsize=12.5)
        y += bh + 20
    c.footer(disclaimer)
    return c.save(path)
