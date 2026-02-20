"""Tests for OIDC token validation and RBAC enforcement."""

import pytest

from app.auth import _validate_claims, _user_roles
from app import config


# ---------------------------------------------------------------------------
# _validate_claims tests
# ---------------------------------------------------------------------------

class TestValidateClaims:
    """Verify aud/iss claim validation rejects invalid tokens."""

    def setup_method(self):
        self._orig_client_id = config.OIDC_CLIENT_ID
        self._orig_tenant_id = config.OIDC_TENANT_ID
        config.OIDC_CLIENT_ID = "test-client-id"
        config.OIDC_TENANT_ID = "test-tenant-id"

    def teardown_method(self):
        config.OIDC_CLIENT_ID = self._orig_client_id
        config.OIDC_TENANT_ID = self._orig_tenant_id

    def test_valid_claims(self):
        claims = {
            "aud": "test-client-id",
            "iss": "https://login.microsoftonline.com/test-tenant-id/v2.0",
        }
        _validate_claims(claims)  # Should not raise

    def test_wrong_audience_string(self):
        claims = {
            "aud": "wrong-client-id",
            "iss": "https://login.microsoftonline.com/test-tenant-id/v2.0",
        }
        with pytest.raises(ValueError, match="audience"):
            _validate_claims(claims)

    def test_wrong_audience_list(self):
        claims = {
            "aud": ["other-app-id", "another-app-id"],
            "iss": "https://login.microsoftonline.com/test-tenant-id/v2.0",
        }
        with pytest.raises(ValueError, match="audience"):
            _validate_claims(claims)

    def test_valid_audience_in_list(self):
        claims = {
            "aud": ["other-app-id", "test-client-id"],
            "iss": "https://login.microsoftonline.com/test-tenant-id/v2.0",
        }
        _validate_claims(claims)  # Should not raise

    def test_wrong_issuer(self):
        claims = {
            "aud": "test-client-id",
            "iss": "https://login.microsoftonline.com/other-tenant/v2.0",
        }
        with pytest.raises(ValueError, match="issuer"):
            _validate_claims(claims)

    def test_wrong_issuer_different_domain(self):
        claims = {
            "aud": "test-client-id",
            "iss": "https://evil.example.com/test-tenant-id/v2.0",
        }
        with pytest.raises(ValueError, match="issuer"):
            _validate_claims(claims)


# ---------------------------------------------------------------------------
# _user_roles tests
# ---------------------------------------------------------------------------

class TestUserRoles:
    """Verify role derivation from group membership."""

    def setup_method(self):
        self._orig_admin = config.OIDC_ADMIN_GROUP_IDS
        self._orig_analyst = config.OIDC_ANALYST_GROUP_IDS

    def teardown_method(self):
        config.OIDC_ADMIN_GROUP_IDS = self._orig_admin
        config.OIDC_ANALYST_GROUP_IDS = self._orig_analyst

    def test_no_groups_configured_grants_all_roles(self):
        config.OIDC_ADMIN_GROUP_IDS = []
        config.OIDC_ANALYST_GROUP_IDS = []
        user = {"groups": ["some-group"]}
        roles = _user_roles(user)
        assert roles == {"viewer", "analyst", "admin"}

    def test_admin_group_grants_all_roles(self):
        config.OIDC_ADMIN_GROUP_IDS = ["admin-gid"]
        config.OIDC_ANALYST_GROUP_IDS = ["analyst-gid"]
        user = {"groups": ["admin-gid"]}
        roles = _user_roles(user)
        assert "admin" in roles
        assert "analyst" in roles
        assert "viewer" in roles

    def test_analyst_group_grants_analyst_and_viewer(self):
        config.OIDC_ADMIN_GROUP_IDS = ["admin-gid"]
        config.OIDC_ANALYST_GROUP_IDS = ["analyst-gid"]
        user = {"groups": ["analyst-gid"]}
        roles = _user_roles(user)
        assert "analyst" in roles
        assert "viewer" in roles
        assert "admin" not in roles

    def test_no_matching_groups_gets_viewer_only(self):
        config.OIDC_ADMIN_GROUP_IDS = ["admin-gid"]
        config.OIDC_ANALYST_GROUP_IDS = ["analyst-gid"]
        user = {"groups": ["unrelated-gid"]}
        roles = _user_roles(user)
        assert roles == {"viewer"}

    def test_empty_user_groups(self):
        config.OIDC_ADMIN_GROUP_IDS = ["admin-gid"]
        config.OIDC_ANALYST_GROUP_IDS = ["analyst-gid"]
        user = {"groups": []}
        roles = _user_roles(user)
        assert roles == {"viewer"}
