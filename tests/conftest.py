import importlib.util

import pytest

if importlib.util.find_spec("pytest_homeassistant_custom_component"):
    pytest_plugins = ["pytest_homeassistant_custom_component"]

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        yield
else:
    collect_ignore = ["test_integration.py"]
