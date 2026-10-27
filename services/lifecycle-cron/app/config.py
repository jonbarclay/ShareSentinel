"""Configuration for the lifecycle cron service."""

import os
from dataclasses import dataclass


@dataclass
class LifecycleConfig:
    # Database
    database_url: str = ""

    # Microsoft Graph API
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_certificate_path: str = ""
    azure_certificate_password: str = ""

    # SMTP
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = ""

    # Security team BCC
    security_email: str = ""

    # Lifecycle settings
    check_interval_hours: int = 24
    max_days: int = 180
    # Send countdown + removal-confirmation emails to file owners. When false,
    # links are still removed at max_days; only the owner-facing emails are
    # suppressed.
    lifecycle_notifications_enabled: bool = True

    # Audit log polling
    redis_url: str = ""
    audit_poll_enabled: bool = True
    audit_poll_interval_minutes: int = 15
    audit_poll_operations: str = "AnonymousLinkCreated,CompanyLinkCreated"
    # Also ingest provisioning events (SiteCollectionCreated, "Add group.",
    # "Add user.") so newly created sites/OneDrives/groups get their sharing,
    # CWS, and visibility settings enforced immediately instead of waiting for
    # the next scheduled sweep. Requires the app registration to hold the full
    # AuditLogsQuery.Read.All permission (the SharePoint-scoped variant cannot
    # read azureActiveDirectory records).
    audit_poll_provisioning_enabled: bool = False
    # Reconciliation pass: audit records can surface hours after the activity;
    # re-query the trailing window periodically (dedup drops known events).
    # 0 disables. Look-back is clamped below the Redis dedup TTL (72h).
    audit_poll_lookback_hours: int = 12
    audit_poll_reconcile_interval_hours: int = 6

    # Site policy enforcement (dual: visibility + sharing)
    site_policy_enabled: bool = False
    site_policy_interval_hours: int = 24
    site_policy_enabled_sharing_capability: str = "ExternalUserAndGuestSharing"
    site_policy_disabled_sharing_capability: str = "ExternalUserSharingOnly"
    # Run the weekly OneDrive sharing-capability sweep. OneDrives are subject
    # to the same anonymous-sharing policy as SharePoint sites but are far more
    # numerous (~150K+), so they get their own cadence instead of riding the
    # daily scan; excluding them leaves event-driven remediation as the only
    # coverage.
    site_policy_include_onedrive: bool = True
    site_policy_onedrive_interval_hours: int = 168
    # Fail a scan whose enumeration returns fewer than this percentage of the
    # recent peak for the same scan type (guards against silent inventory
    # truncation — a truncated enumeration must never read as a clean scan).
    # 0 disables the check.
    site_policy_scan_floor_pct: int = 50
    sharepoint_admin_url: str = ""

    # Folder rescan
    folder_rescan_enabled: bool = False
    folder_rescan_interval_hours: int = 168  # 7 days
    folder_rescan_batch_size: int = 50

    # Company-wide sharing exception group (used by PowerShell scripts)
    company_wide_sharing_exception_group_id: str = ""

    # CWS verification sweep (PowerShell, detect-only). Full-tenant enumeration
    # is slow (Get-PnPTenantSite over every site), so it runs weekly; the daily
    # drift remediation drain applies any drift recorded between sweeps.
    cws_verification_enabled: bool = False
    cws_verification_interval_hours: int = 168

    # CWS drift remediation (PowerShell, daily drain of unresolved drifts)
    cws_drift_remediation_enabled: bool = False
    cws_drift_remediation_interval_hours: int = 24
    cws_drift_remediation_dry_run: bool = True

    # CWS bulk remediation (PowerShell, monthly full-tenant safety net)
    cws_bulk_remediation_enabled: bool = False
    cws_bulk_remediation_interval_hours: int = 720  # ~30 days
    cws_bulk_remediation_dry_run: bool = True
    cws_bulk_remediation_throttle: int = 5

    # CWS allowlist-action consumer (drains dashboard-pushed add/remove triggers)
    cws_allowlist_action_enabled: bool = True

    # CWS exception-group sync (10-min Graph poll, fires PS only on diff)
    cws_exception_group_sync_enabled: bool = False
    cws_exception_group_sync_interval_minutes: int = 10
    cws_exception_group_sync_dry_run: bool = True
    # Periodic full-membership reconciliation: re-enable any current exception
    # member whose OneDrive has drifted to Disabled (the delta path only catches
    # membership changes, never an external re-disable of an existing member).
    cws_exception_reconcile_interval_hours: int = 24

    # CWS failure diagnostic loop (wraps Diagnose-OneDriveSite.ps1)
    cws_failure_diagnostic_enabled: bool = True
    cws_failure_diagnostic_interval_minutes: int = 30
    cws_failure_diagnostic_max_per_sweep: int = 20
    cws_failure_diagnostic_refresh_after_hours: int = 72

    # Logging
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, db_overrides: dict[str, str] | None = None) -> "LifecycleConfig":
        ov = db_overrides or {}

        def _g(db_key: str, env_key: str, default: str) -> str:
            """DB override > env var > default."""
            return ov.get(db_key) or os.environ.get(env_key, default)

        return cls(
            database_url=os.environ.get("DATABASE_URL", ""),
            azure_tenant_id=os.environ.get("AZURE_TENANT_ID", ""),
            azure_client_id=os.environ.get("AZURE_CLIENT_ID", ""),
            azure_client_secret=os.environ.get("AZURE_CLIENT_SECRET", ""),
            azure_certificate_path=os.environ.get("AZURE_CERTIFICATE", ""),
            azure_certificate_password=os.environ.get("AZURE_CERTIFICATE_PASS", ""),
            smtp_host=os.environ.get("SMTP_HOST", ""),
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_user=os.environ.get("SMTP_USER", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_use_tls=os.environ.get("SMTP_USE_TLS", "true").lower() == "true",
            email_from=_g("email_from", "EMAIL_FROM", ""),
            security_email=_g("security_email", "SECURITY_EMAIL", ""),
            check_interval_hours=int(_g("lifecycle_check_interval_hours", "LIFECYCLE_CHECK_INTERVAL_HOURS", "24")),
            max_days=int(_g("lifecycle_max_days", "LIFECYCLE_MAX_DAYS", "180")),
            lifecycle_notifications_enabled=_g(
                "lifecycle_notifications_enabled",
                "LIFECYCLE_NOTIFICATIONS_ENABLED",
                "true",
            ).lower() == "true",
            redis_url=os.environ.get("REDIS_URL", ""),
            audit_poll_enabled=_g("audit_poll_enabled", "AUDIT_POLL_ENABLED", "true").lower() == "true",
            audit_poll_interval_minutes=int(_g("audit_poll_interval_minutes", "AUDIT_POLL_INTERVAL_MINUTES", "15")),
            audit_poll_operations=os.environ.get(
                "AUDIT_POLL_OPERATIONS",
                "AnonymousLinkCreated,CompanyLinkCreated",
            ),
            audit_poll_provisioning_enabled=_g(
                "audit_poll_provisioning_enabled",
                "AUDIT_POLL_PROVISIONING_ENABLED",
                "false",
            ).lower() == "true",
            audit_poll_lookback_hours=int(_g(
                "audit_poll_lookback_hours",
                "AUDIT_POLL_LOOKBACK_HOURS",
                "12",
            )),
            audit_poll_reconcile_interval_hours=int(_g(
                "audit_poll_reconcile_interval_hours",
                "AUDIT_POLL_RECONCILE_INTERVAL_HOURS",
                "6",
            )),
            site_policy_enabled=os.environ.get(
                "SITE_POLICY_ENABLED", "false"
            ).lower() == "true",
            site_policy_interval_hours=int(
                os.environ.get("SITE_POLICY_INTERVAL_HOURS", "24")
            ),
            site_policy_enabled_sharing_capability=os.environ.get(
                "SITE_POLICY_ENABLED_SHARING_CAPABILITY", "ExternalUserAndGuestSharing"
            ),
            site_policy_disabled_sharing_capability=os.environ.get(
                "SITE_POLICY_DISABLED_SHARING_CAPABILITY", "ExternalUserSharingOnly"
            ),
            site_policy_include_onedrive=_g(
                "site_policy_include_onedrive",
                "SITE_POLICY_INCLUDE_ONEDRIVE",
                "true",
            ).lower() == "true",
            site_policy_onedrive_interval_hours=int(_g(
                "site_policy_onedrive_interval_hours",
                "SITE_POLICY_ONEDRIVE_INTERVAL_HOURS",
                "168",
            )),
            site_policy_scan_floor_pct=int(_g(
                "site_policy_scan_floor_pct",
                "SITE_POLICY_SCAN_FLOOR_PCT",
                "50",
            )),
            sharepoint_admin_url=os.environ.get("SHAREPOINT_ADMIN_URL", ""),
            folder_rescan_enabled=_g(
                "folder_rescan_enabled", "FOLDER_RESCAN_ENABLED", "false"
            ).lower() == "true",
            folder_rescan_interval_hours=int(_g(
                "folder_rescan_interval_hours", "FOLDER_RESCAN_INTERVAL_HOURS", "168"
            )),
            folder_rescan_batch_size=int(_g(
                "folder_rescan_batch_size", "FOLDER_RESCAN_BATCH_SIZE", "50"
            )),
            company_wide_sharing_exception_group_id=_g(
                "company_wide_sharing_exception_group_id",
                "COMPANY_WIDE_SHARING_EXCEPTION_GROUP_ID",
                "",
            ),
            cws_verification_enabled=_g(
                "cws_verification_enabled", "CWS_VERIFICATION_ENABLED", "false",
            ).lower() == "true",
            cws_verification_interval_hours=int(_g(
                "cws_verification_interval_hours",
                "CWS_VERIFICATION_INTERVAL_HOURS",
                "168",
            )),
            cws_drift_remediation_enabled=_g(
                "cws_drift_remediation_enabled",
                "CWS_DRIFT_REMEDIATION_ENABLED",
                "false",
            ).lower() == "true",
            cws_drift_remediation_interval_hours=int(_g(
                "cws_drift_remediation_interval_hours",
                "CWS_DRIFT_REMEDIATION_INTERVAL_HOURS",
                "24",
            )),
            cws_drift_remediation_dry_run=_g(
                "cws_drift_remediation_dry_run",
                "CWS_DRIFT_REMEDIATION_DRY_RUN",
                "true",
            ).lower() == "true",
            cws_bulk_remediation_enabled=_g(
                "cws_bulk_remediation_enabled",
                "CWS_BULK_REMEDIATION_ENABLED",
                "false",
            ).lower() == "true",
            cws_bulk_remediation_interval_hours=int(_g(
                "cws_bulk_remediation_interval_hours",
                "CWS_BULK_REMEDIATION_INTERVAL_HOURS",
                "720",
            )),
            cws_bulk_remediation_dry_run=_g(
                "cws_bulk_remediation_dry_run",
                "CWS_BULK_REMEDIATION_DRY_RUN",
                "true",
            ).lower() == "true",
            cws_bulk_remediation_throttle=int(_g(
                "cws_bulk_remediation_throttle",
                "CWS_BULK_REMEDIATION_THROTTLE",
                "5",
            )),
            cws_allowlist_action_enabled=_g(
                "cws_allowlist_action_enabled",
                "CWS_ALLOWLIST_ACTION_ENABLED",
                "true",
            ).lower() == "true",
            cws_exception_group_sync_enabled=_g(
                "cws_exception_group_sync_enabled",
                "CWS_EXCEPTION_GROUP_SYNC_ENABLED",
                "false",
            ).lower() == "true",
            cws_exception_group_sync_interval_minutes=int(_g(
                "cws_exception_group_sync_interval_minutes",
                "CWS_EXCEPTION_GROUP_SYNC_INTERVAL_MINUTES",
                "10",
            )),
            cws_exception_group_sync_dry_run=_g(
                "cws_exception_group_sync_dry_run",
                "CWS_EXCEPTION_GROUP_SYNC_DRY_RUN",
                "true",
            ).lower() == "true",
            cws_exception_reconcile_interval_hours=int(_g(
                "cws_exception_reconcile_interval_hours",
                "CWS_EXCEPTION_RECONCILE_INTERVAL_HOURS",
                "24",
            )),
            cws_failure_diagnostic_enabled=_g(
                "cws_failure_diagnostic_enabled",
                "CWS_FAILURE_DIAGNOSTIC_ENABLED",
                "true",
            ).lower() == "true",
            cws_failure_diagnostic_interval_minutes=int(_g(
                "cws_failure_diagnostic_interval_minutes",
                "CWS_FAILURE_DIAGNOSTIC_INTERVAL_MINUTES",
                "30",
            )),
            cws_failure_diagnostic_max_per_sweep=int(_g(
                "cws_failure_diagnostic_max_per_sweep",
                "CWS_FAILURE_DIAGNOSTIC_MAX_PER_SWEEP",
                "20",
            )),
            cws_failure_diagnostic_refresh_after_hours=int(_g(
                "cws_failure_diagnostic_refresh_after_hours",
                "CWS_FAILURE_DIAGNOSTIC_REFRESH_AFTER_HOURS",
                "72",
            )),
            log_level=_g("log_level", "LOG_LEVEL", "INFO"),
        )
