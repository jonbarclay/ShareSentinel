# 08 - Notification Service

## Purpose

The notification service alerts human analysts when a file or folder requires review. Phase 1 uses email notifications via SMTP. Phase 2 adds Jira ticket creation. Both implement a common interface so the system can use either (or both) notification channels.

## Notifier Interface

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class AlertPayload:
    """All the information an analyst needs to act on an alert."""
    event_id: str
    alert_type: str                     # "high_sensitivity_file", "folder_share", "processing_failure"
    
    # File/item details
    file_name: str
    file_path: str
    file_size_human: str                # e.g., "2.3 MB"
    item_type: str                      # "File" or "Folder"
    
    # Sharing details
    sharing_user: str                   # Email/UPN of the person who shared
    sharing_type: str                   # "Anonymous" or "Organization-wide"
    sharing_permission: str             # "View" or "Edit"
    event_time: str                     # When the sharing link was created
    sharing_link_url: Optional[str]     # Clickable link to the shared item
    
    # AI analysis results (NULL for folder shares and processing failures)
    categories_detected: Optional[List[str]]  # e.g., ["pii_financial", "ferpa"]
    context: Optional[str]              # "mixed", "educational", "personal", etc.
    summary: Optional[str]
    recommendation: Optional[str]
    analysis_mode: Optional[str]        # "text", "multimodal", "filename_only"
    
    # Additional context
    filename_flagged: bool = False
    filename_flag_keywords: Optional[List[str]] = None
    was_sampled: bool = False
    sampling_description: Optional[str] = None
    failure_reason: Optional[str] = None  # For processing failure alerts

class BaseNotifier(ABC):
    """Abstract base class for notification channels."""
    
    @abstractmethod
    async def send_alert(self, payload: AlertPayload) -> bool:
        """
        Send an alert to analysts.
        Returns True if the notification was sent successfully.
        """
        pass
    
    @abstractmethod
    def get_channel_name(self) -> str:
        """Return the notification channel name (e.g., 'email', 'jira')."""
        pass
```

## Phase 1: Email Notifier

Send formatted HTML emails to one or more analyst email addresses via SMTP.

### Email Template Structure

**Subject line patterns:**
- Sensitive file (Tier 1): `[ShareSentinel] ⚠️ Urgent: Sensitive File Detected - {file_name}`
- Sensitive file (Tier 2): `[ShareSentinel] Sensitive File Detected - {file_name}`
- Folder share: `[ShareSentinel] 📁 Folder Shared with {sharing_type} Access - {file_name}`
- Processing failure: `[ShareSentinel] ❌ Processing Failed - {file_name}`

**Email body** (HTML template stored in `config/notification_templates/analyst_alert.html`):

The email should contain the following sections:

1. **Alert Summary Banner**
   - Alert type (color-coded: red for Tier 1 categories, orange for Tier 2, yellow for folder share)
   - Detected categories (if applicable)
   - Quick action link (the sharing link)

2. **File Details**
   - File name
   - File path
   - File size
   - Item type

3. **Sharing Details**
   - Who shared it
   - Sharing type (anonymous or org-wide)
   - Permission level (view or edit)
   - When the link was created

4. **AI Analysis Results** (for file alerts)
   - Categories detected with tier indicator
   - Summary of findings
   - Confidence level
   - AI recommendation
   - Analysis mode used (text, multimodal, filename-only)
   - Note if content was sampled

5. **Action Required**
   - Clear call to action for the analyst
   - Link to view the file
   - Note about what to do (contact the user, request sharing restriction, etc.)

6. **Technical Details** (collapsed/footer)
   - Event ID
   - Processing timestamp
   - AI provider and model used

### Email Implementation

```python
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from jinja2 import Template

class EmailNotifier(BaseNotifier):
    def __init__(self, smtp_host: str, smtp_port: int, smtp_user: str, 
                 smtp_password: str, from_address: str, to_addresses: List[str],
                 use_tls: bool = True):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_address = from_address
        self.to_addresses = to_addresses
        self.use_tls = use_tls
        self.template = self._load_template()
    
    def _load_template(self) -> Template:
        template_path = Path("config/notification_templates/analyst_alert.html")
        return Template(template_path.read_text())
    
    async def send_alert(self, payload: AlertPayload) -> bool:
        try:
            subject = self._build_subject(payload)
            html_body = self.template.render(**payload.__dict__)
            
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = self.from_address
            msg['To'] = ', '.join(self.to_addresses)
            
            # Plain text version (fallback)
            text_body = self._build_plain_text(payload)
            msg.attach(MIMEText(text_body, 'plain'))
            msg.attach(MIMEText(html_body, 'html'))
            
            # Send
            if self.use_tls:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                server.starttls()
            else:
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
            
            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)
            
            server.sendmail(self.from_address, self.to_addresses, msg.as_string())
            server.quit()
            
            return True
        except Exception as e:
            logger.error(f"Failed to send email notification: {e}")
            return False
    
    def get_channel_name(self) -> str:
        return "email"
```

### Daily Summary Email (Optional Enhancement)

In addition to individual alerts, send a daily summary email containing:
- Total sharing events processed in the last 24 hours
- Breakdown by sensitivity rating
- Any processing failures
- Any folder shares detected
- Top sharing users (most frequent sharers)

This provides analysts with a birds-eye view of sharing activity even when individual files don't trigger alerts.

## Phase 2: Jira Notifier

Create Jira tickets for each alert, enabling proper tracking and workflow management.

### Jira Ticket Structure

**Project**: Configurable (e.g., "SECOPS" or "DLP")

**Issue Type**: "Task" or a custom issue type like "DLP Alert"

**Fields:**
- **Summary**: Same pattern as email subject line
- **Description**: Formatted version of the email body (using Jira wiki markup or ADF)
- **Priority**:
  - Tier 1 categories (pii_government_id, ferpa, hipaa, etc.) → "Highest"
  - Tier 2 categories (hr_personnel, legal_confidential, pii_contact) → "High"
  - Folder share → "Medium"
  - Processing failure → "Low"
- **Labels**: `sharesentinel`, `dlp`, category labels (e.g., `pii_financial`, `ferpa`)
- **Assignee**: Configurable (assign to a team or individual)
- **Components**: Configurable (e.g., "Data Loss Prevention")
- **Custom fields** (if available): sensitivity_rating, sharing_user, file_name

### Jira Implementation

```python
import httpx

class JiraNotifier(BaseNotifier):
    def __init__(self, jira_url: str, jira_email: str, jira_api_token: str,
                 project_key: str, issue_type: str = "Task"):
        self.jira_url = jira_url.rstrip('/')
        self.auth = (jira_email, jira_api_token)
        self.project_key = project_key
        self.issue_type = issue_type
    
    async def send_alert(self, payload: AlertPayload) -> bool:
        try:
            issue_data = self._build_issue(payload)
            
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{self.jira_url}/rest/api/3/issue",
                    json=issue_data,
                    auth=self.auth,
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
                
                ticket_key = response.json()["key"]
                logger.info(f"Created Jira ticket {ticket_key} for event {payload.event_id}")
                return True
                
        except Exception as e:
            logger.error(f"Failed to create Jira ticket: {e}")
            return False
    
    def get_channel_name(self) -> str:
        return "jira"
```

## Notification Dispatcher

The dispatcher manages which notification channels are active and sends alerts through all of them.

```python
class NotificationDispatcher:
    def __init__(self, notifiers: List[BaseNotifier]):
        self.notifiers = notifiers
    
    async def dispatch(self, payload: AlertPayload) -> dict:
        """
        Send alert through all configured notification channels.
        Returns a dict of {channel_name: success_bool}.
        """
        results = {}
        for notifier in self.notifiers:
            channel = notifier.get_channel_name()
            try:
                success = await notifier.send_alert(payload)
                results[channel] = success
            except Exception as e:
                logger.error(f"Notification channel {channel} failed: {e}")
                results[channel] = False
        return results
```

## Alert Triggering Logic

The pipeline triggers notifications in these cases:

1. **Sensitive file** (Tier 1 or Tier 2 category detected): The AI determined the file contains sensitive content. Escalation is deterministic — any Tier 1 or Tier 2 category triggers notification with no configurable threshold.
2. **Folder share**: A folder was shared with anonymous or org-wide access (always triggers, regardless of AI).
3. **Processing failure** (optional): A file could not be analyzed due to repeated failures. The analyst should be aware that a sharing event occurred but couldn't be evaluated.

**Category tiers for escalation:**
- **Tier 1 (urgent)**: `pii_government_id`, `pii_financial`, `ferpa`, `hipaa`, `security_credentials`
- **Tier 2 (normal)**: `hr_personnel`, `legal_confidential`, `pii_contact`
- **Tier 3 (no escalation)**: `coursework`, `casual_personal`, `none`

```python
# Tier definitions
TIER_1 = {"pii_government_id", "pii_financial", "ferpa", "hipaa", "security_credentials"}
TIER_2 = {"hr_personnel", "legal_confidential", "pii_contact"}

async def maybe_notify(event: dict, verdict: dict, dispatcher: NotificationDispatcher, db):
    """Determine if notification is needed and send it."""

    should_notify = False
    alert_type = None

    if event["item_type"] == "Folder":
        should_notify = True
        alert_type = "folder_share"
    elif verdict:
        categories = set(verdict.get("categories_detected", []))
        if categories & TIER_1:
            should_notify = True
            alert_type = "high_sensitivity_file"  # urgent
        elif categories & TIER_2:
            should_notify = True
            alert_type = "high_sensitivity_file"
    elif event["status"] == "failed":
        should_notify = True
        alert_type = "processing_failure"

    if should_notify:
        payload = build_alert_payload(event, verdict, alert_type)
        results = await dispatcher.dispatch(payload)

        for channel, success in results.items():
            await db.update_notification_status(
                event_id=event["event_id"],
                sent=success,
                channel=channel,
                reference=""
            )
```

## Configuration

| Variable | Description | Default |
|----------|-------------|---------|
| `NOTIFICATION_CHANNELS` | Comma-separated list of active channels | `email` |
| `NOTIFY_ON_FOLDER_SHARE` | Whether to notify on folder shares | `true` |
| `NOTIFY_ON_FAILURE` | Whether to notify on processing failures | `true` |
| `SMTP_HOST` | SMTP server hostname | (required for email) |
| `SMTP_PORT` | SMTP server port | `587` |
| `SMTP_USER` | SMTP username | (required for email) |
| `SMTP_PASSWORD` | SMTP password | (required for email) |
| `SMTP_USE_TLS` | Use TLS for SMTP | `true` |
| `EMAIL_FROM` | Sender email address | (required for email) |
| `EMAIL_TO` | Comma-separated analyst email addresses | (required for email) |
| `JIRA_URL` | Jira instance URL | (required for jira) |
| `JIRA_EMAIL` | Jira authentication email | (required for jira) |
| `JIRA_API_TOKEN` | Jira API token | (required for jira) |
| `JIRA_PROJECT_KEY` | Jira project key for tickets | (required for jira) |
| `JIRA_ISSUE_TYPE` | Jira issue type | `Task` |
