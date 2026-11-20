import pytest

from ticketing.booking.create import fee_for
from ticketing.booking.idempotency import request_hash
from ticketing.booking.models import SpecificSeats


@pytest.mark.parametrize(("subtotal", "fee"), [(0, 0), (9, 0), (10, 1), (2810, 141)])
def test_fee_rounds_half_up(subtotal, fee):
    assert fee_for(subtotal) == fee


def test_idempotency_hash_binds_operation_resource_and_selection():
    ordered = SpecificSeats(seat_ids=[3, 1]).model_dump()
    reversed_order = SpecificSeats(seat_ids=[1, 3]).model_dump()
    digest = request_hash("book", 123, ordered)
    assert digest == request_hash("book", 123, reversed_order)
    assert digest != request_hash("book", 124, ordered)
    assert digest != request_hash("cancel", 123, ordered)
    assert digest != request_hash("book", 123, {"seat_ids": [1]})
