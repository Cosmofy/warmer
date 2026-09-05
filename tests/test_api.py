import asyncio
from unittest.mock import Mock

import httpx

from app.config import Settings
from app.main import app


def test_authentication_health_and_named_operations(monkeypatch, tmp_path):
    async def scenario():
        settings = Settings(warmer_api_token="t" * 32, warmer_state_db=tmp_path / "state.db")
        monkeypatch.setattr("app.main.Settings", lambda: settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                assert (await client.get("/health/live")).status_code == 200
                assert (await client.get("/health/ready")).status_code == 200
                assert (await client.post("/extract-ip")).status_code == 401
                assert (await client.get("/pops")).status_code == 401
                client.headers["Authorization"] = "Bearer " + "t" * 32
                assert (await client.get("/queries")).json() == {"queries": ["articles", "picture"]}
                assert (await client.post("/warm/unknown")).status_code == 404
                assert (await client.post("/warm/articles")).status_code == 409
                assert (await client.get("/runs/missing")).status_code == 404
                assert (await client.get("/pops")).json()["complete"] is False
    asyncio.run(scenario())


def test_unexpected_error_has_safe_json_and_request_id(monkeypatch, tmp_path):
    async def scenario():
        settings = Settings(warmer_api_token="t" * 32, warmer_state_db=tmp_path / "state.db")
        monkeypatch.setattr("app.main.Settings", lambda: settings)
        async with app.router.lifespan_context(app):
            monkeypatch.setattr(app.state.store, "latest_runs", Mock(side_effect=RuntimeError("private-secret")))
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/runs", headers={"Authorization": "Bearer " + "t" * 32, "x-request-id": "test-500"})
                assert response.status_code == 500
                assert response.json()["error"]["code"] == "INTERNAL_ERROR"
                assert "private-secret" not in response.text
                assert response.headers["x-request-id"] == "test-500"
    asyncio.run(scenario())
