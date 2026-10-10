import asyncio
import contextlib
import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.core.errors import AppError
from app.db.database import SessionLocal
from app.services import fairseat_pricing, membership_service, modules, reminder_service
from app.web import dev_auth, event_pages, member2_pages, module_pages, pages
from app.web.templating import APP_DIR, render

logger = logging.getLogger("whodoo")
settings = get_settings()
settings.check()



async def _renewal_reminder_job(every: int) -> None:
    """Write due membership renewal reminders on a timer. Idempotent, so several processes
    running it at once is harmless (the unique key keeps one row per stage)."""
    while True:
        try:
            async with SessionLocal() as db:
                written = await reminder_service.run_all(db)
                await db.commit()
            if written:
                logger.info("Renewal reminders written: %s", written)
        except Exception:
            logger.exception("Renewal reminder job failed; it will run again")
        await asyncio.sleep(every)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    every = settings.renewal_reminder_interval_seconds
    job = asyncio.create_task(_renewal_reminder_job(every)) if every else None
    yield
    if job:
        job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job


app = FastAPI(
    lifespan=lifespan,
    title="WhoDoo API",
    description="WhoDoo is a club management platform for college students. This service is its finance module: "
    "TrustLedger, Snap-to-Reimburse and MerchFlow.",
    version="0.1.0",
    # Finds out once which optional modules have their tables; every page's menu reads the answer.
    dependencies=[Depends(modules.load)],
)
# Ticket prices take member discounts from MemberCore's records (none until its tables exist).
fairseat_pricing.set_membership_source(membership_service.MemberCoreMemberships())

app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

app.include_router(api_router)
app.include_router(pages.router)
app.include_router(event_pages.router)
app.include_router(module_pages.router)
app.include_router(member2_pages.router)
if settings.demo_auth:
    app.include_router(dev_auth.router)


async def _signed_in_user(request: Request):
    """Who is asking, if anyone, so an error page keeps the person's navigation."""
    try:
        async with SessionLocal() as db:
            return await get_current_user(request, db)
    except Exception:
        return None


async def _error(request: Request, status_code: int, code: str, message: str, details=None):
    """JSON for the API, an HTML page (or a redirect to sign-in) for browser pages."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": {"code": code, "message": message, "details": details}}, status_code=status_code
        )
    if status_code == 401 and settings.demo_auth:
        # Keep the query string: a payment return carries its session_id in it.
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/dev/login?next={quote(target)}", status_code=303)
    user = await _signed_in_user(request)
    return render(request, "error.html", user, status_code=status_code, code=code, message=message)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return await _error(request, exc.status_code, exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    details = [
        {"field": ".".join(str(part) for part in err["loc"] if part != "body"), "message": err["msg"]}
        for err in exc.errors()
    ]
    summary = "; ".join(f"{d['field']}: {d['message']}" if d["field"] else d["message"] for d in details)
    return await _error(request, 422, "validation_error", summary or "Invalid request", details)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return await _error(request, exc.status_code, code, str(exc.detail))


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return await _error(request, 500, "internal_error", "Something went wrong on our side")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}
