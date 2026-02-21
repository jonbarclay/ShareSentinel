"""Worker service configuration loaded from environment variables."""

import os
from dataclasses import dataclass, field
from typing import List


@dataclass
class Config:
    # Redis
    redis_url: str = "redis://redis:6379/0"

    # Database
    database_url: str = ""

    # Microsoft Graph API
    azure_tenant_id: str = ""
    azure_client_id: str = ""
    azure_client_secret: str = ""
    azure_certificate_path: str = ""
    azure_certificate_password: str = ""

    # AI Provider
    ai_provider: str = "anthropic"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-5-20250929"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    vertex_project: str = ""
    vertex_location: str = "us-central1"
    ai_temperature: float = 0.0
    ai_max_tokens: int = 1024

    # Processing
    max_file_size_bytes: int = 52_428_800  # 50MB
    text_content_limit: int = 100_000  # ~25K tokens
    sensitivity_threshold: int = 4
    hash_reuse_days: int = 30
    tmpfs_path: str = "/tmp/sharesentinel"

    # Notifications
    notification_channels: List[str] = field(default_factory=lambda: ["email"])
    notify_on_folder_share: bool = True
    notify_on_failure: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    email_from: str = ""
    email_to: List[str] = field(default_factory=list)

    # Jira
    jira_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_project_key: str = ""
    jira_issue_type: str = "Task"

    # Security / remediation
    security_email: str = "security@uvu.edu"

    # User profiles
    user_profile_cache_days: int = 7
    upn_domain: str = "uvu.edu"

    # Logging
    log_level: str = "INFO"

    # Prompt templates
    prompt_template_dir: str = "config/prompt_templates"

    @classmethod
    def from_env(cls) -> "Config":
        email_to_raw = os.environ.get("EMAIL_TO", "")
        channels_raw = os.environ.get("NOTIFICATION_CHANNELS", "email")

        return cls(
            redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
            database_url=os.environ.get("DATABASE_URL", ""),
            azure_tenant_id=os.environ.get("AZURE_TENANT_ID", ""),
            azure_client_id=os.environ.get("AZURE_CLIENT_ID", ""),
            azure_client_secret=os.environ.get("AZURE_CLIENT_SECRET", ""),
            azure_certificate_path=os.environ.get("AZURE_CERTIFICATE", ""),
            azure_certificate_password=os.environ.get("AZURE_CERTIFICATE_PASS", ""),
            ai_provider=os.environ.get("AI_PROVIDER", "anthropic"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-4o"),
            gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
            gemini_model=os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
            vertex_project=os.environ.get("VERTEX_PROJECT", ""),
            vertex_location=os.environ.get("VERTEX_LOCATION", "us-central1"),
            ai_temperature=float(os.environ.get("AI_TEMPERATURE", "0")),
            ai_max_tokens=int(os.environ.get("AI_MAX_TOKENS", "1024")),
            max_file_size_bytes=int(os.environ.get("MAX_FILE_SIZE_BYTES", "52428800")),
            text_content_limit=int(os.environ.get("TEXT_CONTENT_LIMIT", "100000")),
            sensitivity_threshold=int(os.environ.get("SENSITIVITY_THRESHOLD", "4")),
            hash_reuse_days=int(os.environ.get("HASH_REUSE_DAYS", "30")),
            notification_channels=[c.strip() for c in channels_raw.split(",") if c.strip()],
            notify_on_folder_share=os.environ.get("NOTIFY_ON_FOLDER_SHARE", "true").lower() == "true",
            notify_on_failure=os.environ.get("NOTIFY_ON_FAILURE", "true").lower() == "true",
            smtp_host=os.environ.get("SMTP_HOST", ""),
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_user=os.environ.get("SMTP_USER", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_use_tls=os.environ.get("SMTP_USE_TLS", "true").lower() == "true",
            email_from=os.environ.get("EMAIL_FROM", ""),
            email_to=[e.strip() for e in email_to_raw.split(",") if e.strip()],
            jira_url=os.environ.get("JIRA_URL", ""),
            jira_email=os.environ.get("JIRA_EMAIL", ""),
            jira_api_token=os.environ.get("JIRA_API_TOKEN", ""),
            jira_project_key=os.environ.get("JIRA_PROJECT_KEY", ""),
            jira_issue_type=os.environ.get("JIRA_ISSUE_TYPE", "Task"),
            security_email=os.environ.get("SECURITY_EMAIL", "security@uvu.edu"),
            user_profile_cache_days=int(os.environ.get("USER_PROFILE_CACHE_DAYS", "7")),
            upn_domain=os.environ.get("UPN_DOMAIN", "uvu.edu"),
            log_level=os.environ.get("LOG_LEVEL", "INFO"),
            prompt_template_dir=os.environ.get("PROMPT_TEMPLATE_DIR", "config/prompt_templates"),
        )
