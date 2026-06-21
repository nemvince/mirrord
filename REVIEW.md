# Mirrord — Comprehensive Review & Roadmap

> **Date:** 2026-06-21
> **Scope:** Full codebase audit (Python app, templates, Docker, CI/CD, config)
> **Application:** Self-hosted Linux distribution mirror sync server (FastAPI + APScheduler + rsync/ftpsync)

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Fix Status](#2-fix-status)
3. [Critical Issues](#3-critical-issues)
4. [High-Severity Issues](#4-high-severity-issues)
5. [Medium-Severity Issues](#5-medium-severity-issues)
6. [Low-Severity Issues](#6-low-severity-issues)
7. [Feature Roadmap](#7-feature-roadmap)

---

## 1. Executive Summary

**mirrord** is a well-architected Python application for managing Linux distribution mirrors. The plugin system is clean, the UI is functional, and the hot-reload capability is a nice touch. However, the codebase has several **thread-safety bugs** that will manifest under concurrent load, **potential injection vectors** in the sync plugins, and **missing infrastructure hardening** (running as root, unpinned CI actions, no tests).

### Issue Count by Severity

| Severity | Count |
|----------|-------|
| 🔴 Critical | 6 |
| 🟠 High | 15 |
| 🟡 Medium | 25 |
| 🟢 Low | 18 |
| **Total** | **64** |

---

## 2. Fix Status

### ✅ Top 5 Fixes — COMPLETED

| # | Fix | Status | Files Changed |
|---|-----|--------|---------------|
| 1 | **`get_config()` deadlock** — inlined `Config()` construction instead of calling `load_config()` which re-acquired the same non-reentrant lock | ✅ Fixed | `app/config.py` |
| 2 | **Input validation in `arch_rsync.py`** — added `_validate_config()` that validates `source_url` scheme (must be rsync/https/http), `lastupdate_url` scheme, `bwlimit` (non-negative int), and `excludes` (non-empty strings) | ✅ Fixed | `app/plugins/arch_rsync.py` |
| 3 | **Shell injection in `ftpsync.py`** — added `_conf_value()` escaping function that escapes `\\`, `"`, `$`, and backticks in values interpolated into ftpsync config files. Applied to all interpolated values in `_write_config()`. Added `_validate_config()` for host, rsync_path, and bwlimit. | ✅ Fixed | `app/plugins/ftpsync.py` |
| 4 | **`self.plugins` race condition** — added `_plugins_lock` for thread-safe access. All reads use `_get_plugins_snapshot()`. `_reload_engine()` builds locally and swaps atomically. `_plugin_locks` no longer cleared during reload (prevents orphaned lock references). Replaced bare `_started` bool with `threading.Event`. Added 64KB read-size limit on control socket. Added `scheduler.remove_job()` error handling. | ✅ Fixed | `app/sync_engine.py` |
| 5 | **Container runs as root** — added non-root `appuser` with `USER` directive, set ownership on `/app`, `/var/lock/mirrord`, `/tmp/mirrord`. Added `HEALTHCHECK`. Added `apt-get` cache cleanup. | ✅ Fixed | `Dockerfile` |
| 6 | **GitHub Actions pinned to SHAs** — all 7 actions across both workflows pinned to full commit SHA digests with version comments | ✅ Fixed | `.github/workflows/docker-publish.yml`, `.github/workflows/lint.yml` |

---

## 3. Critical Issues

### 3.1 — ~~Deadlock in `get_config()`~~ ✅ FIXED
- **File:** `app/config.py:73-78`
- **Problem:** `get_config()` acquired `_config_lock`, then called `load_config()` which acquired the same non-reentrant `threading.Lock`. Immediate deadlock.
- **Fix:** Inlined `Config()` construction inside `get_config()`.

### 3.2 — ~~Command Injection in `arch_rsync.py`~~ ✅ MITIGATED
- **File:** `app/plugins/arch_rsync.py`
- **Problem:** `source_url` from YAML config could contain malicious values.
- **Fix:** Added `_validate_config()` with scheme allowlist validation. Since the URL is passed as a single argument to `subprocess.Popen` (not through shell), direct command injection is not possible. The validation adds defense-in-depth.

### 3.3 — ~~Shell Injection in `ftpsync.py`~~ ✅ FIXED
- **File:** `app/plugins/ftpsync.py`
- **Problem:** Config values like `host`, `rsync_path`, `mirrorname` were interpolated into a shell-sourced config file without escaping. A value containing `"`, `$`, or backticks could inject arbitrary shell commands.
- **Fix:** Added `_conf_value()` that escapes `\\`, `"`, `$`, and backticks. Applied to all interpolated values in `_write_config()`.

### 3.4 — ~~Unsynchronized `self.plugins` List Mutation~~ ✅ FIXED
- **File:** `app/sync_engine.py:154,186-242`
- **Problem:** `self.plugins` was read by many methods without any lock, while `_reload_engine()` called `self.plugins.clear()` and re-populated it.
- **Fix:** Added `_plugins_lock`, `_get_plugins_snapshot()`, and `plugins` property. All reads go through the snapshot. `_reload_engine()` builds locally and swaps atomically.

### 3.5 — ~~Orphaned Lock References During Reload~~ ✅ FIXED
- **File:** `app/sync_engine.py:125-132`
- **Problem:** `_reload_engine()` cleared `_plugin_locks` while running sync threads held references to old locks.
- **Fix:** `_plugin_locks` is no longer cleared during reload. Locks are reused when a slug survives the reload.

### 3.6 — ~~Container Runs as Root~~ ✅ FIXED
- **File:** `Dockerfile`
- **Fix:** Added `useradd -r appuser`, `USER appuser`, directory ownership, and `HEALTHCHECK`.

---

## 4. High-Severity Issues

### Thread Safety & Concurrency

| # | File | Line(s) | Issue | Status |
|---|------|---------|-------|--------|
| 4.1 | `app/sync_engine.py` | 32-35 | `_started` flag check-then-set not atomic | ✅ Fixed (Event) |
| 4.2 | `app/models.py` | 55,65,73 | `_save()` called **outside** the lock in `success()`, `failed()`, `skipped()` | 🔴 Open |
| 4.3 | `app/models.py` | 75-77 | `set_progress()` writes without acquiring `_lock` | 🔴 Open |
| 4.4 | `app/download_stats.py` | 48-63 | `save()` reads fields without holding the lock | 🔴 Open |
| 4.5 | `app/download_stats.py` | 94-100 | `record()` and `save()` are separate calls — updates can be lost | 🔴 Open |

### Data Integrity

| # | File | Line(s) | Issue | Status |
|---|------|---------|-------|--------|
| 4.6 | `app/models.py` | 85-98 | Non-atomic file writes — process kill mid-write corrupts stats JSON | 🔴 Open |
| 4.7 | `app/download_stats.py` | 53-61 | Same non-atomic write issue for download stats | 🔴 Open |
| 4.8 | `app/models.py` | 99-100 | `except OSError: pass` silently swallows errors | 🔴 Open |

### Security

| # | File | Line(s) | Issue | Status |
|---|------|---------|-------|--------|
| 4.9 | `app/sync_engine.py` | 279-286 | Control socket had no read-size limit — OOM risk | ✅ Fixed (64KB cap) |
| 4.10 | CI workflows | All | GitHub Actions pinned by mutable tag only | ✅ Fixed (SHA digests) |

### Reliability

| # | File | Line(s) | Issue | Status |
|---|------|---------|-------|--------|
| 4.11 | `app/sync_engine.py` | 125-146 | Exception after `plugins.clear()` left engine broken | ✅ Fixed (local build + swap) |
| 4.12 | `app/sync_engine.py` | 139 | `scheduler.remove_job()` could raise `JobLookupError` | ✅ Fixed (try/except) |
| 4.13 | `app/sync_engine.py` | 55-59 | `stop()` doesn't wait for in-flight sync threads | 🔴 Open |
| 4.14 | `app/download_stats.py` | 30 | `top_files` dict grows unboundedly (memory leak) | 🔴 Open |

### Infrastructure

| # | File | Line(s) | Issue | Status |
|---|------|---------|-------|--------|
| 4.15 | Dockerfile | 1,12 | Unpinned builder/runtime images | 🔴 Open |
| 4.16 | Dockerfile | 26,34 | Unpinned `uv` image | 🔴 Open |
| 4.17 | CI/CD | — | No container image vulnerability scanning | 🔴 Open |
| 4.18 | CI/CD | — | No image signing | 🔴 Open |
| 4.19 | `.gitignore` | — | Missing `.env`, `.env.*`, `*.pem`, `*.key` | 🔴 Open |
| 4.20 | `.dockerignore` | 7 | References old `docker-compose.yml` — actual `compose.yml` NOT excluded | 🔴 Open |

---

## 5. Medium-Severity Issues

### Security

| # | File | Issue |
|---|------|-------|
| 5.1 | `app/sync_engine.py:251-262` | Unix socket TOCTOU race — symlink attack on shared `/tmp` |
| 5.2 | `app/sync_engine.py` | Control socket has no authentication |
| 5.3 | `app/templates/browse.html:104` | XSS via JavaScript context injection — `browse_slug` not escaped in JS |
| 5.4 | `app/config.py:35` | `ServerConfig.host` defaults to `0.0.0.0` |
| 5.5 | `config.example.yaml:53` | `ignore_release_gpg: true` disables GPG verification |
| 5.6 | Dockerfile | `curl` and `openssl` in production image increase attack surface |

### Performance & Reliability

| # | File | Issue |
|---|------|-------|
| 5.7 | `app/sync_engine.py:27,95-108` | `_config_mtime` data race (safe on CPython GIL, not on PyPy/nogil) |
| 5.8 | `app/sync_engine.py:76` | No slug collision detection |
| 5.9 | `app/config.py:84-86` | `reload_config` holds lock during file I/O |
| 5.10 | `app/plugins/arch_rsync.py` | Character-by-character stdout reading is slow |
| 5.11 | `app/plugins/ftpsync.py` | Character-by-character stdout reading (same issue) |
| 5.12 | `app/models.py:99-100` | `load()` doesn't acquire `_lock` |
| 5.13 | `app/download_stats.py:65-76` | `load()` can overwrite in-flight data |
| 5.14 | `app/download_stats.py:62-63` | `save()` silently swallows all OS errors |

### Infrastructure

| # | File | Issue |
|---|------|-------|
| 5.15 | `compose.yml:3` | Image tag `:latest` — unpredictable deploys |
| 5.16 | `compose.yml` | No resource limits, healthcheck, or security_opt |
| 5.17 | CI | No dependency caching |
| 5.18 | `pyproject.toml:6-12` | Loose dependency ranges |
| 5.19 | `.dockerignore` | Missing `.github/`, `.env*` |
| 5.20 | `config.example.yaml:15` | `lock_dir: "/tmp/mirrord"` — world-readable |

---

## 6. Low-Severity Issues

| # | File | Issue |
|---|------|-------|
| 6.1 | `app/config.py:25` | Unfriendly `KeyError` when plugin `type` is missing |
| 6.2 | `app/config.py:36` | No port validation |
| 6.3 | `app/models.py:34` | `_stats_path` is a "private" field leaked into a dataclass |
| 6.4 | `app/download_stats.py:118-121` | `load_all()` is a documented no-op |
| 6.5 | `app/plugins/base.py` | DRY violations across plugins — common subprocess patterns not extracted |
| 6.6 | `app/plugins/arch_rsync.py` | `lastupdate` TLS verification not enforced when `tls: true` |
| 6.7 | `app/plugins/local_dir.py` | `du` subprocess has no timeout |
| 6.8 | `app/plugins/local_dir.py` | Target directory existence not checked at init time |
| 6.9 | `app/cli.py:13,44-45` | Socket not used as context manager |
| 6.10 | `app/cli.py` | CLI unbounded response — no max size for socket reads |
| 6.11 | `app/main.py:71` | Module-level side effects on import |
| 6.12 | `app/main.py:33-34` | Config loaded twice |
| 6.13 | `app/main.py:55-59` | Static/template directory existence not checked |
| 6.14 | `app/sync_engine.py:155-157,246` | Daemon threads prevent graceful shutdown |
| 6.15 | `Dockerfile` | No `apt-get clean` / cache removal |
| 6.16 | `compose.dev.yml:7` | Dev port bound to `0.0.0.0` |
| 6.17 | `.gitignore` | Missing `*.log`, `.idea/`, `.DS_Store` |
| 6.18 | `pyproject.toml` | No test dependencies, no security linting |

---

## 7. Feature Roadmap

### Phase 1 — Foundation (Weeks 1-3)
> *Fix critical bugs and establish engineering foundations*

- [x] **Fix all 🔴 Critical issues** (deadlock, injection, synchronization, root container, CI pinning)
- [ ] **Add unit test framework** — `pytest` + `pytest-asyncio` + `httpx` for FastAPI testing
- [ ] **Add integration tests** — Docker Compose test environment with mock rsync server
- [ ] **Add security linting** — `bandit` for Python, `pip-audit` for dependency CVEs
- [ ] **Fix remaining 🟠 High issues** (thread safety in models, atomic writes, memory leak)

### Phase 2 — Reliability & Observability (Weeks 4-6)
> *Make it production-ready*

- [ ] **Structured logging** — `structlog` or `loguru` with JSON output for containers
- [ ] **Health check endpoint** — `GET /health` returning system status
- [ ] **Metrics endpoint** — Prometheus format (sync duration, bytes transferred, disk usage)
- [ ] **Graceful shutdown** — SIGTERM handler that waits for in-flight syncs
- [ ] **Config validation on startup** — Validate all plugin configs before starting
- [ ] **Database backend for stats** — Replace JSON files with SQLite

### Phase 3 — User Experience (Weeks 7-9)
> *Make it pleasant to use and manage*

- [ ] **Authentication & authorization** — API keys, basic auth, or OIDC
- [ ] **Sync history dashboard** — Timeline of sync runs, duration charts, error log viewer
- [ ] **Real-time notifications** — Email, webhooks (Slack, Discord, generic HTTP)
- [ ] **Configuration web UI** — Edit config.yaml through the web interface
- [ ] **Improved directory browser** — Search, sort, package index preview

### Phase 4 — Scale & Advanced Features (Weeks 10-14)
> *Handle larger deployments and more use cases*

- [ ] **Multi-node mirroring** — Primary/secondary topology with centralized dashboard
- [ ] **Additional mirror types** — yum/createrepo, aptly, container-image, generic-rsync
- [ ] **Bandwidth management** — Global limits, time-of-day scheduling, per-client rate limiting
- [ ] **Mirror health verification** — Post-sync checksums, upstream freshness monitoring
- [ ] **REST API v2** — Full CRUD for plugins, sync job management, OpenAPI spec

### Phase 5 — Ecosystem (Ongoing)
> *Build community and integrations*

- [ ] **Plugin SDK** — Document and formalize the plugin API for third-party plugins
- [ ] **Terraform/Ansible modules** — Infrastructure-as-code support
- [ ] **Kubernetes operator** — CRD-based mirror management
- [ ] **Grafana dashboard** — Pre-built template for Prometheus metrics
- [ ] **Documentation site** — Quick-start guides, architecture docs, plugin dev guide
