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

    # Processing
    max_file_size_bytes: int = 4_294_967_296  # 4GB
    text_content_limit: int = 100_000  # ~25K tokens
    hash_reuse_days: int = 30
    tmpfs_path: str = "/tmp/sharesentinel"

    # Notifications
    analyst_notification_enabled: bool = True
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

    # Dashboard
    dashboard_url: str = ""

    # Security / remediation
    security_email: str = ""

    # User notifications (post-disposition emails to file owners)
    user_notification_enabled: bool = False
    user_notification_override_email: str = ""
    user_notification_bcc: str = ""
    user_notification_ai_provider: str = ""
    user_notification_ai_model: str = ""

    # User profiles
    user_profile_cache_days: int = 7
    upn_domain: str = ""

    # Second-look AI review
    second_look_enabled: bool = False
    second_look_provider: str = "gemini"
    second_look_model: str = "gemini-3.1-pro-preview"

    # Transcription (audio/video pipeline)
    transcription_enabled: bool = True
    max_av_file_size_bytes: int = 4_294_967_296  # 4GB
    graph_transcript_timeout_seconds: int = 30
    whisper_enabled: bool = True
    whisper_model: str = "base.en"
    whisper_service_url: str = "http://transcriber:8090"

    # Video keyframe extraction (multimodal transcript analysis)
    video_frame_extraction_enabled: bool = True
    max_keyframes_per_video: int = 3

    # Requeue / retry
    max_event_retries: int = 3
    requeue_base_delay_seconds: int = 60
    stuck_processing_timeout_minutes: int = 30

    # Concurrency
    max_concurrent_jobs: int = 5
    max_concurrent_av_jobs: int = 2

    # Logging
    log_level: str = "INFO"

    # Prompt templates
    prompt_template_dir: str = "config/prompt_templates"

    @classmethod
    def from_env(cls, db_overrides: dict[str, str] | None = None) -> "Config":
        ov = db_overrides or {}

        def _g(db_key: str, env_key: str, default: str) -> str:
            """DB override > env var > default."""
            return ov.get(db_key) or os.environ.get(env_key, default)

        email_to_raw = _g("email_to", "EMAIL_TO", "")
        channels_raw = _g("notification_channels", "NOTIFICATION_CHANNELS", "email")

        return cls(
            redis_url=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
            database_url=os.environ.get("DATABASE_URL", ""),
            azure_tenant_id=os.environ.get("AZURE_TENANT_ID", ""),
            azure_client_id=os.environ.get("AZURE_CLIENT_ID", ""),
            azure_client_secret=os.environ.get("AZURE_CLIENT_SECRET", ""),
            azure_certificate_path=os.environ.get("AZURE_CERTIFICATE", ""),
            azure_certificate_password=os.environ.get("AZURE_CERTIFICATE_PASS", ""),
            ai_provider=_g("ai_provider", "AI_PROVIDER", "anthropic"),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            anthropic_model=_g("anthropic_model", "ANTHROPIC_MODEL", "claude-sonnet-4-5-20250929"),
            openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
            openai_model=_g("openai_model", "OPENAI_MODEL", "gpt-4o"),
            gemini_api_key=os.environ.get("GEMINI_API_KEY", ""),
            gemini_model=_g("gemini_model", "GEMINI_MODEL", "gemini-2.0-flash"),
            vertex_project=os.environ.get("VERTEX_PROJECT", ""),
            vertex_location=os.environ.get("VERTEX_LOCATION", "us-central1"),
            ai_temperature=float(_g("ai_temperature", "AI_TEMPERATURE", "0")),
            max_file_size_bytes=int(_g("max_file_size_bytes", "MAX_FILE_SIZE_BYTES", "4294967296")),
            text_content_limit=int(_g("text_content_limit", "TEXT_CONTENT_LIMIT", "100000")),
            hash_reuse_days=int(_g("hash_reuse_days", "HASH_REUSE_DAYS", "30")),
            analyst_notification_enabled=_g("analyst_notification_enabled", "ANALYST_NOTIFICATION_ENABLED", "true").lower() == "true",
            notification_channels=[c.strip() for c in channels_raw.split(",") if c.strip()],
            notify_on_folder_share=_g("notify_on_folder_share", "NOTIFY_ON_FOLDER_SHARE", "true").lower() == "true",
            notify_on_failure=_g("notify_on_failure", "NOTIFY_ON_FAILURE", "true").lower() == "true",
            smtp_host=os.environ.get("SMTP_HOST", ""),
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_user=os.environ.get("SMTP_USER", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            smtp_use_tls=os.environ.get("SMTP_USE_TLS", "true").lower() == "true",
            email_from=_g("email_from", "EMAIL_FROM", ""),
            email_to=[e.strip() for e in email_to_raw.split(",") if e.strip()],
            jira_url=os.environ.get("JIRA_URL", ""),
            jira_email=os.environ.get("JIRA_EMAIL", ""),
            jira_api_token=os.environ.get("JIRA_API_TOKEN", ""),
            jira_project_key=os.environ.get("JIRA_PROJECT_KEY", ""),
            jira_issue_type=os.environ.get("JIRA_ISSUE_TYPE", "Task"),
            dashboard_url=_g("dashboard_url", "DASHBOARD_URL", ""),
            security_email=_g("security_email", "SECURITY_EMAIL", ""),
            user_notification_enabled=_g("user_notification_enabled", "USER_NOTIFICATION_ENABLED", "false").lower() == "true",
            user_notification_override_email=_g("user_notification_override_email", "USER_NOTIFICATION_OVERRIDE_EMAIL", ""),
            user_notification_bcc=_g("user_notification_bcc", "USER_NOTIFICATION_BCC", ""),
            user_notification_ai_provider=os.environ.get("USER_NOTIFICATION_AI_PROVIDER", ""),
            user_notification_ai_model=os.environ.get("USER_NOTIFICATION_AI_MODEL", ""),
            user_profile_cache_days=int(os.environ.get("USER_PROFILE_CACHE_DAYS", "7")),
            upn_domain=os.environ.get("UPN_DOMAIN", ""),
            second_look_enabled=_g("second_look_enabled", "SECOND_LOOK_ENABLED", "false").lower() == "true",
            second_look_provider=_g("second_look_provider", "SECOND_LOOK_PROVIDER", "gemini"),
            second_look_model=_g("second_look_model", "SECOND_LOOK_MODEL", "gemini-3.1-pro-preview"),
            transcription_enabled=os.environ.get("TRANSCRIPTION_ENABLED", "true").lower() == "true",
            max_av_file_size_bytes=int(os.environ.get("MAX_AV_FILE_SIZE_BYTES", "4294967296")),
            graph_transcript_timeout_seconds=int(os.environ.get("GRAPH_TRANSCRIPT_TIMEOUT_SECONDS", "30")),
            whisper_enabled=os.environ.get("WHISPER_ENABLED", "true").lower() == "true",
            whisper_model=os.environ.get("WHISPER_MODEL", "base.en"),
            whisper_service_url=os.environ.get("WHISPER_SERVICE_URL", "http://transcriber:8090"),
            video_frame_extraction_enabled=os.environ.get("FRAME_EXTRACTION_ENABLED", "true").lower() == "true",
            max_keyframes_per_video=int(os.environ.get("MAX_KEYFRAMES_PER_VIDEO", "3")),
            max_event_retries=int(os.environ.get("MAX_EVENT_RETRIES", "3")),
            requeue_base_delay_seconds=int(os.environ.get("REQUEUE_BASE_DELAY_SECONDS", "60")),
            stuck_processing_timeout_minutes=int(os.environ.get("STUCK_PROCESSING_TIMEOUT_MINUTES", "30")),
            max_concurrent_jobs=int(_g("max_concurrent_jobs", "MAX_CONCURRENT_JOBS", "5")),
            max_concurrent_av_jobs=int(_g("max_concurrent_av_jobs", "MAX_CONCURRENT_AV_JOBS", "2")),
            log_level=_g("log_level", "LOG_LEVEL", "INFO"),
            prompt_template_dir=os.environ.get("PROMPT_TEMPLATE_DIR", "config/prompt_templates"),
        )
