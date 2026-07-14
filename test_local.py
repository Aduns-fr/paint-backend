# quick local test: real iNaturalist fetch -> pixelate, no server needed
import asyncio

import httpx

from main import UA, find_animal_image, pixelate


async def test():
    url = await find_animal_image("husky")
    print("image url:", url)
    if not url:
        return
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=UA) as c:
        r = await c.get(url)
        print("fetch status:", r.status_code, "bytes:", len(r.content))
        data = pixelate(r.content, 32, 24)
        print("grid:", data["width"], "x", data["height"], "| palette colors:", len(data["palette"]))
        print("palette[0..2]:", data["palette"][:3])
        print("row1[0..7]:", data["grid"][0][:8])


asyncio.run(test())
