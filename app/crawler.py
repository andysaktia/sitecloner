"""Crawler async: domain -> mirror lokal yang bisa dibuka offline.

Alur: crawl (HTML/CSS mentah disimpan di raw/, aset langsung di mirror/)
      -> finalize (semua link ditulis ulang menjadi relatif -> mirror/).
Status antrean disimpan di SQLite (crawl.db) sehingga bisa stop/resume.
"""
import asyncio
import hashlib
import posixpath
import re
import sqlite3
from pathlib import Path
from urllib import robotparser
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; SiteCloner/0.1)"
MAX_BYTES = 60 * 1024 * 1024
ATTRS = {"a": ["href"], "area": ["href"], "link": ["href"], "script": ["src"],
         "img": ["src", "data-src", "srcset", "data-srcset"], "source": ["src", "srcset"],
         "video": ["src", "poster"], "audio": ["src"], "track": ["src"], "embed": ["src"]}
LINK_RELS = {"stylesheet", "icon", "shortcut", "apple-touch-icon", "preload",
             "modulepreload", "manifest", "mask-icon"}
CSS_RE = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)|@import\s+(['"])(.*?)\3""", re.I)
SKIP = ("data:", "javascript:", "mailto:", "tel:", "#", "blob:", "about:")


def norm(url):
    url, _ = urldefrag((url or "").strip())
    try:
        p = urlsplit(url)
        host, port = (p.hostname or "").lower(), p.port
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not host:
        return None
    return urlunsplit((p.scheme, host + (f":{port}" if port and port not in (80, 443) else ""),
                       p.path or "/", p.query, ""))


def same_site(a, b):
    return a.lower().removeprefix("www.") == b.lower().removeprefix("www.")


def local_path(url, page, site):
    p = urlsplit(url)
    path = unquote(p.path)
    if path.endswith("/"):
        path += "index.html"
    elif page and not path.lower().endswith((".html", ".htm")):
        path += ".html"
    path = re.sub(r'[<>:"|?*\\]', "_", path)
    if p.query:
        stem, ext = posixpath.splitext(path)
        path = f"{stem}__{hashlib.md5(p.query.encode()).hexdigest()[:8]}{ext}"
    if not same_site(p.netloc, site):
        path = f"_ext/{re.sub(r'[^\w.-]', '_', p.netloc)}/{path}"
    parts = [s[:120] for s in path.split("/") if s not in ("", ".", "..")]
    return "/".join(parts) or "index.html"


def split_srcset(v):
    return [p.strip().split(None, 1) for p in v.split(",") if p.strip()]


def iter_refs(soup, skip_scripts):
    for tag in soup.find_all(ATTRS):
        if tag.name == "script" and skip_scripts:
            continue
        if tag.name == "link" and not ({r.lower() for r in tag.get("rel", [])} & LINK_RELS):
            continue
        for attr in ATTRS[tag.name]:
            if tag.get(attr):
                yield tag, attr, tag[attr]


def read_stats(dbfile):
    s = dict(pages_done=0, pages_total=0, assets_done=0, assets_total=0, pending=0, errors=0)
    if not Path(dbfile).exists():
        return s
    c = sqlite3.connect(dbfile)
    try:
        for page, status, n in c.execute("SELECT page,status,COUNT(*) FROM urls GROUP BY 1,2"):
            k = "pages" if page else "assets"
            s[k + "_total"] += n
            if status == "done":
                s[k + "_done"] += n
            elif status == "error":
                s["errors"] += n
            elif status in ("pending", "running"):
                s["pending"] += n
        return s
    finally:
        c.close()


def read_errors(dbfile, limit=200):
    c = sqlite3.connect(dbfile)
    try:
        return [{"url": u, "error": e} for u, e in
                c.execute("SELECT url,err FROM urls WHERE status='error' LIMIT ?", (limit,))]
    finally:
        c.close()


class Crawler:
    def __init__(self, root, cfg):
        self.root, self.cfg = Path(root), cfg
        self.raw, self.mirror = self.root / "raw", self.root / "mirror"
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "crawl.db", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("CREATE TABLE IF NOT EXISTS urls(url TEXT PRIMARY KEY, page INT, depth INT,"
                        " status TEXT DEFAULT 'pending', local TEXT, base TEXT, kind TEXT, err TEXT)")
        self.start = norm(cfg["url"])
        self.site = urlsplit(self.start).netloc
        self.state, self.stop, self.error, self.task = "idle", False, None, None
        self.inc = [re.compile(x) for x in cfg.get("include", []) if x.strip()]
        self.exc = [re.compile(x) for x in cfg.get("exclude", []) if x.strip()]
        self.strip = cfg.get("strip_js", False)

    # ---------- antrean ----------
    def set(self, url, status, **kw):
        cols = "".join(f",{k}=?" for k in kw)
        self.db.execute(f"UPDATE urls SET status=?{cols} WHERE url=?", (status, *kw.values(), url))
        self.db.commit()

    def add(self, url, page, depth):
        if self.db.execute("INSERT OR IGNORE INTO urls(url,page,depth) VALUES(?,?,?)",
                           (url, int(page), depth)).rowcount:
            self.q.put_nowait((url, int(page), depth))

    def enqueue(self, ref, base, page, depth):
        ref = (ref or "").strip()
        if not ref or ref.lower().startswith(SKIP):
            return
        u = norm(urljoin(base, ref))
        if not u:
            return
        inside = same_site(urlsplit(u).netloc, self.site)
        if page:
            if not inside or depth > self.cfg["max_depth"]:
                return
            if (self.inc and not any(r.search(u) for r in self.inc)) or any(r.search(u) for r in self.exc):
                return
        elif not inside and not self.cfg["external_assets"]:
            return
        self.add(u, page, depth)

    # ---------- eksekusi ----------
    async def run(self):
        self.state, self.stop, self.error = "running", False, None
        self.q = asyncio.Queue()
        try:
            self.db.execute("UPDATE urls SET status='pending' WHERE status IN ('running','error')")
            self.reserved = self.db.execute(
                "SELECT COUNT(*) FROM urls WHERE page=1 AND status='done'").fetchone()[0]
            for row in self.db.execute("SELECT url,page,depth FROM urls WHERE status='pending'").fetchall():
                self.q.put_nowait(row)
            self.add(self.start, True, 0)
            self.rp = None
            async with httpx.AsyncClient(headers={"User-Agent": UA}, follow_redirects=True, timeout=30,
                                         limits=httpx.Limits(max_connections=64)) as self.client:
                if self.cfg["respect_robots"]:
                    try:
                        r = await self.client.get(urljoin(self.start, "/robots.txt"))
                        if r.status_code == 200:
                            self.rp = robotparser.RobotFileParser()
                            self.rp.parse(r.text.splitlines())
                    except httpx.HTTPError:
                        pass
                await self.start_browser()
                workers = [asyncio.create_task(self.worker()) for _ in range(self.cfg["concurrency"])]
                await self.q.join()
                for w in workers:
                    w.cancel()
                await self.close_browser()
            self.state = "finalizing"
            await asyncio.to_thread(self.finalize)
            if not self.db.execute("SELECT 1 FROM urls WHERE status='done' LIMIT 1").fetchone():
                e = self.db.execute("SELECT err FROM urls WHERE status='error' LIMIT 1").fetchone()
                raise RuntimeError("Halaman awal gagal diunduh" + (f" ({e[0]})" if e else ""))
            self.state = "stopped" if self.stop else "done"
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "error", f"{type(e).__name__}: {e}"

    async def worker(self):
        while True:
            url, page, depth = await self.q.get()
            try:
                if self.stop or (page and self.reserved >= self.cfg["max_pages"]):
                    continue
                if page:
                    self.reserved += 1
                self.set(url, "running")
                await self.fetch(url, page, depth)
                await asyncio.sleep(self.cfg["delay"])
            except Exception as e:  # noqa: BLE001
                self.set(url, "error", err=f"{type(e).__name__}: {e}"[:300])
            finally:
                self.q.task_done()

    async def start_browser(self):
        self.browser = None
        if self.cfg.get("render_js"):
            from playwright.async_api import async_playwright
            self.pw = await async_playwright().start()
            self.browser = await self.pw.chromium.launch()

    async def close_browser(self):
        if self.browser:
            await self.browser.close()
            await self.pw.stop()

    async def render(self, url):
        pg = await self.browser.new_page(user_agent=UA)
        try:
            resp = await pg.goto(url, wait_until="networkidle", timeout=45000)
            await pg.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await pg.wait_for_timeout(500)
            ctype = (resp.headers.get("content-type", "text/html") if resp else "text/html").split(";")[0]
            return pg.url, (await pg.content()).encode(), ctype.lower()
        finally:
            await pg.close()

    async def fetch(self, url, page, depth):
        if page and self.rp and not self.rp.can_fetch(UA, url):
            return self.set(url, "blocked")
        if page and self.browser:
            final, body, ctype = await self.render(url)
        else:
            r, err = None, None
            for i in range(3):
                try:
                    r = await self.client.get(url)
                    break
                except httpx.HTTPError as e:
                    err = e
                    await asyncio.sleep(1 + i)
            if r is None:
                raise err
            if r.status_code >= 400:
                return self.set(url, "error", err=f"HTTP {r.status_code}")
            final, body = str(r.url), r.content
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if len(body) > MAX_BYTES:
            return self.set(url, "error", err="File terlalu besar")
        is_html = ctype in ("text/html", "application/xhtml+xml") or (
            page and not ctype and body[:200].lstrip().lower().startswith((b"<!doctype", b"<html")))
        is_css = ctype == "text/css" or urlsplit(final).path.lower().endswith(".css")
        kind = "html" if is_html else "css" if is_css else "file"
        rel = local_path(url, is_html, self.site)
        dest = (self.raw if kind != "file" else self.mirror) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        base = final
        if is_html and same_site(urlsplit(final).netloc, self.site):
            base = self.extract_html(body, final, depth)
        elif is_css:
            self.css_refs(body.decode("utf-8", "ignore"), final)
        self.set(url, "done", local=rel, base=base, kind=kind)

    # ---------- ekstraksi ----------
    def css_refs(self, text, base):
        for m in CSS_RE.finditer(text):
            self.enqueue(m.group(2) or m.group(4), base, False, 0)

    def extract_html(self, body, base, depth):
        soup = BeautifulSoup(body, "lxml")
        b = soup.find("base", href=True)
        if b:
            base = urljoin(base, b["href"])
        for tag, attr, v in iter_refs(soup, self.strip):
            refs = [p[0] for p in split_srcset(v)] if "srcset" in attr else [v]
            for r in refs:
                self.enqueue(r, base, tag.name in ("a", "area"), depth + 1)
        for t in soup.find_all("style"):
            self.css_refs(t.string or "", base)
        for t in soup.find_all(style=True):
            self.css_refs(t["style"], base)
        return base

    # ---------- penulisan ulang link ----------
    def fixer(self, base, local, mapping):
        here = posixpath.dirname(local) or "."

        def fix(ref):
            r = (ref or "").strip()
            if not r or r.lower().startswith(SKIP):
                return ref
            absu = urljoin(base, r)
            tgt = mapping.get(norm(absu))
            if not tgt:
                return absu
            frag = urlsplit(absu).fragment
            return quote(posixpath.relpath(tgt, here)) + (f"#{frag}" if frag else "")
        return fix

    @staticmethod
    def rewrite_css(text, fix):
        def sub(m):
            if m.group(4) is not None:
                return f"@import {m.group(3)}{fix(m.group(4))}{m.group(3)}"
            return f"url({m.group(1)}{fix(m.group(2))}{m.group(1)})"
        return CSS_RE.sub(sub, text)

    def rewrite_html(self, body, fix):
        soup = BeautifulSoup(body, "lxml")
        for b in soup.find_all("base"):
            b.decompose()
        if self.strip:
            for s in soup.find_all("script"):
                s.decompose()
        for tag, attr, v in iter_refs(soup, False):
            if "srcset" in attr:
                tag[attr] = ", ".join(" ".join([fix(p[0])] + p[1:]) for p in split_srcset(v))
            else:
                tag[attr] = fix(v)
        for t in soup.find_all("style"):
            if t.string:
                t.string = self.rewrite_css(t.string, fix)
        for t in soup.find_all(style=True):
            t["style"] = self.rewrite_css(t["style"], fix)
        for t in soup.find_all(["link", "script"]):
            t.attrs.pop("integrity", None)
            t.attrs.pop("crossorigin", None)
        return soup.encode("utf-8")

    def finalize(self):
        rows = self.db.execute("SELECT url,local,base,kind FROM urls WHERE status='done'").fetchall()
        mapping = {u: loc for u, loc, _, _ in rows}
        for _, local, base, kind in rows:
            if kind == "file":
                continue
            src, dest = self.raw / local, self.mirror / local
            fix = self.fixer(base, local, mapping)
            if kind == "html":
                out = self.rewrite_html(src.read_bytes(), fix)
            else:
                out = self.rewrite_css(src.read_text("utf-8", "ignore"), fix).encode("utf-8")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(out)
