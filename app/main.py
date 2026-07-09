from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.bootstrap import ensure_admin_user
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.app_state import get_time_override
from app.routers import admin, attendance, auth, debug, offices, reports, rosters
from app.timeutil import set_time_override

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncSessionLocal() as db:
        await ensure_admin_user(db)
    # Scheduler is intentionally NOT started here. In Phase 1 it runs as a separate
    # process (app/scheduler_main.py, gated by RUN_SCHEDULER) so gunicorn workers never
    # double-run the jobs. Wired up in Step 5.
    yield


app = FastAPI(title="Office Attendance POC", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


if settings.dev_mode:
    # DEV-only: load the app_state time override into the request's ContextVar so now_utc()
    # honours /debug/set-time. Never mounted in production, so zero cost there.
    @app.middleware("http")
    async def _apply_time_override(request, call_next):
        async with AsyncSessionLocal() as db:
            dt = await get_time_override(db)
        set_time_override(dt)
        return await call_next(request)


app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(rosters.router, prefix="/api/rosters", tags=["rosters"])
app.include_router(offices.router, prefix="/api/admin/offices", tags=["offices"])
app.include_router(attendance.router, prefix="/api/attendance", tags=["attendance"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(reports.router, prefix="/api/admin/reports", tags=["reports"])
if settings.dev_mode:
    app.include_router(debug.router, prefix="/api/debug", tags=["debug"])


@app.get("/api/health")
async def health():
    return {"status": "ok"}


# Serves the built frontend (frontend/dist) when present, so the whole app can ship as a
# single deployable. In local dev, frontend/dist doesn't exist (Vite's dev server handles
# the frontend instead), so this block is skipped entirely.
FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"

if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")

    # index.html names the content-hashed bundle, and sw.js is the PWA's precache manifest.
    # If either is cached, the browser keeps running an old build after a rebuild — the app
    # never learns a newer bundle exists. Files under /assets are hash-named, so they don't
    # need this (a new build means a new URL).
    NEVER_CACHE = {"index.html", "sw.js", "registerSW.js", "manifest.webmanifest"}

    def _serve(path: Path) -> FileResponse:
        headers = {"Cache-Control": "no-cache"} if path.name in NEVER_CACHE else None
        return FileResponse(path, headers=headers)

    def _safe_candidate(full_path: str) -> Path | None:
        """Resolve a request path inside dist/, or None. Uvicorn hands us the raw target
        without collapsing '..', so an unchecked join would escape dist/ and happily serve
        backend/.env (JWT_SECRET, DATABASE_URL)."""
        try:
            candidate = (FRONTEND_DIST / full_path).resolve()
        except (OSError, ValueError):
            return None
        if not candidate.is_relative_to(FRONTEND_DIST.resolve()):
            return None
        return candidate if candidate.is_file() else None

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        if full_path:
            candidate = _safe_candidate(full_path)
            if candidate is not None:
                return _serve(candidate)
        return _serve(FRONTEND_DIST / "index.html")
