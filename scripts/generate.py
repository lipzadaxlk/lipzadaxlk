#!/usr/bin/env python3
"""Rebuilds README.md and assets/*.svg from profile.json, assets/art/*.png and the public GitHub API.

    pip install -r scripts/requirements.txt
    python scripts/generate.py

Token: GH_TOKEN or GITHUB_TOKEN, falling back to `gh auth token`.
Only public data is read. Private work shows up solely as the aggregate
restrictedContributionsCount, which GitHub exposes when the owner turns on
"Include private contributions on my profile".

Look: chapter 1 of a dark manga. Red cover, sumi ink, bone paper, screentone; the heat-tint of
tempered metal is the only accent. Every raster image is a slot: a PNG in assets/art/ with a fixed
aspect ratio (listed in assets/art/README.md). Swap a PNG and run this again; the workflow does it on
push. Output is deterministic for the same data and art.
"""
import base64
import json
import math
import os
import random
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
FONTS = ROOT / "scripts" / "fonts"
ASSETS = ROOT / "assets"
ART = ASSETS / "art"
BRT = timezone(timedelta(hours=-3))  # Brazil has had no DST since 2019
W = 846  # widest README column; every banner SVG is drawn on this width
CARD_W = 420  # contract cards: two per row on desktop, stacked on phones
PHONE = 560  # rendered width (px) below which the banner SVGs switch to the phone layout
MONTHS = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()

C = {
    "capa": "#C8161E",     # flat cover red
    "capa_d": "#8E0D13",   # screentone on red
    "sumi": "#15120E",     # ink
    "sumi2": "#221C17",    # panels on ink
    "sumi3": "#30271F",    # chips, empty bars
    "papel": "#D3CABF",    # the paper of the character sheet
    "osso": "#EFE6D2",     # text on ink and on red
    "osso2": "#B4A996",    # muted text on ink
    "tinta2": "#4A3F36",   # muted text on paper
    "sinal": "#FF5A50",    # red text on ink: targets, links
    "carimbo": "#A3121A",  # red text on paper
    "palha": "#E8BE5E",    # live values (first stop of the temper ramp)
}
RAMP = ("#E3B75B", "#C2713A", "#8A5BD6", "#4C7BE8")  # heat tint of tempered metal: accent only


# ---------------------------------------------------------------- fonts

class Font:
    def __init__(self, family, file, fallback):
        self.family = family
        self.fallback = fallback
        self.path = FONTS / file
        tt = TTFont(self.path, recalcTimestamp=False)
        self.upm = tt["head"].unitsPerEm
        self.cap = tt["OS/2"].sCapHeight / self.upm
        self.cmap = tt.getBestCmap()
        self.hmtx = tt["hmtx"]

    def width(self, text, size, ls=0.0):
        adv = 0
        for ch in text:
            glyph = self.cmap.get(ord(ch)) or self.cmap[ord("?")]
            adv += self.hmtx[glyph][0]
        return adv / self.upm * size + ls * len(text)

    def fit(self, text, size, max_w, ls=0.0, min_size=None):
        """Shrinks down to min_size, then truncates with an ellipsis."""
        min_size = min_size or size
        while size > min_size and self.width(text, size, ls) > max_w:
            size -= 1
        if self.width(text, size, ls) <= max_w:
            return text, size
        while text and self.width(text + "…", size, ls) > max_w:
            text = text[:-1]
        return text.rstrip() + "…", size

    def embed(self, chars):
        tt = TTFont(self.path, recalcTimestamp=False)
        opts = Options()
        opts.flavor = "woff2"
        opts.layout_features = []
        opts.hinting = False
        opts.desubroutinize = True
        opts.name_IDs = [0, 1, 2, 13, 14]  # keep copyright + OFL notice inside the subset
        sub = Subsetter(opts)
        sub.populate(unicodes={ord(c) for c in chars} | {ord("?")})
        sub.subset(tt)
        tt.flavor = "woff2"
        buf = BytesIO()
        tt.save(buf)
        return base64.b64encode(buf.getvalue()).decode()


DG = Font("Dela Gothic", "DelaGothicOne-Latin.ttf", "'Arial Black',Impact,sans-serif")   # titles, names
ZK = Font("Zen Kaku Black", "ZenKakuGothicNew-Black-Latin.ttf", "'Arial Black',sans-serif")
ZB = Font("Zen Kaku Bold", "ZenKakuGothicNew-Bold-Latin.ttf", "Arial,sans-serif")      # balloons, captions
ZM = Font("Zen Kaku Medium", "ZenKakuGothicNew-Medium-Latin.ttf", "Arial,sans-serif")  # PT captions
CM = Font("MPlus Code", "MPLUS1Code-Medium-Latin.ttf", "monospace")                     # numbers, dates
CB = Font("MPlus Code Bold", "MPLUS1Code-Bold-Latin.ttf", "monospace")
ALL_FONTS = (DG, ZK, ZB, ZM, CM, CB)


def wrap(text, font, size, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and font.width(trial, size) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    return lines + [cur] if cur else lines


# ---------------------------------------------------------------- art (raster slots)
# Each slot is assets/art/<name>.png in a fixed aspect ratio. It is centre-cropped to the box,
# resized to 2x pixels and embedded. Nothing is drawn over a slot except its frame and text.

_art_cache = {}


def art_uri(name, box_w, box_h, alpha=False):
    """data: URI of assets/art/<name>.png covering a box_w x box_h box, or None if the file is missing."""
    key = (name, round(box_w, 1), round(box_h, 1), alpha)
    if key in _art_cache:
        return _art_cache[key]
    path = ART / f"{name}.png"
    if not path.is_file():
        print(f"warning: {path.relative_to(ROOT)} is missing, slot left empty", file=sys.stderr)
        _art_cache[key] = None
        return None
    im = Image.open(path).convert("RGBA")
    if not alpha:  # flatten transparency onto white so JPEG never turns it black
        im = Image.alpha_composite(Image.new("RGBA", im.size, (255, 255, 255, 255)), im).convert("RGB")
    iw, ih = im.size
    sc = max(box_w / iw, box_h / ih)
    vw, vh = box_w / sc, box_h / sc
    x0, y0 = (iw - vw) / 2, (ih - vh) / 2
    out = im.resize((max(1, round(box_w * 2)), max(1, round(box_h * 2))), Image.LANCZOS,
                    box=(x0, y0, x0 + vw, y0 + vh))
    buf = BytesIO()
    if alpha:
        out.save(buf, "WEBP", quality=86, method=6)
        mime = "image/webp"
    else:
        out.save(buf, "JPEG", quality=86, optimize=True)
        mime = "image/jpeg"
    _art_cache[key] = f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode()}"
    return _art_cache[key]


def image(href, x, y, w, h, extra=""):
    return f'<image href="{href}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" preserveAspectRatio="none"{extra}/>'


def slot(name, x, y, w, h, alpha=False):
    """A raster slot; an empty paper rectangle if the PNG is missing."""
    href = art_uri(name, w, h, alpha)
    if href is None:
        return f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" fill="{C["papel"]}"/>'
    return image(href, x, y, w, h)


# ---------------------------------------------------------------- svg

DEFS = {
    "dots-ink": '<pattern id="dots-ink" width="8" height="8" patternUnits="userSpaceOnUse">'
                f'<circle cx="4" cy="4" r="1.7" fill="{C["sumi"]}"/></pattern>',
    "dots-red": '<pattern id="dots-red" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(30)">'
                f'<circle cx="4.5" cy="4.5" r="2.4" fill="{C["capa_d"]}"/></pattern>',
    "dots-bone": '<pattern id="dots-bone" width="8" height="8" patternUnits="userSpaceOnUse">'
                 f'<circle cx="4" cy="4" r="1.3" fill="{C["sumi3"]}"/></pattern>',
    "hatch": '<pattern id="hatch" width="18" height="18" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
             f'<rect width="7" height="18" fill="{C["sumi2"]}"/></pattern>',
    "tape": '<pattern id="tape" width="28" height="28" patternUnits="userSpaceOnUse" patternTransform="rotate(-35)">'
            f'<rect width="28" height="28" fill="{C["capa"]}"/><rect width="13" height="28" fill="{C["sumi"]}"/></pattern>',
    "ramp": '<linearGradient id="ramp" x1="0" y1="0" x2="1" y2="0">'
            + "".join(f'<stop offset="{i / 3:.2f}" stop-color="{c}"/>' for i, c in enumerate(RAMP)) + "</linearGradient>",
    "ramp-v": '<linearGradient id="ramp-v" x1="0" y1="1" x2="0" y2="0">'
              + "".join(f'<stop offset="{i / 3:.2f}" stop-color="{c}"/>' for i, c in enumerate(RAMP)) + "</linearGradient>",
    "ramp-r": '<radialGradient id="ramp-r">'
              + "".join(f'<stop offset="{i / 3:.2f}" stop-color="{c}"/>' for i, c in enumerate(RAMP)) + "</radialGradient>",
    "metal": f'<linearGradient id="metal" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{C["osso"]}"/>'
             '<stop offset=".55" stop-color="#C9BFAF"/><stop offset="1" stop-color="#8F8577"/></linearGradient>',
    "fade-l": '<linearGradient id="fade-l-g" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#fff"/>'
              '<stop offset=".6" stop-color="#fff" stop-opacity="0"/></linearGradient>'
              '<mask id="fade-l"><rect width="100%" height="100%" fill="url(#fade-l-g)"/></mask>',
    "fade-r": '<linearGradient id="fade-r-g" x1="1" y1="1" x2="0" y2="0"><stop offset="0" stop-color="#fff"/>'
              '<stop offset=".7" stop-color="#fff" stop-opacity="0"/></linearGradient>'
              '<mask id="fade-r"><rect width="100%" height="100%" fill="url(#fade-r-g)"/></mask>',
}


class Svg:
    """One <img>-safe SVG: fonts subset + embedded, CSS animation only, reduced motion respected."""

    def __init__(self, height, title, width=W, bg=None, edge=None):
        self.w, self.h = width, height
        self.title = title
        self.bg = bg or C["sumi"]
        self.edge = edge  # (colour, stroke width) of the outer manga panel border
        self.used = {f: set() for f in ALL_FONTS}
        self.defs = []
        self.need_defs = set()
        self.css = []
        self.under = []  # drawn below both layouts (shared art)
        self.ids = 0

    def uid(self, prefix):
        self.ids += 1
        return f"{prefix}{self.ids}"

    def need(self, *names):
        self.need_defs.update(names)

    def text(self, x, y, s, font, size, fill, ls=0.0, anchor="start", cls="", extra=""):
        self.used[font].update(s)
        cls_attr = f' class="{cls}"' if cls else ""
        ls_attr = f' letter-spacing="{ls}"' if ls else ""
        anchor_attr = f' text-anchor="{anchor}"' if anchor != "start" else ""
        family = f"'{font.family}',{font.fallback}"
        return (f'<text x="{x:.1f}" y="{y:.1f}" font-family="{family}" font-size="{size}" fill="{fill}"'
                f'{ls_attr}{anchor_attr}{cls_attr}{extra}>{escape(s)}</text>')

    def title_text(self, x, y, s, font, size, fill=None, stroke=None, sw=None, shadow=6, ls=0.0, anchor="start"):
        """Manga cover lettering: fill, thick ink outline and a hard offset shadow."""
        fill = fill or C["osso"]
        stroke = stroke or C["sumi"]
        sw = sw if sw is not None else max(4, size * 0.11)
        out = []
        if shadow:
            out.append(self.text(x + shadow, y + shadow, s, font, size, stroke, ls, anchor,
                                 extra=f' stroke="{stroke}" stroke-width="{sw:.1f}" stroke-linejoin="round"'))
        out.append(self.text(x, y, s, font, size, fill, ls, anchor,
                             extra=f' stroke="{stroke}" stroke-width="{sw:.1f}" stroke-linejoin="round" paint-order="stroke"'))
        return "".join(out)

    def render(self, desktop, phone=None):
        faces = "".join(
            f"@font-face{{font-family:'{f.family}';src:url(data:font/woff2;base64,{f.embed(chars)}) format('woff2')}}"
            for f, chars in self.used.items() if chars)
        css = faces + "text{font-kerning:none;font-variant-ligatures:none}"
        if phone is not None:
            css += f".p{{display:none}}@media (max-width:{PHONE}px){{.d{{display:none}}.p{{display:inline}}}}"
        css += "".join(self.css) + "@media (prefers-reduced-motion:reduce){*{animation:none!important}}"
        w, h = self.w, self.h
        defs = "".join(DEFS[n] for n in sorted(self.need_defs)) + "".join(self.defs)
        body = (f'<g class="d">{"".join(desktop)}</g><g class="p">{"".join(phone)}</g>'
                if phone is not None else "".join(desktop))
        edge = ""
        if self.edge:
            col, sw = self.edge
            edge = (f'<rect x="{sw / 2}" y="{sw / 2}" width="{w - sw}" height="{h - sw}" fill="none" '
                    f'stroke="{col}" stroke-width="{sw}"/>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img">'
                f'<title>{escape(self.title)}</title>'
                f'<defs><style>{css}</style>'
                f'<clipPath id="frame"><rect width="{w}" height="{h}"/></clipPath>{defs}</defs>'
                f'<rect width="{w}" height="{h}" fill="{self.bg}"/>'
                f'<g clip-path="url(#frame)">{"".join(self.under)}{body}</g>{edge}</svg>\n')


def baseline(top, height, font, size):
    """Baseline that centres capitals vertically inside a box."""
    return top + height / 2 + font.cap * size / 2


def pts(points):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in points)


def speed_burst(cx, cy, r0, r1, n, seed, color, wmin=1.5, wmax=7.0):
    """Manga focus lines: thin ink wedges radiating from (cx, cy). One path, deterministic."""
    rnd = random.Random(seed)
    d = []
    for k in range(n):
        a = (k + rnd.random() * 0.8) / n * 2 * math.pi
        half = rnd.uniform(wmin, wmax) / 2 / r1
        ra = r0 * rnd.uniform(0.85, 1.35)
        x0, y0 = cx + math.cos(a) * ra, cy + math.sin(a) * ra
        x1, y1 = cx + math.cos(a - half) * r1, cy + math.sin(a - half) * r1
        x2, y2 = cx + math.cos(a + half) * r1, cy + math.sin(a + half) * r1
        d.append(f"M{x0:.0f} {y0:.0f}L{x1:.0f} {y1:.0f}L{x2:.0f} {y2:.0f}Z")
    return f'<path d="{"".join(d)}" fill="{color}"/>'


def speed_h(x0, x1, y0, y1, n, seed, color, wmax=3.0):
    """Horizontal motion lines, tapered."""
    rnd = random.Random(seed)
    d = []
    for _ in range(n):
        y = rnd.uniform(y0, y1)
        a = rnd.uniform(x0, x0 + (x1 - x0) * 0.5)
        b = rnd.uniform(a + (x1 - x0) * 0.25, x1)
        t = rnd.uniform(0.8, wmax) / 2
        d.append(f"M{a:.0f} {y:.1f}L{b:.0f} {y - t:.1f}L{b:.0f} {y + t:.1f}Z")
    return f'<path d="{"".join(d)}" fill="{color}"/>'


def brush(x0, y0, x1, y1, seed, color, rough=5.0):
    """A dry-brush ink slab with ragged edges, from (x0,y0) to (x1,y1) (box)."""
    rnd = random.Random(seed)
    top, bot = [], []
    steps = max(8, int((x1 - x0) / 14))
    for i in range(steps + 1):
        x = x0 + (x1 - x0) * i / steps
        top.append((x, y0 + rnd.uniform(-rough, rough) + (i / steps) * 3))
        bot.append((x, y1 + rnd.uniform(-rough, rough) - (i / steps) * 6))
    tail = [(x1 + rnd.uniform(6, 26), (y0 + y1) / 2 + rnd.uniform(-4, 4))]
    head = [(x0 - rnd.uniform(4, 12), (y0 + y1) / 2)]
    return f'<polygon points="{pts(head + top + tail + bot[::-1])}" fill="{color}"/>'



def sparks(x0, x1, y0, y1, n, seed, cls="spark"):
    rnd = random.Random(seed)
    out = []
    for k in range(n):
        x, y, s = rnd.uniform(x0, x1), rnd.uniform(y0, y1), rnd.uniform(3, 7)
        col = RAMP[k % 4]
        delay = rnd.uniform(0, 2.4)
        out.append(f'<path class="{cls}" style="animation-delay:{delay:.2f}s" d="M{x:.0f} {y - s:.0f}L{x + s * 0.5:.0f} '
                   f'{y:.0f}L{x:.0f} {y + s:.0f}L{x - s * 0.5:.0f} {y:.0f}Z" fill="{col}"/>')
    return "".join(out)


CSS_FX = {
    "boil": "@keyframes boil{0%,33%{opacity:1}34%,100%{opacity:0}}"
            ".b0,.b1,.b2{animation:boil .5s steps(1) 6}.b1{animation-delay:-.17s}.b2{animation-delay:-.34s}"
            ".b1,.b2{opacity:0}",
    "reveal": "@keyframes reveal{from{clip-path:inset(-20% 100% -20% -2%)}to{clip-path:inset(-20% -2% -20% -2%)}}"
              ".reveal{animation:reveal .85s cubic-bezier(.7,0,.2,1) .25s both}"
              ".reveal2{animation:reveal .6s cubic-bezier(.7,0,.2,1) .05s both}",
    "pop": "@keyframes pop{0%{opacity:0;transform:scale(2.2) rotate(-14deg)}60%{opacity:1;transform:scale(.92)}"
           "100%{opacity:1;transform:none}}"
           ".pop{transform-box:fill-box;transform-origin:center;animation:pop .42s cubic-bezier(.3,1.4,.5,1) 1.05s both}",
    "spark": "@keyframes spark{0%{opacity:0;transform:translate(0,0)}15%{opacity:1}"
             "100%{opacity:0;transform:translate(14px,-70px)}}"
             ".spark{animation:spark 2.4s ease-out infinite}",
    "spin": "@keyframes spin{to{transform:rotate(360deg)}}"
            ".spin{transform-box:fill-box;transform-origin:center;animation:spin 7s linear infinite}",
}


def fx(svg, *names):
    for n in names:
        if CSS_FX[n] not in svg.css:
            svg.css.append(CSS_FX[n])


# ---------------------------------------------------------------- data

def token():
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    try:
        return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        sys.exit("Set GH_TOKEN (or log in with `gh auth login`) to read the GitHub API.")


TOKEN = None


def api(url, body=None):
    if not url.startswith("https://"):
        url = "https://api.github.com/" + url
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json",
                 "User-Agent": "profile-hud-generator", "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as res:
        return json.load(res)


def graphql(query, **variables):
    out = api("graphql", {"query": query, "variables": variables})
    if out.get("errors"):
        raise RuntimeError(out["errors"])
    return out["data"]


QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions totalIssueContributions totalPullRequestContributions
      totalPullRequestReviewContributions totalRepositoryContributions restrictedContributionsCount
      contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
    }
    repositories(first: 100, privacy: PUBLIC, ownerAffiliations: OWNER, isFork: false,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name createdAt pushedAt stargazerCount
        releases(first: 10, orderBy: {field: CREATED_AT, direction: DESC}) {
          totalCount nodes { tagName publishedAt isDraft }
        }
        defaultBranchRef { target { ... on Commit {
          history(first: 50) { totalCount nodes { committedDate author { name user { login } } } }
        } } }
      }
    }
  }
}"""


def ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(BRT)


def is_bot(author):
    login = ((author or {}).get("user") or {}).get("login") or ""
    return login.endswith("[bot]") or "[bot]" in ((author or {}).get("name") or "")


def collect(profile, now):
    login = profile["login"]
    season_start = datetime(now.year, 1, 1, tzinfo=BRT)
    data = graphql(QUERY, login=login, **{"from": season_start.isoformat(), "to": now.isoformat()})["user"]
    cc = data["contributionsCollection"]
    repos = {r["name"]: r for r in data["repositories"]["nodes"]}

    # Feed: commits grouped per repo per day, repo creations, releases (all public).
    feed = []
    for r in repos.values():
        if r["name"] == login:  # the profile repo is the stage, not the play
            continue
        feed.append({"kind": "repo", "repo": r["name"], "t": ts(r["createdAt"])})
        for rel in r["releases"]["nodes"]:
            if not rel["isDraft"] and rel["publishedAt"]:
                feed.append({"kind": "release", "repo": r["name"], "tag": rel["tagName"], "t": ts(rel["publishedAt"])})
        target = (r["defaultBranchRef"] or {}).get("target") or {}
        days = {}
        for c in (target.get("history") or {}).get("nodes", []):
            if is_bot(c["author"]):
                continue
            t = ts(c["committedDate"])
            item = days.setdefault(t.date(), {"kind": "commit", "repo": r["name"], "t": t, "n": 0})
            item["n"] += 1
            item["t"] = max(item["t"], t)
        feed.extend(days.values())

    # Events add what the repo data can't: a repo turning public, merged PRs elsewhere.
    for ev in api(f"users/{login}/events/public?per_page=100"):
        name = ev["repo"]["name"]
        short = name.split("/", 1)[1] if name.startswith(login + "/") else name
        if short == login:
            continue
        if ev["type"] == "PublicEvent":
            feed.append({"kind": "unlock", "repo": short, "t": ts(ev["created_at"])})
        elif ev["type"] == "PullRequestEvent":
            pr = ev["payload"].get("pull_request") or {}
            if ev["payload"].get("action") == "closed" and pr.get("merged"):
                feed.append({"kind": "merge", "repo": short, "t": ts(ev["created_at"])})
    feed.sort(key=lambda i: i["t"], reverse=True)

    restricted = cc["restrictedContributionsCount"]
    rows = feed[: 4 if restricted else 5]
    if restricted:
        rows.append({"kind": "classified", "repo": None, "t": None, "n": restricted})

    contributions = sum(cc[k] for k in (
        "totalCommitContributions", "totalIssueContributions", "totalPullRequestContributions",
        "totalPullRequestReviewContributions", "totalRepositoryContributions", "restrictedContributionsCount"))
    calendar = [d for w in cc["contributionCalendar"]["weeks"] for d in w["contributionDays"]]
    active = [datetime.fromisoformat(d["date"]).date() for d in calendar if d["contributionCount"] > 0]
    active = [d for d in active if d.year == now.year]
    weeks_played = {week_of(d) for d in active}
    window = list(range(max(1, week_of(now.date()) - 11), week_of(now.date()) + 1))
    last_seen = max([d for d in active] + [i["t"].date() for i in feed[:1]], default=None)

    slots = []
    for s in profile["public_slots"]:
        r = repos.get(s["repo"])
        if not r:
            print(f"warning: public repo {s['repo']!r} not found, slot skipped", file=sys.stderr)
            continue
        hist = ((r["defaultBranchRef"] or {}).get("target") or {}).get("history") or {}
        slots.append({**s, "pushed": ts(r["pushedAt"]), "commits": hist.get("totalCount", 0),
                      "stars": r["stargazerCount"], "releases": r["releases"]["totalCount"]})
    slots.sort(key=lambda s: s["pushed"], reverse=True)

    # Contracts: public ones are signed on the day the repo was created; numbered in that order.
    contracts = []
    for c in profile["contracts"]:
        if c.get("repo"):
            r = repos.get(c["repo"])
            if not r:
                print(f"warning: contract repo {c['repo']!r} not found, skipped", file=sys.stderr)
                continue
            contracts.append({**c, "title": c["repo"], "done": True, "signed": ts(r["createdAt"])})
        else:
            contracts.append({**c, "title": c["name"], "done": False, "signed": None})
    for n, c in enumerate(sorted((c for c in contracts if c["done"]), key=lambda c: c["signed"]), 1):
        c["no"] = n

    stats = {
        "round": week_of(now.date()), "rounds_total": week_of(datetime(now.year, 12, 31).date()),
        "days": len(active), "contributions": contributions, "restricted": restricted,
        "calendar_total": cc["contributionCalendar"]["totalContributions"],
        "weeks_played": weeks_played, "window": window, "last_seen": last_seen, "season": now.year,
    }
    return rows, stats, slots, contracts


def week_of(day):
    """Round = 7-day block of the season; Dec 31 (and Dec 30 in leap years) joins round 52."""
    return min((day.timetuple().tm_yday - 1) // 7 + 1, 52)


def fmt_day(d):
    return f"{d.day:02d} {MONTHS[d.month - 1]}"


def fmt_time(t):
    return f"{fmt_day(t)} {t:%H:%M}"


def fmt_day_long(d):
    return f"{d.day} {MONTHS[d.month - 1].title()}"


def contract_file(c, contracts):
    """Public contracts are named after the repo; sealed ones only get a number (no private names in paths)."""
    if c["done"]:
        return f"contract-{slug(c['title'])}.svg"
    sealed = [x for x in contracts if not x["done"]]
    return f"contract-sealed-{sealed.index(c) + 1}.svg"


def slug(name):
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")


# ---------------------------------------------------------------- hero.svg

def build_hero(profile):
    """Cover: the 21:9 art slot fills the banner; the name is brushed over it."""
    login = profile["login"]
    H = round(W * 9 / 21)
    svg = Svg(H, f"{login}, {profile['alias']['en'].lower()}", bg=C["sumi"], edge=(C["sumi"], 6))
    fx(svg, "reveal", "spark")
    svg.under.append(slot("hero", 0, 0, W, H))
    name = login.upper()
    alias = profile["alias"]
    d, p = [], []

    # desktop: narration boxes top-left, brushed name and tagline caption bottom-left, subtle sparks
    d.append(f'<rect x="20" y="20" width="78" height="38" fill="{C["osso"]}" stroke="{C["sumi"]}" stroke-width="3"/>')
    d.append(svg.text(59, baseline(20, 38, CB, 22), "CH.01", CB, 22, C["carimbo"], anchor="middle"))
    aw = ZK.width(alias["en"], 24, 1) + 28
    d.append(f'<rect x="98" y="20" width="{aw:.0f}" height="38" fill="{C["sumi"]}"/>')
    d.append(svg.text(112, baseline(20, 38, ZK, 24), alias["en"], ZK, 24, C["osso"], ls=1))
    pw = ZM.width(alias["pt"], 17) + 24
    d.append(f'<rect x="98" y="58" width="{pw:.0f}" height="28" fill="{C["sumi"]}"/>')
    d.append(svg.text(110, baseline(58, 28, ZM, 17), alias["pt"], ZM, 17, C["osso2"]))
    d.append(f'<g class="reveal2">{brush(14, 200, 760, 266, 5, C["sumi"], 5)}</g>')
    d.append(f'<g class="reveal">{svg.title_text(22, 254, name, DG, 84, shadow=6)}</g>')
    tw = max(ZK.width(profile["tagline"]["en"], 23), ZM.width(profile["tagline"]["pt"], 17)) + 32
    d.append(f'<rect x="18" y="284" width="{tw:.0f}" height="62" fill="{C["sumi"]}"/>')
    d.append(svg.text(34, 311, profile["tagline"]["en"], ZK, 23, C["osso"]))
    d.append(svg.text(34, 335, profile["tagline"]["pt"], ZM, 17, C["osso2"]))
    d.append(sparks(560, 830, 250, 350, 8, 4))

    # phone: bigger name and one-line tagline
    pa = ZK.width(alias["en"], 32, 1) + 32
    p.append(f'<rect x="16" y="16" width="{pa:.0f}" height="52" fill="{C["sumi"]}"/>')
    p.append(svg.text(32, baseline(16, 52, ZK, 32), alias["en"], ZK, 32, C["osso"], ls=1))
    psize = DG.fit(name, 98, W - 64, min_size=60)[1]
    p.append(f'<g class="reveal2">{brush(10, 172, 836, 246, 6, C["sumi"], 5)}</g>')
    p.append(f'<g class="reveal">{svg.title_text(18, 234, name, DG, psize, shadow=6)}</g>')
    ten, tsz = ZK.fit(profile["tagline"]["en"], 34, W - 70, min_size=31)
    p.append(f'<rect x="16" y="268" width="{ZK.width(ten, tsz) + 32:.0f}" height="66" fill="{C["sumi"]}"/>')
    p.append(svg.text(32, baseline(268, 66, ZK, tsz), ten, ZK, tsz, C["osso"]))
    p.append(sparks(560, 830, 250, 350, 6, 5))
    return svg.render(d, p)


# ---------------------------------------------------------------- about.svg

def balloon(x, y, w, h, tail_to, fill, seed=1):
    """Rounded speech balloon with a tail pointing at tail_to."""
    cx, cy = x + w / 2, y + h / 2
    tx, ty = tail_to
    ang = math.atan2(ty - cy, tx - cx)
    bx1, by1 = cx + math.cos(ang - 0.18) * w * 0.42, cy + math.sin(ang - 0.18) * h * 0.42
    bx2, by2 = cx + math.cos(ang + 0.18) * w * 0.42, cy + math.sin(ang + 0.18) * h * 0.42
    r = min(h / 2, 46)
    body = (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{fill}" stroke="{C["sumi"]}" stroke-width="4"/>')
    tail = f'<polygon points="{pts([(bx1, by1), (tx, ty), (bx2, by2)])}" fill="{fill}" stroke="{C["sumi"]}" stroke-width="4" stroke-linejoin="round"/>'
    patch = f'<polygon points="{pts([(bx1, by1), ((bx1 + bx2) / 2 + (tx - cx) * 0.02, (by1 + by2) / 2), (bx2, by2)])}" fill="{fill}"/>'
    return tail + body + patch.replace('fill=', 'stroke="none" fill=')


def build_about(profile):
    about = profile["about"]
    H = 440
    svg = Svg(H, "About", bg=C["papel"], edge=(C["sumi"], 6))
    svg.need("dots-ink", "fade-r")
    d, p = [], []
    G = 14  # gutter

    def face_panel(x0, y0, size, who, small, big, show_pt):
        """Square bust slot with a WHO caption, and the favourite-character box under it."""
        out = [slot("bust", x0, y0, size, size),
               f'<rect x="{x0}" y="{y0}" width="{size}" height="{size}" fill="none" stroke="{C["sumi"]}" stroke-width="5"/>']
        bw = DG.width("WHO", who) + 26
        out.append(f'<rect x="{x0 + 10}" y="{y0 + 10}" width="{bw:.0f}" height="{who * 1.8:.0f}" fill="{C["osso"]}" '
                   f'stroke="{C["sumi"]}" stroke-width="3"/>')
        out.append(svg.text(x0 + 10 + bw / 2, y0 + 10 + who * 1.8 / 2 + DG.cap * who / 2, "WHO", DG, who, C["sumi"], anchor="middle"))
        fav = about["favorite"]
        fy0, fy1 = y0 + size + G, H - G
        out.append(f'<rect x="{x0}" y="{fy0}" width="{size}" height="{fy1 - fy0}" fill="{C["sumi"]}"/>')
        lines = [(fav["label_en"], CB, small, C["palha"])]
        if show_pt:
            lines.append((fav["label_pt"], ZM, small, C["osso2"]))
        lines.append((fav["name"], ZK, big, C["osso"]))
        total = sum(sz * 1.3 for _, _, sz, _ in lines)
        y = fy0 + (fy1 - fy0 - total) / 2
        for text, font, sz, col in lines:
            y += sz * 1.3
            out.append(svg.text(x0 + 14, y - sz * 0.3, text, font, sz, col, ls=1 if font is CB else 0))
        return out

    # ---- desktop: tall face panel, balloon panel, now-forging panel
    ax1 = G + 286
    d += face_panel(G, G, 286, 24, 17, 25, True)
    bx0, by1 = ax1 + G, 214
    d.append(f'<rect x="{bx0}" y="{G}" width="{W - G - bx0}" height="{by1 - G}" fill="url(#dots-ink)" opacity=".5" mask="url(#fade-r)"/>')
    d.append(speed_h(bx0, W - G, G + 8, by1 - 8, 22, 41, C["sumi"], 2.2))
    lines_en = wrap(about["who"]["en"], ZB, 21, 430)
    lines_pt = wrap(about["who"]["pt"], ZM, 17, 430)
    bh = 30 + 27 * len(lines_en) + 22 * len(lines_pt) + 8
    d.append(balloon(bx0 + 34, G + 16, 474, bh, (ax1 - 10, 130), C["osso"]))
    yy = G + 16 + 36
    for line in lines_en:
        d.append(svg.text(bx0 + 64, yy, line, ZB, 21, C["sumi"]))
        yy += 27
    yy += 4
    for line in lines_pt:
        d.append(svg.text(bx0 + 64, yy, line, ZM, 17, C["tinta2"]))
        yy += 22
    d.append(f'<rect x="{bx0}" y="{G}" width="{W - G - bx0}" height="{by1 - G}" fill="none" stroke="{C["sumi"]}" stroke-width="5"/>')
    # now forging
    cy0 = by1 + G
    d.append(f'<rect x="{bx0}" y="{cy0}" width="{W - G - bx0}" height="{H - G - cy0}" fill="{C["osso"]}" stroke="{C["sumi"]}" stroke-width="5"/>')
    d.append(f'<rect x="{bx0}" y="{cy0}" width="236" height="44" fill="{C["sumi"]}"/>')
    d.append(svg.text(bx0 + 14, cy0 + 31, "NOW FORGING", DG, 21, C["osso"]))
    d.append(svg.text(bx0 + 250, cy0 + 30, "forjando agora", ZM, 17, C["tinta2"]))
    for j, item in enumerate(about["now"]):
        y = cy0 + 76 + j * 44
        d.append(f'<rect x="{bx0 + 18}" y="{y - 17}" width="10" height="10" fill="{C["capa"]}" transform="rotate(45 {bx0 + 23} {y - 12})"/>')
        d.append(svg.text(bx0 + 40, y - 4, item["name"], ZK, 22, C["sumi"]))
        nx = bx0 + 40 + ZK.width(item["name"], 22) + 12
        en, esz = ZB.fit(item["en"], 18, W - G - 20 - nx, min_size=17)
        d.append(svg.text(nx, y - 4, en, ZB, esz, C["sumi"]))
        d.append(svg.text(bx0 + 40, y + 17, item["pt"], ZM, 17, C["tinta2"]))
    d.append(f'<g transform="translate({W - G - 134} {cy0 + 34}) rotate(-6)">'
             f'{svg.title_text(0, 0, "TAK TAK", DG, 20, fill=C["capa"], stroke=C["sumi"], sw=4, shadow=0)}</g>')

    # ---- phone: face + balloon with the tagline + short now-forging list
    pax1 = G + 300
    p += face_panel(G, G, 300, 32, 31, 34, False)
    px0 = pax1 + G
    p.append(f'<rect x="{px0}" y="{G}" width="{W - G - px0}" height="{H - 2 * G}" fill="{C["osso"]}" stroke="{C["sumi"]}" stroke-width="5"/>')
    tl = wrap(profile["tagline"]["en"], ZK, 36, 420)
    p.append(balloon(px0 + 18, G + 14, 452, 30 + 42 * len(tl), (pax1 - 6, 120), C["papel"]))
    for j, line in enumerate(tl):
        p.append(svg.text(px0 + 46, G + 14 + 52 + j * 42, line, ZK, 36, C["sumi"]))
    ny = G + 14 + 30 + 42 * len(tl) + 50
    p.append(f'<rect x="{px0}" y="{ny - 36}" width="{W - G - px0}" height="50" fill="{C["sumi"]}"/>')
    p.append(svg.text(px0 + 16, ny, "NOW FORGING", DG, 32, C["osso"]))
    for j, item in enumerate(about["now"][:3]):
        p.append(svg.text(px0 + 22, ny + 52 + j * 44, item["name"], ZK, 34, C["sumi"]))
    return svg.render(d, p)


def build_contact(profile):
    H = 132
    svg = Svg(H, "Got a contract? Open an issue.", bg=C["capa"], edge=(C["sumi"], 6))
    svg.need("dots-red")
    d, p = [], []

    def shout(x0, y0, x1, y1, seed):
        rnd = random.Random(seed)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        rx, ry = (x1 - x0) / 2, (y1 - y0) / 2
        pts_ = []
        n = 44
        for k in range(n):
            a = k / n * 2 * math.pi
            r = 1.0 if k % 2 == 0 else rnd.uniform(0.86, 0.93)
            pts_.append((cx + math.cos(a) * rx * r, cy + math.sin(a) * ry * r))
        return f'<polygon points="{pts(pts_)}" fill="{C["osso"]}" stroke="{C["sumi"]}" stroke-width="5" stroke-linejoin="round"/>'

    svg.under.append(f'<rect width="{W}" height="{H}" fill="url(#dots-red)" opacity=".7"/>'
                     + speed_burst(W / 2, H / 2, 300, 700, 60, 51, C["sumi"], 1, 5))
    d.append(shout(40, 8, W - 40, 124, 61))
    d.append(svg.text(W / 2, 66, "GOT A CONTRACT? OPEN AN ISSUE.", ZK, 31, C["sumi"], anchor="middle"))
    d.append(svg.text(W / 2, 96, "tem um contrato? abre uma issue.", ZB, 20, C["tinta2"], anchor="middle"))
    p.append(shout(20, 4, W - 20, 128, 62))
    p.append(svg.text(W / 2, 60, "GOT A CONTRACT?", ZB, 36, C["tinta2"], anchor="middle"))
    p.append(svg.text(W / 2, 106, "OPEN AN ISSUE", ZK, 50, C["sumi"], anchor="middle"))
    return svg.render(d, p)


# ---------------------------------------------------------------- arsenal

INK, MET, EDGE, WRAPC = C["sumi"], "url(#metal)", "url(#ramp)", C["capa"]


def weapon(kind):
    """Original ink drawings of the techs-as-weapons, in a 240x130 box."""
    s = f'stroke="{INK}" stroke-width="3.5" stroke-linejoin="round"'
    if kind == "crossbow":
        return (f'<path d="M150 18Q190 70 150 122" fill="none" stroke="{INK}" stroke-width="11" stroke-linecap="round"/>'
                f'<path d="M150 18Q190 70 150 122" fill="none" stroke="{MET}" stroke-width="5" stroke-linecap="round"/>'
                f'<path d="M150 18L108 71L150 122" fill="none" stroke="{INK}" stroke-width="2"/>'
                f'<path d="M18 72L150 63L198 66L203 72L198 78L150 81L62 83L44 100L22 100L28 83Z" fill="{MET}" {s}/>'
                f'<path d="M44 84L62 84L48 101L30 101Z" fill="{WRAPC}" {s}/>'
                f'<path d="M98 72H224" stroke="{INK}" stroke-width="4"/>'
                f'<path d="M222 63L240 72L222 81Z" fill="{EDGE}" {s}/>'
                f'<path d="M98 72L86 61L112 66ZM98 72L86 83L112 78Z" fill="{WRAPC}" {s}/>'
                f'<path d="M70 70h8M86 69h8M118 68h8" stroke="{INK}" stroke-width="2"/>')
    if kind == "routeblade":
        return (f'<g transform="rotate(-24 120 65)">'
                f'<path d="M92 57L212 57L236 65L212 73L92 73Z" fill="{MET}" {s}/>'
                f'<path d="M92 68L212 68L236 65L212 73L92 73Z" fill="{EDGE}"/>'
                f'<path d="M100 62H196" stroke="{INK}" stroke-width="1.6" opacity=".7"/>'
                f'<path d="M72 40L94 65L72 90L62 90L82 65L62 40Z" fill="{INK}"/>'
                f'<rect x="30" y="59" width="34" height="12" fill="{WRAPC}" {s}/>'
                f'<path d="M40 59v12M50 59v12" stroke="{INK}" stroke-width="2.5"/>'
                f'<circle cx="24" cy="65" r="8" fill="{MET}" {s}/></g>')
    if kind == "chakram":
        teeth = []
        for k in range(20):
            a = k / 20 * 2 * math.pi
            r = 60 if k % 2 == 0 else 46
            teeth.append((120 + math.cos(a) * r, 65 + math.sin(a) * r))
        return (f'<polygon points="{pts(teeth)}" fill="{EDGE}" {s}/>'
                f'<circle cx="120" cy="65" r="46" fill="{MET}" {s}/>'
                f'<circle cx="120" cy="65" r="25" fill="{C["papel"]}" {s}/>'
                f'<path d="M93 41A36 36 0 0 1 147 41" fill="none" stroke="{INK}" stroke-width="15"/>'
                f'<path d="M93 41A36 36 0 0 1 147 41" fill="none" stroke="{WRAPC}" stroke-width="9"/>'
                + "".join(f'<circle cx="{120 + math.cos(a) * 35:.1f}" cy="{65 + math.sin(a) * 35:.1f}" r="3.2" fill="{INK}"/>'
                          for a in (math.pi * 0.3, math.pi * 0.95, math.pi * 1.6)))
    if kind == "knuckles":
        hexes = "".join(
            f'<polygon points="{pts([(cx + math.cos(k / 6 * 2 * math.pi) * 12, 38 + math.sin(k / 6 * 2 * math.pi) * 12) for k in range(6)])}" fill="{EDGE}" {s}/>'
            for cx in (62, 101, 140, 179))
        rings = "".join(f'<circle cx="{cx}" cy="80" r="17" fill="none" stroke="{INK}" stroke-width="13"/>'
                        f'<circle cx="{cx}" cy="80" r="17" fill="none" stroke="{MET}" stroke-width="6"/>'
                        for cx in (62, 101, 140, 179))
        return (rings + f'<rect x="40" y="46" width="161" height="22" rx="7" fill="{MET}" {s}/>' + hexes +
                f'<path d="M46 104Q120 132 196 104" fill="none" stroke="{INK}" stroke-width="13" stroke-linecap="round"/>'
                f'<path d="M46 104Q120 132 196 104" fill="none" stroke="{WRAPC}" stroke-width="6" stroke-linecap="round"/>')
    if kind == "arrow":
        def one(tr, scale):
            return (f'<g transform="{tr} scale({scale})">'
                    f'<path d="M24 65H198" stroke="{INK}" stroke-width="8"/><path d="M24 65H198" stroke="{MET}" stroke-width="3"/>'
                    f'<path d="M196 46L238 65L196 84L206 65Z" fill="{EDGE}" {s}/>'
                    f'<path d="M30 65L10 48L52 58ZM30 65L10 82L52 72Z" fill="{WRAPC}" {s}/></g>')
        return one("rotate(-20 120 65) translate(0 10)", 1) + one("rotate(-38 120 65) translate(40 -10)", 0.7)
    if kind == "crowbar":
        return (f'<g transform="rotate(-22 120 65)">'
                f'<path d="M30 70H190Q224 70 224 44Q224 28 206 26" fill="none" stroke="{INK}" stroke-width="15" stroke-linecap="round"/>'
                f'<path d="M30 70H190Q224 70 224 44Q224 28 206 26" fill="none" stroke="{MET}" stroke-width="7" stroke-linecap="round"/>'
                f'<path d="M32 70L10 58M32 70L10 82" stroke="{INK}" stroke-width="11" stroke-linecap="round"/>'
                f'<path d="M32 70L10 58M32 70L10 82" stroke="{MET}" stroke-width="4" stroke-linecap="round"/>'
                f'<path d="M92 70H134" stroke="{INK}" stroke-width="19"/><path d="M95 70H131" stroke="{WRAPC}" stroke-width="13"/>'
                f'<path d="M104 62v16M114 62v16M124 62v16" stroke="{INK}" stroke-width="2"/></g>')
    if kind == "hook":
        prong = ("M120 96Q80 100 76 66", "M120 96Q160 100 164 66", "M120 96Q118 126 146 122")
        return (f'<path d="M120 6C92 -6 60 20 22 4" fill="none" stroke="{WRAPC}" stroke-width="5" stroke-dasharray="9 5"/>'
                f'<circle cx="120" cy="12" r="9" fill="none" stroke="{INK}" stroke-width="5"/>'
                f'<path d="M120 20V96" stroke="{INK}" stroke-width="12"/><path d="M120 20V96" stroke="{MET}" stroke-width="5"/>'
                + "".join(f'<path d="{pp}" fill="none" stroke="{INK}" stroke-width="12" stroke-linecap="round"/>'
                          f'<path d="{pp}" fill="none" stroke="{MET}" stroke-width="5" stroke-linecap="round"/>' for pp in prong) +
                f'<path d="M70 70L76 54L84 68ZM158 68L164 54L172 70ZM144 128L156 120L142 114Z" fill="{EDGE}" stroke="{INK}" stroke-width="2"/>'
                f'<g transform="rotate(18 186 74)"><rect x="168" y="58" width="44" height="32" fill="{INK}"/>'
                + "".join(f'<rect x="{171 + k * 9}" y="61" width="5" height="5" fill="{C["osso"]}"/>'
                          f'<rect x="{171 + k * 9}" y="82" width="5" height="5" fill="{C["osso"]}"/>' for k in range(5)) +
                f'<rect x="174" y="69" width="32" height="11" fill="{RAMP[3]}"/></g>')
    if kind == "horn":
        return (f'<path d="M30 52Q118 14 198 30L214 20L222 80L204 72Q126 62 40 82Z" fill="{MET}" {s}/>'
                f'<ellipse cx="216" cy="50" rx="9" ry="31" fill="{INK}"/>'
                f'<path d="M84 36L92 74M124 26L130 66M164 26L168 68" stroke="{WRAPC}" stroke-width="9"/>'
                f'<path d="M84 36L92 74M124 26L130 66M164 26L168 68" stroke="{INK}" stroke-width="2" fill="none"/>'
                f'<rect x="14" y="56" width="20" height="20" rx="3" fill="{INK}"/>'
                f'<path d="M60 80Q115 128 172 72" fill="none" stroke="{WRAPC}" stroke-width="5"/>'
                f'<path d="M232 30Q242 50 232 70M240 20Q256 50 240 80" fill="none" stroke="{INK}" stroke-width="3.5" stroke-linecap="round"/>')
    if kind == "saw":
        teeth = []
        for k in range(36):
            a = k / 36 * 2 * math.pi
            r = 60 if k % 2 == 0 else 51
            teeth.append((120 + math.cos(a + (0.06 if k % 2 else 0)) * r, 65 + math.sin(a + (0.06 if k % 2 else 0)) * r))
        bolts = "".join(f'<circle cx="{120 + math.cos(k / 6 * 2 * math.pi) * 27:.1f}" cy="{65 + math.sin(k / 6 * 2 * math.pi) * 27:.1f}" '
                        f'r="3.4" fill="{INK}"/>' for k in range(6))
        return (f'<path d="M48 20A82 82 0 0 0 40 108M200 22A82 82 0 0 1 206 104" fill="none" stroke="{INK}" stroke-width="3.5" stroke-linecap="round"/>'
                f'<g class="spin"><polygon points="{pts(teeth)}" fill="{EDGE}" {s}/>'
                f'<circle cx="120" cy="65" r="50" fill="{MET}" {s}/>{bolts}'
                f'<circle cx="120" cy="65" r="13" fill="{INK}"/><path d="M120 22V40" stroke="{INK}" stroke-width="3"/></g>')
    raise ValueError(kind)


def build_arsenal_head(profile):
    n = len(profile["arsenal"])
    H = 150
    svg = Svg(H, "Arsenal", bg=C["sumi"], edge=(C["capa"], 4))
    svg.need("dots-bone")
    d, p = [], []
    for g, big in ((d, False), (p, True)):
        g.append(f'<rect width="{W}" height="{H}" fill="url(#dots-bone)"/>')
        g.append(speed_h(0, W, 10, H - 10, 26, 71 + big, C["sumi3"], 2.4))
        slab = 618 if not big else 812
        g.append(f'<polygon points="{pts([(0, 14), (slab, 6), (slab - 26, H - 10), (0, H - 4)])}" fill="{C["capa"]}"/>')
    d.append(svg.title_text(24, 112, "ARSENAL", DG, 88, shadow=6))
    d.append(svg.text(632, 58, f"{n} WEAPONS", CB, 22, C["palha"], ls=1))
    d.append(svg.text(632, 88, "all tools I really use", ZB, 18, C["osso"]))
    d.append(svg.text(632, 114, "ferramentas que eu uso", ZM, 17, C["osso2"]))
    p.append(svg.title_text(22, 116, "ARSENAL", DG, 104, shadow=6))
    p.append(svg.text(W - 30, 112, str(n), CB, 64, C["osso"], anchor="end"))
    return svg.render(d, p)


def build_arsenal_strip(items, start, signature):
    H = 300 if signature else 270
    ty = 206 if signature else 180  # first text baseline (desktop)
    title = ", ".join(f"{i['tech']} ({i['weapon']['en']})" for i in items)
    svg = Svg(H, f"Arsenal: {title}", bg=C["sumi"] if signature else C["papel"], edge=(C["sumi"], 6))
    svg.need("metal", "ramp", "dots-ink", "dots-red")
    fx(svg, "spin")
    d, p = [], []
    G = 12
    cw = (W - G * (len(items) + 1)) / len(items)
    for k, it in enumerate(items):
        x0 = G + k * (cw + G)
        cell_bg = C["capa"] if signature else C["osso"]
        ink_text = C["osso"] if signature else C["sumi"]
        sub_text = C["osso"] if signature else C["tinta2"]
        for g, big in ((d, False), (p, True)):
            g.append(f'<rect x="{x0:.1f}" y="{G}" width="{cw:.1f}" height="{H - 2 * G}" fill="{cell_bg}"/>')
            g.append(f'<rect x="{x0:.1f}" y="{G}" width="{cw:.1f}" height="{(H - 2 * G) * 0.62:.1f}" '
                     f'fill="url(#{"dots-red" if signature else "dots-ink"})" opacity="{0.9 if signature else 0.22}"/>')
            acid = svg.uid("aa")
            svg.defs.append(f'<clipPath id="{acid}"><rect x="{x0:.1f}" y="{G}" width="{cw:.1f}" height="{(H - 2 * G) * 0.6:.1f}"/></clipPath>')
            g.append(f'<g clip-path="url(#{acid})">'
                     + speed_burst(x0 + cw / 2, G + 80, 70, 260, 34, 100 + start + k + big, C["sumi"] if signature else C["osso2"], 0.8, 4)
                     + "</g>")
            art_h = 124 if not big else 150
            art = it["art"]
            if art.startswith("slot:"):
                sz, sy = (150, G + 22) if not big else (150, G + 54)
                sx = x0 + (cw - sz) / 2
                g.append(slot(art[5:], sx, sy, sz, sz, alpha=True))
                g.append(f'<rect x="{sx:.1f}" y="{sy}" width="{sz}" height="{sz}" fill="none" stroke="{C["sumi"]}" stroke-width="3"/>')
            else:
                sc = min((cw - 24) / 240, art_h / 130)
                g.append(f'<g transform="translate({x0 + (cw - 240 * sc) / 2:.1f} {G + 14:.1f}) scale({sc:.3f})">{weapon(art[5:])}</g>')
            g.append(f'<rect x="{x0:.1f}" y="{G}" width="{cw:.1f}" height="{H - 2 * G}" fill="none" stroke="{C["sumi"]}" stroke-width="5"/>')
            # number tab
            no = f"{start + k:02d}"
            g.append(f'<rect x="{x0:.1f}" y="{G}" width="{(48 if not big else 70):.0f}" height="{(32 if not big else 46):.0f}" fill="{C["sumi"]}"/>')
            g.append(svg.text(x0 + (24 if not big else 35), G + (23 if not big else 35), no, CB, 18 if not big else 32,
                              C["palha"], anchor="middle"))
        # desktop text
        tname, tsz = DG.fit(it["tech"], 28, cw - 28, min_size=19)
        d.append(svg.text(x0 + 14, ty, tname, DG, tsz, ink_text))
        d.append(svg.text(x0 + 14, ty + 24, it["weapon"]["en"].upper(), ZB, 17, C["palha"] if signature else C["carimbo"], ls=0.5))
        used = wrap("used in: " + it["used"], ZB, 17, cw - 28)[:2]
        for j, line in enumerate(used):
            d.append(svg.text(x0 + 14, ty + 48 + j * 20, line, ZB, 17, sub_text))
        # phone text: just the name, big
        pname, psz = ZK.fit(it["tech"], 40, cw - 18, min_size=32)
        p.append(svg.text(x0 + cw / 2, H - 34, pname, ZK, psz, ink_text, anchor="middle"))
    return svg.render(d, p)


# ---------------------------------------------------------------- contracts


def build_contract(c):
    """Card: header, 3:4 art slot, title, target, signature and the smith's stamp."""
    w = CARD_W
    ax, ay = 16, 78
    aw = w - 32
    ah = round(aw * 4 / 3)
    H = ay + ah + 275
    title = c["title"]
    status = "fulfilled" if c["done"] else "in progress"
    svg = Svg(H, f"Contract: {title}, {status}", width=w, bg=C["papel"], edge=(C["sumi"], 7))
    svg.need("ramp")
    o = []
    o.append(f'<rect x="0" y="0" width="{w}" height="64" fill="{C["capa"] if c["done"] else C["sumi"]}"/>')
    o.append(svg.title_text(20, 43, "CONTRACT", DG, 28, shadow=3))
    no = f"Nº {c['no']:02d}" if c["done"] else "OPEN"
    o.append(svg.text(w - 20, 43, no, CB, 26, C["osso"], anchor="end"))
    o.append(slot(c["art"], ax, ay, aw, ah))
    o.append(f'<rect x="{ax}" y="{ay}" width="{aw}" height="{ah}" fill="none" stroke="{C["sumi"]}" stroke-width="5"/>')
    y = ay + ah + 46
    tname, tsz = DG.fit(title.upper(), 34, w - 40, min_size=24)
    o.append(svg.text(20, y, tname, DG, tsz, C["sumi"]))
    y += 26
    o.append(svg.text(20, y, c["type"]["en"].upper(), CB, 17, C["carimbo"], ls=1))
    o.append(svg.text(20 + CB.width(c["type"]["en"].upper(), 17, 1) + 12, y, c["type"]["pt"], ZM, 17, C["tinta2"]))
    y += 30
    o.append(svg.text(20, y, "TARGET", CB, 17, C["carimbo"], ls=1))
    for line in wrap(c["target"]["en"], ZB, 19, w - 120)[:2]:
        o.append(svg.text(100, y, line, ZB, 19, C["sumi"]))
        y += 23
    for line in wrap(c["target"]["pt"], ZM, 17, w - 120)[:2]:
        o.append(svg.text(100, y, line, ZM, 17, C["tinta2"]))
        y += 21
    sy = H - 52
    if c["done"]:
        s_ = c["signed"]
        o.append(svg.text(20, sy, f"SIGNED {s_.day:02d} {MONTHS[s_.month - 1]} {s_.year}", CB, 18, C["sumi"]))
        o.append(svg.text(20, sy + 22, "cumprido", ZM, 17, C["tinta2"]))
        o.append(f'<g transform="translate(336 {H - 72}) rotate(-12)">'
                 f'<circle r="40" fill="{C["osso"]}" stroke="url(#ramp)" stroke-width="6"/>'
                 f'<circle r="32" fill="none" stroke="url(#ramp)" stroke-width="2"/>'
                 + svg.text(0, 10, "LZX", DG, 24, C["sumi"], anchor="middle") +
                 f'<rect x="-56" y="22" width="112" height="26" fill="{C["capa"]}" stroke="{C["sumi"]}" stroke-width="3"/>'
                 + svg.text(0, 41, "FULFILLED", DG, 17, C["osso"], anchor="middle") + "</g>")
    else:
        o.append(svg.text(20, sy, "IN PROGRESS", CB, 18, C["sumi"]))
        o.append(svg.text(20, sy + 22, "em andamento", ZM, 17, C["tinta2"]))
        o.append(f'<g transform="translate(336 {H - 62}) rotate(-8)">'
                 f'<circle r="40" fill="none" stroke="{C["sumi"]}" stroke-width="4" stroke-dasharray="9 7"/>'
                 + svg.text(0, 9, "LZX", DG, 24, C["osso2"], anchor="middle") + "</g>")
    return svg.render(o)


def build_contracts_head(contracts):
    done = sum(1 for c in contracts if c["done"])
    open_ = len(contracts) - done
    H = 140
    svg = Svg(H, "Contracts", bg=C["sumi"], edge=(C["capa"], 4))
    svg.need("dots-bone")
    d, p = [], []
    for g, big in ((d, False), (p, True)):
        g.append(f'<rect width="{W}" height="{H}" fill="url(#dots-bone)"/>')
        slab = 600 if not big else 812
        g.append(f'<polygon points="{pts([(0, 10), (slab, 4), (slab - 24, H - 8), (0, H - 2)])}" fill="{C["capa"]}"/>')
    d.append(svg.title_text(22, 98, "CONTRACTS", DG, 62, shadow=5))
    d.append(svg.text(622, 54, f"{done} FULFILLED", CB, 22, C["palha"], ls=1))
    d.append(svg.text(622, 82, f"{open_} IN PROGRESS", CB, 22, C["osso"], ls=1))
    d.append(svg.text(622, 112, "com a minha marca", ZM, 17, C["osso2"]))
    p.append(svg.title_text(20, 104, "CONTRACTS", DG, DG.fit("CONTRACTS", 90, 770, min_size=60)[1], shadow=5))
    return svg.render(d, p)


# ---------------------------------------------------------------- hud.svg (mission log)

ICONS = {  # 24x24 pictograms, drawn for this profile
    "commit": '<circle cx="12" cy="12" r="4.5"/><path d="M1.5 12H7.5M16.5 12H22.5"/>',
    "repo": '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M12 8v8M8 12h8"/>',
    "release": '<path d="M3 3h8.5l9.5 9.5-8.5 8.5L3 11.5z"/><circle cx="8" cy="8" r="1.6"/>',
    "unlock": '<rect x="4" y="11" width="16" height="10" rx="1.5"/><path d="M8 11V7a4 4 0 0 1 7.7-1.6"/>',
    "lock": '<rect x="4" y="11" width="16" height="10" rx="1.5"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
    "merge": '<circle cx="6" cy="5" r="2.5"/><circle cx="6" cy="19" r="2.5"/><circle cx="18" cy="12" r="2.5"/>'
             '<path d="M6 7.5v9M6 7.5c0 3.5 3.5 4.5 9.5 4.5"/>',
    "classified": '<path d="M3 7h18M3 12h18M3 17h11" stroke-width="3.5"/>',
}


def icon(name, x, y, size, color):
    s = size / 24
    return (f'<g transform="translate({x:.1f} {y:.1f}) scale({s:.3f})" fill="none" stroke="{color}" '
            f'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</g>')


LABEL = {"commit": "COMMIT", "repo": "NEW REPO", "release": "RELEASE", "unlock": "UNLOCKED",
         "merge": "MERGED PR", "classified": "CLASSIFIED"}


def row_label(r):
    if r["kind"] == "release":
        return f"RELEASE {r['tag']}"
    if r.get("n", 1) > 1:
        return f"{LABEL[r['kind']]} ×{r['n']}"
    return LABEL[r["kind"]]


def row_alt(r):
    k, repo, n = r["kind"], r["repo"], r.get("n", 1)
    when = f" on {fmt_day_long(r['t'])}" if r["t"] else ""
    return {
        "commit": f"pushed {n} commit{'s' if n > 1 else ''} to {repo}",
        "repo": f"created the repo {repo}",
        "release": f"released {r.get('tag')} of {repo}",
        "unlock": f"made {repo} public",
        "merge": f"merged a pull request into {repo}",
        "classified": f"{n} private contribution{'s' if n > 1 else ''} this season, details classified",
    }[k] + when


def plate(x, y, w, h, fill, slant=10):
    return f'<polygon points="{pts([(x + slant, y), (x + w, y), (x + w - slant, y + h), (x, y + h)])}" fill="{fill}"/>'


def build_hud(profile, rows):
    login = profile["login"]
    TS, PH, STEP, Y0 = 40, 54, 64, 84          # desktop rows
    PSTEP, PPH, PY0 = 104, 90, 124             # phone rows
    BAND = 18 + 32 + 16
    shown_p = rows[:3]
    desk_h = Y0 + max(len(rows), 1) * STEP - (STEP - PH) + BAND
    phone_h = PY0 + max(len(shown_p), 1) * PSTEP - (PSTEP - PPH) + 20
    H = max(desk_h, phone_h)
    if len(rows) > 1:
        STEP = min(76, int((H - BAND - PH - Y0) / (len(rows) - 1)))
    svg = Svg(H, f"{login} mission log", bg=C["sumi"], edge=(C["capa"], 4))
    svg.need("dots-bone")
    d, p = [], []

    # --- desktop header
    d.append(f'<rect width="{W}" height="{H}" fill="url(#dots-bone)" opacity=".7"/>')
    d.append(f'<rect x="0" y="0" width="{W}" height="60" fill="{C["sumi2"]}"/>')
    d.append(f'<polygon points="{pts([(0, 0), (382, 0), (360, 60), (0, 60)])}" fill="{C["capa"]}"/>')
    d.append(svg.title_text(22, 44, "MISSION LOG", DG, 28, shadow=3))
    d.append(f'<circle cx="298" cy="31" r="7" fill="{C["palha"]}"/>')
    d.append(svg.text(310, 38, "LIVE", CB, 18, C["osso"]))
    newest = next((r for r in rows if r["t"]), None)
    if newest:
        stamp = f"{fmt_time(newest['t'])} BRT"
        sx = W - 26 - CB.width(stamp, 20)
        d.append(svg.text(W - 26, baseline(0, 60, CB, 20), stamp, CB, 20, C["palha"], anchor="end"))
        d.append(svg.text(sx - 12, baseline(0, 60, ZB, 18), "LAST ACTION", ZB, 18, C["osso2"], ls=1, anchor="end"))

    right = W - 24
    for i, r in enumerate(rows):
        y = Y0 + i * STEP
        label = row_label(r)
        chip_w = 12 + 22 + 10 + CM.width(label, 19) + 14
        actor_w = ZK.width(login, 30)
        fixed = 22 + actor_w + 14 + chip_w + 14 + 22
        if r["kind"] == "classified":
            target, tsz, target_w = None, TS, 132
        else:
            target, tsz = DG.fit(r["repo"], 32, right - 24 - fixed, min_size=26)
            target_w = DG.width(target, tsz)
        plate_w = fixed + target_w
        px = right - plate_w
        g = [plate(px, y, plate_w, PH, C["sumi2"])]
        if i == 0:
            g.append(plate(px, y, 26, PH, C["capa"]))
        cx = px + 26
        g.append(svg.text(cx, baseline(y, PH, ZK, 30), login, ZK, 30, C["osso"] if i == 0 else C["osso2"]))
        cx += actor_w + 14
        g.append(f'<rect x="{cx:.1f}" y="{y + 10}" width="{chip_w:.1f}" height="{PH - 20}" fill="{C["sumi"]}"/>')
        g.append(icon(r["kind"], cx + 12, y + PH / 2 - 11, 22, C["palha"] if i == 0 else C["osso"]))
        g.append(svg.text(cx + 44, baseline(y + 10, PH - 20, CM, 19), label, CM, 19, C["osso"]))
        cx += chip_w + 14
        if target is None:
            g.append(f'<rect x="{cx:.1f}" y="{y + 15}" width="{target_w}" height="{PH - 30}" fill="{C["sumi3"]}"/>')
        else:
            g.append(svg.text(cx, baseline(y, PH, DG, tsz), target, DG, tsz, C["sinal"]))
        when = fmt_time(r["t"]) if r["t"] else f"SEASON {datetime.now(BRT).year}"
        if px - 24 - CB.width(when, 19) >= 18:
            g.append(svg.text(24, baseline(y, PH, CB, 19), when, CB, 19, C["palha"] if i == 0 else C["osso2"]))
        d.append(animated(i, g, STEP, "d"))
    if not rows:
        d.append(svg.text(28, 120, "No public action yet this season.", ZB, 30, C["osso"]))
        d.append(svg.text(28, 156, "nenhuma ação pública nesta temporada ainda.", ZM, 22, C["osso2"]))

    sub = profile["hud_subtitle_pt"]
    sw = ZM.width(sub, 18) + 28
    sy = H - 16 - 32
    d.append(f'<rect x="{(W - sw) / 2:.1f}" y="{sy}" width="{sw:.1f}" height="32" fill="{C["sumi2"]}"/>')
    d.append(svg.text(W / 2, baseline(sy, 32, ZM, 18), sub, ZM, 18, C["osso2"], anchor="middle"))

    # --- phone
    p.append(f'<rect width="{W}" height="{H}" fill="url(#dots-bone)" opacity=".7"/>')
    p.append(f'<rect x="0" y="0" width="{W}" height="100" fill="{C["sumi2"]}"/>')
    p.append(f'<polygon points="{pts([(0, 0), (560, 0), (530, 100), (0, 100)])}" fill="{C["capa"]}"/>')
    p.append(svg.title_text(24, 70, "MISSION LOG", DG, 44, shadow=4))
    p.append(f'<circle cx="{W - 120}" cy="50" r="13" fill="{C["palha"]}"/>')
    p.append(svg.text(W - 26, baseline(0, 100, CB, 38), "LIVE", CB, 38, C["osso"], anchor="end"))
    for i, r in enumerate(shown_p):
        y = PY0 + i * PSTEP
        when = fmt_day(r["t"]) if r["t"] else str(datetime.now(BRT).year)
        when_w = CB.width(when, 34)
        g = [plate(24, y, W - 48, PPH, C["sumi2"], 14)]
        if i == 0:
            g.append(plate(24, y, 34, PPH, C["capa"], 14))
        g.append(f'<rect x="66" y="{y + 12}" width="66" height="66" fill="{C["sumi"]}"/>')
        g.append(icon(r["kind"], 77, y + 23, 44, C["palha"] if i == 0 else C["osso"]))
        tx = 150
        count = f"×{r['n']}" if r.get("n", 1) > 1 else ""
        count_w = CB.width(count, 34) + 16 if count else 0
        if r["kind"] == "classified":
            g.append(f'<rect x="{tx}" y="{y + 27}" width="180" height="36" fill="{C["sumi3"]}"/>')
            tend = tx + 180
        else:
            target, tsz = DG.fit(r["repo"], 50, W - 24 - 30 - when_w - 24 - count_w - tx, min_size=38)
            g.append(svg.text(tx, baseline(y, PPH, DG, tsz), target, DG, tsz, C["sinal"]))
            tend = tx + DG.width(target, tsz)
        if count:
            g.append(svg.text(tend + 16, baseline(y, PPH, CB, 34), count, CB, 34, C["osso"]))
        g.append(svg.text(W - 24 - 30, baseline(y, PPH, CB, 34), when, CB, 34,
                          C["palha"] if i == 0 else C["osso2"], anchor="end"))
        p.append(animated(i, g, PSTEP, "p"))
    if not rows:
        p.append(svg.text(28, 170, "No public action yet.", ZB, 44, C["osso"]))

    svg.css.append(
        "@keyframes in{from{opacity:0;transform:translateX(160px)}}"
        "@keyframes fade{from{opacity:0;transform:translateX(40px)}}"
        f"@keyframes pushd{{from{{transform:translateY(-{STEP}px)}}}}"
        f"@keyframes pushp{{from{{transform:translateY(-{PSTEP}px)}}}}"
        ".new{animation:in .5s cubic-bezier(.2,.9,.25,1) 1.05s both}"
        ".od{animation:pushd .38s cubic-bezier(.3,.8,.3,1) 1s both}"
        ".op{animation:pushp .38s cubic-bezier(.3,.8,.3,1) 1s both}"
        + "".join(f".f{i}{{animation:fade .32s ease-out {0.12 + i * 0.1:.2f}s both}}" for i in range(1, 6)))
    return svg.render(d, p)


def animated(i, parts, step, layout):
    """One orchestrated moment: older rows fade in one slot up, get pushed down, newest slides in."""
    inner = "".join(parts)
    if i == 0:
        return f'<g class="new">{inner}</g>'
    return f'<g class="o{layout}"><g class="f{i}">{inner}</g></g>'


# ---------------------------------------------------------------- player.svg (mercenary file)

def build_player(profile, stats):
    login = profile["login"]
    H = 340
    svg = Svg(H, f"{login} mercenary file", bg=C["sumi"], edge=(C["capa"], 4))
    svg.need("dots-ink", "dots-bone", "ramp")
    d, p = [], []
    last = fmt_day(stats["last_seen"]) if stats["last_seen"] else "—"
    values = [("DAYS PLAYED", str(stats["days"]), C["osso"]),
              ("CONTRIBUTIONS", str(stats["contributions"]), C["osso"]),
              ("LAST SEEN", last, C["palha"])]

    def portrait(x, y, w, h):
        cid = svg.uid("pf")
        poly = [(x + 8, y), (x + w, y + 6), (x + w - 6, y + h), (x, y + h - 8)]
        svg.defs.append(f'<clipPath id="{cid}"><polygon points="{pts(poly)}"/></clipPath>')
        return [f'<polygon points="{pts([(px + 8, py + 8) for px, py in poly])}" fill="{C["capa"]}"/>',
                f'<g clip-path="url(#{cid})">{slot("bust", x, y, w, h)}</g>',
                f'<polygon points="{pts(poly)}" fill="none" stroke="{C["osso"]}" stroke-width="3"/>']

    d.append(f'<rect width="{W}" height="{H}" fill="url(#dots-bone)" opacity=".6"/>')
    d += portrait(24, 24, 236, 236)
    x0 = 290
    d.append(f'<rect x="{x0}" y="26" width="196" height="30" fill="{C["capa"]}"/>')
    d.append(svg.text(x0 + 12, 48, "MERCENARY FILE", CB, 18, C["osso"], ls=1))
    d.append(svg.text(x0 + 208, 48, "ficha do mercenário", ZM, 17, C["osso2"]))
    name, nsz = DG.fit(login.upper(), 56, W - 28 - x0)
    d.append(svg.title_text(x0, 116, name, DG, nsz, shadow=4))
    d.append(svg.text(x0, 154, profile["tagline"]["en"], ZK, 26, C["osso"]))
    d.append(svg.text(x0, 182, profile["tagline"]["pt"], ZM, 19, C["osso2"]))
    col = (W - 28 - x0) / 3
    for k, (lab, val, color) in enumerate(values):
        cx = x0 + k * col
        d.append(svg.text(cx, 222, lab, ZB, 17, C["osso2"], ls=1))
        v, vs = CB.fit(val, 32, col - 12, min_size=24)
        d.append(svg.text(cx, 258, v, CB, vs, color))
    win = stats["window"]
    label = f"ROUNDS {win[0]}–{win[-1]}"
    d.append(svg.text(24, 316, label, ZB, 18, C["osso2"], ls=1))
    played = sum(1 for wk in win if wk in stats["weeks_played"])
    tally = f"{played}/{len(win)} PLAYED"
    d.append(svg.text(W - 24, 316, tally, CB, 19, C["osso"], anchor="end"))
    bx0 = 24 + ZB.width(label, 18, 1) + 22
    bx1 = W - 24 - CB.width(tally, 19) - 22
    step = (bx1 - bx0) / len(win)
    for j, wk in enumerate(win):
        bx = bx0 + j * step
        on = wk in stats["weeks_played"]
        d.append(f'<polygon points="{pts([(bx + 5, 294), (bx + step - 6, 294), (bx + step - 11, 320), (bx, 320)])}" '
                 f'fill="{"url(#ramp)" if on else C["sumi3"]}"/>')
        if wk == win[-1]:
            d.append(f'<rect x="{bx - 4:.1f}" y="289" width="{step:.1f}" height="36" fill="none" stroke="{C["osso"]}" stroke-width="2"/>')

    p.append(f'<rect width="{W}" height="{H}" fill="url(#dots-bone)" opacity=".6"/>')
    p += portrait(24, 24, 190, 190)
    px0 = 244
    name, nsz = DG.fit(login.upper(), 70, W - 26 - px0, min_size=48)
    p.append(svg.title_text(px0, 92, name, DG, nsz, shadow=4))
    for j, line in enumerate(wrap(profile["tagline"]["en"], ZK, 38, W - 26 - px0)[:2]):
        p.append(svg.text(px0, 146 + j * 44, line, ZK, 38, C["osso"]))
    pcol = (W - 48) / 3
    short = {"DAYS PLAYED": "DAYS", "CONTRIBUTIONS": "CONTRIBS", "LAST SEEN": "LAST SEEN"}
    for k, (lab, val, color) in enumerate(values):
        cx = 24 + k * pcol
        p.append(svg.text(cx, 262, short[lab], ZB, 34, C["osso2"]))
        v, vs = CB.fit(val, 46, pcol - 16, min_size=36)
        p.append(svg.text(cx, 314, v, CB, vs, color))
    return svg.render(d, p)


# ---------------------------------------------------------------- loadout

def build_loadout_head(unlocked, locked):
    H = 84
    svg = Svg(H, "Loadout", bg=C["sumi"], edge=(C["capa"], 4))
    d, p = [], []
    d.append(f'<polygon points="{pts([(0, 0), (300, 0), (280, H), (0, H)])}" fill="{C["capa"]}"/>')
    d.append(svg.title_text(22, 60, "LOADOUT", DG, 40, shadow=4))
    d.append(svg.text(300, 54, "equipamento", ZM, 19, C["osso2"]))
    x = W - 24
    for num, word in ((locked, "LOCKED"), (unlocked, "UNLOCKED")):
        d.append(svg.text(x, baseline(0, H, ZB, 19), word, ZB, 19, C["osso2"], ls=1, anchor="end"))
        x -= ZB.width(word, 19, 1) + 8
        d.append(svg.text(x, baseline(0, H, CB, 24), str(num), CB, 24, C["osso"], anchor="end"))
        x -= CB.width(str(num), 24) + 26
    p.append(f'<polygon points="{pts([(0, 0), (420, 0), (396, H), (0, H)])}" fill="{C["capa"]}"/>')
    p.append(svg.title_text(20, 66, "LOADOUT", DG, 54, shadow=4))
    x = W - 24
    for num, word in ((locked, "LOCKED"), (unlocked, "OPEN")):
        p.append(svg.text(x, baseline(0, H, ZB, 34), word, ZB, 34, C["osso2"], anchor="end"))
        x -= ZB.width(word, 34) + 10
        p.append(svg.text(x, baseline(0, H, CB, 36), str(num), CB, 36, C["osso"], anchor="end"))
        x -= CB.width(str(num), 36) + 30
    return svg.render(d, p)


def build_slot(slot, number, now):
    H = 150
    svg = Svg(H, f"Loadout slot {number}: {slot['repo']}", bg=C["sumi"], edge=(C["capa"], 4))
    svg.need("dots-bone")
    d, p = [], []
    active = (now - slot["pushed"]).days < 30
    status = "ACTIVE" if active else "STABLE"

    d.append(f'<rect width="{W}" height="{H}" fill="url(#dots-bone)" opacity=".5"/>')
    d.append(f'<polygon points="{pts([(0, 0), (86, 0), (70, H), (0, H)])}" fill="{C["capa"]}"/>')
    d.append(svg.title_text(40, baseline(0, H, DG, 46), str(number), DG, 46, anchor="middle", shadow=3))
    lines = [("PUSHED", fmt_day(slot["pushed"])), ("COMMITS", str(slot["commits"]))]
    if slot["stars"]:
        lines.append(("STARS", str(slot["stars"])))
    if slot["releases"]:
        lines.append(("RELEASES", str(slot["releases"])))
    lines = lines[:3]
    stat_w = 0
    for j, (lab, val) in enumerate(lines):
        y = 50 + j * 32
        vw = CB.width(val, 22)
        d.append(svg.text(W - 26, y, val, CB, 22, C["palha"] if lab == "PUSHED" else C["osso"], anchor="end"))
        d.append(svg.text(W - 26 - vw - 10, y, lab, ZB, 17, C["osso2"], ls=1, anchor="end"))
        stat_w = max(stat_w, vw + 10 + ZB.width(lab, 17, 1))
    sy = 50 + len(lines) * 32
    if len(lines) < 3:
        d.append(svg.text(W - 26, sy, status, DG, 20, C["osso"], ls=1, anchor="end"))
        d.append(f'<circle cx="{W - 26 - DG.width(status, 20, 1) - 14:.1f}" cy="{sy - 8}" r="6" '
                 f'fill="{C["palha"] if active else C["osso2"]}"/>')
    x0, max_w = 104, W - 26 - stat_w - 36 - 104
    name, nsz = DG.fit(slot["repo"].upper(), 38, max_w - 150, min_size=28)
    d.append(svg.text(x0, 60, name, DG, nsz, C["sinal"]))
    d.append(svg.text(x0 + DG.width(name, nsz) + 14, 60, slot["type"].upper(), ZB, 17, C["osso2"], ls=1.5))
    en, esz = ZB.fit(slot["blurb"]["en"], 24, max_w, min_size=19)
    d.append(svg.text(x0, 98, en, ZB, esz, C["osso"]))
    pt, psz = ZM.fit(slot["blurb"]["pt"], 19, max_w, min_size=17)
    d.append(svg.text(x0, 126, pt, ZM, psz, C["osso2"]))

    sw = DG.width(status, 36, 1)
    p.append(f'<rect width="{W}" height="{H}" fill="url(#dots-bone)" opacity=".5"/>')
    p.append(f'<polygon points="{pts([(0, 0), (96, 0), (80, H), (0, H)])}" fill="{C["capa"]}"/>')
    p.append(svg.title_text(44, baseline(0, H, DG, 60), str(number), DG, 60, anchor="middle", shadow=3))
    p.append(f'<circle cx="{W - 26 - sw - 24:.1f}" cy="{baseline(8, 70, DG, 36) - 13:.1f}" r="9" '
             f'fill="{C["palha"] if active else C["osso2"]}"/>')
    p.append(svg.text(W - 26, baseline(8, 70, DG, 36), status, DG, 36, C["osso"], ls=1, anchor="end"))
    name, nsz = DG.fit(slot["repo"].upper(), 52, W - 26 - sw - 54 - 118, min_size=40)
    p.append(svg.text(118, baseline(8, 70, DG, nsz), name, DG, nsz, C["sinal"]))
    p.append(svg.text(118, 130, f"PUSHED {fmt_day(slot['pushed'])}", CB, 34, C["palha"]))
    commits = f"{slot['commits']} COMMITS" if slot["commits"] != 1 else "1 COMMIT"
    p.append(svg.text(W - 26, 130, commits, CB, 34, C["osso2"], anchor="end"))
    return svg.render(d, p)


def build_locked(slot, classified):
    H = 128
    name = "???" if classified else slot["name"]
    svg = Svg(H, "Classified loadout slot" if classified else f"Locked loadout slot: {name}", bg=C["sumi"], edge=(C["capa"], 4))
    svg.need("hatch", "tape")
    d, p = [], []
    word, sub = ("CLASSIFIED", "confidencial") if classified else ("LOCKED", "coming soon, em breve")
    for g in (d, p):
        g.append(f'<rect width="{W}" height="{H}" fill="url(#hatch)"/>')
        g.append(f'<polygon points="{pts([(0, 0), (86, 0), (70, H), (0, H)])}" fill="url(#tape)"/>')
        g.append(f'<rect x="20" y="{H / 2 - 24}" width="44" height="48" fill="{C["sumi"]}"/>')
        g.append(icon("lock", 26, H / 2 - 16, 32, C["osso"]))
    d.append(svg.text(W - 26, 60, word, DG, 28, C["osso"], ls=2, anchor="end"))
    d.append(svg.text(W - 26, 90, sub, ZM, 18, C["osso2"], anchor="end"))
    avail = W - 26 - max(DG.width(word, 28, 2), ZM.width(sub, 18)) - 32 - 104
    dname, dsz = DG.fit(name.upper(), 36, avail, min_size=28)
    d.append(svg.text(104, 56, dname, DG, dsz, C["osso2"]))
    if classified:
        d.append(f'<rect x="104" y="72" width="210" height="20" fill="{C["sumi3"]}"/>')
        d.append(f'<rect x="322" y="72" width="96" height="20" fill="{C["sumi3"]}"/>')
        d.append(f'<rect x="104" y="98" width="140" height="16" fill="{C["sumi3"]}"/>')
    else:
        cat = slot["category"]
        en, esz = ZB.fit(cat["en"], 22, avail, min_size=18)
        d.append(svg.text(104, 88, en, ZB, esz, C["osso"]))
        pt, psz = ZM.fit(cat["pt"], 18, avail, min_size=17)
        d.append(svg.text(104, 112, pt, ZM, psz, C["osso2"]))
    ww = DG.width(word, 36, 1)
    p.append(svg.text(W - 26, baseline(0, H, DG, 36), word, DG, 36, C["osso"], ls=1, anchor="end"))
    pname, psz = DG.fit(name.upper(), 52, W - 26 - ww - 30 - 104, min_size=31)
    p.append(svg.text(104, baseline(0, H, DG, psz), pname, DG, psz, C["osso2"]))
    return svg.render(d, p)


# ---------------------------------------------------------------- readme

def build_readme(profile, rows, stats, slots, contracts):
    login = profile["login"]
    q = {chr(34): "&quot;"}

    def a(text):
        return escape(text, q)

    hero_alt = (f"Cover art with the name {login.upper()}, {profile['alias']['en'].lower()} "
                f"({profile['alias']['pt']}). {profile['tagline']['en']} PT: {profile['tagline']['pt']}")
    ab = profile["about"]
    now_txt = "; ".join(f"{n['name']}, {n['en']}" for n in ab["now"])
    about_alt = (f"About, as a manga page. Who: {ab['who']['en']} PT: {ab['who']['pt']} Now forging: {now_txt}. "
                 f"{ab['favorite']['label_en'].capitalize()}: {ab['favorite']['name']} "
                 f"({ab['favorite']['label_pt']}: {ab['favorite']['name']}).")
    arsenal = profile["arsenal"]
    groups = [arsenal[:3]] + [arsenal[i:i + 3] for i in range(3, len(arsenal), 3)]
    lines = [f'<img src="assets/hero.svg" alt="{a(hero_alt)}">',
             f'<img src="assets/about.svg" alt="{a(about_alt)}">',
             f'<a href="{profile["contact_issue"]}"><img src="assets/contact.svg" '
             f'alt="Got a contract? Open an issue. PT: tem um contrato? abre uma issue."></a>',
             "",
             f'<img src="assets/arsenal.svg" alt="Arsenal: {len(arsenal)} weapons, every one a tool I really use.">']
    start = 1
    for k, grp in enumerate(groups):
        desc = "; ".join(f"{start + j:02d} {it['tech']}, {it['weapon']['en']}, used in {it['used']}" for j, it in enumerate(grp))
        lines.append(f'<img src="assets/arsenal-{k + 1}.svg" alt="{a(desc)}">')
        start += len(grp)
    lines.append("")
    done = sum(1 for c in contracts if c["done"])
    lines.append(f'<img src="assets/contracts.svg" alt="Contracts: {done} fulfilled, {len(contracts) - done} in progress.">')
    cards = []
    for c in contracts:
        if c["done"]:
            s = c["signed"]
            alt = (f"Contract number {c['no']}, fulfilled: {c['title']}, {c['type']['en']}. Target: {c['target']['en']}. "
                   f"PT: {c['target']['pt']}. Signed {s.day} {MONTHS[s.month - 1].title()} {s.year}. Opens the repo.")
            cards.append(f'<a href="https://github.com/{login}/{c["repo"]}"><img src="assets/{contract_file(c, contracts)}" '
                         f'width="{CARD_W}" alt="{a(alt)}"></a>')
        else:
            alt = (f"Contract in progress: {c['title']}, {c['type']['en']}. Target: {c['target']['en']}. "
                   f"PT: {c['target']['pt']}. Sealed until it opens.")
            cards.append(f'<img src="assets/{contract_file(c, contracts)}" width="{CARD_W}" alt="{a(alt)}">')
    lines += cards
    feed_alt = "; ".join(row_alt(r) for r in rows) or "no public action yet this season"
    hud_alt = f"Mission log of {login}, newest first: {feed_alt}. PT: {profile['hud_subtitle_pt']}."
    last = f", last seen {fmt_day_long(stats['last_seen'])}" if stats["last_seen"] else ""
    win = stats["window"]
    played = sum(1 for wk in win if wk in stats["weeks_played"])
    player_alt = (f"Mercenary file of {login}, with portrait. {profile['tagline']['en']} "
                  f"Season {stats['season']}: {stats['days']} days played, {stats['contributions']} contributions{last}. "
                  f"Active in {played} of the last {len(win)} weekly rounds.")
    locked_n = len(profile["locked_slots"]) + profile.get("classified_slots", 0)
    lines += ["", f'<img src="assets/hud.svg" alt="{a(hud_alt)}">', f'<img src="assets/player.svg" alt="{a(player_alt)}">', "",
              f'<h3><img src="assets/loadout.svg" alt="Loadout: {len(slots)} unlocked, {locked_n} locked"></h3>', ""]
    for i, s in enumerate(slots, 1):
        alt = (f"Slot {i}: {s['repo']}, {s['type']}. {s['blurb']['en']} PT: {s['blurb']['pt']} "
               f"Last push {fmt_day_long(s['pushed'])}, {s['commits']} commits. Opens the repo.")
        lines.append(f'<a href="https://github.com/{login}/{s["repo"]}"><img src="assets/slot-{s["repo"]}.svg" alt="{a(alt)}"></a>')
    for i, s in enumerate(profile["locked_slots"], 1):
        alt = f"Locked slot: {s['name']}, {s['category']['en']}. PT: {s['category']['pt']}. Coming soon."
        lines.append(f'<img src="assets/slot-locked-{i}.svg" alt="{a(alt)}">')
    for j in range(profile.get("classified_slots", 0)):
        n = len(profile["locked_slots"]) + j + 1
        lines.append(f'<img src="assets/slot-locked-{n}.svg" alt="Classified slot: a project that is not public yet.">')

    tpl = (ROOT / "scripts" / "readme.template.md").read_text(encoding="utf-8")
    return tpl.replace("{{BODY}}", "\n".join(lines)).replace("{{CONTACT}}", profile["contact_issue"])


# ---------------------------------------------------------------- main

def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old != content:
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"updated {path.relative_to(ROOT)}")


def main():
    global TOKEN
    TOKEN = token()
    profile = json.loads((ROOT / "profile.json").read_text(encoding="utf-8"))
    now = datetime.now(BRT)
    rows, stats, slots, contracts = collect(profile, now)

    print(f"season {stats['season']} round {stats['round']}/{stats['rounds_total']} | days played {stats['days']} | "
          f"contributions {stats['contributions']} (private {stats['restricted']}, calendar {stats['calendar_total']}) | "
          f"weeks played {len(stats['weeks_played'])} | last seen {stats['last_seen']}")
    for r in rows:
        print("  feed:", row_alt(r))

    out = {
        "hero.svg": build_hero(profile),
        "about.svg": build_about(profile),
        "contact.svg": build_contact(profile),
        "arsenal.svg": build_arsenal_head(profile),
        "contracts.svg": build_contracts_head(contracts),
        "hud.svg": build_hud(profile, rows),
        "player.svg": build_player(profile, stats),
    }
    arsenal = profile["arsenal"]
    groups = [arsenal[:3]] + [arsenal[i:i + 3] for i in range(3, len(arsenal), 3)]
    start = 1
    for k, grp in enumerate(groups):
        out[f"arsenal-{k + 1}.svg"] = build_arsenal_strip(grp, start, signature=(k == 0))
        start += len(grp)
    for c in contracts:
        out[contract_file(c, contracts)] = build_contract(c)
    locked = profile["locked_slots"]
    classified = profile.get("classified_slots", 0)
    out["loadout.svg"] = build_loadout_head(len(slots), len(locked) + classified)
    for i, s in enumerate(slots, 1):
        out[f"slot-{s['repo']}.svg"] = build_slot(s, i, now)
    for i, s in enumerate(locked, 1):
        out[f"slot-locked-{i}.svg"] = build_locked(s, False)
    for j in range(classified):
        out[f"slot-locked-{len(locked) + j + 1}.svg"] = build_locked(None, True)

    for name, content in out.items():
        write(ASSETS / name, content)
    for stale in ASSETS.glob("*.svg"):
        if stale.name not in out:
            stale.unlink()
            print(f"removed {stale.relative_to(ROOT)}")
    write(ROOT / "README.md", build_readme(profile, rows, stats, slots, contracts))


if __name__ == "__main__":
    main()
