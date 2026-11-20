"""Order creation, cancellation and owner-scoped reads."""

from typing import Annotated
from uuid import UUID

from fastapi import Depends, Path, Query, Request
from fastapi.responses import JSONResponse

from ticketing.api.security import (
    Principal,
    Roles,
    SecureRouter,
    current_principal,
    problem_forbidden,
)
from ticketing.booking.api_docs import READ_ERRORS, REPLAY_HEADER, WRITE_ERRORS, problem_responses
from ticketing.booking.create import create_order
from ticketing.booking.database import connection, fence, transaction
from ticketing.booking.idempotency import (
    RETRY_DESCRIPTION,
    Claim,
    IdempotencyKey,
    Result,
    request_hash,
)
from ticketing.booking.models import BIGINT_MAX, Order, OrderPage, SpecificSeats, ZoneSeats
from ticketing.booking.orders import cancel_order, order_models, order_page, visible_order
from ticketing.telemetry import business

router = SecureRouter(prefix="/api/v1", tags=["orders"])
CUSTOMER = Roles("admin", "user")
Caller = Annotated[Principal, Depends(current_principal)]
OrderId = Annotated[
    UUID, Path(description="Public order ID.", examples=["58d20683-a297-4ca4-a009-42452bbfb212"])
]


@router.post(
    "/events/{event_id}/orders",
    policy=CUSTOMER,
    status_code=201,
    response_model=Order,
    summary="Hold event seats",
    description=(
        "Users and admins can reserve 1 to 4 seats for a published event during its sales window. "
        "Specific seats are locked without waiting. Zone allocation skips seats locked by other "
        "transactions. Quota and all seats are reserved atomically. " + RETRY_DESCRIPTION
    ),
    responses={
        **problem_responses(
            {
                **WRITE_ERRORS,
                409: (
                    "seat-unavailable",
                    "seat-contended",
                    "insufficient-seats",
                    "allocation-contended",
                    "purchase-limit-exceeded",
                    "event-not-on-sale",
                    "idempotency-in-progress",
                ),
            }
        ),
        201: {
            "headers": {
                **REPLAY_HEADER,
                "Location": {"description": "Order detail path.", "schema": {"type": "string"}},
            }
        },
    },
)
async def book(
    event_id: Annotated[
        int, Path(gt=0, le=BIGINT_MAX, description="Published event ID.", examples=[25])
    ],
    body: SpecificSeats | ZoneSeats,
    request: Request,
    principal: Caller,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    state = request.app.state
    operation = "create-order"
    claim = Claim(
        principal.user_id,
        idempotency_key,
        operation,
        request_hash(operation, event_id, body.model_dump(mode="json")),
    )
    result = await create_order(
        state.db, state.settings, principal.session_id, claim, event_id, body
    )
    return result.response()


@router.get(
    "/orders",
    policy=CUSTOMER,
    response_model=OrderPage,
    summary="List orders",
    description=(
        "Users and admins can list their own orders. Admins may filter by another owner's user ID. "
        "Read-only pagination uses descending order IDs. Newer orders do not shift a page."
    ),
    responses=problem_responses({**READ_ERRORS, 403: ("forbidden",)}),
)
async def list_orders(
    request: Request,
    principal: Caller,
    limit: Annotated[
        int, Query(ge=1, le=100, description="Maximum page size.", examples=[50])
    ] = 50,
    cursor: Annotated[
        int | None,
        Query(
            gt=0, le=BIGINT_MAX, description="Next cursor from the previous page.", examples=[108]
        ),
    ] = None,
    user_id: Annotated[
        int | None,
        Query(
            gt=0,
            le=BIGINT_MAX,
            description="Order owner ID. Admin-only for other users.",
            examples=[42],
        ),
    ] = None,
) -> OrderPage:
    if user_id is not None and user_id != principal.user_id and not principal.is_admin:
        raise problem_forbidden()
    state = request.app.state
    async with connection(state.db, state.settings) as conn:
        return await order_page(conn, user_id or principal.user_id, cursor, limit)


@router.get(
    "/orders/{order_id}",
    policy=CUSTOMER,
    response_model=Order,
    summary="Get order details",
    description="Read-only lookup of the order's current state by its owner or an admin.",
    responses=problem_responses({**READ_ERRORS, 403: ("forbidden",), 404: ("not-found",)}),
)
async def get_order(order_id: OrderId, request: Request, principal: Caller) -> Order:
    state = request.app.state
    async with connection(state.db, state.settings) as conn:
        row = await visible_order(conn, order_id, None if principal.is_admin else principal.user_id)
        (model,) = await order_models(conn, [row])
    return model


@router.post(
    "/orders/{order_id}/cancel",
    policy=CUSTOMER,
    response_model=Order,
    summary="Cancel an order or request its refund",
    description=(
        "Owners and admins can cancel a pending order and release its seats immediately, "
        "or request a refund for a paid order. Paid seats remain reserved until the refund is "
        "confirmed. Cancellation, expiry and payment decisions serialize on the order. "
        + RETRY_DESCRIPTION
    ),
    responses={
        **problem_responses(
            {**WRITE_ERRORS, 409: ("order-not-cancellable", "idempotency-in-progress")}
        ),
        200: {"headers": REPLAY_HEADER},
    },
)
async def cancel(
    order_id: OrderId,
    request: Request,
    principal: Caller,
    idempotency_key: IdempotencyKey,
) -> JSONResponse:
    state = request.app.state
    operation = "cancel-order"
    claim = Claim(
        principal.user_id, idempotency_key, operation, request_hash(operation, order_id, {})
    )
    async with transaction(state.db, state.settings) as conn:
        await fence(conn, principal.session_id, principal.user_id)
        owner_id = None if principal.is_admin else principal.user_id
        prior = await claim.check(conn)
        if prior is not None:
            # An admin may have lost access to this order since the original cancellation.
            await visible_order(conn, order_id, owner_id)
            return prior.response()
        order = await visible_order(conn, order_id, owner_id, lock=True)
        row = await cancel_order(conn, order)
        (model,) = await order_models(conn, [row])
        result = Result(200, model.model_dump(mode="json"))
        await claim.save(conn, result, resource_ref=order_id)
    business.record(model.event_id, "cancel")
    return result.response()
