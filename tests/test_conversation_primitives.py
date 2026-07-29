import pytest

from pynchy.conversation.models import (
    ExternalDeliveryId,
    ExternalDeliveryIdentity,
    ExternalProvider,
    ExternalRoute,
)


@pytest.mark.parametrize(
    ("provider", "route", "delivery_id", "message"),
    [
        (" ", "project", "delivery", "External provider must not be empty"),
        ("linear", " ", "delivery", "External route must not be empty"),
        ("linear", "project", " ", "External delivery ID must not be empty"),
    ],
)
def test_external_delivery_identity_rejects_blank_identity_parts(
    provider: str, route: str, delivery_id: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ExternalDeliveryIdentity(
            provider=ExternalProvider(provider),
            route=ExternalRoute(route),
            delivery_id=ExternalDeliveryId(delivery_id),
        )
