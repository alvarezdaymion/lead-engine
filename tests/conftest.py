import pytest
from helpers import Clock


@pytest.fixture
def clock():
    return Clock()
