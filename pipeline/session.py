"""
session.py — Redis-backed session store.

Messages are stored as simple {"role": "user"|"model", "text": "..."} dicts.
Images are stored as file_ids only — re-fetched from Telegram each turn.
No base64 blobs, no Part objects, no serialization complexity.
"""

import json
import redis
from config import settings

_redis = redis.from_url(settings.redis_url, decode_responses=True)
SESSION_PREFIX = "session:v3:"


def _key(user_id: str) -> str:
    return f"{SESSION_PREFIX}{user_id}"


def _save(user_id: str, session: dict):
    _redis.setex(_key(user_id), settings.session_ttl_seconds, json.dumps(session))


def get_session(user_id: str) -> dict | None:
    raw = _redis.get(_key(user_id))
    return json.loads(raw) if raw else None


def get_or_create_session(user_id: str) -> dict:
    session = get_session(user_id)
    if session:
        return session
    session = {
        "user_id": str(user_id),
        "messages": [],          # [{"role": "user"|"model", "text": "..."}]
        "pending_media": [],     # [{"type": "voice"|"image", "file_id": "..."}]
        "image_file_ids": [],    # all image file_ids seen — persisted across turns
        "last_preview": None,    # last preview_content payload dict
        "publish_ready": False,
    }
    _save(user_id, session)
    return session


def add_pending_media(user_id: str, media_type: str, file_id: str) -> dict:
    session = get_or_create_session(user_id)
    session["pending_media"].append({"type": media_type, "file_id": file_id})
    if media_type == "image" and file_id not in session["image_file_ids"]:
        session["image_file_ids"].append(file_id)
    _save(user_id, session)
    return session


def set_last_preview(user_id: str, preview: dict) -> dict:
    session = get_or_create_session(user_id)
    session["last_preview"] = preview
    session["publish_ready"] = True
    _save(user_id, session)
    return session


def mark_published(user_id: str) -> dict:
    session = get_or_create_session(user_id)
    session["publish_ready"] = False
    _save(user_id, session)
    return session


def append_messages(user_id: str, new_messages: list[dict]) -> dict:
    session = get_or_create_session(user_id)
    session["messages"].extend(new_messages)
    if len(session["messages"]) > 60:
        session["messages"] = session["messages"][-60:]
    _save(user_id, session)
    return session


def clear_pending_media(user_id: str) -> dict:
    session = get_or_create_session(user_id)
    session["pending_media"] = []
    _save(user_id, session)
    return session


def delete_session(user_id: str):
    _redis.delete(_key(user_id))
