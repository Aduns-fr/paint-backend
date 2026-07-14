# local test for the /search flow: candidates + inline previews + url pixelate
import asyncio

import httpx

from main import UA, gather_candidates, pixelate


async def test():
    cands = await gather_candidates("husky", "animals", 6)
    print("candidates:", [(c["title"], c["url"][:60]) for c in cands])
    if not cands:
        return
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=UA) as c:
        r = await c.get(cands[0]["url"])
        d = pixelate(r.content, 40, 12)
        print("preview:", d["width"], "x", d["height"], "colors:", len(d["palette"]))


asyncio.run(test())
