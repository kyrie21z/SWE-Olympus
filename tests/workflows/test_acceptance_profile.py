"""Model-authored checks cannot escalate beyond the registered local test profile."""

import pytest

from olympus.agents.runtime import OutputSchemaError
from olympus.workflows.acceptance import validate_check


@pytest.mark.parametrize("source", [
    "import os\nos.system('echo unsafe')\nassert 1 == 1",
    "import concurrent.futures\nconcurrent.futures.process.os.system('echo unsafe')\nassert 1 == 1",
    "from packages.core import inventory\nassert inventory.__dict__",
    "print('passed')",
    "assert True",
    "exec('print(1)')\nassert 1 == 1",
])
def test_check_rejects_privileged_or_vacuous_sources(source):
    with pytest.raises(OutputSchemaError):
        validate_check(source, ["packages/core"])


def test_check_allows_real_assertions_against_authorized_module():
    validate_check(
        "from packages.core.inventory import InventoryService\n"
        "service = InventoryService()\nassert service.get_sku('missing')['ok'] is False\n",
        ["packages/core"],
    )
