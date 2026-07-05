# ShareSentinel Tier 1 Support Guide

A plain-language overview of what ShareSentinel does, how exceptions are
granted, and the automated checks that keep the tenant in compliance. This
guide is for front-line technicians — for technical/container internals see
the rest of `docs/`.

---

## What ShareSentinel Enforces

ShareSentinel watches three things across the Microsoft 365 tenant and
automatically pulls them back into compliance:

| Policy | What "compliant" looks like | What ShareSentinel does if it finds a violation |
|---|---|---|
| **Anonymous Sharing** | SharePoint sites do **not** allow "Anyone with the link" sharing. | Downgrades the site so anonymous links can no longer be created. |
| **Public Visibility** | M365 Groups (and the Teams/SharePoint sites behind them) are set to **Private**. | Flips the group from Public to Private via the Graph API. |
| **Company-Wide Sharing (CWS) Links** | Users **cannot** create "People in your organization" sharing links from their OneDrive. | Disables company-wide sharing on the user's OneDrive site. |

Each policy has its own allow list. If a site or user is on the allow list,
the violation is recorded but **not** remediated.

---

## How Violations Are Detected & Fixed

### 1. Anonymous Sharing on Sites

- A daily scan reads every SharePoint site's `SharingCapability` setting.
- Any site set to `ExternalUserAndGuestSharing` (anonymous links allowed)
  that is **not** on the **Anonymous Sharing allow list** is automatically
  downgraded to `ExternalUserSharingOnly` (guest sharing only, no anonymous).
- Existing anonymous links on the site are not removed by this step — the
  separate sharing-link lifecycle expires them on a 180-day schedule.

### 2. Public Visibility on M365 Groups

- The same daily scan enumerates all M365 Unified Groups.
- Any group whose visibility is `Public` and that is **not** on the
  **Public Visibility allow list** is automatically flipped to `Private`.
- When a group is flipped to Private, the cascade also disables anonymous
  sharing on the underlying site (unless that site is on the Anonymous
  Sharing allow list).

### 3. Company-Wide Sharing (CWS) Links

CWS is enforced per-user, on each user's OneDrive site, using the
`DisableCompanyWideSharingLinks` property.

- **Default state**: Every user's OneDrive should have CWS **disabled**.
- **Exception**: Users in the designated **Entra ID exception group** are
  allowed to create company-wide links. Their OneDrive sites are left with
  CWS **enabled (NotDisabled)**.
- **Site-level CWS allow list**: Specific SharePoint sites (not OneDrive)
  can be allow-listed to keep CWS enabled on the site itself.

A daily verification sweep enumerates all OneDrive and SharePoint sites,
compares each site's CWS state against what it should be (based on
exception-group membership and the CWS allow list), and queues drift rows.
A second daily job drains the drift queue and applies the correct CWS
setting to each out-of-compliance site.

---

## Granting Exceptions (Admin Actions)

All exceptions are managed from the dashboard **Site Policy Management**
page. Each tab corresponds to one allow list.

### Allowing a SharePoint Site to Use Anonymous Links

1. Open the dashboard → **Site Policy Management** → **Anonymous Sharing**
   tab.
2. Search for the site by name or URL.
3. Click **Add to allow list**.
4. ShareSentinel immediately re-enables anonymous sharing on that site (no
   need to wait for the next scheduled scan).

Removing a site from this list immediately disables anonymous sharing on
that site again.

### Allowing an M365 Group / Site to Be Public

1. Open the dashboard → **Site Policy Management** → **Public Visibility**
   tab.
2. Search for the group by name or email.
3. Click **Add to allow list**.
4. ShareSentinel immediately flips the group back to Public (if it had
   been changed to Private).

Removing a group from this list immediately flips it back to Private and
cascades the anonymous-sharing policy onto the underlying site.

### Allowing a SharePoint Site to Use Company-Wide Sharing Links

1. Open the dashboard → **Site Policy Management** → **Company-Wide
   Sharing** tab.
2. Search for the SharePoint site.
3. Click **Add to allow list**.
4. ShareSentinel queues the change and applies it to the site within a
   few seconds.

This tab is for **SharePoint sites**, not individual user OneDrives — for
user exceptions, see the next section.

### Allowing a User to Create Company-Wide Sharing Links

User exceptions are **not** managed in the dashboard. They are managed by
membership in an Entra ID security group.

1. Open the Entra ID admin center.
2. Locate the **Company-Wide Sharing Exception** group (group ID is
   configured in the `COMPANY_WIDE_SHARING_EXCEPTION_GROUP_ID` setting).
3. Add the user to the group.
4. Within **10 minutes**, the scheduled exception-group sync detects the
   new member, finds that user's OneDrive site, and enables CWS on it.

Removing the user from the group reverses the action on the next 10-minute
sync — their OneDrive has CWS disabled again.

> Read-only view: the **Company-Wide Sharing** tab in the dashboard has a
> section that lists current exception-group members so you can confirm
> who is exempted without leaving the dashboard.

---

## Scheduled Compliance Jobs (Cheat Sheet)

These run in the background inside the `lifecycle-cron` container. All
intervals are defaults — they can be changed by an admin.

| Job | Cadence | What it checks | What it does |
|---|---|---|---|
| **Audit log poller** | Every 15 min | New `AnonymousLinkCreated` / `CompanyLinkCreated` events from the Graph audit log. | Queues sharing events for content sensitivity analysis. |
| **Sharing link lifecycle** | Daily | Anonymous/org-wide links nearing or past 180 days. | Sends countdown emails at 120/150/165/173/178 days; removes the link at 180 days. |
| **Site policy scan** | Daily | All M365 Groups (Public vs Private) and all SharePoint sites (anonymous sharing on/off). | Flips Public groups to Private; disables anonymous sharing on sites — unless allow-listed. |
| **CWS verification sweep** | Daily | Every OneDrive + SharePoint site's `DisableCompanyWideSharingLinks` state. | Detect-only: writes drift rows. Does **not** change anything. |
| **CWS drift remediation** | Daily | Open drift rows from the verification sweep. | Applies the correct CWS setting to each drifted site. |
| **CWS bulk remediation** | Monthly | Full-tenant safety-net pass. | Same fix as drift remediation, but tenant-wide. |
| **CWS exception-group sync** | **Every 10 min** | Changes in the Entra ID exception group membership. | Adds → enables CWS on the new member's OneDrive. Removes → disables it again. |
| **CWS failure diagnostic** | Every 30 min | CWS sites that failed to remediate. | Re-runs read-only diagnostics so the dashboard can show a current cause. |
| **Folder rescan** | Weekly | Previously-shared folders. | Re-scans contents for new/modified files inheriting the share. |

All of these jobs can also be triggered manually from the dashboard
(buttons like **Scan Now** and **Re-diagnose**) when an admin doesn't
want to wait for the next scheduled run.

---

## When Things Go Wrong

- **An admin added a site to an allow list but the policy didn't lift.**
  Allow-list adds push a Redis action that is normally processed within
  seconds. If it doesn't take effect, the most common cause is the
  `lifecycle-cron` container being down — check container health, then
  the dashboard's **Enforcement Events** tab for an error message.
- **A user reports they can't create a company-wide link.**
  Confirm they are in the Entra ID exception group. If they were just
  added, wait up to 10 minutes for the next sync. If the sync ran but
  their OneDrive still has CWS disabled, the **Company-Wide Sharing**
  tab's failures table will show the diagnostic (most common: their
  OneDrive hasn't been provisioned yet).
- **A site keeps getting flipped back to Public / anonymous-on.**
  It is being remediated because it is **not** on the relevant allow
  list. Add it to the appropriate allow list to grant the exception.

For anything that the dashboard's **Enforcement Events** tab can't
explain, escalate to tier 2 with the affected site URL or user UPN and
the timestamp of the event.
