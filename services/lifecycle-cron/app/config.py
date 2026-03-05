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

    # Audit log polling
    redis_url: str = ""
    audit_poll_enabled: bool = True
    audit_poll_interval_minutes: int = 15
    audit_poll_operations: str = "AnonymousLinkCreated,CompanyLinkCreated"

    # Allowlist enforcement
    allowlist_enforcement_enabled: bool = False
    allowlist_enforcement_interval_hours: int = 168  # 7 days
    allowlist_enabled_capability: str = "ExternalUserAndGuestSharing"
    allowlist_disabled_capability: str = "ExternalUserSharingOnly"
    sharepoint_admin_url: str = ""

    # Folder rescan
    folder_rescan_enabled: bool = False
    folder_rescan_interval_hours: int = 168  # 7 days
    folder_rescan_batch_size: int = 50

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
            redis_url=os.environ.get("REDIS_URL", ""),
            audit_poll_enabled=_g("audit_poll_enabled", "AUDIT_POLL_ENABLED", "true").lower() == "true",
            audit_poll_interval_minutes=int(_g("audit_poll_interval_minutes", "AUDIT_POLL_INTERVAL_MINUTES", "15")),
            audit_poll_operations=os.environ.get(
                "AUDIT_POLL_OPERATIONS",
                "AnonymousLinkCreated,CompanyLinkCreated",
            ),
            allowlist_enforcement_enabled=os.environ.get(
                "ALLOWLIST_ENFORCEMENT_ENABLED", "false"
            ).lower() == "true",
            allowlist_enforcement_interval_hours=int(
                os.environ.get("ALLOWLIST_ENFORCEMENT_INTERVAL_HOURS", "168")
            ),
            allowlist_enabled_capability=os.environ.get(
                "ALLOWLIST_ENABLED_CAPABILITY", "ExternalUserAndGuestSharing"
            ),
            allowlist_disabled_capability=os.environ.get(
                "ALLOWLIST_DISABLED_CAPABILITY", "ExternalUserSharingOnly"
            ),
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
            log_level=_g("log_level", "LOG_LEVEL", "INFO"),
        )
