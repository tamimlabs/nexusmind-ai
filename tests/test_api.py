"""Tests for the API endpoints."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from api.main import app
    return TestClient(app)


class TestAPI:
    def test_health(self, client):
        res = client.get("/api/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        assert data["service"] == "nexusmind-ai"

    def test_dashboard(self, client):
        res = client.get("/")
        assert res.status_code == 200
        assert "NexusMind AI" in res.text

    def test_list_tasks(self, client):
        res = client.get("/api/tasks")
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    def test_get_approvals(self, client):
        res = client.get("/api/approvals")
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    def test_traces(self, client):
        res = client.get("/api/traces")
        assert res.status_code == 200
        assert isinstance(res.json(), list)
