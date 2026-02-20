"""Test Graph API transcript content fetch — the Feb 2026 transcript.

Organizer e2de7a95 has a transcript from 2026-02-11 — should still be available.
"""

import asyncio
import sys
import json

sys.path.insert(0, "/app")

import httpx
from app.config import Config
from app.graph_api.auth import GraphAuth

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_BETA = "https://graph.microsoft.com/beta"


async def main():
    config = Config.from_env()
    auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )
    token = auth.get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    organizer_id = "e2de7a95-3588-4ff6-9d26-7e7ae8a486d8"

    async with httpx.AsyncClient(timeout=30.0) as client:

        # ── Step 1: Get transcript details ──
        print("=" * 60)
        print(f"Fetching transcripts for organizer {organizer_id}")
        print("=" * 60)
        url = (
            f"{GRAPH_BETA}/users/{organizer_id}/onlineMeetings"
            f"/getAllTranscripts(meetingOrganizerUserId='{organizer_id}')"
        )
        resp = await client.get(url, headers=headers)
        print(f"  Status: {resp.status_code}")

        if resp.status_code != 200:
            print(f"  Error: {resp.text[:500]}")
            return

        transcripts = resp.json().get("value", [])
        print(f"  Transcripts: {len(transcripts)}")

        for t in transcripts:
            tid = t.get("id", "")
            meeting_id = t.get("meetingId", "")
            created = t.get("createdDateTime", "")
            content_url = t.get("transcriptContentUrl", "")
            call_id = t.get("callId", "")
            end_dt = t.get("endDateTime", "")

            print(f"\n  Transcript details:")
            print(f"    ID:          {tid[:60]}...")
            print(f"    Created:     {created}")
            print(f"    End:         {end_dt}")
            print(f"    Call ID:     {call_id}")
            print(f"    Meeting ID:  {meeting_id[:60]}...")
            print(f"    Content URL: {content_url[:120]}...")
            print(f"    All data:    {json.dumps(t, indent=2)[:500]}")

            # ── Step 2: Try to fetch VTT via beta ──
            print(f"\n  --- Fetching VTT (beta) ---")
            vtt_url = (
                f"{GRAPH_BETA}/users/{organizer_id}/onlineMeetings/{meeting_id}"
                f"/transcripts/{tid}/content?$format=text/vtt"
            )
            c_resp = await client.get(vtt_url, headers=headers)
            print(f"    Status: {c_resp.status_code}")
            if c_resp.status_code == 200:
                vtt = c_resp.text
                print(f"    VTT length: {len(vtt)} chars")
                print(f"\n    --- First 1500 chars ---")
                print(vtt[:1500])
                print(f"\n    --- Last 500 chars ---")
                print(vtt[-500:])
            else:
                print(f"    Error: {c_resp.text[:500]}")

            # ── Step 3: Try v1.0 endpoint ──
            print(f"\n  --- Fetching VTT (v1.0) ---")
            vtt_url_v1 = (
                f"{GRAPH_BASE}/users/{organizer_id}/onlineMeetings/{meeting_id}"
                f"/transcripts/{tid}/content?$format=text/vtt"
            )
            c_resp2 = await client.get(vtt_url_v1, headers=headers)
            print(f"    Status: {c_resp2.status_code}")
            if c_resp2.status_code == 200:
                vtt2 = c_resp2.text
                print(f"    VTT length: {len(vtt2)} chars")
                print(f"    First 200 chars: {vtt2[:200]}")
            else:
                print(f"    Error: {c_resp2.text[:300]}")

            # ── Step 4: Try content URL directly ──
            if content_url:
                print(f"\n  --- Fetching via transcriptContentUrl ---")
                c_resp3 = await client.get(content_url, headers=headers)
                print(f"    Status: {c_resp3.status_code}")
                if c_resp3.status_code == 200:
                    print(f"    Length: {len(c_resp3.text)} chars")
                    print(f"    First 200 chars: {c_resp3.text[:200]}")
                else:
                    print(f"    Error: {c_resp3.text[:300]}")

            # ── Step 5: Get meeting details ──
            print(f"\n  --- Meeting details ---")
            m_url = f"{GRAPH_BETA}/users/{organizer_id}/onlineMeetings/{meeting_id}"
            m_resp = await client.get(m_url, headers=headers)
            print(f"    Status: {m_resp.status_code}")
            if m_resp.status_code == 200:
                m = m_resp.json()
                print(f"    Subject:  {m.get('subject', '?')}")
                print(f"    Start:    {m.get('startDateTime', '?')}")
                print(f"    End:      {m.get('endDateTime', '?')}")
                print(f"    JoinUrl:  {m.get('joinWebUrl', '?')[:80]}...")
            else:
                print(f"    Error: {m_resp.text[:300]}")

        print()


if __name__ == "__main__":
    asyncio.run(main())
