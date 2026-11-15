import pytest

from olympus.common.workspace import temporary_workspace


@pytest.fixture
def workspace(tmp_path):
    with temporary_workspace(tmp_path) as path:
        yield path
