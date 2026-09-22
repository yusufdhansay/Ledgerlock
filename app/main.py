"""Ledgerlock application entrypoint.

Startup deliberately fails loudly rather than degrading: if MongoDB is
not a transaction-capable replica set, or the required indexes cannot be
created, the process refuses to serve traffic. Both of those are the
foundation of the guarantees in PRD.md, so a deployment that has lost
them should not look healthy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, status
from fastapi.responses import JSONResponse

from app.core import db
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging_config import configure_logging, get_logger
from app.routes import accounts, auth, reconciliation, transactions
from app.services import account_service

logger = get_logger(__name__)

API_TITLE = "Ledgerlock"
API_VERSION = "0.1.0"
API_DESCRIPTION = """
A double-entry bookkeeping API.

Every movement of money is recorded as a matched pair of immutable ledger
entries: a DEBIT on one account and a CREDIT on another, for the same
amount, written in a single MongoDB transaction. An account's balance is
never a stored field; it is computed by aggregating that account's ledger
entries.

**Error shape.** Every error response, without exception, is
`{"error": {"code": "...", "message": "..."}}`. The `code` is a stable,
machine-readable value; match on it rather than on the message.
""".strip()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Connect to MongoDB and prepare the schema before serving traffic."""
    settings = get_settings()
    configure_logging(settings.log_level)

    logger.info(
        "startup_begin",
        extra={"app_env": settings.app_env, "database": settings.mongodb_db_name},
    )

    await db.connect_to_mongo(settings.mongodb_uri, settings.mongodb_db_name)
    await db.initialise_database()

    # One SYSTEM boundary account per supported currency. These are the
    # only accounts allowed to hold a negative balance; they represent
    # value crossing the ledger's boundary with the outside world, and
    # their negative balance equals the total held across USER accounts in
    # that currency. Provisioned here rather than exposed as an API
    # operation, because an account exempt from the overdraft check must
    # not be creatable on request.
    system_accounts = await account_service.ensure_system_accounts(
        settings.supported_currencies
    )
    logger.info(
        "system_accounts_ready",
        extra={"currencies": sorted(system_accounts)},
    )

    logger.info("startup_complete", extra={})
    try:
        yield
    finally:
        await db.close_mongo_connection()
        logger.info("shutdown_complete", extra={})


def create_app() -> FastAPI:
    """Build the application. A factory, so tests can construct instances."""
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        description=API_DESCRIPTION,
        lifespan=lifespan,
    )

    register_exception_handlers(app)
    app.include_router(auth.router)
    app.include_router(accounts.router)
    app.include_router(transactions.router)
    app.include_router(reconciliation.router)

    @app.get("/health", tags=["health"], summary="Liveness probe")
    async def health() -> dict[str, str]:
        """Liveness: is the process up? Does not touch the database.

        Kept dependency-free on purpose. A liveness probe that fails when
        the database is unreachable makes Kubernetes restart healthy
        application pods during a database outage, which turns a partial
        outage into a full one.
        """
        return {"status": "ok", "service": API_TITLE, "version": API_VERSION}

    @app.get("/health/ready", tags=["health"], summary="Readiness probe")
    async def readiness() -> JSONResponse:
        """Readiness: can this instance actually serve requests?

        Pings MongoDB and confirms the connection is still to a replica set
        primary. If it is not, this instance cannot honour the atomicity
        guarantee, so it reports not-ready and Kubernetes takes it out of
        the Service's endpoints instead of sending it traffic.
        """
        try:
            client = db.get_client()
            hello = await client.admin.command("hello")
        except Exception as exc:
            logger.warning(
                "readiness_failed", extra={"exception_type": type(exc).__name__}
            )
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "not_ready", "reason": "database_unreachable"},
            )

        if not hello.get("setName") or not hello.get("isWritablePrimary", False):
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "not_ready",
                    "reason": "no_transaction_capable_primary",
                },
            )

        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ready", "replica_set": hello["setName"]},
        )

    return app


app = create_app()
