"""Tests for payload validation logic."""

import pytest

from app.models import SharingEventResult
from app.validation import ValidationError, validate_payload


def _make_result(**overrides) -> SharingEventResult:
    """Build a valid SharingEventResult with optional overrides."""
    defaults = {
        "Operation": "AnonymousLinkCreated",
        "UserId": "user@org.com",
        "ObjectId": "https://org-my.sharepoint.com/personal/user/Documents/file.pdf",
        "ItemType": "File",
    }
    defaults.update(overrides)
    return SharingEventResult(**defaults)


class TestRequiredFields:
    def test_valid_payload_passes(self):
        warnings = validate_payload(_make_result())
        assert warnings == []

    def test_empty_operation_rejected(self):
        with pytest.raises(ValidationError, match="Operation"):
            validate_payload(_make_result(Operation=""))

    def test_whitespace_operation_rejected(self):
        with pytest.raises(ValidationError, match="Operation"):
            validate_payload(_make_result(Operation="   "))

    def test_empty_user_id_rejected(self):
        with pytest.raises(ValidationError, match="UserId"):
            validate_payload(_make_result(UserId=""))

    def test_empty_object_id_rejected(self):
        with pytest.raises(ValidationError, match="ObjectId"):
            validate_payload(_make_result(ObjectId=""))

    def test_empty_item_type_rejected(self):
        with pytest.raises(ValidationError, match="ItemType"):
            validate_payload(_make_result(ItemType=""))


class TestObjectIdUrlValidation:
    def test_valid_https_url_passes(self):
        validate_payload(_make_result(ObjectId="https://sharepoint.com/path"))

    def test_valid_http_url_passes(self):
        validate_payload(_make_result(ObjectId="http://sharepoint.com/path"))

    def test_invalid_scheme_rejected(self):
        with pytest.raises(ValidationError, match="not a valid URL"):
            validate_payload(_make_result(ObjectId="ftp://sharepoint.com/path"))

    def test_no_scheme_rejected(self):
        with pytest.raises(ValidationError, match="not a valid URL"):
            validate_payload(_make_result(ObjectId="not-a-url"))

    def test_bare_scheme_rejected(self):
        with pytest.raises(ValidationError, match="not a valid URL"):
            validate_payload(_make_result(ObjectId="https://"))


class TestSoftValidation:
    def test_known_operation_no_warning(self):
        warnings = validate_payload(
            _make_result(Operation="CompanySharingLinkCreated")
        )
        assert warnings == []

    def test_unrecognized_operation_warns(self):
        warnings = validate_payload(_make_result(Operation="SomeNewOperation"))
        assert len(warnings) == 1
        assert "Unrecognized sharing operation" in warnings[0]

    def test_file_item_type_no_warning(self):
        warnings = validate_payload(_make_result(ItemType="File"))
        assert warnings == []

    def test_folder_item_type_no_warning(self):
        warnings = validate_payload(_make_result(ItemType="Folder"))
        assert warnings == []

    def test_unrecognized_item_type_warns(self):
        warnings = validate_payload(_make_result(ItemType="Notebook"))
        assert any("Unrecognized item type" in w for w in warnings)

    def test_multiple_warnings_returned(self):
        warnings = validate_payload(
            _make_result(Operation="UnknownOp", ItemType="UnknownType")
        )
        assert len(warnings) == 2

    def test_extra_fields_accepted(self):
        """Pydantic extra='allow' should not cause validation failures."""
        result = SharingEventResult(
            Operation="AnonymousLinkCreated",
            UserId="user@org.com",
            ObjectId="https://sharepoint.com/file.pdf",
            ItemType="File",
            CustomField="extra_value",
        )
        warnings = validate_payload(result)
        assert warnings == []
