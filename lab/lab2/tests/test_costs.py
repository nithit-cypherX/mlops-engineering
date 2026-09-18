"""Check the verified Lab 2 compute rate without contacting Azure."""
from __future__ import annotations

import pytest

from src import costs


def test_verified_dedicated_hourly_rate():
    rate = costs.hourly_rate("azure", "Standard_F2s_v2", spot=False)
    assert rate == pytest.approx(2.900677, rel=1e-12)


def test_default_rate_does_not_apply_spot_discount():
    rate = costs.hourly_rate("azure", "Standard_F2s_v2")
    assert rate == costs.hourly_rate("azure", "Standard_F2s_v2", spot=False)
    assert rate == pytest.approx(2.900677, rel=1e-12)


@pytest.mark.parametrize(
    ("provider", "instance", "message"),
    [
        ("unknown", "Standard_F2s_v2", "No price table for provider"),
        ("azure", "Standard_F2s_v2_typo", "No rate for"),
    ],
)
def test_unknown_provider_or_instance_fails_instead_of_substituting(provider, instance, message):
    with pytest.raises(KeyError, match=message):
        costs.hourly_rate(provider, instance)
