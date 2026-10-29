"""Shared fixtures."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(request):
    """Enable custom integrations for the Home Assistant tests only."""
    if "hass" in request.fixturenames:
        request.getfixturevalue("enable_custom_integrations")
    yield
