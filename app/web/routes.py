import os
import shutil
import time
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
)

from app.charts import svg_daily_chart, svg_hbar_chart
from app.geo import GeoIP
from app.sync_engine import SyncEngine

router = APIRouter()

logger = logging.getLogger("mirrord.web")

_engine: SyncEngine | None = None
_geoip = GeoIP()


class RequestLike(Protocol):
    """Structural subset of starlette.Request used for client-IP resolution."""

    @property
    def client(self) -> "_ClientInfo | None": ...

    @property
    def headers(self) -> Mapping[str, str]: ...


class _ClientInfo(Protocol):
    @property
    def host(self) -> str: ...


def set_engine(engine: SyncEngine) -> None:
    global _engine
    _engine = engine


def _strip_port(token: str) -> str:
    """Extract the bare IP from an address token that may carry a port.

    Handles the forms that appear in proxy headers:
      - ``1.2.3.4``            -> ``1.2.3.4``
      - ``1.2.3.4:443``        -> ``1.2.3.4``
      - ``[2001:db8::1]:443``  -> ``2001:db8::1``
      - ``2001:db8::1``        -> ``2001:db8::1`` (bare IPv6, must NOT be split)
    """
    token = token.strip()
    if not token:
        return token
    if token.startswith("["):  # bracketed IPv6 literal, optionally with :port
        return token[1:].split("]", 1)[0]
    # A single colon means IPv4:port; multiple colons means a bare IPv6 address
    # (which we must leave intact).
    if token.count(":") == 1:
        return token.split(":", 1)[0]
    return token


def _client_ip(request: RequestLike) -> str | None:
    """Return the real client IP, honouring proxy headers only from trusted peers.

    If the immediate peer (the TCP client) is a trusted proxy, we unwrap the
    original client address from X-Forwarded-For / X-Real-IP / Forwarded.
    Otherwise those headers are treated as spoofed and the peer IP is used.
    """
    peer = request.client.host if request.client else None
    engine = _engine
    if engine is None or peer is None:
        return peer

    sync_cfg = engine.config.sync
    if not sync_cfg.is_trusted_proxy(peer):
        return peer

    # Walk X-Forwarded-For from right to left, skipping trusted hops, to find
    # the first untrusted IP — that's the real client.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        for hop in reversed(hops):
            ip = _strip_port(hop)
            if not sync_cfg.is_trusted_proxy(ip):
                logger.debug(
                    "Resolved client IP %s from XFF %r via trusted peer %s",
                    ip,
                    xff,
                    peer,
                )
                return ip
        # Every hop was a trusted proxy; fall back to leftmost.
        logger.debug(
            "All XFF hops trusted (%r), falling back to leftmost via peer %s",
            xff,
            peer,
        )
        return _strip_port(hops[0])

    x_real = request.headers.get("x-real-ip")
    if x_real:
        return _strip_port(x_real)

    fwd = request.headers.get("forwarded")
    if fwd:
        # RFC 7239: pick the first "for=" with an IP; strip brackets/port.
        for part in fwd.split(";"):
            part = part.strip()
            if part.lower().startswith("for="):
                token = part[4:].strip().strip('"')
                return _strip_port(token)
    return peer


def _valid_slug(slug: str) -> bool:
    """Reject slugs that contain dots (reserved for file extensions)."""
    return "." not in slug


def _format_size(size: int) -> str:
    if size == 0:
        return "0 B"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    i = 0
    val = float(size)
    while val >= 1024 and i < len(units) - 1:
        val /= 1024
        i += 1
    return f"{val:.1f} {units[i]}"


def _format_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _safe_getsize(path: str) -> int:
    """Return file size, or 0 if the file can't be stat'd (e.g. deleted mid-request)."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _disk_usage_for_plugin(plugin) -> tuple[int, int, int]:
    """Return (total, used, free) for the filesystem containing the plugin's target."""
    try:
        target = str(plugin.target_dir)
        usage = shutil.disk_usage(target)
        return usage.total, usage.used, usage.free
    except Exception:
        return 0, 0, 0


def _browse_context(slug: str, subpath: str) -> dict:
    ctx = {
        "browse_slug": slug,
        "browse_plugin": "",
        "browse_path": "/",
        "browse_entries": [],
        "browse_breadcrumbs": [],
        "browse_error": None,
        "disk_total": 0,
        "disk_used": 0,
        "disk_free": 0,
    }

    if _engine is None:
        ctx["browse_error"] = "Engine not ready"
        return ctx

    plugin = _engine.get_plugin_by_slug(slug)
    if plugin is None:
        ctx["browse_error"] = "Mirror not found"
        return ctx

    ctx["browse_plugin"] = plugin.stats.plugin_name
    ctx["browse_stats"] = plugin.stats.snapshot()
    ctx["browse_dl_stats"] = _engine.get_download_stats().get(slug, {})

    target = plugin.target_dir.resolve()
    if not target.exists():
        ctx["browse_error"] = "Directory does not exist yet"
        return ctx

    rel_path = Path((subpath or "").lstrip("/"))
    requested = (target / rel_path).resolve()

    # Security boundary: prevent directory traversal via `..` components.
    # `relative_to` raises ValueError if `requested` is not under `target`.
    try:
        requested.relative_to(target)
    except ValueError:
        ctx["browse_error"] = "Path is outside mirror root"
        return ctx

    if not requested.exists():
        ctx["browse_error"] = "Path not found"
        return ctx
    if requested.is_file():
        ctx["is_file"] = True
        ctx["file_path"] = str(requested)
        return ctx

    display_path = "/" + str(rel_path) if rel_path != Path(".") else "/"
    ctx["browse_path"] = display_path

    entries = []
    with os.scandir(requested) as it:
        for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat()
            except OSError:
                stat = None
            entry_path = (
                f"/{entry.name}"
                if display_path == "/"
                else f"{display_path}/{entry.name}"
            )
            href = f"/{slug}{quote(entry_path, safe='/')}"
            entries.append(
                {
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": stat.st_size if stat else 0,
                    "size_fmt": _format_size(stat.st_size) if stat else "—",
                    "mtime": stat.st_mtime if stat else 0,
                    "mtime_fmt": _format_mtime(stat.st_mtime) if stat else "—",
                    "href": href,
                }
            )
    ctx["browse_entries"] = entries

    bc = [{"name": plugin.stats.plugin_name, "href": f"/{slug}/"}]
    accum = Path(".")
    for part in rel_path.parts:
        accum = accum / part
        bc.append(
            {
                "name": part,
                "href": f"/{slug}/{quote(str(accum), safe='/')}",
            }
        )
    ctx["browse_breadcrumbs"] = bc

    ctx["disk_total"], ctx["disk_used"], ctx["disk_free"] = _disk_usage_for_plugin(
        plugin
    )
    ctx["next_sync_ts"] = _engine.get_next_sync_time() if _engine else None

    return ctx


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    stats = _engine.get_all_stats() if _engine else []

    next_sync_ts = _engine.get_next_sync_time() if _engine else None

    dl_stats = _engine.get_download_stats() if _engine else {}
    max_dir_size = max((s.dir_size for s in stats), default=0)

    ctx = {
        "now": time.time(),
        "stats": stats,
        "next_sync_ts": next_sync_ts,
        "max_dir_size": max_dir_size,
        "dl_stats": dl_stats,
    }
    return request.app.state.templates.TemplateResponse(request, "index.html", ctx)


@router.get("/api/stats")
async def api_stats():
    if _engine is None:
        return {"plugins": [], "next_sync_ts": None}
    dl_stats = _engine.get_download_stats()
    plugins = []
    for s in _engine.get_all_stats():
        dl = dl_stats.get(s.slug, {})
        plugins.append(
            {
                "name": s.plugin_name,
                "slug": s.slug,
                "type": s.plugin_type,
                "description": s.description,
                "status": s.status.value,
                "last_sync": s.last_sync,
                "last_duration": s.last_duration,
                "last_size_bytes": s.last_size_bytes,
                "last_error": s.last_error,
                "sync_started_at": s.sync_started_at,
                "progress_pct": s.progress_pct,
                "dir_size": s.dir_size,
                "total_syncs": s.total_syncs,
                "total_failures": s.total_failures,
                "total_bytes_transferred": s.total_bytes_transferred,
                "total_downloads": dl.get("total_downloads", 0),
                "total_bytes_served": dl.get("total_bytes_served", 0),
                "last_download": dl.get("last_download"),
                "top_files": dl.get("top_files", []),
            }
        )
    total_downloads = sum(p["total_downloads"] for p in plugins)
    total_bytes_served = sum(p["total_bytes_served"] for p in plugins)
    return {
        "plugins": plugins,
        "next_sync_ts": _engine.get_next_sync_time(),
        "total_downloads": total_downloads,
        "total_bytes_served": total_bytes_served,
    }


@router.get("/stats/", response_class=HTMLResponse)
async def download_stats_page(request: Request):
    if _engine is None or _engine.download_db is None:
        return HTMLResponse("Engine not ready", status_code=503)
    db = _engine.download_db

    overview = db.get_overview()
    daily = db.get_daily_totals(days=30)
    top_files = [f for f in db.get_top_files(limit=15) if f["path"] != "__legacy__"]
    recent = [r for r in db.get_recent(limit=50) if r["path"] != "__legacy__"]
    geo_data = overview["by_geocode"]
    by_slug = overview["by_slug"]

    daily_svg = svg_daily_chart(daily, title="Downloads per Day")
    # Geography and per-mirror charts share styling (size, label width) so the
    # two side-by-side panels line up; only the bar colour differs.
    _side_chart_kw = {"width": 360, "height": 240, "max_label_w": 80}
    geo_svg = svg_hbar_chart(
        geo_data or [{"geocode": "—", "count": 0}],
        label_key="geocode",
        value_key="count",
        title="Downloads by Geography",
        bar_color="#86b300",
        **_side_chart_kw,
    )
    top_svg = svg_hbar_chart(
        top_files,
        label_key="path",
        value_key="count",
        title="Top Files",
        bar_color="#39bae6",
        max_label_w=200,
    )
    all_stats = _engine.get_all_stats() if _engine else []
    slug_labels = {s.slug: s.plugin_name for s in all_stats}

    disk_total = disk_used = disk_free = 0
    if _engine and _engine.plugins:
        disk_total, disk_used, disk_free = _disk_usage_for_plugin(_engine.plugins[0])

    total_syncs = sum(s.total_syncs for s in all_stats)
    total_failures = sum(s.total_failures for s in all_stats)
    total_bytes = sum(s.total_bytes_transferred for s in all_stats)
    now_syncing = sum(1 for s in all_stats if s.status.value == "running")
    next_sync_ts = _engine.get_next_sync_time() if _engine else None

    by_slug_named = [
        {"slug": slug_labels.get(s["slug"], s["slug"]), "count": s["count"]}
        for s in by_slug
    ]

    by_slug_svg = ""
    if by_slug_named:
        by_slug_svg = svg_hbar_chart(
            by_slug_named,
            label_key="slug",
            value_key="count",
            title="Downloads per Mirror",
            bar_color="#ffb454",
            **_side_chart_kw,
        )

    ctx = {
        "now": time.time(),
        "overview": overview,
        "daily_svg": daily_svg,
        "geo_svg": geo_svg,
        "top_svg": top_svg,
        "by_slug_svg": by_slug_svg,
        "recent": recent,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
        "mirrors": len(all_stats),
        "total_syncs": total_syncs,
        "total_failures": total_failures,
        "total_bytes": total_bytes,
        "now_syncing": now_syncing,
        "next_sync_ts": next_sync_ts,
    }
    return request.app.state.templates.TemplateResponse(request, "stats.html", ctx)


@router.get("/api/stats/downloads")
async def api_download_stats(
    slug: str | None = Query(default=None),
    days: int = Query(default=30),
):
    if _engine is None or _engine.download_db is None:
        return {"error": "Engine not ready"}
    db = _engine.download_db
    return {
        "overview": db.get_overview(),
        "daily": db.get_daily_totals(slug=slug, days=days),
        "top_files": db.get_top_files(slug=slug, limit=25),
        "recent": db.get_recent(limit=50),
    }


@router.get("/api/browse/{plugin_name}")
async def api_browse(plugin_name: str, q: str = Query(default="")):
    if _engine is None:
        raise HTTPException(status_code=503, detail="Engine not ready")

    plugin = _engine.get_plugin_by_name(plugin_name)
    if plugin is None:
        raise HTTPException(status_code=404, detail="Plugin not found")

    target = plugin.target_dir.resolve()
    if not target.exists():
        return {
            "path": "/",
            "parent": None,
            "entries": [],
            "error": "Directory does not exist",
        }

    rel_path = Path((q or "").lstrip("/"))
    requested = (target / rel_path).resolve()

    # Security boundary: prevent directory traversal via `..` components.
    try:
        requested.relative_to(target)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path escapes mirror root")

    if not requested.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if requested.is_file():
        raise HTTPException(status_code=400, detail="Path is a file, not a directory")

    entries = []
    with os.scandir(requested) as it:
        for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat()
            except OSError:
                stat = None
            entries.append(
                {
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "size": stat.st_size if stat else 0,
                    "mtime": stat.st_mtime if stat else 0,
                }
            )

    # Compute parent path correctly
    rel_str = str(rel_path)
    if rel_str in ("", "."):
        parent_str = None
    else:
        parent = rel_path.parent
        parent_str = "/" + str(parent) if str(parent) not in ("", ".") else "/"

    return {
        "path": "/" + rel_str if rel_str not in ("", ".") else "/",
        "parent": parent_str,
        "entries": entries,
    }


@router.get("/{slug}", response_class=HTMLResponse)
async def browse_redirect(request: Request, slug: str):
    if not _valid_slug(slug):
        return Response(status_code=404)
    return RedirectResponse(url=f"/{slug}/", status_code=301)


@router.get("/{slug}/", response_class=HTMLResponse)
async def browse_root(request: Request, slug: str):
    if not _valid_slug(slug):
        return Response(status_code=404)
    ctx = _browse_context(slug, "/")
    if ctx.get("is_file"):
        if _engine:
            ip = _client_ip(request)
            geocode = _geoip.lookup(ip) if ip else None
            _engine.record_download(
                slug,
                "/",
                _safe_getsize(ctx["file_path"]),
                ua=request.headers.get("user-agent"),
                geocode=geocode,
            )
        return FileResponse(ctx["file_path"])
    if ctx.get("browse_error"):
        status = 404 if "not found" in ctx["browse_error"].lower() else 500
        return Response(
            ctx["browse_error"], status_code=status, media_type="text/plain"
        )
    ctx["now"] = time.time()
    return request.app.state.templates.TemplateResponse(request, "browse.html", ctx)


@router.get("/{slug}/{rest:path}", response_class=HTMLResponse)
async def browse_path(request: Request, slug: str, rest: str):
    if not _valid_slug(slug):
        return Response(status_code=404)
    ctx = _browse_context(slug, rest)
    if ctx.get("is_file"):
        if _engine:
            ip = _client_ip(request)
            geocode = _geoip.lookup(ip) if ip else None
            _engine.record_download(
                slug,
                "/" + rest,
                _safe_getsize(ctx["file_path"]),
                ua=request.headers.get("user-agent"),
                geocode=geocode,
            )
        return FileResponse(ctx["file_path"])
    if ctx.get("browse_error"):
        status = 404 if "not found" in ctx["browse_error"].lower() else 500
        return Response(
            ctx["browse_error"], status_code=status, media_type="text/plain"
        )
    ctx["now"] = time.time()
    return request.app.state.templates.TemplateResponse(request, "browse.html", ctx)
