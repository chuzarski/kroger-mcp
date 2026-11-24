"""The HTTP side of hosting: a liveness route, and requests that arrive through a reverse proxy"""

from contextlib import asynccontextmanager

import httpx2 as httpx
import pytest

from kroger_mcp.server import create_server
from kroger_mcp.tools.signin_status import status

PROXY_HOST = "mcp.example.test"
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}


@pytest.fixture(autouse=True)
def fresh_status():
    status.reset()
    yield
    status.reset()


@asynccontextmanager
async def http_client():
    """A client of the server's HTTP app, which is running (its lifespan has started)"""
    app = create_server().http_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8000") as client:
            yield client


async def test_health_answers_without_asking_kroger(requests_mock):
    async with http_client() as http:
        response = await http.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["refresh"]["state"] == "unknown"
    assert requests_mock.call_count == 0


async def test_health_stays_up_when_the_sign_in_is_dead():
    status.mark_signin_needed("invalid refresh_token")
    async with http_client() as http:
        response = await http.get("/health")
    assert response.status_code == 200
    assert response.json()["refresh"]["state"] == "signin_needed"


async def test_health_never_shows_a_token(requests_mock):
    from conftest import save_user_token
    save_user_token(access_token="ACCESS-9f3a", refresh_token="REFRESH-9f3a")
    async with http_client() as http:
        response = await http.get("/health")
    assert "9f3a" not in response.text


async def test_a_request_through_a_proxy_with_its_own_host_name_is_answered():
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "Host": PROXY_HOST, "Origin": f"https://{PROXY_HOST}"}
    async with http_client() as http:
        response = await http.post("/mcp", headers=headers, json=INITIALIZE)
    assert response.status_code == 200
    assert '"result"' in response.text
