"""
Google Drive as the storage layer.

Why Drive and not a database: the whole point of this pipeline is that Claude
can read the output. Claude reaches Google Drive through a connector that sees
files *owned by Bailey*. So this authenticates as the user via OAuth rather
than as a service account — a service-account-owned file would live in a
namespace the connector cannot see, and service accounts have no Drive storage
quota of their own anyway.

Google refresh tokens do NOT rotate (unlike WHOOP's), so the Google one lives
in a GitHub secret and the WHOOP one lives in _state.json here, which is the
only writable durable storage the workflow has.

Layout inside the target folder:

    _state.json          sync cursor + the rotating WHOOP token
    daily.csv            one row per physiological day  <- the trend file
    workouts.csv         one row per workout
    raw/YYYY-MM.json     verbatim API records, monthly, full fidelity
"""

from __future__ import annotations

import csv
import io
import json
import logging
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

log = logging.getLogger(__name__)

DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
STATE_FILE = "_state.json"


class DriveStore:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        folder_id: str,
    ) -> None:
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=DRIVE_SCOPES,
        )
        creds.refresh(Request())
        self._svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        self._folder_id = folder_id
        self._id_cache: dict[str, str] = {}

    # -------------------------------------------------------------- lookup

    def _find(self, name: str, parent: str | None = None) -> str | None:
        parent = parent or self._folder_id
        cache_key = f"{parent}/{name}"
        if cache_key in self._id_cache:
            return self._id_cache[cache_key]

        safe = name.replace("'", "\\'")
        resp = (
            self._svc.files()
            .list(
                q=f"name = '{safe}' and '{parent}' in parents and trashed = false",
                fields="files(id, name)",
                pageSize=10,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = resp.get("files", [])
        if not files:
            return None
        self._id_cache[cache_key] = files[0]["id"]
        return files[0]["id"]

    def _ensure_subfolder(self, name: str) -> str:
        existing = self._find(name)
        if existing:
            return existing
        meta = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [self._folder_id],
        }
        created = self._svc.files().create(
            body=meta, fields="id", supportsAllDrives=True
        ).execute()
        self._id_cache[f"{self._folder_id}/{name}"] = created["id"]
        return created["id"]

    # ---------------------------------------------------------------- i/o

    def _read_text(self, name: str, parent: str | None = None) -> str | None:
        file_id = self._find(name, parent)
        if not file_id:
            return None
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(
            buf, self._svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        )
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue().decode("utf-8")

    def _write_text(
        self, name: str, content: str, mime: str, parent: str | None = None
    ) -> None:
        parent = parent or self._folder_id
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")), mimetype=mime, resumable=False
        )
        file_id = self._find(name, parent)
        if file_id:
            self._svc.files().update(
                fileId=file_id, media_body=media, supportsAllDrives=True
            ).execute()
        else:
            created = self._svc.files().create(
                body={"name": name, "parents": [parent]},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()
            self._id_cache[f"{parent}/{name}"] = created["id"]

    # -------------------------------------------------------------- state

    def read_state(self) -> dict:
        raw = self._read_text(STATE_FILE)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.error("_state.json is corrupt; treating as empty")
            return {}

    def write_state(self, state: dict) -> None:
        self._write_text(STATE_FILE, json.dumps(state, indent=2), "application/json")

    # ---------------------------------------------------------------- csv

    def read_csv(self, name: str) -> list[dict]:
        raw = self._read_text(name)
        if not raw:
            return []
        return list(csv.DictReader(io.StringIO(raw)))

    def write_csv(self, name: str, rows: list[dict], columns: list[str]) -> None:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _blank(row.get(c)) for c in columns})
        self._write_text(name, buf.getvalue(), "text/csv")

    # ---------------------------------------------------------------- raw

    def merge_raw(self, month: str, payload: dict[str, list[dict]]) -> None:
        """
        Upsert verbatim records into raw/<month>.json, deduplicating on record id.

        Keeping the untouched API response means any question the flattened CSV
        can't answer is still answerable later without a re-backfill.
        """
        folder = self._ensure_subfolder("raw")
        name = f"{month}.json"
        existing_raw = self._read_text(name, parent=folder)
        existing: dict[str, list[dict]] = {}
        if existing_raw:
            try:
                existing = json.loads(existing_raw)
            except json.JSONDecodeError:
                log.warning("raw/%s is corrupt; rewriting", name)

        for collection, records in payload.items():
            by_id = {
                str(r.get("id")): r
                for r in existing.get(collection, [])
                if r.get("id") is not None
            }
            for r in records:
                if r.get("id") is not None:
                    by_id[str(r["id"])] = r
            existing[collection] = [by_id[k] for k in sorted(by_id)]

        self._write_text(
            name, json.dumps(existing, indent=1, sort_keys=True), "application/json",
            parent=folder,
        )


def _blank(value: Any) -> Any:
    """None -> empty cell, so downstream readers see a gap rather than 'None'."""
    return "" if value is None else value


def folder_id_from_url(url: str) -> str:
    """Accept either a bare folder ID or a full Drive folder URL."""
    url = url.strip()
    if "/folders/" in url:
        return url.split("/folders/")[1].split("?")[0].split("/")[0]
    return url
