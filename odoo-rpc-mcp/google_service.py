"""
Google Services Integration — Gmail & Calendar via OAuth2.

Provides Gmail (search, read, send, reply) and Calendar (list, events, CRUD)
for the MCP server using Google API Python Client.

Auth: OAuth2 with saved tokens. Requires credentials.json from Google Cloud Console.
"""
import base64
import json
import logging
import os
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Any

logger = logging.getLogger("google-service")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar",
]

CREDENTIALS_FILE = Path(os.environ.get(
    "GOOGLE_CREDENTIALS_FILE", "/data/google_credentials.json"
))
TOKEN_FILE = Path(os.environ.get(
    "GOOGLE_TOKEN_FILE", "/data/google_token.json"
))


class GoogleServiceManager:
    """Manages Google OAuth2 authentication and API access."""

    def __init__(self):
        self._credentials = None
        self._gmail_service = None
        self._calendar_service = None
        self._try_load_token()

    def _try_load_token(self):
        if not TOKEN_FILE.exists():
            return
        try:
            from google.oauth2.credentials import Credentials
            self._credentials = Credentials.from_authorized_user_file(
                str(TOKEN_FILE), SCOPES
            )
            if self._credentials and self._credentials.expired and self._credentials.refresh_token:
                from google.auth.transport.requests import Request
                self._credentials.refresh(Request())
                self._save_token()
            logger.info("Google token loaded successfully")
        except Exception as e:
            logger.warning(f"Failed to load Google token: {e}")
            self._credentials = None

    def _save_token(self):
        if self._credentials:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            TOKEN_FILE.write_text(self._credentials.to_json())

    @property
    def is_authenticated(self) -> bool:
        return self._credentials is not None and self._credentials.valid

    def authenticate(self, credentials_file: str = "") -> dict:
        """Run OAuth2 flow. Returns status dict."""
        creds_path = Path(credentials_file) if credentials_file else CREDENTIALS_FILE

        if not creds_path.exists():
            return {
                "status": "credentials_needed",
                "message": (
                    f"Google credentials file not found at {creds_path}.\n"
                    "Steps:\n"
                    "1. Go to https://console.cloud.google.com/\n"
                    "2. Create/select a project\n"
                    "3. Enable Gmail API and Google Calendar API\n"
                    "4. Credentials → Create OAuth 2.0 Client ID (Desktop app)\n"
                    "5. Download JSON and save as:\n"
                    f"   {creds_path}"
                ),
            }

        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            self._credentials = flow.run_local_server(port=0, open_browser=True)
            self._save_token()
            self._gmail_service = None
            self._calendar_service = None
            return {"status": "authenticated", "email": self._get_email()}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _get_email(self) -> str:
        try:
            service = self._get_gmail()
            profile = service.users().getProfile(userId="me").execute()
            return profile.get("emailAddress", "unknown")
        except Exception:
            return "unknown"

    def _get_gmail(self):
        if not self.is_authenticated:
            raise Exception("Not authenticated with Google. Call google_auth first.")
        if self._gmail_service is None:
            from googleapiclient.discovery import build
            self._gmail_service = build("gmail", "v1", credentials=self._credentials)
        return self._gmail_service

    def _get_calendar(self):
        if not self.is_authenticated:
            raise Exception("Not authenticated with Google. Call google_auth first.")
        if self._calendar_service is None:
            from googleapiclient.discovery import build
            self._calendar_service = build(
                "calendar", "v3", credentials=self._credentials
            )
        return self._calendar_service

    # ── Gmail ──

    def gmail_search(
        self, query: str, max_results: int = 10, label_ids: list = None
    ) -> list:
        service = self._get_gmail()
        kwargs: dict[str, Any] = {
            "userId": "me", "q": query, "maxResults": max_results,
        }
        if label_ids:
            kwargs["labelIds"] = label_ids
        results = service.users().messages().list(**kwargs).execute()
        messages = results.get("messages", [])

        output = []
        for msg in messages:
            detail = service.users().messages().get(
                userId="me", id=msg["id"], format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()
            headers = {
                h["name"]: h["value"]
                for h in detail.get("payload", {}).get("headers", [])
            }
            output.append({
                "id": msg["id"],
                "threadId": msg.get("threadId"),
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": detail.get("snippet", ""),
                "labelIds": detail.get("labelIds", []),
            })
        return output

    def gmail_read(self, message_id: str) -> dict:
        service = self._get_gmail()
        msg = service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        body = self._extract_body(msg.get("payload", {}))

        return {
            "id": msg["id"],
            "threadId": msg.get("threadId"),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "cc": headers.get("Cc", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
            "body": body,
            "labelIds": msg.get("labelIds", []),
            "snippet": msg.get("snippet", ""),
        }

    def _extract_body(self, payload: dict) -> str:
        if payload.get("body", {}).get("data"):
            return base64.urlsafe_b64decode(
                payload["body"]["data"]
            ).decode("utf-8", errors="replace")

        for mime in ("text/plain", "text/html"):
            for part in payload.get("parts", []):
                if part.get("mimeType") == mime and part.get("body", {}).get("data"):
                    return base64.urlsafe_b64decode(
                        part["body"]["data"]
                    ).decode("utf-8", errors="replace")
                # Nested multipart
                if part.get("parts"):
                    result = self._extract_body(part)
                    if result:
                        return result
        return ""

    def gmail_send(
        self, to: str, subject: str, body: str,
        cc: str = "", bcc: str = "", html: bool = False,
        reply_to_message_id: str = "",
    ) -> dict:
        service = self._get_gmail()

        if html:
            message = MIMEMultipart("alternative")
            message.attach(MIMEText(body, "html"))
        else:
            message = MIMEText(body)

        message["to"] = to
        message["subject"] = subject
        if cc:
            message["cc"] = cc
        if bcc:
            message["bcc"] = bcc

        body_dict: dict[str, Any] = {}

        if reply_to_message_id:
            original = service.users().messages().get(
                userId="me", id=reply_to_message_id, format="metadata",
                metadataHeaders=["Message-ID", "Subject"],
            ).execute()
            orig_headers = {
                h["name"]: h["value"]
                for h in original.get("payload", {}).get("headers", [])
            }
            body_dict["threadId"] = original.get("threadId")
            message["In-Reply-To"] = orig_headers.get("Message-ID", "")
            message["References"] = orig_headers.get("Message-ID", "")

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body_dict["raw"] = raw

        result = service.users().messages().send(
            userId="me", body=body_dict
        ).execute()
        return {
            "status": "sent",
            "id": result["id"],
            "threadId": result.get("threadId"),
        }

    def gmail_labels(self) -> list:
        service = self._get_gmail()
        results = service.users().labels().list(userId="me").execute()
        return results.get("labels", [])

    # ── Calendar ──

    def calendar_list(self) -> list:
        service = self._get_calendar()
        results = service.calendarList().list().execute()
        return [
            {
                "id": c["id"],
                "summary": c.get("summary", ""),
                "primary": c.get("primary", False),
                "accessRole": c.get("accessRole", ""),
            }
            for c in results.get("items", [])
        ]

    def calendar_events(
        self, calendar_id: str = "primary", time_min: str = "",
        time_max: str = "", max_results: int = 10, query: str = "",
    ) -> list:
        service = self._get_calendar()
        kwargs: dict[str, Any] = {
            "calendarId": calendar_id,
            "maxResults": max_results,
            "singleEvents": True,
            "orderBy": "startTime",
        }
        if time_min:
            kwargs["timeMin"] = time_min
        else:
            kwargs["timeMin"] = datetime.now(timezone.utc).isoformat()
        if time_max:
            kwargs["timeMax"] = time_max
        if query:
            kwargs["q"] = query

        results = service.events().list(**kwargs).execute()
        return [
            {
                "id": e["id"],
                "summary": e.get("summary", ""),
                "description": e.get("description", ""),
                "start": e.get("start", {}),
                "end": e.get("end", {}),
                "location": e.get("location", ""),
                "attendees": e.get("attendees", []),
                "status": e.get("status", ""),
                "htmlLink": e.get("htmlLink", ""),
            }
            for e in results.get("items", [])
        ]

    def calendar_create_event(
        self, summary: str, start: str, end: str,
        calendar_id: str = "primary", description: str = "",
        location: str = "", attendees: list = None,
        timezone_str: str = "Europe/Sofia",
    ) -> dict:
        service = self._get_calendar()
        event: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start, "timeZone": timezone_str},
            "end": {"dateTime": end, "timeZone": timezone_str},
        }
        if description:
            event["description"] = description
        if location:
            event["location"] = location
        if attendees:
            event["attendees"] = [{"email": a} for a in attendees]

        result = service.events().insert(
            calendarId=calendar_id, body=event
        ).execute()
        return {
            "status": "created",
            "id": result["id"],
            "htmlLink": result.get("htmlLink", ""),
            "summary": result.get("summary", ""),
            "start": result.get("start", {}),
            "end": result.get("end", {}),
        }

    def calendar_update_event(
        self, event_id: str, calendar_id: str = "primary", **kwargs
    ) -> dict:
        service = self._get_calendar()
        event = service.events().get(
            calendarId=calendar_id, eventId=event_id
        ).execute()

        tz = kwargs.get("timezone", "Europe/Sofia")
        for field in ("summary", "description", "location"):
            if field in kwargs:
                event[field] = kwargs[field]
        if "start" in kwargs:
            event["start"] = {"dateTime": kwargs["start"], "timeZone": tz}
        if "end" in kwargs:
            event["end"] = {"dateTime": kwargs["end"], "timeZone": tz}
        if "attendees" in kwargs:
            event["attendees"] = [{"email": a} for a in kwargs["attendees"]]

        result = service.events().update(
            calendarId=calendar_id, eventId=event_id, body=event
        ).execute()
        return {
            "status": "updated",
            "id": result["id"],
            "htmlLink": result.get("htmlLink", ""),
        }

    def calendar_delete_event(
        self, event_id: str, calendar_id: str = "primary"
    ) -> dict:
        service = self._get_calendar()
        service.events().delete(
            calendarId=calendar_id, eventId=event_id
        ).execute()
        return {"status": "deleted", "event_id": event_id}
