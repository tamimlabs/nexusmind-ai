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


class TestMemoryDeletion:
    """Users can delete unnecessary memory entries (single, bulk, by category)."""

    def _fresh_store(self, monkeypatch, tmp_path):
        import agent.core.memory as mem_mod
        import api.main as api_main

        monkeypatch.setattr(mem_mod, "_DB_PATH", tmp_path / "memory.db")
        monkeypatch.setattr(mem_mod, "_LEGACY_JSON_PATH", tmp_path / "no-legacy.json")
        store = mem_mod.MemoryStore()
        monkeypatch.setattr(api_main, "memory_store", store)
        return store

    def test_delete_single_entry(self, client, monkeypatch, tmp_path):
        store = self._fresh_store(monkeypatch, tmp_path)
        store.save_instruction("when a pr arrives, merge if clean")
        entry_id = store.get_by_category("instruction")[0].id

        res = client.delete(f"/api/memory/{entry_id}")
        assert res.status_code == 200
        assert res.json() == {"deleted": entry_id}
        assert memory_size(store) == 0

    def test_delete_missing_entry_404(self, client, monkeypatch, tmp_path):
        self._fresh_store(monkeypatch, tmp_path)
        assert client.delete("/api/memory/nope").status_code == 404

    def test_bulk_delete(self, client, monkeypatch, tmp_path):
        store = self._fresh_store(monkeypatch, tmp_path)
        store.save_instruction("a")
        store.save_instruction("b")
        ids = [e.id for e in store.get_by_category("instruction")]

        res = client.post("/api/memory/delete", json={"ids": ids})
        assert res.json()["count"] == 2
        assert memory_size(store) == 0

    def test_clear_category_keeps_others(self, client, monkeypatch, tmp_path):
        store = self._fresh_store(monkeypatch, tmp_path)
        store.save_instruction("old order")
        store.save_task_outcome("goal", "ok", True)

        res = client.post("/api/memory/clear/instruction")
        assert res.json() == {"cleared": 1, "category": "instruction"}
        assert memory_size(store) == 1
        assert store.get_by_category("task_outcome")

    def test_listing_includes_ids_for_deletion(self, client, monkeypatch, tmp_path):
        store = self._fresh_store(monkeypatch, tmp_path)
        store.save_instruction("review prs automatically")
        entries = client.get("/api/memory").json()
        assert entries and all("id" in e for e in entries)


class TestManualMemoryAdd:
    """Users can add memory entries manually (dashboard 'Add Memory')."""

    def _fresh_store(self, monkeypatch, tmp_path):
        import agent.core.memory as mem_mod
        import api.main as api_main

        monkeypatch.setattr(mem_mod, "_DB_PATH", tmp_path / "memory.db")
        monkeypatch.setattr(mem_mod, "_LEGACY_JSON_PATH", tmp_path / "no-legacy.json")
        store = mem_mod.MemoryStore()
        monkeypatch.setattr(api_main, "memory_store", store)
        return store

    def test_add_entry(self, client, monkeypatch, tmp_path):
        store = self._fresh_store(monkeypatch, tmp_path)
        res = client.post(
            "/api/memory",
            json={"content": "always deploy with ruff clean", "category": "skill"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["category"] == "skill"
        assert memory_size(store) == 1

    def test_add_instruction_is_visible_and_categorised(self, client, monkeypatch, tmp_path):
        store = self._fresh_store(monkeypatch, tmp_path)
        client.post(
            "/api/memory", json={"content": "when a pr opens, review it", "category": "instruction"}
        )
        instructions = store.get_by_category("instruction")
        assert len(instructions) == 1
        # Shows up in the API listing too
        listing = client.get("/api/memory?category=instruction").json()
        assert any("pr opens" in e["content"] for e in listing)

    def test_instruction_phrasing_autodetected_without_category(
        self, client, monkeypatch, tmp_path
    ):
        self._fresh_store(monkeypatch, tmp_path)
        res = client.post(
            "/api/memory", json={"content": "whenever a pr opens, review it and merge if clean"}
        )
        assert res.json()["category"] == "instruction"

    def test_empty_content_rejected(self, client, monkeypatch, tmp_path):
        self._fresh_store(monkeypatch, tmp_path)
        res = client.post("/api/memory", json={"content": "   "})
        assert res.status_code == 400

    def test_unknown_category_falls_back_to_general(self, client, monkeypatch, tmp_path):
        self._fresh_store(monkeypatch, tmp_path)
        res = client.post("/api/memory", json={"content": "note", "category": "bogus"})
        assert res.json()["category"] == "general"

    def test_manually_added_instruction_gates_watcher(self, client, monkeypatch, tmp_path):
        """End-to-end: add via API -> watcher now has permission to act."""
        import asyncio

        import agent.core.memory as mem_mod
        import api.main as api_main
        from agent.watchers.github import GitHubWatcher

        monkeypatch.setattr(mem_mod, "_DB_PATH", tmp_path / "memory.db")
        monkeypatch.setattr(mem_mod, "_LEGACY_JSON_PATH", tmp_path / "no-legacy.json")
        store = mem_mod.MemoryStore()
        monkeypatch.setattr(api_main, "memory_store", store)
        # The watcher resolves the singleton from the memory module at call time
        monkeypatch.setattr(mem_mod, "memory_store", store)

        client.post(
            "/api/memory",
            json={
                "content": "when a pr arrives then test and merge or decline it",
                "category": "instruction",
            },
        )

        watcher = GitHubWatcher("w1", {"repo": "tamimlabs/nexusmind-ai"})
        event = {
            "event_type": "github.pr.opened",
            "payload": {"number": 9, "title": "feat: x", "author": "dev"},
        }
        goal = asyncio.run(watcher.process_event(event))
        assert goal is not None and "#9" in goal


def memory_size(store) -> int:
    return len(store.get_recent(1000))
