# paint game backend

Turns a search ("husky", "red panda") into color-by-number pixel data for the Roblox paint game.

## Run locally

```
pip install -r requirements.txt
uvicorn main:app --reload
```

Test: http://127.0.0.1:8000/pixelate?q=husky&size=32&colors=24

## Deploy free on Render

1. Push this folder to a GitHub repo.
2. render.com → New → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. Free instance type is fine.

Then paste the service URL (e.g. `https://paint-backend.onrender.com`) into
`ReplicatedStorage.Shared.PaintConfig` → `BackendUrl` in Studio.

## Catalogs

- `animals` (default) — iNaturalist, no key needed. Wikipedia fallback.
- `games` — RAWG. Set the `RAWG_KEY` env var on Render (free key from rawg.io/apidocs).
  The video-game version of the game passes `catalog=games`.
