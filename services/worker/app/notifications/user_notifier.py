"""User notification service — AI-generated emails to file owners after analyst disposition."""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncpg
from jinja2 import Environment, FileSystemLoader

from ..ai.base_provider import CATEGORY_LABELS, BaseAIProvider
from ..config import Config
from ..database.repositories import AuditLogRepository
from ..graph_api.auth import GraphAuth
from ..graph_api.client import GraphClient

logger = logging.getLogger(__name__)

# Dispositions that trigger user notification
NOTIFIABLE_DISPOSITIONS = {"true_positive", "moderate_risk"}

# Template directory (same as email_notifier pattern)
_TEMPLATE_DIR = Path("/app/config/notification_templates")
_PROMPT_DIR = Path("/app/config/prompt_templates")


def _find_dir(docker_path: Path, dirname: str) -> Path:
    """Find a config subdirectory, preferring the Docker mount."""
    if docker_path.is_dir():
        return docker_path
    current = Path(__file__).resolve().parent
    for _ in range(10):
        candidate = current / "config" / dirname
        if candidate.is_dir():
            return candidate
        current = current.parent
    return Path("config") / dirname


class UserNotifier:
    """Orchestrates AI-generated notification emails to file owners."""

    def __init__(
        self,
        config: Config,
        db_pool: asyncpg.Pool,
        ai_provider: BaseAIProvider,
        graph_auth: GraphAuth,
    ) -> None:
        self._config = config
        self._db_pool = db_pool
        self._ai = ai_provider
        self._graph_auth = graph_auth
        self._graph_client = GraphClient(auth=graph_auth)
        self._audit = AuditLogRepository(db_pool)

        template_dir = _find_dir(_TEMPLATE_DIR, "notification_templates")
        self._jinja_env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            autoescape=True,
        )

        prompt_dir = _find_dir(_PROMPT_DIR, "prompt_templates")
        self._prompt_template = self._load_prompt_template(prompt_dir)

    # ------------------------------------------------------------------
    # Prompt template loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_prompt_template(prompt_dir: Path) -> Dict[str, str]:
        """Load the user_notification_email.txt prompt into mode sections."""
        path = prompt_dir / "user_notification_email.txt"
        if not path.exists():
            logger.error("User notification prompt template not found at %s", path)
            return {}

        raw = path.read_text(encoding="utf-8")
        templates: Dict[str, str] = {}
        system_prompt = ""

        sections = raw.split("### MODE: ")
        for section in sections:
            if not section.strip():
                continue
            stripped = section.strip().lstrip("#").strip()
            if stripped.startswith("SYSTEM PROMPT"):
                idx = section.find("###", section.find("SYSTEM PROMPT"))
                if idx != -1:
                    system_prompt = section[idx + 3:].strip()
                else:
                    system_prompt = stripped.replace("SYSTEM PROMPT", "", 1).strip()
                continue
            if "###" in section:
                mode, body = section.split("###", 1)
                mode = mode.strip()
                body = body.strip()
                templates[mode] = body

        templates["_system"] = system_prompt
        logger.info("Loaded user notification prompt modes: %s", [k for k in templates if k != "_system"])
        return templates

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_user_notification(self, event_id: str, disposition: str) -> bool:
        """Full notification flow for a single event.

        Returns True if notification was sent successfully.
        """
        if not self._config.user_notification_enabled:
            logger.debug("User notifications disabled, skipping event %s", event_id)
            return False

        if disposition not in NOTIFIABLE_DISPOSITIONS:
            logger.debug("Disposition %s not notifiable, skipping event %s", disposition, event_id)
            return False

        try:
            # Load event + verdict from DB
            async with self._db_pool.acquire() as conn:
                event = await conn.fetchrow(
                    "SELECT * FROM events WHERE event_id = $1", event_id
                )
                if not event:
                    logger.error("Event %s not found for user notification", event_id)
                    return False

                verdict = await conn.fetchrow(
                    "SELECT * FROM verdicts WHERE event_id = $1 ORDER BY id DESC LIMIT 1",
                    event_id,
                )

            # Resolve recipients
            recipients = await self._resolve_recipients(event)
            if not recipients:
                if self._config.user_notification_bcc:
                    # No end-user recipient found, but BCC is configured —
                    # send directly to BCC so the admin still gets a copy.
                    logger.info(
                        "No recipients resolved for event %s, sending to BCC address",
                        event_id,
                    )
                    recipients = [{
                        "email": self._config.user_notification_bcc,
                        "name": "ShareSentinel Admin",
                        "type": "bcc_fallback",
                    }]
                else:
                    logger.warning("No recipients resolved for event %s", event_id)
                    await self._audit.log(event_id, "user_notification_no_recipients")
                    return False

            # Get category labels
            labels = self._get_category_labels(verdict)

            # Apply override if configured
            recipients = self._apply_override(recipients)

            # Send to each recipient
            all_sent = True
            for recipient in recipients:
                try:
                    sent = await self._notify_recipient(
                        event_id=event_id,
                        disposition=disposition,
                        event=event,
                        verdict=verdict,
                        recipient=recipient,
                        labels=labels,
                    )
                    if not sent:
                        all_sent = False
                except Exception:
                    logger.exception(
                        "Failed to notify recipient %s for event %s",
                        recipient.get("email"), event_id,
                    )
                    all_sent = False

            return all_sent

        except Exception:
            logger.exception("Unhandled error in user notification for event %s", event_id)
            return False

    # ------------------------------------------------------------------
    # Recipient resolution
    # ------------------------------------------------------------------

    async def _resolve_recipients(self, event: Any) -> List[Dict[str, str]]:
        """Determine who should receive the notification.

        - Personal OneDrive: email the sharing user
        - SharePoint site: email the site owner(s)
        """
        recipients: List[Dict[str, str]] = []

        site_url = event.get("site_url") or event.get("object_id") or ""
        user_id = event.get("user_id") or ""

        # Detect SharePoint site vs personal OneDrive
        is_sharepoint_site = (
            ".sharepoint.com/sites/" in site_url
            and "-my.sharepoint.com/personal/" not in site_url
        )

        if is_sharepoint_site:
            # Get site owners via Graph API
            owners = await self._graph_client.get_site_owners(site_url)
            for owner in owners:
                mail = owner.get("mail") or ""
                name = owner.get("displayName") or ""
                if mail:
                    recipients.append({
                        "email": mail,
                        "name": name,
                        "type": "site_owner",
                    })
            if recipients:
                logger.info(
                    "Resolved %d site owner(s) for %s", len(recipients), site_url
                )
                return recipients
            # Fall through to sharing user if no site owners found
            logger.warning(
                "No site owners found for %s, falling back to sharing user", site_url
            )

        # Personal OneDrive or fallback: email the sharing user
        if user_id and user_id != "unknown@unknown.com":
            # Try Graph API first, then fall back to cached profile in DB
            try:
                profile = await self._graph_client.get_user_profile(user_id)
                mail = profile.get("mail") or ""
                name = profile.get("displayName") or ""
                if mail:
                    recipients.append({
                        "email": mail,
                        "name": name,
                        "type": "sharing_user",
                    })
            except Exception:
                logger.warning("Graph profile lookup failed for %s, trying DB cache", user_id)

            # If Graph didn't work, try the cached user_profiles table
            if not recipients:
                try:
                    from ..database.repositories import UserProfileRepository
                    user_repo = UserProfileRepository(self._db_pool)
                    # user_id in events is UPN like "10001213@uvu.edu"; strip domain
                    uid = user_id.split("@")[0] if "@" in user_id else user_id
                    cached = await user_repo.get_cached(uid, cache_days=30)
                    if cached:
                        mail = cached.get("mail") or ""
                        name = cached.get("display_name") or cached.get("displayName") or ""
                        if mail:
                            recipients.append({
                                "email": mail,
                                "name": name,
                                "type": "sharing_user",
                            })
                            logger.info("Resolved recipient from cached profile: %s", mail)
                except Exception:
                    logger.warning("DB profile lookup also failed for %s", user_id)

        return recipients

    # ------------------------------------------------------------------
    # Category labels
    # ------------------------------------------------------------------

    @staticmethod
    def _get_category_labels(verdict: Any) -> List[str]:
        """Map verdict category IDs to human-readable labels."""
        if not verdict:
            return []

        cat_assessments = verdict.get("category_assessments") or []
        if isinstance(cat_assessments, str):
            try:
                cat_assessments = json.loads(cat_assessments)
            except (ValueError, TypeError):
                cat_assessments = []

        labels = []
        seen = set()
        for ca in cat_assessments:
            if not isinstance(ca, dict):
                continue
            cat_id = ca.get("id", "")
            if cat_id and cat_id not in seen and cat_id not in ("none", "coursework", "casual_personal", "directory_info"):
                label = CATEGORY_LABELS.get(cat_id, cat_id)
                labels.append(label)
                seen.add(cat_id)

        return labels

    # ------------------------------------------------------------------
    # Per-recipient notification
    # ------------------------------------------------------------------

    async def _notify_recipient(
        self,
        event_id: str,
        disposition: str,
        event: Any,
        verdict: Any,
        recipient: Dict[str, str],
        labels: List[str],
    ) -> bool:
        """Generate AI email body, render template, send email, record in DB."""
        recipient_email = recipient["email"]
        recipient_name = recipient.get("name") or recipient_email.split("@")[0]
        recipient_type = recipient.get("type", "sharing_user")
        override_active = recipient.get("override_active", False)
        original_email = recipient.get("original_email", "")

        file_name = event.get("confirmed_file_name") or event.get("file_name") or "Unknown"
        file_path = event.get("relative_path") or event.get("object_id") or ""
        sharing_type = event.get("sharing_type") or "Unknown"

        # Generate email body via AI
        ai_body, ai_meta = await self._generate_email_body(
            disposition=disposition,
            recipient_name=recipient_name,
            file_name=file_name,
            file_path=file_path,
            sharing_type=sharing_type,
            category_labels=labels,
            event_id=event_id,
            verdict=verdict,
        )

        if not ai_body:
            # Record failure
            await self._record_notification(
                event_id=event_id,
                disposition=disposition,
                recipient_email=recipient_email,
                recipient_name=recipient_name,
                recipient_type=recipient_type,
                ai_meta=ai_meta,
                generated_body="",
                subject="",
                labels=labels,
                status="failed",
                error="AI email generation failed",
                override_active=override_active,
                original_email=original_email,
            )
            return False

        # Render full HTML template
        html_body = self._render_template(
            disposition=disposition,
            ai_generated_body=ai_body,
            file_name=file_name,
            file_path=file_path,
            sharing_type=sharing_type,
            category_labels=labels,
            event_id=event_id,
        )

        # Build subject line
        if disposition == "true_positive":
            subject = f"[ShareSentinel] Sharing Link Removed - {file_name}"
        else:
            subject = f"[ShareSentinel] Action Requested: Review Shared File - {file_name}"

        # Send email
        sent = await self._send_email(
            to_address=recipient_email,
            subject=subject,
            html_body=html_body,
        )

        # Record in DB
        await self._record_notification(
            event_id=event_id,
            disposition=disposition,
            recipient_email=recipient_email,
            recipient_name=recipient_name,
            recipient_type=recipient_type,
            ai_meta=ai_meta,
            generated_body=ai_body,
            subject=subject,
            labels=labels,
            status="sent" if sent else "failed",
            error="" if sent else "SMTP send failed",
            override_active=override_active,
            original_email=original_email,
        )

        if sent:
            await self._audit.log(
                event_id, "user_notification_sent",
                {
                    "recipient": recipient_email,
                    "disposition": disposition,
                    "recipient_type": recipient_type,
                    "override_active": override_active,
                },
            )
            logger.info(
                "User notification sent for event %s to %s (%s)",
                event_id, recipient_email, disposition,
            )
        else:
            await self._audit.log(
                event_id, "user_notification_failed",
                {"recipient": recipient_email},
                status="error", error="SMTP send failed",
            )

        return sent

    # ------------------------------------------------------------------
    # AI email body generation
    # ------------------------------------------------------------------

    async def _generate_email_body(
        self,
        disposition: str,
        recipient_name: str,
        file_name: str,
        file_path: str,
        sharing_type: str,
        category_labels: List[str],
        event_id: str,
        verdict: Any = None,
    ) -> tuple[str, Dict[str, Any]]:
        """Call the AI provider to generate the email body HTML.

        Returns (html_body_string, metadata_dict).
        """
        meta: Dict[str, Any] = {
            "provider": "",
            "model": "",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost": 0.0,
        }

        # Choose the prompt mode
        mode = "true_positive" if disposition == "true_positive" else "moderate_risk"
        template_body = self._prompt_template.get(mode)
        system_prompt = self._prompt_template.get("_system", "")

        if not template_body:
            logger.error("No prompt template for mode %s", mode)
            return "", meta

        # Fill template variables
        labels_str = ", ".join(category_labels) if category_labels else "potentially sensitive content"

        # Extract analysis context from verdict
        analysis_summary = ""
        analysis_reasoning = ""
        analysis_recommendation = ""
        second_look_summary = ""
        second_look_reasoning = ""
        if verdict:
            analysis_summary = verdict.get("summary") or ""
            analysis_reasoning = verdict.get("reasoning") or ""
            analysis_recommendation = verdict.get("recommendation") or ""
            second_look_summary = verdict.get("second_look_summary") or ""
            second_look_reasoning = verdict.get("second_look_reasoning") or ""

        class _Default(dict):
            def __missing__(self, key: str) -> str:
                return "{" + key + "}"

        prompt = template_body.format_map(_Default({
            "recipient_name": recipient_name,
            "file_name": file_name,
            "file_path": file_path,
            "sharing_type": sharing_type,
            "category_labels": labels_str,
            "event_id": event_id,
            "analysis_summary": analysis_summary,
            "analysis_reasoning": analysis_reasoning,
            "analysis_recommendation": analysis_recommendation,
            "second_look_summary": second_look_summary or "Not performed",
            "second_look_reasoning": second_look_reasoning or "Not performed",
        }))

        try:
            start = time.time()

            # Use the AI provider's underlying client directly
            # This avoids coupling to AnalysisRequest/AnalysisResponse
            provider_name = self._ai.get_provider_name()
            model_name = self._ai.get_model_name()

            if provider_name == "anthropic":
                import anthropic
                response = await self._ai.client.messages.create(
                    model=model_name,
                    max_tokens=1024,
                    temperature=0.4,
                    system=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                )
                body = response.content[0].text
                meta["input_tokens"] = response.usage.input_tokens
                meta["output_tokens"] = response.usage.output_tokens
            elif provider_name == "openai":
                # Some OpenAI models (e.g. gpt-5-nano) don't support
                # custom temperature or max_tokens — use only universally
                # supported params and let the model use its defaults.
                oai_kwargs: dict[str, Any] = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": prompt},
                    ],
                }
                # Try to set optional params; omit if unsupported
                try:
                    response = await self._ai.client.chat.completions.create(
                        **oai_kwargs,
                        max_completion_tokens=1024,
                        temperature=0.4,
                    )
                except Exception:
                    # Retry without optional params
                    response = await self._ai.client.chat.completions.create(**oai_kwargs)
                body = response.choices[0].message.content
                meta["input_tokens"] = response.usage.prompt_tokens
                meta["output_tokens"] = response.usage.completion_tokens
            elif provider_name == "gemini":
                # Use the Vertex AI REST API via the provider's httpx client
                request_body = {
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "generationConfig": {
                        "temperature": 0.4,
                        "maxOutputTokens": 1024,
                    },
                }
                url = f"{self._ai._base_url}/{model_name}:generateContent?key={self._ai._api_key}"
                # Retry with backoff on 429 rate limits
                resp = None
                for attempt in range(4):
                    resp = await self._ai._http.post(url, json=request_body)
                    if resp.status_code != 429:
                        break
                    wait = (attempt + 1) * 15
                    logger.warning("Gemini 429 rate limited, retrying in %ds (attempt %d)", wait, attempt + 1)
                    await asyncio.sleep(wait)
                resp.raise_for_status()
                data = resp.json()
                candidates = data.get("candidates", [])
                body = ""
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        body = parts[0].get("text", "")
                usage = data.get("usageMetadata", {})
                meta["input_tokens"] = usage.get("promptTokenCount", 0)
                meta["output_tokens"] = usage.get("candidatesTokenCount", 0)
            else:
                logger.error("Unsupported AI provider for user notification: %s", provider_name)
                return "", meta

            processing_time = time.time() - start
            meta["provider"] = provider_name
            meta["model"] = model_name
            meta["processing_time"] = processing_time

            # Strip any accidental wrapper tags the AI might have included
            body = body.strip()
            for tag in ("```html", "```", "<html>", "</html>", "<body>", "</body>", "<!DOCTYPE html>", "<head>", "</head>"):
                body = body.replace(tag, "")
            body = body.strip()

            logger.info(
                "AI generated user notification email (%s/%s, %d tokens) in %.1fs",
                provider_name, model_name,
                meta["input_tokens"] + meta["output_tokens"],
                processing_time,
            )
            return body, meta

        except Exception:
            logger.exception("Failed to generate AI email body for event %s", event_id)
            return "", meta

    # ------------------------------------------------------------------
    # Template rendering
    # ------------------------------------------------------------------

    def _render_template(
        self,
        disposition: str,
        ai_generated_body: str,
        file_name: str,
        file_path: str,
        sharing_type: str,
        category_labels: List[str],
        event_id: str,
    ) -> str:
        """Render the Jinja2 HTML wrapper template."""
        template = self._jinja_env.get_template("user_notification.html")
        return template.render(
            disposition=disposition,
            ai_generated_body=ai_generated_body,
            file_name=file_name,
            file_path=file_path,
            sharing_type=sharing_type,
            category_labels=category_labels,
            event_id=event_id,
        )

    # ------------------------------------------------------------------
    # SMTP sending
    # ------------------------------------------------------------------

    async def _send_email(self, to_address: str, subject: str, html_body: str) -> bool:
        """Send the notification email via SMTP."""
        config = self._config
        if not config.smtp_host:
            logger.warning("SMTP not configured, cannot send user notification")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = config.email_from
            msg["To"] = to_address
            if config.user_notification_bcc:
                msg["Bcc"] = config.user_notification_bcc

            # Plain text fallback
            plain = (
                "This email contains an HTML notification from ShareSentinel. "
                "Please view it in an HTML-capable email client, or contact "
                "security@uvu.edu for details."
            )
            msg.attach(MIMEText(plain, "plain"))
            msg.attach(MIMEText(html_body, "html"))

            # Build envelope recipients (To + Bcc)
            envelope_recipients = [to_address]
            if config.user_notification_bcc:
                envelope_recipients.append(config.user_notification_bcc)

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self._send_smtp, msg, envelope_recipients
            )
            return True

        except Exception:
            logger.exception("Failed to send user notification to %s", to_address)
            return False

    def _send_smtp(self, msg: MIMEMultipart, recipients: List[str]) -> None:
        """Blocking SMTP send, run in executor."""
        config = self._config
        server: Optional[smtplib.SMTP] = None
        try:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port)
            server.ehlo()
            if config.smtp_use_tls:
                server.starttls()
                server.ehlo()
            if config.smtp_user and config.smtp_password:
                server.login(config.smtp_user, config.smtp_password)
            server.sendmail(config.email_from, recipients, msg.as_string())
        finally:
            if server is not None:
                try:
                    server.quit()
                except smtplib.SMTPException:
                    pass

    # ------------------------------------------------------------------
    # Override logic
    # ------------------------------------------------------------------

    def _apply_override(self, recipients: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """If override email is configured, redirect all emails there."""
        override = self._config.user_notification_override_email
        if not override:
            return recipients

        overridden = []
        for r in recipients:
            overridden.append({
                "email": override,
                "name": r.get("name", ""),
                "type": r.get("type", "sharing_user"),
                "override_active": True,
                "original_email": r["email"],
            })
        logger.info(
            "Override active: redirecting %d recipient(s) to %s",
            len(recipients), override,
        )
        return overridden

    # ------------------------------------------------------------------
    # DB recording
    # ------------------------------------------------------------------

    async def _record_notification(
        self,
        event_id: str,
        disposition: str,
        recipient_email: str,
        recipient_name: str,
        recipient_type: str,
        ai_meta: Dict[str, Any],
        generated_body: str,
        subject: str,
        labels: List[str],
        status: str,
        error: str,
        override_active: bool,
        original_email: str,
    ) -> None:
        """Insert a row into the user_notifications table."""
        try:
            async with self._db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO user_notifications (
                        event_id, trigger_disposition,
                        recipient_email, recipient_name, recipient_type,
                        ai_provider, ai_model, generated_subject, generated_body,
                        input_tokens, output_tokens, estimated_cost_usd,
                        category_labels,
                        status, sent_at, error_message,
                        override_active, original_recipient_email
                    ) VALUES (
                        $1, $2,
                        $3, $4, $5,
                        $6, $7, $8, $9,
                        $10, $11, $12,
                        $13,
                        $14, $15, $16,
                        $17, $18
                    )
                    """,
                    event_id,
                    disposition,
                    recipient_email,
                    recipient_name,
                    recipient_type,
                    ai_meta.get("provider", ""),
                    ai_meta.get("model", ""),
                    subject,
                    generated_body,
                    ai_meta.get("input_tokens", 0),
                    ai_meta.get("output_tokens", 0),
                    ai_meta.get("cost", 0.0),
                    labels,
                    status,
                    datetime.now(timezone.utc) if status == "sent" else None,
                    error or None,
                    override_active,
                    original_email or None,
                )
        except Exception:
            logger.exception("Failed to record user notification for event %s", event_id)
