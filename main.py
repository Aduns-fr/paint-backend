"""paint game backend. turns a search query into paintable pixel data.

GET /pixelate?q=husky&size=32&colors=24&catalog=animals
-> { "width": 32, "height": 32, "palette": [[r,g,b], ...], "grid": [[1-based palette index, ...], ...] }

the roblox game (ImageService) calls this and renders the grid as a color-by-number canvas.
catalogs:
  animals -> iNaturalist (free, no key) with a wikipedia fallback
  games   -> RAWG (needs RAWG_KEY env var, free tier is fine)
"""

import asyncio
import os
import re
import time
import unicodedata
from io import BytesIO

import httpx
from fastapi import FastAPI, HTTPException, Query
from PIL import Image, ImageEnhance, ImageOps

app = FastAPI()

# in-memory TTL cache so repeated searches don't re-fetch or re-pixelate. this is what lets a
# cheap Render plan handle real traffic — most searches are for the same popular animals.
_CACHE = {}
_CACHE_TTL = 6 * 3600
# each /pixelate entry holds a full 160x160 grid (~0.7 MB as nested lists), so this cap is a
# hard memory budget, not just a hit-rate knob. 120 keeps us comfortably under a 512 MB instance.
_CACHE_MAX = 120


def cache_get(key):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    return None


def cache_put(key, value):
    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        del _CACHE[oldest]
    _CACHE[key] = (time.time(), value)

# wikipedia blocks generic UAs, they want a descriptive one
UA = {"User-Agent": "paint-game-backend/1.0 (roblox paint game; contact: erioluwaaduleye@gmail.com)"}
RAWG_KEY = os.environ.get("RAWG_KEY", "")


async def find_animal_image(q: str) -> str | None:
    async with httpx.AsyncClient(timeout=10, headers=UA, follow_redirects=True) as client:
        # wikipedia first: handles breeds and common names kids actually type (husky, corgi...)
        try:
            r = await client.get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{httpx.URL(q).path or q}",
                params={"redirect": "true"},
            )
            if r.status_code == 200:
                thumb = r.json().get("thumbnail", {}).get("source")
                if thumb:
                    return thumb
        except Exception:
            pass
        # then iNaturalist: photos for basically every real species on earth
        try:
            r = await client.get(
                "https://api.inaturalist.org/v1/taxa",
                params={"q": q, "per_page": 5},
            )
            for taxon in r.json().get("results", []):
                photo = taxon.get("default_photo") or {}
                url = photo.get("medium_url")
                if url:
                    return url
        except Exception:
            pass
    return None


async def find_game_image(q: str) -> str | None:
    if not RAWG_KEY:
        return None
    async with httpx.AsyncClient(timeout=10, headers=UA, follow_redirects=True) as client:
        try:
            r = await client.get(
                "https://api.rawg.io/api/games",
                params={"key": RAWG_KEY, "search": q, "page_size": 1},
            )
            results = r.json().get("results", [])
            if results:
                return results[0].get("background_image")
        except Exception:
            pass
    return None


# catalogs that are real people (Paint a Celebrity / Paint a Footballer share one pipeline).
PEOPLE_CATALOGS = {"celebrity", "celebrities", "footballer", "footballers", "people", "person"}


def is_people(catalog: str) -> bool:
    return (catalog or "").lower() in PEOPLE_CATALOGS


# a famous person's commons files / article images include tons of stuff that ISN'T a photo of
# them: their car, boots, jersey, statue, signature, graffiti, childhood home, a license plate,
# fan crowds, artwork. we reject any file whose name contains one of these WHOLE words. (whole-word
# match via tokenising, so "art" doesn't nuke "Bart" and "car" doesn't nuke "Oscar".)
_PEOPLE_REJECT = {
    "logo", "signature", "firma", "autograph", "wax", "waxwork", "tussaud", "statue",
    "sculpture", "bust", "mural", "graffiti", "caricature", "cartoon", "art", "sketch",
    "drawing", "painting", "poster", "magazine", "cover", "trophy", "stamp", "banner",
    "plaque", "tattoo", "meme", "diagram", "map", "chart", "svg", "icon", "collage",
    "montage", "timeline", "badge", "crest", "flag", "medal", "coin", "boot", "boots",
    "cleat", "cleats", "shoe", "shoes", "jersey", "shirt", "kit", "sign", "signage",
    "billboard", "stadium", "stade", "arena", "mosaic", "replica", "museum", "exhibit",
    "figurine", "toy", "doll", "grave", "gravestone", "memorial", "monument", "building",
    "car", "plate", "license", "vanity", "fan", "fans", "hinchas", "street", "casa",
    "predio", "paseo", "house", "academy", "masia",
}


def person_photo_ok(url: str) -> bool:
    low = url.lower()
    if not low.endswith((".jpg", ".jpeg", ".png")):
        return False
    toks = set(re.split(r"[^a-z0-9]+", _ascii(url)))
    return not (toks & _PEOPLE_REJECT)


# a wikipedia short-description almost always states the occupation for a real person
# ("Argentine footballer", "American singer-songwriter"). we use that to CONFIRM a search hit
# is actually a person, so a query like "apple" resolves to nothing instead of a logo.
_PERSON_WORDS = (
    "footballer", "soccer", "player", "athlete", "sprinter", "singer", "rapper",
    "musician", "songwriter", "guitarist", "drummer", "actor", "actress", "model",
    "presenter", "personality", "youtuber", "streamer", "influencer", "comedian",
    "dancer", "boxer", "wrestler", "swimmer", "cyclist", "golfer", "racing driver",
    "tennis", "basketball", "baseball", "cricketer", "rugby", "entertainer", "producer",
    "author", "writer", "director", "gymnast", "skater", "quarterback", "coach",
    "manager", "politician", "born ", "singer and", "actor and", "rapper and",
)


def _ascii(s: str) -> str:
    """lower-case + strip accents so 'Beyoncé' matches a 'Beyonce_2018.jpg' filename."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)
    ).lower()


# brand/ToS safety for a kids' Roblox game: never resolve people whose wikipedia description
# marks them as adult-industry or violent-crime figures. checked against BOTH the query and the
# resolved page's description, so neither a direct search nor a sneaky alias gets through.
_BLOCKED_WORDS = (
    "pornograph", "porn", "adult film", "adult actress", "adult actor", "adult entertainer",
    "adult model", "onlyfans", "erotic", "stripper", "sex worker", "escort", "camgirl",
    "cam girl", "hentai", "xxx", "nude", "playmate", "penthouse",
    "serial killer", "murderer", "terrorist", "war criminal", "sex offender", "rapist",
    "cult leader", "drug lord", "mobster", "gangster",
)


def blocked_text(s: str) -> bool:
    low = _ascii(s)
    return any(b in low for b in _BLOCKED_WORDS)


def looks_like_person(description: str) -> bool:
    d = (description or "").lower()
    if not d:
        return False
    if "disambiguation" in d or "may refer to" in d:
        return False
    return any(k in d for k in _PERSON_WORDS)


async def resolve_person(client, q: str):
    """turn a typed name into the RIGHT wikipedia person + their infobox portrait.
    full-text search means 'messi', 'ronaldo', lowercase and small typos all land on the real
    person (the summary/{title} endpoint we used before only worked for exact page titles).
    returns (title, image_url) or (None, None) when nothing looks like a (safe) person."""
    if blocked_text(q):
        return None, None
    try:
        r = await client.get("https://en.wikipedia.org/w/api.php", params={
            "action": "query", "generator": "search", "gsrsearch": q, "gsrlimit": 6,
            "prop": "pageimages|description", "piprop": "original|thumbnail",
            "pithumbsize": 800, "pilimit": 6, "format": "json", "redirects": 1,
        })
        pages = list(r.json().get("query", {}).get("pages", {}).values())
    except Exception:
        return None, None
    pages.sort(key=lambda p: p.get("index", 999))  # search relevance order
    fallback = None  # top image-bearing page with an EMPTY description (obscure but real person)
    for p in pages:
        title = p.get("title", "")
        if title.lower().startswith(("list of", "category:")):
            continue
        img = (p.get("original") or {}).get("source") or (p.get("thumbnail") or {}).get("source")
        if not img:
            continue
        desc = p.get("description", "")
        if blocked_text(title) or blocked_text(desc):
            continue
        if looks_like_person(desc):
            return title, img
        if fallback is None and not desc:
            fallback = (title, img)
    return fallback if fallback else (None, None)


async def find_person_image(q: str) -> str | None:
    """best single portrait for a famous name (freely licensed wikipedia lead image)."""
    async with httpx.AsyncClient(timeout=10, headers=UA, follow_redirects=True) as client:
        _, img = await resolve_person(client, q)
        return img


def pixelate(img_bytes: bytes, size: int, colors: int, focus: str = "center") -> dict:
    img = Image.open(BytesIO(img_bytes))
    # MEMORY GUARD: a full-res wikipedia original can decode to 100+ MB and OOM a 512 MB box.
    # draft() decodes JPEGs at a reduced scale cheaply; thumbnail() then hard-caps the bitmap.
    # we're about to shrink to `size` (<=192) anyway, so 1024px loses nothing visible.
    try:
        img.draft("RGB", (1024, 1024))
    except Exception:
        pass
    img = img.convert("RGB")
    if max(img.size) > 1024:
        img.thumbnail((1024, 1024), Image.LANCZOS)

    # crop to a square so the picture FILLS the whole canvas face (paint boards are square).
    # for faces we bias the crop toward the TOP — portraits put the head high, and a centered
    # crop of a full-body shot would slice the face off. "top" keeps the head in frame.
    w, h = img.size
    side = min(w, h)
    x0 = (w - side) // 2
    if focus == "top":
        y0 = int((h - side) * 0.12)
    else:
        y0 = (h - side) // 2
    img = img.crop((x0, y0, x0 + side, y0 + side))

    # grade it before quantizing so a limited palette reads as a clear, punchy picture instead
    # of muddy grey. autocontrast stretches washed-out wildlife photos across the full range,
    # then we pop saturation and lift contrast a touch. this is the single biggest quality win.
    img = ImageOps.autocontrast(img, cutoff=1)
    img = ImageEnhance.Color(img).enhance(1.45)
    img = ImageEnhance.Contrast(img).enhance(1.12)
    img = ImageEnhance.Brightness(img).enhance(1.03)

    img = img.resize((size, size), Image.LANCZOS)
    img = ImageEnhance.Sharpness(img).enhance(1.4)

    img = img.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)

    raw_palette = img.getpalette()
    pixels = list(img.getdata())

    # count how often each palette slot is used, drop unused slots,
    # and order colors most-used first so color 1 is the big satisfying fill
    used = {}
    for p in pixels:
        used[p] = used.get(p, 0) + 1
    order = sorted(used, key=used.get, reverse=True)
    remap = {old: new + 1 for new, old in enumerate(order)}  # 1-based

    palette = [
        [raw_palette[i * 3], raw_palette[i * 3 + 1], raw_palette[i * 3 + 2]]
        for i in order
    ]
    grid = [
        [remap[pixels[y * size + x]] for x in range(size)]
        for y in range(size)
    ]
    return {"width": size, "height": size, "palette": palette, "grid": grid}


# only fetch images from hosts our own catalogs use
ALLOWED_IMAGE_HOSTS = (
    "upload.wikimedia.org",
    "static.inaturalist.org",
    "inaturalist-open-data.s3.amazonaws.com",
    "live.staticflickr.com",
    "media.rawg.io",
)


def host_allowed(url: str) -> bool:
    try:
        return httpx.URL(url).host in ALLOWED_IMAGE_HOSTS
    except Exception:
        return False


# commons is full of junk that isn't a clean photo of the animal: maps, range charts, logos,
# coats of arms, museum specimens, skeletons, diagrams, stamps. reject those by filename.
_COMMONS_JUNK = (
    "map", "range", "distribution", "locator", "logo", "coat_of_arms", "coat of arms",
    "diagram", "chart", "seal", "flag", "stamp", "icon", "skeleton", "skull", "bone",
    "specimen", "fossil", "illustration", "drawing", "sketch", "painting", "engraving",
    "sign", "label", "graph", "phylogen", "cladogram", "anatomy", "svg",
)


def commons_ok(url: str) -> bool:
    low = url.lower()
    return not any(bad in low for bad in _COMMONS_JUNK)


async def gather_candidates(q: str, catalog: str, n: int) -> list[dict]:
    """collect up to n {title, url} image candidates for a query"""
    out, seen = [], set()

    def add(title, url):
        if url and url not in seen and host_allowed(url):
            seen.add(url)
            out.append({"title": title, "url": url})

    async with httpx.AsyncClient(timeout=10, headers=UA, follow_redirects=True) as client:
        if catalog == "games":
            if RAWG_KEY:
                try:
                    r = await client.get(
                        "https://api.rawg.io/api/games",
                        params={"key": RAWG_KEY, "search": q, "page_size": n},
                    )
                    for g in r.json().get("results", []):
                        add(g.get("name", q), g.get("background_image"))
                except Exception:
                    pass
        elif is_people(catalog):
            # PEOPLE pipeline (celebrities + footballers): freely-licensed photos of the RIGHT
            # person only. step 1: resolve the typed name to the real wikipedia person + portrait.
            title, lead = await resolve_person(client, q)
            if not lead:
                return []  # nothing matched a real person — return nothing, never random pics
            add(title, lead)
            # variety: pull from their WIKIPEDIA ARTICLE images — editors curate real photos of the
            # person there. keep a file only when its name (a) names the person AND (b) isn't an
            # object/artwork. we do NOT use their raw commons category: for a famous person it's
            # mostly memorabilia (their car, boots, statue, signature, childhood home), not them.
            tokens = [_ascii(t) for t in title.replace("-", " ").split() if len(t) >= 4]
            try:
                r = await client.get(
                    f"https://en.wikipedia.org/api/rest_v1/page/media-list/{title}",
                    params={"redirect": "true"},
                )
                if r.status_code == 200:
                    for item in r.json().get("items", []):
                        if item.get("type") != "image":
                            continue
                        srcset = item.get("srcset") or []
                        src = (srcset[-1] if srcset else {}).get("src")  # biggest available thumb
                        if not src:
                            continue
                        if src.startswith("//"):
                            src = "https:" + src
                        low = _ascii(src)
                        if tokens and any(tok in low for tok in tokens) and person_photo_ok(src):
                            add(title, src)
            except Exception:
                pass
        else:
            title = q
            taxon_id = None
            # resolve the taxon so we get its proper common name + can pull its photos.
            # taxa/autocomplete ranks by relevance so "fox" lands on the actual fox, and each
            # taxon carries a curated default photo — the cleanest, most iconic shot we have.
            try:
                r = await client.get(
                    "https://api.inaturalist.org/v1/taxa/autocomplete",
                    params={"q": q, "per_page": 4},
                )
                res = r.json().get("results", [])
                if res:
                    taxon_id = res[0].get("id")
                    title = res[0].get("preferred_common_name") or res[0].get("name") or q
                    # pull the curated default photo from every close taxon match first
                    for t in res:
                        photo = t.get("default_photo") or {}
                        add(t.get("preferred_common_name") or t.get("name") or title,
                            (photo.get("medium_url") or "").replace("/square.", "/medium."))
            except Exception:
                pass
            # wikipedia summary: reliable, iconic hero shot for the common name
            try:
                r = await client.get(
                    f"https://en.wikipedia.org/api/rest_v1/page/summary/{q}",
                    params={"redirect": "true"},
                )
                if r.status_code == 200:
                    j = r.json()
                    add(j.get("title", q), j.get("thumbnail", {}).get("source"))
            except Exception:
                pass
            # the deep well: top-voted research-grade observation photos. this is where the
            # VARIETY comes from — dozens of real, verified photos of the species from every
            # angle. grab up to 2 photos per observation and page deep so scrolling never runs dry.
            try:
                params = {
                    "photos": "true", "per_page": 50,
                    "order_by": "votes", "order": "desc",
                    "quality_grade": "research",
                }
                if taxon_id:
                    params["taxon_id"] = taxon_id
                else:
                    params["taxon_name"] = q
                r = await client.get("https://api.inaturalist.org/v1/observations", params=params)
                for obs in r.json().get("results", []):
                    for p in (obs.get("photos") or [])[:2]:
                        u = p.get("url")
                        if u:
                            add(title, u.replace("/square.", "/medium."))
            except Exception:
                pass
            # wikimedia commons last, and only clean photos — filtered hard against junk
            # (maps, diagrams, specimens). it's the noisiest source so it fills, never leads.
            try:
                r = await client.get(
                    "https://commons.wikimedia.org/w/api.php",
                    params={
                        "action": "query", "generator": "search",
                        "gsrsearch": q, "gsrnamespace": 6, "gsrlimit": 24,
                        "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 600, "format": "json",
                    },
                )
                pages = r.json().get("query", {}).get("pages", {})
                for page in pages.values():
                    ii = (page.get("imageinfo") or [{}])[0]
                    url = ii.get("thumburl") or ""
                    if url.lower().endswith((".jpg", ".jpeg", ".png")) and commons_ok(url):
                        add(title, url)
            except Exception:
                pass
    return out[:n]


@app.get("/search")
async def search_route(
    q: str = Query(..., min_length=3, max_length=80),
    catalog: str = Query("animals"),
    n: int = Query(6, ge=1, le=12),
    offset: int = Query(0, ge=0, le=60),
    size: int = Query(40, ge=16, le=64),
    colors: int = Query(12, ge=4, le=24),
):
    """returns candidate images WITH small pixelated previews, all in one call.
    offset pages through candidates so the game can infinite-scroll results."""
    ckey = f"s:{catalog}:{q.lower()}:{offset}:{n}:{size}:{colors}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached
    focus = "top" if is_people(catalog) else "center"
    candidates = (await gather_candidates(q, catalog, offset + n))[offset:]
    # fetch + pixelate the previews CONCURRENTLY (was sequential — 6 round-trips in a row was
    # the slow part of every search). semaphore bounds concurrent decodes so memory stays flat.
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(timeout=12, headers=UA, follow_redirects=True) as client:
        async def preview(c):
            async with sem:
                try:
                    r = await client.get(c["url"])
                    if r.status_code != 200:
                        return None
                    d = pixelate(r.content, size, colors, focus)
                    d["title"] = c["title"]
                    d["url"] = c["url"]
                    return d
                except Exception:
                    return None

        results = [d for d in await asyncio.gather(*(preview(c) for c in candidates)) if d]
    if results:
        cache_put(ckey, results)
    return results


@app.get("/health")
async def health():
    return {"ok": True, "build": "r13-hardened"}


@app.get("/pixelate")
async def pixelate_route(
    q: str = Query("", max_length=80),
    url: str = Query("", max_length=500),
    size: int = Query(32, ge=8, le=192),
    colors: int = Query(24, ge=2, le=48),
    catalog: str = Query("animals"),
):
    # url comes from a prior /search selection; q is the direct-search fallback
    if url:
        if not host_allowed(url):
            raise HTTPException(400, "image host not allowed")
    elif q:
        if catalog == "games":
            url = await find_game_image(q)
        elif is_people(catalog):
            url = await find_person_image(q)
        else:
            url = await find_animal_image(q)
    if not url:
        raise HTTPException(404, "no image found for that search")

    focus = "top" if is_people(catalog) else "center"
    ckey = f"p:{url}:{size}:{colors}:{focus}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached

    async with httpx.AsyncClient(timeout=15, headers=UA, follow_redirects=True) as client:
        r = await client.get(url)
        if r.status_code != 200:
            raise HTTPException(502, "image fetch failed")
        img_bytes = r.content

    try:
        result = pixelate(img_bytes, size, colors, focus)
        cache_put(ckey, result)
        return result
    except Exception:
        raise HTTPException(500, "couldn't process that image")
