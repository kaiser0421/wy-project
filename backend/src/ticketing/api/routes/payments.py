"""Owner checkout requests and the signed provider callback."""

from typing import Annotated

from fastapi import Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ticketing.api.routes.orders import CUSTOMER, Caller, OrderId
from ticketing.api.security import Public, SecureRouter
from ticketing.booking.api_docs import READ_ERRORS, REPLAY_HEADER, WRITE_ERRORS, problem_responses
from ticketing.booking.database import connection, transaction
from ticketing.booking.idempotency import Claim, IdempotencyKey, request_hash
from ticketing.core.errors import ProblemError
from ticketing.core.logging import get_logger
from ticketing.payments.attempts import create_payment, list_attempts
from ticketing.payments.callbacks import apply_event, verify_signature
from ticketing.payments.models import (
    CallbackResult,
    Checkout,
    PaidPayment,
    PaymentAttempt,
    PaymentEvent,
)
from ticketing.payments.provider import Provider

router = SecureRouter(prefix="/api/v1", tags=["payments"])
_MAX_CALLBACK_BYTES = 8192
log = get_logger(__name__)


@router.post(
    "/orders/{order_id}/payments",
    policy=CUSTOMER,
    status_code=201,
    response_model=Checkout,
    summary="Start or resume checkout for an order",
    description=(
        "The owner may request checkout while the hold is active. Concurrent keys use the same "
        "chargeable attempt. Retry the same request with the same Idempotency-Key within 24 "
        "hours to resume that attempt and receive its current result with Idempotent-Replayed: "
        "true. An active checkout returns 201. Replaying the same key after the order was paid "
        "returns 200, and after a terminal attempt returns 402. A new key for an order that is "
        "no longer awaiting payment returns 409 order-not-payable. A declined payment can be "
        "retried with a new key while "
        "the hold remains active. Advisory-lock contention returns 409 with Retry-After: 1. "
        "A committed key reused for another request returns 422."
    ),
    responses={
        **problem_responses(
            {
                **WRITE_ERRORS,
                402: ("payment-declined",),
                409: ("idempotency-in-progress", "order-not-payable"),
                422: (*WRITE_ERRORS[422], "payment-event-mismatch"),
            }
        ),
        200: {"model": PaidPayment, "description": "Order is paid.", "headers": REPLAY_HEADER},
        201: {"headers": REPLAY_HEADER},
    },
)
async def start_payment(
    order_id: OrderId,
    request: Request,
    principal: Caller,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    state = request.app.state
    operation = "create-payment"
    claim = Claim(
        principal.user_id, idempotency_key, operation, request_hash(operation, order_id, {})
    )
    result = await create_payment(
        state.db,
        state.settings,
        Provider(state.http, state.settings),
        principal.session_id,
        order_id,
        claim,
    )
    return result.response()


@router.get(
    "/orders/{order_id}/payments",
    policy=CUSTOMER,
    response_model=list[PaymentAttempt],
    summary="List payment attempts",
    description="Read-only lookup of an order's current payment attempts by its owner or an admin.",
    responses=problem_responses({**READ_ERRORS, 403: ("forbidden",), 404: ("not-found",)}),
)
async def payment_attempts(
    order_id: OrderId,
    request: Request,
    principal: Caller,
) -> list[PaymentAttempt]:
    state = request.app.state
    async with connection(state.db, state.settings) as conn:
        return await list_attempts(
            conn, order_id, None if principal.is_admin else principal.user_id
        )


@router.post(
    "/payments/callbacks",
    policy=Public("payment-callback"),
    response_model=CallbackResult,
    summary="Receive a signed payment outcome",
    description=(
        "The payment provider must sign the raw JSON body with a timestamp within 300 seconds. "
        "Events and captures are deduplicated. Capture decisions serialize with cancellation and "
        "expiry. The API acknowledges only committed outcomes."
    ),
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": PaymentEvent.model_json_schema()}},
        },
    },
    responses=problem_responses(
        {
            401: ("invalid-payment-signature",),
            404: ("not-found",),
            413: ("payment-event-too-large",),
            422: ("validation-failed", "payment-event-mismatch"),
            503: ("server-busy",),
        }
    ),
)
async def payment_callback(
    request: Request,
    mockpay_signature: Annotated[
        str,
        Header(
            description="HMAC-SHA256 over the timestamp, period and raw JSON body.",
            examples=["t=1796126880,v1=" + 64 * "a"],
        ),
    ],
) -> CallbackResult:
    state = request.app.state
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_CALLBACK_BYTES:
            raise ProblemError(413, "payment-event-too-large", "Payment event is too large")
        body.extend(chunk)
    verify_signature(
        bytes(body), mockpay_signature, state.settings.payment_webhook_secret.get_secret_value()
    )
    try:
        event = PaymentEvent.model_validate_json(body)
    except ValidationError as exc:
        raise ProblemError(422, "validation-failed", "Invalid payment event") from exc
    async with transaction(state.db, state.settings) as conn:
        applied = await apply_event(conn, event)
    applied.record()
    log.info(
        "payment.callback_applied" if applied.new_callback else "payment.callback_replayed",
        attempt_id=str(event.merchant_payment_id),
        disposition=applied.disposition,
        origin_request_id=applied.origin_request_id,
    )
    return CallbackResult(disposition=applied.disposition)
