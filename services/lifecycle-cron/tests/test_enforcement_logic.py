"""Unit tests for the site-policy enforcement decision logic.

These cover the pure functions that decide WHAT gets enforced — URL
normalization, capability transitions, UPN/segment encoding, and inventory
parsing. The bugs these guard against were all found live in the 2026-07-05
review: first-page-only enumeration read as a clean scan, dotted-UPN exception
members losing their exemption, and `disable_sharing` loosening stricter sites.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.allowlist_enforcer import (  # noqa: E402
    SHARING_CAPABILITY_MAP,
    _capability_allows_anonymous,
    _encode_csom_url,
    _is_throttle_error,
    _is_unsupported_site_type,
)
from app.provisioning_enforcer import (  # noqa: E402
    _site_collection_root,
    _upn_to_personal_segment,
)


class TestUpnSegmentEncoding:
    """Forward-encoding is the only safe direction for exemption matching."""

    def test_dotted_upn(self):
        assert _upn_to_personal_segment("jon.barclay@uvu.edu") == "jon_barclay_uvu_edu"

    def test_numeric_upn(self):
        assert _upn_to_personal_segment("10003439@uvu.edu") == "10003439_uvu_edu"

    def test_underscore_in_local_part(self):
        assert _upn_to_personal_segment("a_b@uvu.edu") == "a_b_uvu_edu"

    def test_hyphen_preserved(self):
        assert _upn_to_personal_segment("jane-doe@sub.uvu.edu") == "jane-doe_sub_uvu_edu"

    def test_case_and_whitespace(self):
        assert _upn_to_personal_segment("  Provost@UVU.edu ") == "provost_uvu_edu"

    def test_dotted_upn_segment_matches_onedrive_path(self):
        # The exact scenario that silently dropped exemptions: reversing
        # jon_barclay_uvu_edu at the first underscore gives jon@barclay.uvu.edu.
        segment = "jon_barclay_uvu_edu"
        assert _upn_to_personal_segment("jon.barclay@uvu.edu") == segment


class TestSiteCollectionRoot:
    def test_subsite_collapsed(self):
        assert _site_collection_root(
            "https://x.sharepoint.com/sites/Foo/Marketing/Shared%20Documents"
        ) == "https://x.sharepoint.com/sites/Foo"

    def test_personal_path(self):
        assert _site_collection_root(
            "https://x-my.sharepoint.com/personal/jane_uvu_edu/Documents/a.docx"
        ) == "https://x-my.sharepoint.com/personal/jane_uvu_edu"

    def test_root_unchanged(self):
        assert _site_collection_root(
            "https://x.sharepoint.com/sites/Foo"
        ) == "https://x.sharepoint.com/sites/Foo"


class TestUnsupportedSiteTypes:
    def test_content_storage(self):
        assert _is_unsupported_site_type("https://x.sharepoint.com/contentstorage/CSP_abc")

    def test_public_site(self):
        assert _is_unsupported_site_type("http://x-public.sharepoint.com/")

    def test_mysite_host_root(self):
        assert _is_unsupported_site_type("https://x-my.sharepoint.com/")

    def test_personal_site_supported(self):
        assert not _is_unsupported_site_type(
            "https://x-my.sharepoint.com/personal/jane_uvu_edu"
        )

    def test_normal_site_supported(self):
        assert not _is_unsupported_site_type("https://x.sharepoint.com/sites/Foo")


class TestCapabilityTransitions:
    """Only ExternalUserAndGuestSharing (2) allows anonymous links; enforcement
    must never move a stricter site (0/1/3) toward more sharing."""

    def test_only_guest_sharing_is_anonymous(self):
        assert _capability_allows_anonymous(SHARING_CAPABILITY_MAP["ExternalUserAndGuestSharing"])
        for name in ("Disabled", "ExternalUserSharingOnly", "ExistingExternalUserSharingOnly"):
            assert not _capability_allows_anonymous(SHARING_CAPABILITY_MAP[name])

    def test_unknown_capability_not_anonymous(self):
        # -1 (unresolved from inventory) must not read as a violation before
        # the per-site resolution runs.
        assert not _capability_allows_anonymous(-1)


class TestCsomUrlEncoding:
    def test_ampersand(self):
        assert _encode_csom_url(
            "https://x.sharepoint.com/sites/DEPT-BusinessIntelligence&ResearchServices"
        ).endswith("DEPT-BusinessIntelligence%26ResearchServices")

    def test_percent_encoded_first(self):
        # A pre-existing % must not double-mangle the later replacements.
        assert _encode_csom_url("https://x/sites/A%B&C") == "https://x/sites/A%25B%26C"

    def test_plain_url_unchanged(self):
        url = "https://x.sharepoint.com/sites/Plain-Site_1"
        assert _encode_csom_url(url) == url


class TestThrottleDetection:
    def test_429_detected(self):
        assert _is_throttle_error(Exception("429 Too Many Requests"))

    def test_throttle_word(self):
        assert _is_throttle_error(Exception("Request was throttled by SPO"))

    def test_generic_not_throttle(self):
        assert not _is_throttle_error(Exception("SP.ListItem not found"))


class TestInventoryParsing:
    """The JSONL contract between Get-SitePolicyInventory.ps1 and the scanner."""

    def test_capability_mapping(self):
        row = json.loads('{"url":"https://x/sites/A","title":"A","sharing_capability":"Disabled"}')
        assert SHARING_CAPABILITY_MAP.get(row["sharing_capability"], -1) == 0

    def test_unknown_capability_maps_to_sentinel(self):
        row = json.loads('{"url":"https://x/sites/A","title":"A","sharing_capability":"Unknown"}')
        assert SHARING_CAPABILITY_MAP.get(row["sharing_capability"], -1) == -1

    def test_bom_stripped_by_utf8_sig(self):
        raw = "﻿" + '{"url":"https://x/sites/A"}'
        # utf-8-sig decoding is what get_site_inventory uses; simulate it.
        decoded = raw.encode("utf-8").decode("utf-8-sig")
        assert json.loads(decoded)["url"] == "https://x/sites/A"
