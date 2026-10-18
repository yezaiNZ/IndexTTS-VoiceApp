import os

import pytest
import requests


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def prevent_unmocked_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must mock network requests")

    for name in ("get", "post", "put"):
        monkeypatch.setattr(requests, name, blocked)
