# poke both upstream APIs raw to see what they actually return
import asyncio

import httpx


async def test():
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "paint-test/1.0"}) as c:
        r = await c.get("https://api.inaturalist.org/v1/taxa", params={"q": "husky", "per_page": 3})
        print("inat status:", r.status_code)
        for t in r.json().get("results", []):
            photo = t.get("default_photo") or {}
            print("  taxon:", t.get("name"), "| photo:", photo.get("medium_url"))

        r = await c.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "titles": "husky", "prop": "pageimages",
                    "format": "json", "pithumbsize": 600, "redirects": 1},
        )
        print("wiki status:", r.status_code)
        print("wiki pages:", r.json().get("query", {}).get("pages", {}))


asyncio.run(test())
