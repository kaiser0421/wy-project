"""Checkout simulator and merchant-facing intent/refund API."""

from __future__ import annotations

import asyncio
import hmac
import os
import re
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Security
from fastapi import Path as PathParam
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.routing import APIRoute
from fastapi.security import APIKeyHeader
from pydantic import HttpUrl, ValidationError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.requests import Request

from paymock.delivery import run
from paymock.logging import open_service_log
from paymock.models import ID, Health, Intent, IntentInput, Outcome, Refund, RefundInput
from paymock.store import Problem, Store

MerchantId = Annotated[UUID, PathParam(description="Merchant attempt ID.", examples=[ID])]
CheckoutId = Annotated[UUID, PathParam(description="Provider checkout ID.", examples=[ID])]
_DEV_PROVIDER_TOKEN = "dev-provider-token-change-me"  # noqa: S105 - development sentinel
_DEV_WEBHOOK_SECRET = "dev-insecure-change-me"  # noqa: S105 - development sentinel
_MERCHANT_TOKEN = APIKeyHeader(
    name="X-Payment-Provider-Token",
    description="Shared credential for merchant intent and refund operations.",
    auto_error=False,
)
_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


def problem(status: int, code: str, title: str) -> JSONResponse:
    return JSONResponse(
        {
            "type": f"https://paymock.example/problems/{code}",
            "status": status,
            "code": code,
            "title": title,
            "retryable": status == 503,
        },
        status_code=status,
        media_type="application/problem+json",
        headers={"WWW-Authenticate": "ServiceToken"} if status == 401 else None,
    )


def responses(**codes: str) -> dict[int | str, dict[str, Any]]:
    return {
        int(status.removeprefix("e")): {
            "description": code,
            "content": {
                "application/problem+json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string", "enum": code.split(", ")},
                        },
                    }
                }
            },
        }
        for status, code in {"e422": "validation-failed", "e503": "server-busy", **codes}.items()
    }


def _callback_url() -> str:
    configured = os.environ["PAYMOCK_CALLBACK_URL"]
    try:
        url = HttpUrl(configured)
    except ValidationError:
        raise RuntimeError("PAYMOCK_CALLBACK_URL must be a valid HTTP(S) URL") from None
    if url.username or url.password or url.fragment:
        raise RuntimeError("PAYMOCK_CALLBACK_URL must omit credentials and fragments")
    return str(url)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    path = Path(os.getenv("PAYMOCK_STORE_PATH", "/tmp/ticketing-paymock.sqlite3"))  # noqa: S108
    secret = os.environ["PAYMENT_WEBHOOK_SECRET"]
    token = os.environ["PAYMENT_PROVIDER_TOKEN"]
    if not token.strip() or token == secret:
        raise RuntimeError(
            "PAYMENT_PROVIDER_TOKEN must be nonempty and distinct from PAYMENT_WEBHOOK_SECRET"
        )
    if os.getenv("APP_ENV") == "production":
        if token in {_DEV_PROVIDER_TOKEN, _DEV_WEBHOOK_SECRET}:
            raise RuntimeError("PAYMENT_PROVIDER_TOKEN must be set to a non-default value")
        if not secret.strip() or secret == _DEV_WEBHOOK_SECRET:
            raise RuntimeError("PAYMENT_WEBHOOK_SECRET must be set to a non-default value")
    app.state.merchant_token = token.encode()
    store = Store(path, max(0, float(os.getenv("PAYMOCK_DELAY_CALLBACK_S", "0"))), _callback_url())
    await asyncio.to_thread(store.initialize)
    app.state.store = store
    with open_service_log(os.getenv("LOG_DIR")) as service_log:
        app.state.service_log = service_log
        if os.getenv("PAYMOCK_DROP_CALLBACKS") == "1":
            yield
            return
        worker = asyncio.create_task(
            run(store, secret, os.getenv("PAYMOCK_DUPLICATE_CALLBACKS") == "1", service_log)
        )
        try:
            yield
        finally:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker


def create_app() -> FastAPI:
    app = FastAPI(title="Payment Provider Simulator", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    app.add_exception_handler(Problem, problem_handler)
    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(sqlite3.Error, storage_handler)
    app.middleware("http")(log_request)
    return app


async def problem_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, Problem)
    return problem(exc.status, exc.code, exc.title)


async def validation_handler(_: Request, _exc: Exception) -> JSONResponse:
    return problem(422, "validation-failed", "Request validation failed")


async def storage_handler(_: Request, _exc: Exception) -> JSONResponse:
    return problem(503, "server-busy", "Payment store unavailable")


def _store(request: Request) -> Store:
    store: Store = request.app.state.store
    return store


def _request_id(request: Request) -> str | None:
    value = request.headers.get("X-Request-ID", "")
    return value if _REQUEST_ID.fullmatch(value) else None


async def log_request(request: Request, call_next: RequestResponseEndpoint) -> Response:
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        # The route template omits the checkout token and any query string.
        route = request.scope.get("route")
        path = route.path if isinstance(route, APIRoute) else None
        if path != "/healthz/live":
            request.app.state.service_log.write(
                "request.completed",
                level="error" if status >= 500 else "info",
                request_id=_request_id(request),
                method=request.method,
                path=path,
                status=status,
            )
    return response


def require_merchant(
    request: Request, token: Annotated[str | None, Security(_MERCHANT_TOKEN)]
) -> None:
    if token is None or not hmac.compare_digest(token.encode(), request.app.state.merchant_token):
        raise Problem(401, "invalid-service-token", "Merchant authentication required")


router = APIRouter()
_MERCHANT_AUTH = [Depends(require_merchant)]


@router.get(
    "/healthz/live",
    response_model=Health,
    summary="Check simulator liveness",
    description="Read-only internal liveness probe available to the service network.",
    responses={},
)
async def live() -> Health:
    return Health()


@router.post(
    "/v1/intents",
    dependencies=_MERCHANT_AUTH,
    response_model=Intent,
    summary="Create or retrieve an intent",
    description=(
        "Authenticated merchants can reuse a merchant payment ID with the same inputs. "
        "Signed callbacks go to the configured simulator callback endpoint."
    ),
    responses=responses(e401="invalid-service-token", e409="intent-key-reused"),
)
async def create_intent(body: IntentInput, request: Request) -> Intent:
    return await asyncio.to_thread(_store(request).create, body, _request_id(request))


@router.get(
    "/v1/intents/{merchant_id}",
    dependencies=_MERCHANT_AUTH,
    response_model=Intent,
    summary="Get an intent by merchant ID",
    description="Authenticated merchants can poll for the current outcome and its stable event ID.",
    responses=responses(e401="invalid-service-token", e404="intent-not-found"),
)
async def lookup_intent(merchant_id: MerchantId, request: Request) -> Intent:
    return await asyncio.to_thread(_store(request).lookup, merchant_id, _request_id(request))


@router.post(
    "/v1/intents/{merchant_id}/cancel",
    dependencies=_MERCHANT_AUTH,
    response_model=Intent | None,
    summary="Close a merchant intent",
    description=(
        "Authenticated merchants can idempotently close checkout, including while creation is "
        "in flight. A completed capture is returned as recorded."
    ),
    responses=responses(e401="invalid-service-token"),
)
async def cancel_intent(merchant_id: MerchantId, request: Request) -> Intent | None:
    return await asyncio.to_thread(_store(request).cancel, merchant_id, _request_id(request))


@router.get(
    "/pay/{intent_id}",
    response_class=HTMLResponse,
    response_model=None,
    summary="Open checkout",
    description="Anyone with the checkout URL can view and use the simulator's payment form.",
    responses=responses(e404="intent-not-found"),
)
async def checkout_page(intent_id: CheckoutId, request: Request) -> HTMLResponse:
    intent = await asyncio.to_thread(
        _store(request).checkout, intent_id, _request_id(request), None
    )
    if intent.status == "open":
        content = (
            f'<form method="post" action="/pay/{intent_id}/confirm"><button>Pay</button></form>'
            f'<form method="post" action="/pay/{intent_id}/decline"><button>Decline</button></form>'
        )
    else:
        content = f"<p>Payment status: {intent.status}</p>"
    return HTMLResponse(
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Mock checkout</title>'
        f"<main><h1>Mock checkout</h1><p>TWD {intent.amount_twd}</p>{content}</main></html>",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'none'; form-action 'self'; frame-ancestors 'none'"
            ),
        },
    )


async def resolve_checkout(intent_id: UUID, request: Request, outcome: Outcome) -> Intent:
    intent = await asyncio.to_thread(
        _store(request).checkout,
        intent_id,
        _request_id(request),
        outcome,
    )
    if intent.status != outcome:
        raise Problem(409, "intent-closed", "Payment intent is closed")
    return intent


@router.post(
    "/pay/{intent_id}/confirm",
    response_model=Intent,
    summary="Confirm payment",
    description=(
        "Anyone with the checkout URL can confirm. Concurrent actions serialize and "
        "a successful retry returns the original capture. The deadline prevents new captures."
    ),
    responses=responses(e404="intent-not-found", e409="intent-closed"),
)
async def confirm(intent_id: CheckoutId, request: Request) -> Intent:
    return await resolve_checkout(intent_id, request, "captured")


@router.post(
    "/pay/{intent_id}/decline",
    response_model=Intent,
    summary="Decline payment",
    description=(
        "Anyone with the checkout URL can decline an open intent. Retries replay the outcome."
    ),
    responses=responses(e404="intent-not-found", e409="intent-closed"),
)
async def decline(intent_id: CheckoutId, request: Request) -> Intent:
    return await resolve_checkout(intent_id, request, "declined")


@router.post(
    "/v1/refunds",
    dependencies=_MERCHANT_AUTH,
    response_model=Refund,
    summary="Refund a captured payment",
    description=(
        "Authenticated merchants can issue one full refund per capture. "
        "Concurrent retries with the original refund ID and amount return the same refund."
    ),
    responses=responses(
        e401="invalid-service-token",
        e404="capture-not-found",
        e409="refund-key-reused, capture-already-refunded",
        e422="validation-failed, refund-amount-mismatch",
    ),
)
async def refund(body: RefundInput, request: Request) -> Refund:
    return await asyncio.to_thread(_store(request).refund, body)


app = create_app()


def main() -> None:
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - internal network only
        port=int(os.environ.get("PORT", "8000")),
        loop="uvloop",
        http="httptools",
        access_log=False,
        timeout_graceful_shutdown=10,
        # Longer than nginx's upstream keepalive_timeout (60 s), so nginx closes idle ones first.
        timeout_keep_alive=75,
    )


if __name__ == "__main__":
    main()
