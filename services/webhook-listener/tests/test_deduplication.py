"""Tests for deduplication key generation and Redis dedup logic."""

import hashlib
from unittest.mock import AsyncMock

import pytest

from app.deduplication import DEDUP_KEY_PREFIX, generate_dedup_key, is_duplicate
from app.models import SharingEventResult


def _make_result(**overrides) -> SharingEventResult:
    defaults = {
        "Operation": "AnonymousLinkCreated",
        "UserId": "user@org.com",
        "ObjectId": "https://org-my.sharepoint.com/personal/user/Documents/file.pdf",
        "ItemType": "File",
        "CreationTime": "2024-01-15T10:30:00Z",
    }
    defaults.update(overrides)
    return SharingEventResult(**defaults)


class TestGenerateDedupKey:
    def test_deterministic(self):
        result = _make_result()
        key1 = generate_dedup_key(result)
        key2 = generate_dedup_key(result)
        assert key1 == key2

    def test_is_sha256_hex(self):
        key = generate_dedup_key(_make_result())
        assert len(key) == 64
        int(key, 16)  # raises if not hex

    def test_different_object_id_different_key(self):
        k1 = generate_dedup_key(_make_result(ObjectId="https://a.com/file1"))
        k2 = generate_dedup_key(_make_result(ObjectId="https://a.com/file2"))
        assert k1 != k2

    def test_different_operation_different_key(self):
        k1 = generate_dedup_key(_make_result(Operation="AnonymousLinkCreated"))
        k2 = generate_dedup_key(_make_result(Operation="CompanySharingLinkCreated"))
        assert k1 != k2

    def test_different_user_different_key(self):
        k1 = generate_dedup_key(_make_result(UserId="a@org.com"))
        k2 = generate_dedup_key(_make_result(UserId="b@org.com"))
        assert k1 != k2

    def test_different_creation_time_different_key(self):
        k1 = generate_dedup_key(_make_result(CreationTime="2024-01-15T10:30:00Z"))
        k2 = generate_dedup_key(_make_result(CreationTime="2024-01-15T11:00:00Z"))
        assert k1 != k2

    def test_none_creation_time_handled(self):
        key = generate_dedup_key(_make_result(CreationTime=None))
        assert len(key) == 64

    def test_expected_hash_value(self):
        result = _make_result()
        raw = f"{result.ObjectId}{result.Operation}{result.CreationTime}{result.UserId}"
        expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        assert generate_dedup_key(result) == expected


class TestIsDuplicate:
    @pytest.mark.asyncio
    async def test_new_event_returns_false(self):
        mock_redis = AsyncMock()
        mock_redis.set.return_value = True  # SET NX succeeded
        result = await is_duplicate(mock_redis, "abc123", 86400)
        assert result is False
        mock_redis.set.assert_called_once_with(
            f"{DEDUP_KEY_PREFIX}abc123", "1", ex=86400, nx=True
        )

    @pytest.mark.asyncio
    async def test_duplicate_event_returns_true(self):
        mock_redis = AsyncMock()
        mock_redis.set.return_value = None  # SET NX failed (key exists)
        result = await is_duplicate(mock_redis, "abc123", 86400)
        assert result is True

    @pytest.mark.asyncio
    async def test_custom_ttl_passed(self):
        mock_redis = AsyncMock()
        mock_redis.set.return_value = True
        await is_duplicate(mock_redis, "hash", 3600)
        mock_redis.set.assert_called_once_with(
            f"{DEDUP_KEY_PREFIX}hash", "1", ex=3600, nx=True
        )
