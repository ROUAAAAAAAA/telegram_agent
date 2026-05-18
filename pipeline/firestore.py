

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import firebase_admin
from firebase_admin import credentials, firestore

from config import settings

logger = logging.getLogger(__name__)


_app: firebase_admin.App | None = None
_db = None


def _init_firebase():
    """Lazily initialize Firebase Admin SDK (once) for Firestore."""
    global _app, _db

    if _app is not None:
        return

    if settings.firebase_service_account_json:
        import json
        try:
            info = json.loads(settings.firebase_service_account_json)
            cred = credentials.Certificate(info)
        except Exception as e:
            raise ValueError(f"Failed to parse firebase_service_account_json env var: {e}")
    else:
        sa_path = Path(settings.firebase_service_account_path)
        if not sa_path.exists():
            raise FileNotFoundError(
                f"Firebase service account key not found at: {sa_path.resolve()}\n"
                "Download it from Firebase Console → Project Settings → Service Accounts"
            )
        cred = credentials.Certificate(str(sa_path))

    _app = firebase_admin.initialize_app(cred)
    _db = firestore.client()

    logger.info(f"[Firebase] Initialized — project={_app.project_id}")


def get_db():
    """Get Firestore client (initializes on first call)."""
    _init_firebase()
    return _db



async def upload_image(
    image_bytes: bytes,
    filename: str,
    article_id: str,
) -> str:
    """
    Upload an image to terrasol.tn via the PHP upload endpoint.

    Endpoint: {WEBSITE_UPLOAD_URL}  (e.g. https://terrasol.tn/api/upload.php)
    Auth: api_token sent as POST field (Authorization header stripped by proxy).
    Returns the public URL on terrasol.tn.
    """
    upload_url = settings.website_upload_url
    upload_token = settings.website_upload_token

    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            upload_url,
            data={"article_id": article_id, "api_token": upload_token},
            files={"image": (filename, image_bytes, "image/jpeg")},
        )
        r.raise_for_status()
        result = r.json()

    if result.get("status") != "ok":
        raise RuntimeError(f"Upload failed: {result.get('error', 'unknown')}")

    url = result["url"]
    logger.info(f"[Upload] {filename} → {url}")
    return url




async def save_article(
    caption: str,
    content: str,
    image_urls: list[str],
    telegram_user_id: str = "",
    facebook_post_id: str = "",
    linkedin_post_id: str = "",
) -> dict:
    """
    Save a published article to Firestore.

    Collection: "articles"
    Returns {"doc_id": "...", "doc_data": {...}}
    """
    db = get_db()
    article_id = str(uuid.uuid4())[:12]

    doc_data = {
        "article_id": article_id,
        "caption": caption,
        "content": content,
        "images": image_urls,
        "status": "published",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "telegram_user_id": telegram_user_id,
        "facebook_post_id": facebook_post_id,
        "linkedin_post_id": linkedin_post_id,
    }

    doc_ref = db.collection("posts").document(article_id)
    doc_ref.set(doc_data)

    logger.info(f"[Firestore] Article saved: {article_id} — {caption[:50]}")
    return {"doc_id": article_id, "doc_data": doc_data}



async def get_all_articles(limit: int = 50) -> list[dict]:
    """Fetch latest published articles, newest first."""
    db = get_db()
    docs = (
        db.collection("posts")
        .where("status", "==", "published")
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    return [doc.to_dict() for doc in docs]


async def get_article(article_id: str) -> dict | None:
    """Fetch a single article by ID."""
    db = get_db()
    doc = db.collection("posts").document(article_id).get()
    return doc.to_dict() if doc.exists else None
