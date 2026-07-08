from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.bootstrap import ensure_admin_user
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.routers import admin, attendance, auth, checks, debug, networks, reports, schedule, wfh
from app.scheduler import start_scheduler, stop_scheduler

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with AsyncSessionLocal() as db:
        await ensure_admin_user(db)
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Office Attendance POC", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(attendance.router, prefix="/api/attendance", tags=["attendance"])
app.include_router(checks.router, prefix="/api/checks", tags=["checks"])
app.include_router(networks.router, prefix="/api/admin/office-networks", tags=["office-networks"])
app.include_router(admin.router, prefix="/api/admin", tags=["admin"])
app.include_router(wfh.router, prefix="/api", tags=["wfh"])
app.include_router(schedule.router, prefix="/api", tags=["schedule"])
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

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
