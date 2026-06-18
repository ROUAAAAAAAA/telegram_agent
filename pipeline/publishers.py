


import httpx
import asyncio
import logging
from config import settings

logger = logging.getLogger(__name__)



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
        dl = await http.get(
            f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
        )
        dl.raise_for_status()
        return dl.content, filename



async def publish_facebook(text: str, image_file_ids: list[str], bot_token: str) -> dict:
    """Post text + images to the Terrasol Tunisie Facebook page."""
    if settings.dry_run:
        logger.info(f"[DRY RUN] Facebook: {text[:80]}... images={image_file_ids}")
        return {"platform": "facebook", "status": "dry_run", "post_id": "fake-fb-123"}

    page_token = settings.facebook_page_access_token
    if not page_token:
        return {
            "platform": "facebook",
            "status":   "skipped",
            "reason":   "credentials not configured",
        }

    page_id = settings.facebook_page_id
    if not page_id:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(
                    "https://graph.facebook.com/v19.0/me",
                    params={"fields": "id,name", "access_token": page_token},
                )
                r.raise_for_status()
                page_id = r.json()["id"]
                logger.info(
                    f"[Facebook] Resolved page ID from token: "
                    f"{page_id} ({r.json().get('name', '')})"
                )
        except Exception as e:
            return {
                "platform": "facebook",
                "status":   "error",
                "reason":   f"Could not resolve page ID: {e}",
            }

    base = f"https://graph.facebook.com/v19.0/{page_id}"

    try:
        async with httpx.AsyncClient(timeout=60) as http:

            if len(image_file_ids) == 1:
                img_bytes, filename = await _download_telegram_file(
                    image_file_ids[0], bot_token
                )
                r = await http.post(
                    f"{base}/photos",
                    params={"access_token": page_token},
                    data={"caption": text, "published": "true"},
                    files={"source": (filename, img_bytes, "image/jpeg")},
                )
                r.raise_for_status()
                logger.info(f"[Facebook] Raw API response: {r.status_code} {r.text}")
                post_id  = r.json().get("post_id", r.json().get("id", ""))
                post_url = f"https://www.facebook.com/{post_id}" if post_id else None
                logger.info(f"[Facebook] Single-photo post: {post_id}")
                return {
                    "platform": "facebook",
                    "status":   "ok",
                    "post_id":  post_id,
                    "url":      post_url,
                }

            elif len(image_file_ids) > 1:
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

                feed_data: dict[str, str] = {
                    "message": text,
                    "access_token": page_token,
                }
                for i, fbid in enumerate(media_fbids):
                    feed_data[f"attached_media[{i}]"] = f'{{"media_fbid":"{fbid}"}}'

                post_r = await http.post(f"{base}/feed", data=feed_data)
                post_r.raise_for_status()
                logger.info(
                    f"[Facebook] Raw API response: {post_r.status_code} {post_r.text}"
                )
                post_id  = post_r.json().get("id", "")
                post_url = f"https://www.facebook.com/{post_id}" if post_id else None
                logger.info(f"[Facebook] Multi-photo post: {post_id}")
                return {
                    "platform": "facebook",
                    "status":   "ok",
                    "post_id":  post_id,
                    "url":      post_url,
                }

            else:
                post_r = await http.post(
                    f"{base}/feed",
                    params={"access_token": page_token},
                    data={"message": text},
                )
                post_r.raise_for_status()
                logger.info(
                    f"[Facebook] Raw API response: {post_r.status_code} {post_r.text}"
                )
                post_id  = post_r.json().get("id", "")
                post_url = f"https://www.facebook.com/{post_id}" if post_id else None
                logger.info(f"[Facebook] Text post: {post_id}")
                return {
                    "platform": "facebook",
                    "status":   "ok",
                    "post_id":  post_id,
                    "url":      post_url,
                }

    except httpx.HTTPStatusError as e:
        try:
            body = e.response.json()
        except Exception:
            body = e.response.text
        logger.error(f"[Facebook] Failed: {e} | Response: {body}")
        return {"platform": "facebook", "status": "error", "reason": str(body)}



async def _linkedin_register_and_upload(
    http: httpx.AsyncClient,
    token: str,
    owner_urn: str,
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
        headers={
            "Authorization":  f"Bearer {token}",
            "Content-Type":   "application/json",
        },
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
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "image/jpeg",
        },
    )
    put_r.raise_for_status()
    return asset_urn


async def publish_linkedin(text: str, image_file_ids: list[str], bot_token: str) -> dict:
    """Post text + images to LinkedIn as a ugcPost."""
    if settings.dry_run:
        logger.info(f"[DRY RUN] LinkedIn: {text[:80]}... images={image_file_ids}")
        return {"platform": "linkedin", "status": "dry_run", "post_id": "fake-li-456"}

    token     = settings.linkedin_access_token
    owner_urn = settings.linkedin_owner_urn

    if not token or not owner_urn:
        return {
            "platform": "linkedin",
            "status":   "skipped",
            "reason":   "credentials not configured",
        }

    try:
        async with httpx.AsyncClient(timeout=60) as http:
            headers = {
                "Authorization":              f"Bearer {token}",
                "Content-Type":               "application/json",
                "X-Restli-Protocol-Version":  "2.0.0",
            }

            media_list = []
            for fid in image_file_ids:
                img_bytes, _ = await _download_telegram_file(fid, bot_token)
                asset = await _linkedin_register_and_upload(
                    http, token, owner_urn, img_bytes
                )
                if asset:
                    media_list.append({
                        "status":      "READY",
                        "description": {"text": ""},
                        "media":       asset,
                        "title":       {"text": ""},
                    })

            if media_list:
                share_content = {
                    "shareCommentary":   {"text": text},
                    "shareMediaCategory": "IMAGE",
                    "media":             media_list,
                }
            else:
                share_content = {
                    "shareCommentary":   {"text": text},
                    "shareMediaCategory": "NONE",
                }

            ugc = {
                "author":          owner_urn,
                "lifecycleState":  "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": share_content,
                },
                "visibility": {
                    "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC",
                },
            }

            r = await http.post(
                "https://api.linkedin.com/v2/ugcPosts",
                headers=headers,
                json=ugc,
            )
            r.raise_for_status()
            logger.info(f"[LinkedIn] Raw API response: {r.status_code} {r.text}")
            post_id  = r.headers.get("x-restli-id", "")
            post_url = (
                f"https://www.linkedin.com/feed/update/{post_id}" if post_id else None
            )
            logger.info(f"[LinkedIn] Posted: {post_id}")
            return {
                "platform": "linkedin",
                "status":   "ok",
                "post_id":  post_id,
                "url":      post_url,
            }

    except Exception as e:
        logger.error(f"[LinkedIn] Failed: {e}")
        return {"platform": "linkedin", "status": "error", "reason": str(e)}




async def publish_website(
    caption: str,
    full_content: str,
    image_file_ids: list[str],
    bot_token: str,
) -> dict:
    """
    Upload images to terrasol.tn and save the article to Firestore.
    The website developer fetches content from the 'articles' Firestore collection.
    """
    if settings.dry_run:
        logger.info(
            f"[DRY RUN] Website: {caption} | {full_content[:80]}... "
            f"images={image_file_ids}"
        )
        return {
            "platform": "website",
            "status":   "dry_run",
            "post_id":  "fake-web-789",
        }

    from pipeline.firestore import upload_image, save_article
    import uuid

    if not settings.website_upload_url:
        return {
            "platform": "website",
            "status":   "skipped",
            "reason":   "Website upload URL not configured",
        }

    try:
        article_id = str(uuid.uuid4())[:12]

        image_urls = []
        for fid in image_file_ids:
            img_bytes, filename = await _download_telegram_file(fid, bot_token)
            url = await upload_image(img_bytes, filename, article_id)
            image_urls.append(url)
            logger.info(f"[Website] Image uploaded: {url}")

        result = await save_article(
            caption=caption,
            content=full_content,
            image_urls=image_urls,
            telegram_user_id="",
        )

        doc_id = result["doc_id"]
        logger.info(f"[Website] Article saved to Firestore: {doc_id}")
        return {
            "platform": "website",
            "status":   "ok",
            "post_id":  doc_id,
            "url":      f"firestore://articles/{doc_id}",
        }

    except Exception as e:
        logger.error(f"[Website] Failed: {e}", exc_info=True)
        return {"platform": "website", "status": "error", "reason": str(e)}



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
            normalised.append({
                "platform": platform,
                "status":   "error",
                "reason":   str(r),
            })
        else:
            normalised.append(r)

    return normalised
