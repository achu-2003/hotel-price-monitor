"""FastAPI application factory.

Assembles the API, the dashboard, error handling, security headers and
observability. Everything with a decision behind it is commented where it
happens rather than summarised here.

The health endpoints live at the root as well as under ``/api/v1`` because
Docker's healthcheck, Caddy and a load balancer all expect ``/health`` and none
of them should need to know the API's version prefix.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import api_router
from app.config import get_settings
from app.core.logging import configure_logging, get_logger, request_id_var
from app.dashboard.routes import router as dashboard_router
from app.db.session import dispose_engines
from app.schemas.common import HealthStatus, ProblemDetail

log = get_logger("app")

PROBLEM_JSON = "application/problem+json"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start-up and shut-down.

    Deliberately does NOT create tables or run migrations. Schema changes
    belong to the ``migrate`` service, which runs once and exits; doing it here
    would mean two API replicas racing each other through the same DDL.
    """
    configure_logging()
    settings = get_settings()

    if settings.sentry_dsn:
        import sentry_sdk

        from app.core.redaction import sentry_before_send

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.app_env,
            traces_sample_rate=0.1,
            # Redaction applies to error reports as well as to logs; an
            # exception's context is one of the easiest places for a
            # credential to escape.
            before_send=sentry_before_send,
            send_default_pii=False,
        )

    log.info("api_starting", env=settings.app_env, timezone=settings.timezone)
    try:
        yield
    finally:
        await dispose_engines()
        log.info("api_stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Hotel Price Monitor",
        version="1.0.0",
        description=(
            "Tracks competitor room prices, detects real changes, and notifies "
            "the person responsible for each hotel."
        ),
        lifespan=lifespan,
        # Interactive docs are for development. In production they describe
        # every endpoint and every field to anyone who finds the URL.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    _install_middleware(app, settings)
    _install_error_handlers(app)

    app.include_router(api_router)
    app.include_router(dashboard_router)

    static_dir = _static_dir()
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/health", response_model=HealthStatus, include_in_schema=False)
    async def root_health():
        """Liveness at the root, for the container healthcheck and the proxy."""
        return HealthStatus(status="ok", checked_at=datetime.now(UTC))

    if settings.metrics_enabled:
        _install_metrics(app)

    return app


#: Dashboard paths reachable while a password change is outstanding. Anything
#: else redirects, so a forced change cannot simply be navigated around.
_PASSWORD_CHANGE_EXEMPT = (
    "/change-password", "/login", "/logout", "/static", "/health", "/api/",
)


def _must_change_password(request: Request) -> bool:
    """Read the ``chg`` claim from the session cookie.

    One HMAC verification, no database round trip. An unreadable or expired
    cookie returns False and the normal auth path issues the 401 or redirect.
    """
    from app.api.deps import SESSION_COOKIE
    from app.core.security import decode_token

    cookie = request.cookies.get(SESSION_COOKIE)
    if not cookie:
        return False
    try:
        return bool(decode_token(cookie).get("chg"))
    except Exception:  # noqa: BLE001 - an invalid cookie is not this check's problem
        return False


def _install_middleware(app: FastAPI, settings) -> None:
    @app.middleware("http")
    async def force_password_change(request: Request, call_next):
        """Hold a browser on the change-password page until it is done.

        The API is exempt: a script authenticating with a bearer token is not
        the thing this protects, and blocking ``/api/v1/auth/change-password``
        would make the requirement impossible to satisfy.
        """
        path = request.url.path
        if not path.startswith(_PASSWORD_CHANGE_EXEMPT) and _must_change_password(request):
            from fastapi.responses import RedirectResponse

            return RedirectResponse(url="/change-password", status_code=303)
        return await call_next(request)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """Attach a request id and set the security headers.

        The request id is bound into a context variable so every log line
        emitted while handling this request carries it — which is what makes a
        user's report of "it failed at 3:12" traceable.
        """
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        # No inline scripts anywhere in the dashboard, so the CSP can be
        # strict. HTMX is served from /static, which is why 'self' suffices.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


def _install_error_handlers(app: FastAPI) -> None:
    """One error shape everywhere: RFC 7807 problem details."""

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        problem = ProblemDetail(
            title=_title_for(exc.status_code),
            status=exc.status_code,
            detail=str(exc.detail) if exc.detail else None,
            instance=str(request.url.path),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=problem.model_dump(exclude_none=True),
            media_type=PROBLEM_JSON,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        problem = ProblemDetail(
            title="Validation failed",
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="One or more fields are invalid.",
            instance=str(request.url.path),
            errors=[
                {
                    "field": ".".join(str(p) for p in error.get("loc", ())[1:]),
                    "message": error.get("msg"),
                    "type": error.get("type"),
                }
                for error in exc.errors()
            ],
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=problem.model_dump(exclude_none=True),
            media_type=PROBLEM_JSON,
        )

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        """Log the detail, return none of it.

        An unhandled exception's message routinely contains a query, a
        connection string, or a fragment of someone's data. It belongs in the
        log, never in the response body.
        """
        log.exception("unhandled_error", path=str(request.url.path), error=str(exc))
        problem = ProblemDetail(
            title="Internal server error",
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Something went wrong. The failure has been logged.",
            instance=str(request.url.path),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=problem.model_dump(exclude_none=True),
            media_type=PROBLEM_JSON,
        )


def _install_metrics(app: FastAPI) -> None:
    """Prometheus metrics, if the instrumentator is installed.

    Optional on purpose: a missing observability package must not stop the
    application from serving prices.
    """
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
    except ImportError:
        log.info("metrics_unavailable", reason="prometheus-fastapi-instrumentator not installed")
        return

    Instrumentator(
        should_group_status_codes=True,
        excluded_handlers=["/health", "/health/ready", "/metrics"],
    ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


def _title_for(status_code: int) -> str:
    return {
        400: "Bad request",
        401: "Not authenticated",
        403: "Forbidden",
        404: "Not found",
        409: "Conflict",
        422: "Validation failed",
        429: "Too many requests",
        502: "Upstream provider failed",
        503: "Service unavailable",
    }.get(status_code, "Error")


def _static_dir():
    from pathlib import Path

    return Path(__file__).parent / "static"


app = create_app()
