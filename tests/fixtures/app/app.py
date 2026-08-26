import asyncio
import html
import json
import math
import os
import string
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

DATA_PATH = Path(os.environ.get("REPOTRIAL_FIXTURE_DATA_PATH", "/data/items.json"))
TMP_PATH = Path(os.environ.get("REPOTRIAL_FIXTURE_TMP_PATH", "/tmp/repotrial.tmp"))
PROC_STATUS_PATH = Path(
    os.environ.get("REPOTRIAL_FIXTURE_PROC_STATUS", "/proc/self/status")
)


def _startup_delay() -> float:
    raw_delay = os.environ.get("STARTUP_DELAY_S", "0")
    try:
        delay = float(raw_delay)
    except ValueError as error:
        raise RuntimeError(
            "STARTUP_DELAY_S must be a finite non-negative number"
        ) from error
    if not math.isfinite(delay) or delay < 0:
        raise RuntimeError("STARTUP_DELAY_S must be a finite non-negative number")
    return delay


if not os.environ.get("APP_REQUIRED_TOKEN"):
    raise RuntimeError("APP_REQUIRED_TOKEN is required")


STARTUP_DELAY_S = _startup_delay()


@asynccontextmanager
async def _lifespan(_: FastAPI):
    if STARTUP_DELAY_S:
        await asyncio.sleep(STARTUP_DELAY_S)
    yield


app = FastAPI(lifespan=_lifespan)


class ItemInput(BaseModel):
    name: str


class Item(BaseModel):
    id: int
    name: str


def _load_items() -> list[Item]:
    if not DATA_PATH.exists():
        return []
    return [
        Item.model_validate(item)
        for item in json.loads(DATA_PATH.read_text(encoding="utf-8"))
    ]


def _save_items(items: list[Item]) -> None:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(
        json.dumps([item.model_dump() for item in items], separators=(",", ":")),
        encoding="utf-8",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/items")
def list_items() -> list[Item]:
    return _load_items()


@app.post("/items")
def create_item(payload: ItemInput) -> Item:
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name must not be blank")
    items = _load_items()
    item = Item(id=max((existing.id for existing in items), default=0) + 1, name=name)
    items.append(item)
    _save_items(items)
    TMP_PATH.parent.mkdir(parents=True, exist_ok=True)
    TMP_PATH.write_text("created", encoding="utf-8")
    return item


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int) -> Response:
    items = _load_items()
    remaining_items = [item for item in items if item.id != item_id]
    if len(remaining_items) == len(items):
        raise HTTPException(status_code=404, detail="item not found")
    _save_items(remaining_items)
    return Response(status_code=204)


@app.get("/debug/cap-net-raw")
def cap_net_raw() -> dict[str, bool]:
    try:
        for line in PROC_STATUS_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("CapEff:"):
                cap_eff_token = line.split(":", maxsplit=1)[1].strip()
                if not cap_eff_token or not all(
                    character in string.hexdigits for character in cap_eff_token
                ):
                    return {"cap_net_raw": False}
                cap_eff = int(cap_eff_token, 16)
                return {"cap_net_raw": bool(cap_eff & (1 << 13))}
    except (OSError, ValueError):
        pass
    return {"cap_net_raw": False}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    item_rows = "".join(
        (
            "<li>"
            f"{html.escape(item.name)}"
            f'<button aria-label="Delete {html.escape(item.name)}" '
            f'onclick="deleteItem({item.id})">Delete {html.escape(item.name)}</button>'
            "</li>"
        )
        for item in _load_items()
    )
    return f"""<!doctype html>
<html lang="en">
  <body>
    <main>
      <label for="item-name">Item name</label>
      <input id="item-name" name="name" />
      <button aria-label="Create item" onclick="createItem()">Create item</button>
      <ul>{item_rows}</ul>
    </main>
    <script>
      async function createItem() {{
        const name = document.getElementById('item-name').value;
        await fetch('/items', {{method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{name}})}});
        location.reload();
      }}
      async function deleteItem(id) {{
        await fetch('/items/' + id, {{method: 'DELETE'}});
        location.reload();
      }}
    </script>
  </body>
</html>"""
