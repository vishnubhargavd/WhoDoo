import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.errors import AppError
from app.web import dev_auth, pages
from app.web.templating import APP_DIR, render

logger = logging.getLogger("whodoo")
settings = get_settings()
settings.check()

app = FastAPI(
    title="WhoDoo API",
    description="WhoDoo is a club management platform for college students. This service is its finance module: "
    "TrustLedger, Snap-to-Reimburse and MerchFlow.",
    version="0.1.0",
)

app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

app.include_router(api_router)
app.include_router(pages.router)
if settings.demo_auth:
    app.include_router(dev_auth.router)


def _error(request: Request, status_code: int, code: str, message: str, details=None):
    """JSON for the API, an HTML page (or a redirect to sign-in) for browser pages."""
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"error": {"code": code, "message": message, "details": details}}, status_code=status_code
        )
    if status_code == 401 and settings.demo_auth:
        return RedirectResponse(f"/dev/login?next={quote(request.url.path)}", status_code=303)
    return render(request, "error.html", status_code=status_code, code=code, message=message)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    return _error(request, exc.status_code, exc.code, exc.message, exc.details)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    details = [
        {"field": ".".join(str(part) for part in err["loc"] if part != "body"), "message": err["msg"]}
        for err in exc.errors()
    ]
    summary = "; ".join(f"{d['field']}: {d['message']}" if d["field"] else d["message"] for d in details)
    return _error(request, 422, "validation_error", summary or "Invalid request", details)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
    return _error(request, exc.status_code, code, str(exc.detail))


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _error(request, 500, "internal_error", "Something went wrong on our side")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}
