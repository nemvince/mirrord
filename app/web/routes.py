import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.sync_engine import SyncEngine

router = APIRouter()

_engine: Optional[SyncEngine] = None


def set_engine(engine: SyncEngine) -> None:
    global _engine
    _engine = engine


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
        ctx["browse_error"] = "Cannot browse into a file"
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
            entry_path = f"/{entry.name}" if display_path == "/" else f"{display_path}/{entry.name}"
            href = f"/{slug}{quote(entry_path, safe='/')}" if entry.is_dir() else None
            entries.append({
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": stat.st_size if stat else 0,
                "size_fmt": _format_size(stat.st_size) if stat else "—",
                "mtime": stat.st_mtime if stat else 0,
                "mtime_fmt": _format_mtime(stat.st_mtime) if stat else "—",
                "href": href,
            })
    ctx["browse_entries"] = entries

    bc = [{"name": plugin.stats.plugin_name, "href": f"/{slug}/"}]
    accum = Path(".")
    for part in rel_path.parts:
        accum = accum / part
        bc.append({
            "name": part,
            "href": f"/{slug}/{quote(str(accum), safe='/')}",
        })
    ctx["browse_breadcrumbs"] = bc

    ctx["disk_total"], ctx["disk_used"], ctx["disk_free"] = _disk_usage_for_plugin(plugin)
    ctx["next_sync_ts"] = _engine.get_next_sync_time() if _engine else None

    return ctx


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    stats = _engine.get_all_stats() if _engine else []

    # Global disk (first plugin's filesystem)
    disk_total = disk_used = disk_free = 0
    if _engine and _engine.plugins:
        disk_total, disk_used, disk_free = _disk_usage_for_plugin(_engine.plugins[0])

    next_sync_ts = _engine.get_next_sync_time() if _engine else None

    total_syncs = sum(s.total_syncs for s in stats)
    total_failures = sum(s.total_failures for s in stats)
    total_bytes = sum(s.total_bytes_transferred for s in stats)
    now_syncing = sum(1 for s in stats if s.status.value == "running")

    ctx = {
        "now": time.time(),
        "stats": stats,
        "disk_total": disk_total,
        "disk_used": disk_used,
        "disk_free": disk_free,
        "next_sync_ts": next_sync_ts,
        "total_syncs": total_syncs,
        "total_failures": total_failures,
        "total_bytes": total_bytes,
        "now_syncing": now_syncing,
        "max_dir_size": max((s.dir_size for s in stats), default=0),
    }
    return request.app.state.templates.TemplateResponse(request, "index.html", ctx)


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
    ctx["now"] = time.time()
    return request.app.state.templates.TemplateResponse(request, "browse.html", ctx)


@router.get("/{slug}/{rest:path}", response_class=HTMLResponse)
async def browse_path(request: Request, slug: str, rest: str):
    if not _valid_slug(slug):
        return Response(status_code=404)
    ctx = _browse_context(slug, rest)
    ctx["now"] = time.time()
    return request.app.state.templates.TemplateResponse(request, "browse.html", ctx)


@router.get("/api/stats")
async def api_stats():
    if _engine is None:
        return {"plugins": [], "next_sync_ts": None}
    plugins = []
    for s in _engine.get_all_stats():
        plugins.append({
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
        })
    return {
        "plugins": plugins,
        "next_sync_ts": _engine.get_next_sync_time(),
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
        return {"path": "/", "parent": None, "entries": [], "error": "Directory does not exist"}

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
            entries.append({
                "name": entry.name,
                "type": "dir" if entry.is_dir() else "file",
                "size": stat.st_size if stat else 0,
                "mtime": stat.st_mtime if stat else 0,
            })

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
