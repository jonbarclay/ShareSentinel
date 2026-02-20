"""Integration tests for the FastAPI webhook endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models import SplunkWebhookPayload


def _valid_payload() -> dict:
    return {
        "result": {
            "Operation": "AnonymousLinkCreated",
            "Workload": "OneDrive",
            "UserId": "user@org.com",
            "ObjectId": "https://org-my.sharepoint.com/personal/user/Documents/file.pdf",
            "SiteUrl": "https://org-my.sharepoint.com/personal/user/",
            "SourceFileName": "file.pdf",
            "SourceRelativeUrl": "personal/user/Documents",
            "ItemType": "File",
            "EventSource": "SharePoint",
            "CreationTime": "2024-01-15T10:30:00Z",
            "SharingType": "Anonymous",
            "SharingScope": "Anyone",
            "SharingPermission": "View",
        }
    }


@pytest.fixture()
def mock_redis():
    """Patch the module-level Redis client in main."""
    mock = AsyncMock()
    mock.ping.return_value = True
    mock.set.return_value = True  # SET NX succeeds — new event
    mock.rpush.return_value = 1
    with patch("app.main._redis", mock):
        yield mock


@pytest.fixture()
def mock_redis_duplicate():
    """Patch Redis to simulate a duplicate event."""
    mock = AsyncMock()
    mock.ping.return_value = True
    mock.set.return_value = None  # SET NX fails — duplicate
    with patch("app.main._redis", mock):
        yield mock


@pytest.fixture()
def mock_redis_down():
    """Patch Redis to simulate connection failure."""
    mock = AsyncMock()
    mock.set.side_effect = ConnectionError("Redis unavailable")
    with patch("app.main._redis", mock):
        yield mock


@pytest.fixture()
def client():
    """TestClient for the FastAPI app (no lifespan — Redis is mocked)."""
    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


# -------------------------------------------------------------------
# POST /webhook/splunk — success cases
# -------------------------------------------------------------------


class TestWebhookSuccess:
    def test_valid_payload_queued(self, client, mock_redis):
        resp = client.post("/webhook/splunk", json=_valid_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        assert "event_id" in body

    def test_duplicate_returns_200(self, client, mock_redis_duplicate):
        resp = client.post("/webhook/splunk", json=_valid_payload())
        assert resp.status_code == 200
        assert resp.json()["status"] == "duplicate"

    def test_folder_item_type_accepted(self, client, mock_redis):
        payload = _valid_payload()
        payload["result"]["ItemType"] = "Folder"
        resp = client.post("/webhook/splunk", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "queued"

    def test_extra_fields_accepted(self, client, mock_redis):
        payload = _valid_payload()
        payload["result"]["ExtraField"] = "some_value"
        payload["extra_top_level"] = "also_fine"
        resp = client.post("/webhook/splunk", json=payload)
        assert resp.status_code == 200

    def test_unrecognized_operation_accepted_with_warning(self, client, mock_redis):
        payload = _valid_payload()
        payload["result"]["Operation"] = "SomeNewOperation"
        resp = client.post("/webhook/splunk", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "queued"
        assert "warnings" in body


# -------------------------------------------------------------------
# POST /webhook/splunk — bad request cases
# -------------------------------------------------------------------


class TestWebhookBadRequest:
    def test_malformed_json(self, client, mock_redis):
        resp = client.post(
            "/webhook/splunk",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_missing_result_key(self, client, mock_redis):
        resp = client.post("/webhook/splunk", json={"foo": "bar"})
        assert resp.status_code == 400

    def test_missing_required_field(self, client, mock_redis):
        payload = _valid_payload()
        del payload["result"]["Operation"]
        resp = client.post("/webhook/splunk", json=payload)
        assert resp.status_code == 400

    def test_empty_required_field(self, client, mock_redis):
        payload = _valid_payload()
        payload["result"]["UserId"] = ""
        resp = client.post("/webhook/splunk", json=payload)
        assert resp.status_code == 400

    def test_invalid_object_id_url(self, client, mock_redis):
        payload = _valid_payload()
        payload["result"]["ObjectId"] = "not-a-url"
        resp = client.post("/webhook/splunk", json=payload)
        assert resp.status_code == 400


# -------------------------------------------------------------------
# POST /webhook/splunk — auth
# -------------------------------------------------------------------


class TestWebhookAuth:
    def test_auth_disabled_no_header_ok(self, client, mock_redis):
        """When WEBHOOK_AUTH_SECRET is not set, requests without auth pass."""
        with patch("app.main.settings") as mock_settings:
            mock_settings.auth_enabled = False
            mock_settings.dedup_ttl_seconds = 86400
            mock_settings.log_level = "INFO"
            resp = client.post("/webhook/splunk", json=_valid_payload())
        assert resp.status_code == 200

    def test_auth_enabled_valid_token(self, client, mock_redis):
        with patch("app.main.settings") as mock_settings:
            mock_settings.auth_enabled = True
            mock_settings.webhook_auth_secret = "test-secret"
            mock_settings.dedup_ttl_seconds = 86400
            resp = client.post(
                "/webhook/splunk",
                json=_valid_payload(),
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200

    def test_auth_enabled_missing_header(self, client, mock_redis):
        with patch("app.main.settings") as mock_settings:
            mock_settings.auth_enabled = True
            mock_settings.webhook_auth_secret = "test-secret"
            resp = client.post("/webhook/splunk", json=_valid_payload())
        assert resp.status_code == 401

    def test_auth_enabled_wrong_token(self, client, mock_redis):
        with patch("app.main.settings") as mock_settings:
            mock_settings.auth_enabled = True
            mock_settings.webhook_auth_secret = "test-secret"
            resp = client.post(
                "/webhook/splunk",
                json=_valid_payload(),
                headers={"Authorization": "Bearer wrong-token"},
            )
        assert resp.status_code == 401


# -------------------------------------------------------------------
# POST /webhook/splunk — Redis failure
# -------------------------------------------------------------------


class TestWebhookRedisFailure:
    def test_redis_down_returns_500(self, client, mock_redis_down):
        resp = client.post("/webhook/splunk", json=_valid_payload())
        assert resp.status_code == 500
        assert "unavailable" in resp.json()["error"]


# -------------------------------------------------------------------
# GET /health
# -------------------------------------------------------------------


class TestHealthCheck:
    def test_healthy(self, client, mock_redis):
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "healthy"
        assert body["redis_connected"] is True

    def test_unhealthy_redis_down(self, client):
        mock = AsyncMock()
        mock.ping.side_effect = ConnectionError("down")
        with patch("app.main._redis", mock):
            resp = client.get("/health")
        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "unhealthy"
        assert body["redis_connected"] is False

    def test_unhealthy_no_redis_client(self, client):
        with patch("app.main._redis", None):
            resp = client.get("/health")
        assert resp.status_code == 503
