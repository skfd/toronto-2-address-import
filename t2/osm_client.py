"""OSM API 0.6 client: OAuth2 PKCE + changeset lifecycle with crash-safe idempotency."""
import base64
import hashlib
import json
import os
import secrets
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any

import requests
from cryptography.fernet import Fernet

from . import audit, batcher, config as _config, db as _db, osm_export

_CONFIG = _config.load()

_AUTHORIZE = "/oauth2/authorize"
_TOKEN = "/oauth2/token"
_API = "/api/0.6"

SCOPES = "read_prefs write_api write_changesets"


class OsmAuthError(Exception):
    pass


def _fernet() -> Fernet:
    key = _CONFIG.fernet_key
    if not key:
        raise OsmAuthError("FERNET_KEY not set in .env")
    return Fernet(key.encode() if isinstance(key, str) else key)


def _kv_set(key: str, value: str) -> None:
    conn = _db.connect()
    try:
        conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    finally:
        conn.close()


def _kv_get(key: str) -> str | None:
    conn = _db.connect()
    try:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def store_tokens(token_json: dict[str, Any]) -> None:
    blob = _fernet().encrypt(json.dumps(token_json).encode()).decode()
    _kv_set("osm_oauth_tokens", blob)
    _kv_set("osm_oauth_stored_at", datetime.now(timezone.utc).isoformat())


def load_tokens() -> dict[str, Any] | None:
    blob = _kv_get("osm_oauth_tokens")
    if not blob:
        return None
    try:
        return json.loads(_fernet().decrypt(blob.encode()).decode())
    except Exception:
        return None


def build_auth_url() -> tuple[str, str]:
    """Return (authorize_url, state). Also stores PKCE verifier in kv keyed by state."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    _kv_set(f"pkce:{state}", verifier)
    params = {
        "response_type": "code",
        "client_id": _CONFIG.osm_client_id,
        "redirect_uri": _CONFIG.osm_redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    qs = "&".join(f"{k}={requests.utils.quote(str(v), safe='')}" for k, v in params.items())
    return f"{_CONFIG.osm_api_base}{_AUTHORIZE}?{qs}", state


def exchange_code(code: str, state: str) -> None:
    verifier = _kv_get(f"pkce:{state}")
    if not verifier:
        raise OsmAuthError("Unknown OAuth state (PKCE verifier missing).")
    resp = requests.post(
        f"{_CONFIG.osm_api_base}{_TOKEN}",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _CONFIG.osm_redirect_uri,
            "client_id": _CONFIG.osm_client_id,
            "client_secret": _CONFIG.osm_client_secret,
            "code_verifier": verifier,
        },
        timeout=30,
    )
    resp.raise_for_status()
    store_tokens(resp.json())
    audit.log(actor="oauth", event_type="OAUTH_TOKEN_REFRESHED", payload={"method": "exchange_code"})


def _refresh_tokens(tokens: dict) -> dict:
    rt = tokens.get("refresh_token")
    if not rt:
        raise OsmAuthError("No refresh_token available; re-authorize.")
    resp = requests.post(
        f"{_CONFIG.osm_api_base}{_TOKEN}",
        data={
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": _CONFIG.osm_client_id,
            "client_secret": _CONFIG.osm_client_secret,
        },
        timeout=30,
    )
    resp.raise_for_status()
    new_tok = resp.json()
    store_tokens(new_tok)
    audit.log(actor="oauth", event_type="OAUTH_TOKEN_REFRESHED", payload={"method": "refresh"})
    return new_tok


def _request(method: str, path: str, **kwargs) -> requests.Response:
    tokens = load_tokens()
    if not tokens:
        raise OsmAuthError("Not authorized; visit /oauth/start first.")
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {tokens['access_token']}"

    for attempt in range(6):
        resp = requests.request(method, f"{_CONFIG.osm_api_base}{path}", headers=headers, timeout=120, **kwargs)
        if resp.status_code == 401:
            tokens = _refresh_tokens(tokens)
            headers["Authorization"] = f"Bearer {tokens['access_token']}"
            continue
        if resp.status_code in (429, 509):
            wait = min(2 ** attempt, 60)
            time.sleep(wait)
            continue
        return resp
    resp.raise_for_status()
    return resp


def find_changeset_by_client_token(client_token: str) -> int | None:
    """Look up a prior changeset we opened with this client_token tag. Used for crash recovery."""
    tokens = load_tokens()
    if not tokens:
        return None
    # OSM has no direct tag-search for changesets, so we scan recent user changesets.
    resp = _request("GET", f"{_API}/user/details.json")
    if resp.status_code != 200:
        return None
    uid = resp.json().get("user", {}).get("id")
    if not uid:
        return None
    r = _request("GET", f"{_API}/changesets", params={"user": uid})
    if r.status_code != 200:
        return None
    root = ET.fromstring(r.text)
    for cs in root.findall("changeset"):
        for tag in cs.findall("tag"):
            if tag.attrib.get("k") == "import:client_token" and tag.attrib.get("v") == client_token:
                return int(cs.attrib["id"])
    return None


def _create_changeset(batch_id: int) -> int:
    payload = ET.Element("osm")
    cs = ET.SubElement(payload, "changeset")
    for k, v in osm_export.changeset_tags(batch_id).items():
        ET.SubElement(cs, "tag", k=k, v=v)
    body = ET.tostring(payload, encoding="utf-8")
    r = _request("PUT", f"{_API}/changeset/create", data=body, headers={"Content-Type": "text/xml"})
    r.raise_for_status()
    return int(r.text.strip())


def _upload_diff(changeset_id: int, batch_id: int) -> dict[int, int]:
    body = osm_export.osmchange_xml(batch_id, changeset_id)
    r = _request("POST", f"{_API}/changeset/{changeset_id}/upload",
                 data=body, headers={"Content-Type": "text/xml"})
    if r.status_code != 200:
        raise RuntimeError(f"Upload failed: HTTP {r.status_code} {r.text[:500]}")
    root = ET.fromstring(r.text)
    mapping: dict[int, int] = {}
    for el in root.findall("node"):
        mapping[int(el.attrib["old_id"])] = int(el.attrib["new_id"])
    return mapping


def _close_changeset(changeset_id: int) -> None:
    _request("PUT", f"{_API}/changeset/{changeset_id}/close")


def upload(batch_id: int) -> None:
    """Upload a batch via the OSM API, idempotent under Ctrl-C."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _db.connect()
    try:
        row = conn.execute(
            "SELECT b.run_id, b.status, b.changeset_id, b.client_token, b.size, r.name "
            "FROM batches b JOIN runs r ON r.run_id = b.run_id WHERE b.batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"batch {batch_id} not found")
        if row["status"] == "uploaded":
            return
        run_id = row["run_id"]
        run_name = row["name"]
        client_token = row["client_token"]
        changeset_id = row["changeset_id"]
    finally:
        conn.close()

    comment = _CONFIG.changeset_comment_template.format(run_name=run_name, batch_id=batch_id)

    # Crash recovery: if no changeset_id locally but server has one for our token, adopt it.
    if changeset_id is None:
        existing = find_changeset_by_client_token(client_token)
        if existing:
            changeset_id = existing
        else:
            changeset_id = _create_changeset(batch_id)

        conn = _db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE batches SET changeset_id=?, status='uploading' WHERE batch_id=?",
                (changeset_id, batch_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO changesets (changeset_id, run_id, opened_at, comment, status) "
                "VALUES (?, ?, ?, ?, 'open')",
                (changeset_id, run_id, now, comment),
            )
            audit.log(actor="osm_client", event_type="CHANGESET_OPENED",
                      run_id=run_id, batch_id=batch_id,
                      payload={"changeset_id": changeset_id, "comment": comment}, conn=conn)
            conn.execute("COMMIT")
        finally:
            conn.close()

    # Upload diff (idempotent enough: if server already has our nodes from a prior attempt,
    # a retry will 409; we surface that rather than auto-recover).
    try:
        mapping = _upload_diff(changeset_id, batch_id)
    except Exception as e:
        conn = _db.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE batches SET status='needs_attention', error_msg=? WHERE batch_id=?",
                (str(e)[:500], batch_id),
            )
            audit.log(actor="osm_client", event_type="UPLOAD_FAILED",
                      run_id=run_id, batch_id=batch_id, payload={"error": str(e)[:500]}, conn=conn)
            conn.execute("COMMIT")
        finally:
            conn.close()
        raise

    # Fill osm_node_ids, mark items uploaded, close changeset. If the server's
    # diffResult didn't include a mapping for every batch item, flag the batch
    # as needs_attention instead of uploaded — the successful items still
    # transition to UPLOADED, but the operator must reconcile the rest.
    conn = _db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        for local_id, new_id in mapping.items():
            conn.execute(
                "UPDATE batch_items SET osm_node_id=?, upload_status='uploaded' "
                "WHERE batch_id=? AND local_node_id=?",
                (new_id, batch_id, local_id),
            )
        conn.execute(
            "UPDATE candidates SET stage='UPLOADED', stage_updated_at=? WHERE (run_id, candidate_id) IN "
            "(SELECT b.run_id, bi.candidate_id FROM batch_items bi JOIN batches b ON b.batch_id = bi.batch_id "
            " WHERE bi.batch_id = ? AND bi.upload_status = 'uploaded')",
            (now, batch_id),
        )
        pending_rows = conn.execute(
            "SELECT local_node_id FROM batch_items WHERE batch_id=? AND upload_status='pending'",
            (batch_id,),
        ).fetchall()
        unmapped = [int(r["local_node_id"]) for r in pending_rows]
        if unmapped:
            msg = f"partial upload: {len(unmapped)} of {len(unmapped) + len(mapping)} items unmapped"
            conn.execute(
                "UPDATE batches SET status='needs_attention', error_msg=? WHERE batch_id=?",
                (msg, batch_id),
            )
            audit.log(actor="osm_client", event_type="UPLOAD_PARTIAL",
                      run_id=run_id, batch_id=batch_id,
                      payload={"changeset_id": changeset_id,
                               "uploaded": len(mapping),
                               "unmapped_local_node_ids": unmapped}, conn=conn)
        else:
            conn.execute("UPDATE batches SET status='uploaded', uploaded_at=? WHERE batch_id=?", (now, batch_id))
            audit.log(actor="osm_client", event_type="CHANGESET_UPLOADED",
                      run_id=run_id, batch_id=batch_id,
                      payload={"changeset_id": changeset_id, "count": len(mapping)}, conn=conn)
        conn.execute("COMMIT")
    finally:
        conn.close()

    _close_changeset(changeset_id)
    conn = _db.connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE changesets SET closed_at=?, status='closed' WHERE changeset_id=?", (now, changeset_id))
        audit.log(actor="osm_client", event_type="CHANGESET_CLOSED",
                  run_id=run_id, batch_id=batch_id, payload={"changeset_id": changeset_id}, conn=conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
