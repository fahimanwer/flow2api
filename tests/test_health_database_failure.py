from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin


def test_health_returns_503_when_database_cannot_open_thread():
    app = FastAPI()
    app.include_router(admin.router)
    with patch.object(admin, 'build_public_health_snapshot', AsyncMock(
        side_effect=RuntimeError("can't start new thread")
    )):
        response = TestClient(app).get('/health')
    assert response.status_code == 503
    assert response.json()['database_available'] is False
    assert 'thread' not in response.text  # Internal failure details stay private.


def test_health_preserves_healthy_response_even_without_online_accounts():
    app = FastAPI()
    app.include_router(admin.router)
    snapshot = {'backend_running': True, 'has_active_tokens': False, 'total_tokens': 0}
    with patch.object(admin, 'build_public_health_snapshot', AsyncMock(return_value=snapshot)):
        response = TestClient(app).get('/health')
    assert response.status_code == 200
    assert response.json() == snapshot
