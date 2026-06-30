import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from app.auth import create_session, verify_password, verify_session
from app.config import Settings
from app.google_store import GoogleStore, StoreError


LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
SESSION_COOKIE = "focus_board_session"
BACKGROUND_ACTIONS = {
    "create",
    "update",
    "remove",
    "merge",
    "undoMerge",
    "bulkUpdateStatus",
}


class LoginBody(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1000)


class ApiBody(BaseModel):
    action: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)


class LoginLimiter:
    def __init__(self, attempts: int = 8, window_seconds: int = 300):
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.history: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            entries = self.history[key]
            while entries and entries[0] < now - self.window_seconds:
                entries.popleft()
            if len(entries) >= self.attempts:
                return False
            entries.append(now)
            return True

    def clear(self, key: str) -> None:
        with self.lock:
            self.history.pop(key, None)


class JobManager:
    def __init__(self, retention_seconds: int = 3600):
        self.retention_seconds = retention_seconds
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def create(self) -> str:
        job_id = str(uuid.uuid4())
        with self.lock:
            self._cleanup()
            self.jobs[job_id] = {
                "status": "queued",
                "createdAt": time.time(),
                "result": None,
                "error": None,
            }
        return job_id

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            self._cleanup()
            job = self.jobs.get(job_id)
            if not job:
                return None
            return {
                "id": job_id,
                "status": job["status"],
                "result": job["result"],
                "error": job["error"],
            }

    def run(self, job_id: str, callback, *args) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job["status"] = "running"
        try:
            result = callback(*args)
        except StoreError as error:
            self._finish(job_id, "failed", error=str(error))
        except Exception:
            LOGGER.exception("Background Google Sheets job failed: %s", job_id)
            self._finish(job_id, "failed", error="The data service failed.")
        else:
            self._finish(job_id, "succeeded", result=result)

    def _finish(
        self, job_id: str, status: str, result: Any = None, error: str | None = None
    ) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job:
                job.update(status=status, result=result, error=error)

    def _cleanup(self) -> None:
        cutoff = time.time() - self.retention_seconds
        expired = [
            job_id
            for job_id, job in self.jobs.items()
            if job["createdAt"] < cutoff and job["status"] in {"succeeded", "failed"}
        ]
        for job_id in expired:
            self.jobs.pop(job_id, None)


def create_app(settings: Settings | None = None, store: Any | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="Focus Board", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = store
    app.state.store_lock = threading.Lock()
    app.state.login_limiter = LoginLimiter()
    app.state.jobs = JobManager()

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if request.url.path.startswith("/api") or request.url.path.startswith("/auth"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/favicon.svg", include_in_schema=False)
    async def favicon():
        return FileResponse(
            ROOT / "app" / "static" / "favicon.svg", media_type="image/svg+xml"
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if _authenticated(request, settings):
            return RedirectResponse("/", status_code=303)
        return FileResponse(ROOT / "app" / "login.html", media_type="text/html")

    @app.post("/auth/login")
    async def login(request: Request, body: LoginBody):
        configuration_error = settings.auth_error()
        if configuration_error:
            return JSONResponse(
                {"ok": False, "error": configuration_error}, status_code=503
            )
        client_key = request.client.host if request.client else "unknown"
        if not app.state.login_limiter.allow(client_key):
            return JSONResponse(
                {"ok": False, "error": "Too many login attempts. Try again later."},
                status_code=429,
            )
        username_matches = __import__("hmac").compare_digest(
            body.username, settings.username
        )
        password_matches = await run_in_threadpool(
            verify_password, body.password, settings.password_hash
        )
        if not username_matches or not password_matches:
            return JSONResponse(
                {"ok": False, "error": "Invalid username or password."}, status_code=401
            )
        app.state.login_limiter.clear(client_key)
        token = create_session(
            settings.username, settings.session_secret, settings.session_hours * 3600
        )
        response = JSONResponse({"ok": True})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/auth/logout")
    async def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/")
    async def dashboard(request: Request):
        if not _authenticated(request, settings):
            return RedirectResponse("/login", status_code=303)
        return FileResponse(ROOT / "legacy" / "Index.html", media_type="text/html")

    @app.post("/api")
    async def api(request: Request, body: ApiBody, background_tasks: BackgroundTasks):
        if not _authenticated(request, settings):
            return JSONResponse(
                {"ok": False, "error": "Authentication required."}, status_code=401
            )
        try:
            active_store = _get_store(app, settings)
            if body.action in BACKGROUND_ACTIONS:
                job_id = app.state.jobs.create()
                background_tasks.add_task(
                    app.state.jobs.run,
                    job_id,
                    active_store.dispatch,
                    body.action,
                    body.payload,
                )
                return JSONResponse(
                    {"ok": True, "data": {"accepted": True, "jobId": job_id}},
                    status_code=202,
                )
            data = await run_in_threadpool(
                active_store.dispatch, body.action, body.payload
            )
            return {"ok": True, "data": data}
        except StoreError as error:
            return JSONResponse({"ok": False, "error": str(error)}, status_code=400)
        except Exception:
            LOGGER.exception("API action failed: %s", body.action)
            return JSONResponse(
                {"ok": False, "error": "The data service failed."}, status_code=500
            )

    @app.get("/api/jobs/{job_id}")
    async def job_status(request: Request, job_id: str):
        if not _authenticated(request, settings):
            return JSONResponse(
                {"ok": False, "error": "Authentication required."}, status_code=401
            )
        job = app.state.jobs.get(job_id)
        if not job:
            return JSONResponse(
                {"ok": False, "error": "Job not found or expired."}, status_code=404
            )
        return {"ok": True, "data": job}

    return app


def _authenticated(request: Request, settings: Settings) -> bool:
    return verify_session(
        request.cookies.get(SESSION_COOKIE), settings.username, settings.session_secret
    )


def _get_store(app: FastAPI, settings: Settings) -> Any:
    if app.state.store is not None:
        return app.state.store
    with app.state.store_lock:
        if app.state.store is None:
            active_store = GoogleStore(settings)
            active_store.setup()
            app.state.store = active_store
    return app.state.store
