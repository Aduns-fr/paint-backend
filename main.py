"""paint game backend. turns a search query into paintable pixel data.

GET /pixelate?q=husky&size=32&colors=24&catalog=animals
-> { "width": 32, "height": 32, "palette": [[r,g,b], ...], "grid": [[1-based palette index, ...], ...] }

the roblox game (ImageService) calls this and renders the grid as a color-by-number canvas.
catalogs:
  animals -> iNaturalist (free, no key) with a wikipedia fallback
  games   -> RAWG (needs RAWG_KEY env var, free tier is fine)
"""

import os
from io import BytesIO

import httpx
from fastapi import FastAPI, HTTPException, Query
from PIL import Image, ImageEnhance

app = FastAPI()

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


def pixelate(img_bytes: bytes, size: int, colors: int) -> dict:
    img = Image.open(BytesIO(img_bytes)).convert("RGB")

    # grade it a little before quantizing: pop the colors, lift contrast, sharpen edges.
    # limited palettes come out flat and muddy without this.
    img = ImageEnhance.Color(img).enhance(1.25)
    img = ImageEnhance.Contrast(img).enhance(1.08)

    # center-crop to a square so the picture FILLS the whole canvas face (paint boards are square)
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    img = img.resize((size, size), Image.LANCZOS)
    img = ImageEnhance.Sharpness(img).enhance(1.3)

    img = img.quantize(colors=colors, method=Image.Quantize.MEDIANCUT)

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
        else:
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
            try:
                r = await client.get(
                    "https://api.inaturalist.org/v1/taxa",
                    params={"q": q, "per_page": n + 2},
                )
                for taxon in r.json().get("results", []):
                    photo = taxon.get("default_photo") or {}
                    name = taxon.get("preferred_common_name") or taxon.get("name") or q
                    add(name, photo.get("medium_url"))
            except Exception:
                pass
            # wikipedia prefix search: several related pages with images (covers breeds etc)
            try:
                r = await client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query", "generator": "prefixsearch", "gpssearch": q,
                        "gpslimit": n + 2, "prop": "pageimages", "piprop": "thumbnail",
                        "pithumbsize": 600, "format": "json",
                    },
                )
                pages = r.json().get("query", {}).get("pages", {})
                for page in sorted(pages.values(), key=lambda p: p.get("index", 99)):
                    thumb = page.get("thumbnail", {}).get("source")
                    add(page.get("title", q), thumb)
            except Exception:
                pass
    return out[:n]


@app.get("/search")
async def search_route(
    q: str = Query(..., max_length=80),
    catalog: str = Query("animals"),
    n: int = Query(6, ge=1, le=12),
    offset: int = Query(0, ge=0, le=60),
    size: int = Query(40, ge=16, le=64),
    colors: int = Query(12, ge=4, le=24),
):
    """returns candidate images WITH small pixelated previews, all in one call.
    offset pages through candidates so the game can infinite-scroll results."""
    candidates = (await gather_candidates(q, catalog, offset + n))[offset:]
    results = []
    async with httpx.AsyncClient(timeout=12, headers=UA, follow_redirects=True) as client:
        for c in candidates:
            try:
                r = await client.get(c["url"])
                if r.status_code != 200:
                    continue
                d = pixelate(r.content, size, colors)
                d["title"] = c["title"]
                d["url"] = c["url"]
                results.append(d)
            except Exception:
                continue
    return results


@app.get("/health")
async def health():
    return {"ok": True}


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
        else:
            url = await find_animal_image(q)
    if not url:
        raise HTTPException(404, "no image found for that search")

    async with httpx.AsyncClient(timeout=15, headers=UA, follow_redirects=True) as client:
        r = await client.get(url)
        if r.status_code != 200:
            raise HTTPException(502, "image fetch failed")
        img_bytes = r.content

    try:
        return pixelate(img_bytes, size, colors)
    except Exception:
        raise HTTPException(500, "couldn't process that image")
