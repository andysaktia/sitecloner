"""API + server preview SiteCloner."""
import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from .crawler import Crawler, norm, read_errors, read_stats

BASE = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("SITECLONER_DATA", BASE / "data" / "projects"))
DATA.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="SiteCloner", version="0.1.0")
jobs: dict[str, Crawler] = {}


class JobIn(BaseModel):
    url: str
    max_pages: int = Field(200, ge=1, le=200000)
    max_depth: int = Field(3, ge=0, le=30)
    concurrency: int = Field(6, ge=1, le=32)
    delay: float = Field(0.2, ge=0, le=30)
    render_js: bool = False
    strip_js: bool = False
    respect_robots: bool = True
    external_assets: bool = True
    include: list[str] = []
    exclude: list[str] = []
    fresh: bool = False


def launch(name, cfg):
    c = Crawler(DATA / name, cfg)
    jobs[name] = c
    c.task = asyncio.create_task(c.run())


def busy(name):
    return name in jobs and jobs[name].state in ("running", "finalizing")


def release(name):
    c = jobs.pop(name, None)
    if c:
        c.stop = True
        c.db.close()


@app.post("/api/jobs")
async def create(j: JobIn):
    raw = j.url.strip()
    u = norm(raw if "://" in raw else "https://" + raw)
    if not u:
        raise HTTPException(400, "Alamat tidak valid. Contoh: https://contoh.com")
    name = re.sub(r"[^\w.-]", "_", urlsplit(u).netloc)
    if busy(name):
        raise HTTPException(409, "Situs ini sedang di-clone")
    if j.fresh:
        release(name)
        shutil.rmtree(DATA / name, ignore_errors=True)
    cfg = j.model_dump(exclude={"fresh"}) | {"url": u}
    (DATA / name).mkdir(parents=True, exist_ok=True)
    (DATA / name / "config.json").write_text(json.dumps(cfg, indent=2))
    release(name)
    launch(name, cfg)
    return {"name": name}


@app.get("/api/projects")
def projects():
    out = []
    for f in sorted(DATA.glob("*/config.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        name, s = f.parent.name, read_stats(f.parent / "crawl.db")
        c = jobs.get(name)
        state = c.state if c else ("done" if s["pages_done"] and not s["pending"] else "stopped")
        out.append({"name": name, "url": json.loads(f.read_text())["url"], "state": state,
                    "stats": s, "error": c.error if c else None,
                    "ready": (f.parent / "mirror").exists()})
    return out


@app.post("/api/projects/{name}/stop")
def stop(name: str):
    if name in jobs:
        jobs[name].stop = True
    return {"ok": True}


@app.post("/api/projects/{name}/resume")
async def resume(name: str):
    f = DATA / name / "config.json"
    if not f.exists():
        raise HTTPException(404)
    if busy(name):
        raise HTTPException(409, "Masih berjalan")
    release(name)
    launch(name, json.loads(f.read_text()))
    return {"ok": True}


@app.delete("/api/projects/{name}")
def delete(name: str):
    release(name)
    shutil.rmtree(DATA / name, ignore_errors=True)
    return {"ok": True}


@app.get("/api/projects/{name}/errors")
def errors(name: str):
    return read_errors(DATA / name / "crawl.db")


@app.get("/api/projects/{name}/download")
def download(name: str):
    mirror = DATA / name / "mirror"
    if not mirror.exists():
        raise HTTPException(404, "Mirror belum tersedia")
    z = shutil.make_archive(str(Path(tempfile.gettempdir()) / f"{name}-mirror"), "zip", mirror)
    return FileResponse(z, filename=f"{name}-mirror.zip")


@app.get("/preview/{name}")
def preview_root(name: str):
    return RedirectResponse(f"/preview/{name}/")


@app.get("/preview/{name}/{path:path}")
def preview(name: str, path: str = ""):
    root = (DATA / name / "mirror").resolve()
    f = (root / (path or "index.html")).resolve()
    if f.is_dir():
        f = f / "index.html"
    if root not in f.parents or not f.is_file():
        raise HTTPException(404, "Halaman tidak ada di mirror")
    return FileResponse(f)


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")
