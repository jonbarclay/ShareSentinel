# Azure Permissions for Transcript Retrieval

## Overview

The video/audio transcription pipeline uses the Microsoft Graph API to retrieve
Teams meeting transcripts. This requires additional Azure AD permissions and a
Teams Application Access Policy beyond the existing ShareSentinel app registration.

## Required Graph API Permissions

Add these **Application** permissions to the existing ShareSentinel Azure AD app registration:

| Permission | Type | Description |
|---|---|---|
| `OnlineMeetingTranscript.Read.All` | Application | Read all meeting transcripts |
| `OnlineMeetings.Read.All` | Application | Read all online meetings |

### Steps to Grant Permissions

1. Go to the [Azure Portal](https://portal.azure.com) > **Azure Active Directory** > **App registrations**
2. Select the **ShareSentinel** app registration
3. Go to **API permissions** > **Add a permission**
4. Select **Microsoft Graph** > **Application permissions**
5. Search for and add:
   - `OnlineMeetingTranscript.Read.All`
   - `OnlineMeetings.Read.All`
6. Click **Grant admin consent for [your tenant]** (requires Global Administrator or Privileged Role Administrator)

## Teams Application Access Policy

Application permissions for online meetings and transcripts require an **Application Access Policy** in Teams. Without this policy, the Graph API will return `403 Forbidden` when querying meetings.

### Steps to Create the Policy

These commands require the **Microsoft Teams PowerShell module** and a Teams Administrator account.

```powershell
# 1. Install Teams PowerShell module (if not already installed)
Install-Module -Name MicrosoftTeams -Force -AllowClobber

# 2. Connect to Teams
Connect-MicrosoftTeams

# 3. Create the application access policy
#    Replace <client-id> with the ShareSentinel app registration's Application (client) ID
New-CsApplicationAccessPolicy `
    -Identity "ShareSentinel-TranscriptPolicy" `
    -AppIds "<client-id>" `
    -Description "Allow ShareSentinel to read meeting transcripts"

# 4. Grant the policy globally (all users)
#    This allows the app to access meetings for ANY user in the tenant
Grant-CsApplicationAccessPolicy `
    -PolicyName "ShareSentinel-TranscriptPolicy" `
    -Global

# 5. Verify the policy was applied
Get-CsApplicationAccessPolicy -Identity "ShareSentinel-TranscriptPolicy"
```

### Important Notes

- **Propagation delay**: After creating/granting the policy, it can take **up to 30 minutes** to propagate across the Teams infrastructure.
- **Global scope**: The `-Global` flag grants access to meetings of all users. If you need more restrictive access, you can grant the policy to specific users instead:
  ```powershell
  Grant-CsApplicationAccessPolicy `
      -PolicyName "ShareSentinel-TranscriptPolicy" `
      -Identity "user@yourdomain.com"
  ```
- **Transcript availability**: Not all meetings have transcripts. Transcripts are only available when:
  - Meeting transcription was enabled (either by the organizer or via Teams admin policy)
  - The meeting was a Teams meeting (not a PSTN/dial-in only call)
  - The transcript has finished processing (typically a few minutes after the meeting ends)

## Verification

After granting permissions and creating the policy, verify with a test Graph API call:

```bash
# Get an access token for the app
TOKEN=$(curl -s -X POST \
    "https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token" \
    -d "client_id=<client-id>" \
    -d "client_secret=<client-secret>" \
    -d "scope=https://graph.microsoft.com/.default" \
    -d "grant_type=client_credentials" | jq -r '.access_token')

# Query a user's online meetings (replace with a real user ID)
curl -s -H "Authorization: Bearer $TOKEN" \
    "https://graph.microsoft.com/v1.0/users/<user-id>/onlineMeetings?\$top=5" | jq .
```

A successful response returns a list of meetings. A `403` indicates the Application Access Policy has not propagated yet or was not applied correctly.

## Summary of All ShareSentinel Permissions

After adding transcript permissions, the full list of Application permissions for the ShareSentinel app registration is:

| Permission | Purpose |
|---|---|
| `Files.Read.All` | Download shared files for analysis |
| `Sites.Read.All` | Read SharePoint site metadata |
| `Sites.FullControl.All` | Remove sharing links (lifecycle expiration) |
| `AuditLogsQuery.Read.All` | Poll audit log for sharing events |
| `OnlineMeetings.Read.All` | Query online meetings to find transcripts |
| `OnlineMeetingTranscript.Read.All` | Read meeting transcript content |
