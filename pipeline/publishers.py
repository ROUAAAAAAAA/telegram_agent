"""
publishers.py — Auto-post to Facebook, LinkedIn, and Website.

Each publisher receives the generated text + Telegram file IDs,
downloads the images from Telegram, and posts to its platform API.
All credentials are loaded from settings (config.py / .env).
"""

import httpx
import asyncio
import logging
from config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Shared helper
# ─────────────────────────────────────────────────────────────────────────────

async def _download_telegram_file(file_id: str, bot_token: str) -> tuple[bytes, str]:
    """Download a file from Telegram servers. Returns (bytes, filename)."""
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(
            f"https://api.telegram.org/bot{bot_token}/getFile",
            params={"file_id": file_id},
        )
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        filename  = file_path.split("/")[-1]
        dl = await http.get(f"https://api.telegram.org/file/bot{bot_token}/{file_path}")
        dl.raise_for_status()
        return dl.content, filename


# ─────────────────────────────────────────────────────────────────────────────
# Facebook Publisher
# ─────────────────────────────────────────────────────────────────────────────

async def publish_facebook(text: str, image_file_ids: list[str], bot_token: str) -> dict:
    if settings.dry_run:
        logger.info(f"[DRY RUN] Facebook: {text[:80]}... images={image_file_ids}")
        return {"platform": "facebook", "status": "dry_run", "post_id": "fake-fb-123"}
    page_token = settings.facebook_page_access_token

    if not page_token:
        return {"platform": "facebook", "status": "skipped", "reason": "credentials not configured"}

    page_id = settings.facebook_page_id
    if not page_id:
        # Auto-resolve page ID from the token
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(
                    "https://graph.facebook.com/v19.0/me",
                    params={"fields": "id,name", "access_token": page_token},
                )
                r.raise_for_status()
                page_id = r.json()["id"]
                logger.info(f"[Facebook] Resolved page ID from token: {page_id} ({r.json().get('name', '')})")
        except Exception as e:
            return {"platform": "facebook", "status": "error", "reason": f"Could not resolve page ID: {e}"}

    base = f"https://graph.facebook.com/v19.0/{page_id}"

    try:
        async with httpx.AsyncClient(timeout=60) as http:

            if len(image_file_ids) == 1:
                # Single image: publish directly with the caption
                img_bytes, filename = await _download_telegram_file(image_file_ids[0], bot_token)
                r = await http.post(
                    f"{base}/photos",
                    params={"access_token": page_token},
                    data={"caption": text, "published": "true"},
                    files={"source": (filename, img_bytes, "image/jpeg")},
                )
                r.raise_for_status()
                logger.info(f"[Facebook] Raw API response: {r.status_code} {r.text}")
                post_id = r.json().get("post_id", r.json().get("id", ""))
                post_url = f"https://www.facebook.com/{post_id}" if post_id else None
                logger.info(f"[Facebook] Single-photo post: {post_id}")
                return {"platform": "facebook", "status": "ok", "post_id": post_id, "url": post_url}

            elif len(image_file_ids) > 1:
                # Multiple images: upload unpublished, then attach to feed post
                media_fbids = []
                for fid in image_file_ids:
                    img_bytes, filename = await _download_telegram_file(fid, bot_token)
                    r = await http.post(
                        f"{base}/photos",
                        params={"access_token": page_token},
                        data={"published": "false"},
                        files={"source": (filename, img_bytes, "image/jpeg")},
                    )
                    r.raise_for_status()
                    media_fbids.append(r.json()["id"])

                # Build the multipart form fields for attached_media
                feed_data: dict[str, str] = {"message": text, "access_token": page_token}
                for i, fbid in enumerate(media_fbids):
                    feed_data[f"attached_media[{i}]"] = f'{{"media_fbid":"{fbid}"}}'

                post_r = await http.post(f"{base}/feed", data=feed_data)
                post_r.raise_for_status()
                logger.info(f"[Facebook] Raw API response: {post_r.status_code} {post_r.text}")
                post_id = post_r.json().get("id", "")
                post_url = f"https://www.facebook.com/{post_id}" if post_id else None
                logger.info(f"[Facebook] Multi-photo post: {post_id}")
                return {"platform": "facebook", "status": "ok", "post_id": post_id, "url": post_url}

            else:
                # Text-only post
                post_r = await http.post(
                    f"{base}/feed",
                    params={"access_token": page_token},
                    data={"message": text},
                )
                post_r.raise_for_status()
                logger.info(f"[Facebook] Raw API response: {post_r.status_code} {post_r.text}")
                post_id = post_r.json().get("id", "")
                post_url = f"https://www.facebook.com/{post_id}" if post_id else None
                logger.info(f"[Facebook] Text post: {post_id}")
                return {"platform": "facebook", "status": "ok", "post_id": post_id, "url": post_url}

    except httpx.HTTPStatusError as e:
        body = ""
        try:
            body = e.response.json()
        except Exception:
            body = e.response.text
        logger.error(f"[Facebook] Failed: {e} | Response: {body}")
        return {"platform": "facebook", "status": "error", "reason": str(body)}


# ─────────────────────────────────────────────────────────────────────────────
# LinkedIn Publisher
# ─────────────────────────────────────────────────────────────────────────────

async def _linkedin_register_and_upload(
    http: httpx.AsyncClient, token: str, owner_urn: str,
    img_bytes: bytes,
) -> str | None:
    """
    LinkedIn two-step image upload:
      1. POST /assets?action=registerUpload → get uploadUrl + asset URN
      2. PUT bytes to uploadUrl
    Returns asset URN or None on failure.
    """
    body = {
        "registerUploadRequest": {
            "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
            "owner": owner_urn,
            "serviceRelationships": [{
                "relationshipType": "OWNER",
                "identifier": "urn:li:userGeneratedContent",
            }],
        }
    }
    r = await http.post(
        "https://api.linkedin.com/v2/assets?action=registerUpload",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body,
    )
    r.raise_for_status()
    data       = r.json()["value"]
    upload_url = data["uploadMechanism"][
        "com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest"
    ]["uploadUrl"]
    asset_urn  = data["asset"]

    put_r = await http.put(
        upload_url,
        content=img_bytes,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
    )
    put_r.raise_for_status()
    return asset_urn


async def publish_linkedin(text: str, image_file_ids: list[str], bot_token: str) -> dict:
    if settings.dry_run:
        logger.info(f"[DRY RUN] LinkedIn: {text[:80]}... images={image_file_ids}")
        return {"platform": "linkedin", "status": "dry_run", "post_id": "fake-li-456"}
    """Post text + images to LinkedIn as a ugcPost."""
    token     = settings.linkedin_access_token
    owner_urn = settings.linkedin_person_urn

    if not token or not owner_urn:
        return {"platform": "linkedin", "status": "skipped", "reason": "credentials not configured"}

    try:
        async with httpx.AsyncClient(timeout=60) as http:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            }

            media_list = []
            for fid in image_file_ids:
                img_bytes, _ = await _download_telegram_file(fid, bot_token)
                asset = await _linkedin_register_and_upload(http, token, owner_urn, img_bytes)
                if asset:
                    media_list.append({
                        "status": "READY",
                        "description": {"text": ""},
                        "media": asset,
                        "title": {"text": ""},
                    })

            if media_list:
                share_content = {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "IMAGE",
                    "media": media_list,
                }
            else:
                share_content = {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }

            # Use correct visibility key depending on author type (org vs person)
            if owner_urn.startswith("urn:li:organization:"):
                visibility = {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}
            else:
                visibility = {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"}

            ugc = {
                "author": owner_urn,
                "lifecycleState": "PUBLISHED",
                "specificContent": {"com.linkedin.ugc.ShareContent": share_content},
                "visibility": visibility,
            }

            r = await http.post(
                "https://api.linkedin.com/v2/ugcPosts",
                headers=headers,
                json=ugc,
            )
            r.raise_for_status()
            logger.info(f"[LinkedIn] Raw API response: {r.status_code} {r.text}")
            post_id = r.headers.get("x-restli-id", "")
            post_url = f"https://www.linkedin.com/feed/update/{post_id}" if post_id else None
            logger.info(f"[LinkedIn] Posted: {post_id}")
            return {"platform": "linkedin", "status": "ok", "post_id": post_id, "url": post_url}

    except Exception as e:
        logger.error(f"[LinkedIn] Failed: {e}")
        return {"platform": "linkedin", "status": "error", "reason": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Website Publisher
# ─────────────────────────────────────────────────────────────────────────────

async def publish_website(
    caption: str, full_content: str, image_file_ids: list[str], bot_token: str
) -> dict:
    if settings.dry_run:
        logger.info(f"[DRY RUN] Website: {caption} | {full_content[:80]}... images={image_file_ids}")
        return {"platform": "website", "status": "dry_run", "post_id": "fake-web-789"}
    """
    POST a new article to the website via its REST API.
    Sends multipart/form-data: caption + content fields + image files.
    """
    api_url   = settings.website_api_url
    api_token = settings.website_api_token

    if not api_url or not api_token:
        return {"platform": "website", "status": "skipped", "reason": "credentials not configured"}

    try:
        async with httpx.AsyncClient(timeout=60) as http:
            # Text fields come first
            files_payload: list[tuple] = [
                ("caption", (None, caption)),
                ("content", (None, full_content)),
            ]
            # Image files appended
            for fid in image_file_ids:
                img_bytes, filename = await _download_telegram_file(fid, bot_token)
                files_payload.append(("images", (filename, img_bytes, "image/jpeg")))

            r = await http.post(
                api_url,
                headers={"Authorization": f"Bearer {api_token}"},
                files=files_payload,
            )
            r.raise_for_status()
            logger.info(f"[Website] Raw API response: {r.status_code} {r.text}")
            result  = r.json()
            post_id = result.get("id", result.get("slug", ""))
            post_url = result.get("url", result.get("link", None))
            logger.info(f"[Website] Posted: {post_id}")
            return {"platform": "website", "status": "ok", "post_id": post_id, "url": post_url}

    except Exception as e:
        logger.error(f"[Website] Failed: {e}")
        return {"platform": "website", "status": "error", "reason": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

async def publish_all(
    caption: str,
    facebook_text: str,
    linkedin_text: str,
    website_text: str,
    image_file_ids: list[str],
    bot_token: str,
) -> list[dict]:
    """
    Publish to all three platforms in parallel.
    Never raises — exceptions are caught and returned as error dicts.
    """
    results = await asyncio.gather(
        publish_facebook(facebook_text, image_file_ids, bot_token),
        publish_linkedin(linkedin_text, image_file_ids, bot_token),
        publish_website(caption, website_text, image_file_ids, bot_token),
        return_exceptions=True,
    )

    normalised = []
    for platform, r in zip(["facebook", "linkedin", "website"], results):
        if isinstance(r, Exception):
            normalised.append({"platform": platform, "status": "error", "reason": str(r)})
        else:
            normalised.append(r)

    return normalised
