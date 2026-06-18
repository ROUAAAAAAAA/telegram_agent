
import asyncio
import httpx
import logging
import json
import base64

import openai
from openai import AsyncOpenAI

from config import settings
from pipeline.publishers import (
    publish_facebook as _pub_facebook,
    publish_linkedin as _pub_linkedin,
    publish_website  as _pub_website,
)
from pipeline.session import set_last_preview, mark_published, get_session, reset_after_publish

logger = logging.getLogger(__name__)



MODEL = "google/gemini-3-flash-preview"


_client = AsyncOpenAI(
    api_key=settings.openrouter_api_key,
    base_url="https://openrouter.ai/api/v1",
    default_headers={
        "HTTP-Referer": "http://localhost:8080",
        "X-Title": "Terrasol Agent",
    },
)

_key_preview = (
    (settings.openrouter_api_key[:12] + "..." + settings.openrouter_api_key[-4:])
    if settings.openrouter_api_key else "NOT SET"
)
logger.info(f"[STARTUP] OpenRouter client initialized | model={MODEL} | key={_key_preview}")


SYSTEM = """You are a social media content assistant for Terrasol Tunisie, a geotechnical and civil engineering company in Tunisia. You help the team create and publish content on Facebook, LinkedIn, and their website.

You speak French, English, and Tunisian Darija naturally — always match the language the user uses.

Your personality: warm, natural, concise. Never robotic. You talk like a helpful colleague, not a menu system.

---

YOUR JOB:
1. Understand what the user wants to post — they may describe it in text, send a voice note, photos, or a mix. You can hear voice and SEE images directly via vision.
2. NEVER generate content unless the user has explicitly told you what they want to say. This is the most important rule.
3. When you receive media (photos, voice, or both) WITHOUT a clear content brief: acknowledge what you received in 1-2 sentences and ask what they want to say or what message they want to convey. Do NOT generate content.
4. A "clear content brief" means the user has described the subject, message, or angle they want — not just a single word, not just "ok/yes/oui/go", not just an affirmation or filler.
5. Only generate content when you have BOTH: (a) the media or subject matter, AND (b) explicit instructions from the user about what to say.
6. When multiple photos arrive across separate messages: you have already seen and understood each one. Use all of them when generating.
7. Show them a preview with preview_content before publishing.
8. On confirmation (yes/oui/ok/go/publish and publish_ready is true), call the publish tools. On change requests, regenerate and preview again.

---

CONTENT GUIDELINES:

Facebook (80-120 words):
- Punchy opening line naming the project/achievement
- 1-2 relevant emojis
- Engineering challenge + solution in 2-3 sentences
- Closing line expressing pride or inviting reaction

LinkedIn (120-180 words):
- No emojis
- Structure: context -> technical approach -> result
- End with a sharp insight or question for peers

Website article intro (180-250 words):
- H1 title: project type + context
- Paragraphs: context -> challenge -> solution -> outcome
- Natural SEO: geotechnique, renforcement de sol, Tunisie, Terrasol
- No emojis

Style: technically credible, proud, concise. Never generic. Every word earns its place.
Write in whatever language the user requested or is speaking.

---

TOOL USAGE — STRICT RULES:
1. Always call preview_content FIRST before any publish tool. Never skip preview.
2. After preview is shown, the session enters PUBLISH-READY state.
3. In PUBLISH-READY state, ANY confirmation from the user triggers publish tools immediately:
   - "yes", "oui", "ok", "go", "publish", "publier", "go ahead" → publish all previewed platforms
   - "all three" / "all" / "les trois" → publish_facebook + publish_linkedin + publish_website
   - "facebook" alone → publish_facebook only
   - "linkedin" alone → publish_linkedin only
   - "website" alone → publish_website only
4. NEVER ask the user which platforms again if they already said "all three" or confirmed.
5. NEVER ask for images again if image_file_ids are already in the session state block.
6. You can call multiple publish tools in the same turn.
7. After publishing, confirm what was posted. Done.

---

SESSION STATE: At the start of each turn, you will receive a <session_state> block with:
- publish_ready: whether a preview has been shown and is awaiting confirmation
- last_preview: the exact content that was previewed (use this — do not reinvent)
- image_file_ids: all images the user has provided (use these for publish calls)
Use this state. Do not ignore it. Do not ask for information already in it.

---

KEEP REPLIES SHORT. 1-3 sentences. Let the content speak."""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "preview_content",
            "description": "Show the generated content to the user as a preview before publishing. Always call this before any publish tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "caption":  {"type": "string", "description": "One sentence caption, max 12 words, no hashtags."},
                    "facebook": {"type": "string", "description": "Facebook post, 80-120 words."},
                    "linkedin": {"type": "string", "description": "LinkedIn post, 120-180 words, no emojis."},
                    "website":  {"type": "string", "description": "Website article intro with H1 title, 180-250 words."},
                },
                "required": ["caption", "facebook", "linkedin", "website"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_facebook",
            "description": "Publish the confirmed content to the Terrasol Tunisie Facebook page.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text":           {"type": "string"},
                    "image_file_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "image_file_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_linkedin",
            "description": "Publish the confirmed content to LinkedIn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text":           {"type": "string"},
                    "image_file_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "image_file_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "publish_website",
            "description": "Publish the confirmed content as an article on the website.",
            "parameters": {
                "type": "object",
                "properties": {
                    "caption":        {"type": "string"},
                    "full_content":   {"type": "string"},
                    "image_file_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["caption", "full_content", "image_file_ids"],
            },
        },
    },
]


# ── Media helpers ─────────────────────────────────────────────────────────────

async def _download(file_id: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.get(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/getFile",
            params={"file_id": file_id},
        )
        r.raise_for_status()
        file_path = r.json()["result"]["file_path"]
        dl = await http.get(
            f"https://api.telegram.org/file/bot{settings.telegram_bot_token}/{file_path}"
        )
        dl.raise_for_status()
        return dl.content, file_path


def _mime(file_path: str, media_type: str) -> str:
    ext = file_path.split(".")[-1].lower()
    if media_type == "voice":
        return "audio/ogg" if ext in ("oga", "ogg") else "audio/mpeg"
    return "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"


async def _fetch_media_content(media_type: str, file_id: str) -> dict | None:
    """Return an OpenAI-style image_url content block (base64 encoded)."""
    try:
        data, path = await _download(file_id)
        mime = _mime(path, media_type)
        b64 = base64.b64encode(data).decode("utf-8")
        if media_type in ("image", "voice"):
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        return None
    except Exception as e:
        logger.error(f"Failed to fetch {media_type} {file_id}: {e}")
        return None


# ── Session state block ───────────────────────────────────────────────────────

def _session_state_block(session: dict, pending_media: list | None = None) -> str:
    image_ids    = session.get("image_file_ids", [])
    last_preview = session.get("last_preview")
    new_images   = sum(1 for m in (pending_media or []) if m.get("type") == "image")
    new_voices   = sum(1 for m in (pending_media or []) if m.get("type") == "voice")
    lines = [
        "<session_state>",
        f"publish_ready: {str(session.get('publish_ready', False)).lower()}",
        f"image_count: {len(image_ids)}",
        f"image_file_ids: {json.dumps(image_ids)}",
        f"new_images_this_turn: {new_images}",
        f"new_voices_this_turn: {new_voices}",
    ]
    if last_preview:
        lines += [
            "last_preview:",
            f"  caption: {last_preview.get('caption', '')}",
            f"  facebook: {last_preview.get('facebook', '')[:120]}...",
            f"  linkedin: {last_preview.get('linkedin', '')[:120]}...",
            f"  website: {last_preview.get('website', '')[:120]}...",
        ]
    else:
        lines.append("last_preview: null")
    lines.append("</session_state>")
    return "\n".join(lines)


# ── History helpers ───────────────────────────────────────────────────────────

def _history_to_messages(history: list[dict]) -> list[dict]:
    """Convert simple {"role", "text"} dicts to OpenAI message dicts."""
    msgs = []
    for m in history:
        text = m.get("text", "").strip()
        if text:
            role = "assistant" if m["role"] == "model" else m["role"]
            msgs.append({"role": role, "content": text})
    return msgs


# ── Confirm detection ─────────────────────────────────────────────────────────

CONFIRM_PHRASES = {
    "yes", "oui", "ok", "go", "publish", "publier", "go ahead", "allez",
    "all three", "all 3", "all", "les trois", "tout", "tout publier",
    "yes publish", "yes let us publish", "yes let's publish",
    "facebook", "linkedin", "website",
}

def _is_confirm(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    return t in CONFIRM_PHRASES or any(t.startswith(p + " ") for p in CONFIRM_PHRASES)


# ── Tool executor ─────────────────────────────────────────────────────────────

async def _execute_tool(
    name: str, inputs: dict, image_file_ids: list[str], user_id: str
) -> tuple[str, str | None]:
    if name == "preview_content":
        set_last_preview(user_id, inputs)
        preview = (
            f" Caption\n{inputs.get('caption', '')}\n\n"
            f"───────────────────\n\n"
            f" Facebook\n{inputs.get('facebook', '')}\n\n"
            f"───────────────────\n\n"
            f" LinkedIn\n{inputs.get('linkedin', '')}\n\n"
            f"───────────────────\n\n"
            f" Website\n{inputs.get('website', '')}\n\n"
            f"───────────────────\n\n"
            f"Happy with this? Say yes to publish, or tell me what to change."
        )
        return "Preview sent to user successfully.", preview

    elif name == "publish_facebook":
        ids    = inputs.get("image_file_ids") or image_file_ids
        result = await _pub_facebook(
            text=inputs["text"], image_file_ids=ids,
            bot_token=settings.telegram_bot_token,
        )
        reset_after_publish(user_id)
        return str(result), None

    elif name == "publish_linkedin":
        ids    = inputs.get("image_file_ids") or image_file_ids
        result = await _pub_linkedin(
            text=inputs["text"], image_file_ids=ids,
            bot_token=settings.telegram_bot_token,
        )
        reset_after_publish(user_id)
        return str(result), None

    elif name == "publish_website":
        ids    = inputs.get("image_file_ids") or image_file_ids
        result = await _pub_website(
            caption=inputs["caption"],
            full_content=inputs["full_content"],
            image_file_ids=ids,
            bot_token=settings.telegram_bot_token,
        )
        reset_after_publish(user_id)
        return str(result), None

    return f"Unknown tool: {name}", None



def _format_publish_result(result_str: str, tool_name: str) -> str | None:
    """Parse a publish result dict and return a human-readable Telegram message."""
    try:
        res = json.loads(result_str.replace("'", '"'))
    except Exception:
        return None

    platform = res.get("platform", tool_name.replace("publish_", ""))
    status   = res.get("status", "?")
    post_id  = res.get("post_id", "")
    url      = res.get("url", "")
    reason   = res.get("reason", "")

    if status == "ok":
        msg = f" {platform.capitalize()} published\nPost ID: {post_id}"
        if url:
            msg += f"\nLink: {url}"
    else:
        msg = f" {platform.capitalize()} failed ({status})"
        if reason:
            msg += f"\nReason: {reason}"
    return msg




async def run_agent_turn(
    user_text: str | None,
    pending_media: list[dict],
    messages: list[dict],
    send_message_fn,
    image_file_ids: list[str],
    user_id: str,
    session: dict,
) -> list[dict]:
    """
    Run one agent turn using OpenRouter 

    `messages` is a simple list of {"role": "user"|"model", "text": "..."} dicts.
    Images are re-fetched from Telegram each turn — no blobs in Redis.

    Returns the updated messages list for Redis.
    """
    import time as _time
    _turn_start = _time.time()
    logger.info(
        f"[TURN START] user={user_id} | key={_key_preview} | "
        f"user_text={repr(user_text)} | pending_media={len(pending_media)} | "
        f"image_file_ids={len(image_file_ids)} | history_msgs={len(messages)}"
    )

    # Build session state block
    state = _session_state_block(session, pending_media)
    logger.debug(f"Session state injected:\n{state}")

    # Fetch media for this turn
    media_contents = await asyncio.gather(*[
        _fetch_media_content(m["type"], m["file_id"]) for m in pending_media
    ])
    media_contents = [c for c in media_contents if c is not None]

    # Build current user message content
    current_user_content: list[dict] = [{"type": "text", "text": state}]
    current_user_content.extend(media_contents)
    if user_text:
        current_user_content.append({"type": "text", "text": user_text})
    if len(current_user_content) == 1:
        current_user_content.append({"type": "text", "text": "(no message)"})

    # Full messages list for the API
    api_messages: list[dict] = (
        [{"role": "system", "content": SYSTEM}]
        + _history_to_messages(messages)
        + [{"role": "user", "content": current_user_content}]
    )

    # Re-read live session for publish_ready state
    live_session   = get_session(user_id) or session
    is_confirmation = _is_confirm(user_text) and live_session.get("publish_ready", False)
    logger.info(
        f"[CONFIRM CHECK] _is_confirm={_is_confirm(user_text)} | "
        f"publish_ready(live)={live_session.get('publish_ready')} | "
        f"→ is_confirmation={is_confirmation}"
    )
    if live_session.get("last_preview") and not session.get("last_preview"):
        session["last_preview"] = live_session["last_preview"]

    tool_choice = "required" if is_confirmation else "auto"

    user_text_for_history = " ".join(filter(None, [
        f"[{len(media_contents)} media file(s)]" if media_contents else None,
        user_text,
    ])) or "(no message)"
    new_messages = messages + [{"role": "user", "text": user_text_for_history}]

    # ── Confirmation shortcut  ────────────────────────────
    if is_confirmation:
        last_preview = session.get("last_preview")
        if last_preview:
            logger.info("Confirmation shortcut: publishing directly without model call")
            publish_tasks = [
                ("publish_facebook", {
                    "text": last_preview["facebook"],
                    "image_file_ids": image_file_ids,
                }),
                ("publish_linkedin", {
                    "text": last_preview["linkedin"],
                    "image_file_ids": image_file_ids,
                }),
                ("publish_website", {
                    "caption":      last_preview["caption"],
                    "full_content": last_preview["website"],
                    "image_file_ids": image_file_ids,
                }),
            ]
            for tool_name, inputs in publish_tasks:
                logger.info(f"Executing tool: {tool_name}")
                try:
                    result_str, _ = await _execute_tool(
                        tool_name, inputs, image_file_ids, user_id
                    )
                except Exception as e:
                    result_str = str({
                        "platform": tool_name.replace("publish_", ""),
                        "status": "error",
                        "reason": str(e),
                    })

                msg = _format_publish_result(result_str, tool_name)
                if msg:
                    try:
                        await send_message_fn(msg)
                    except Exception as e:
                        logger.error(f"Failed to send publish result: {e}")

            new_messages = new_messages + [
                {"role": "model", "text": "Published to all platforms."}
            ]
            # ✅ Wipe session so the next post starts clean
            reset_after_publish(user_id)
            try:
                await send_message_fn("✅ Done! What would you like to post next?")
            except Exception as e:
                logger.error(f"Failed to send closing message: {e}")
            return new_messages

    # ── LLM agentic loop ─────────────────────────────────────────────────────
    MAX_LOOP_ITERATIONS = 4

    for _loop_iter in range(MAX_LOOP_ITERATIONS):
        if _loop_iter > 0:
            await asyncio.sleep(1)
            tool_choice = "auto"

        _last_exc = None
        for _attempt in range(2):
            try:
                logger.debug(
                    f"OpenRouter call at {_time.time():.3f} "
                    f"(loop={_loop_iter} attempt={_attempt})"
                )
                response = await _client.chat.completions.create(
                    model=MODEL,
                    messages=api_messages,
                    tools=TOOLS,
                    tool_choice=tool_choice,
                    max_tokens=6000,
                )
                _last_exc = None
                break
            except openai.RateLimitError as _exc:
                _last_exc = _exc
                import re as _re
                _wait = 30
                _delay_match = _re.search(r"(\d+)s", str(_exc))
                if _delay_match:
                    _wait = int(_delay_match.group(1)) + 2
                logger.warning(
                    f"[429] Rate limited loop={_loop_iter} attempt={_attempt} "
                    f"| waiting {_wait}s"
                )
                await asyncio.sleep(_wait)
            except Exception as _exc:
                _last_exc = _exc
                logger.error(f"[OPENROUTER ERROR] loop={_loop_iter}: {_exc}")
                break

        if _last_exc is not None:
            raise _last_exc

        choice     = response.choices[0]
        message    = choice.message
        tool_calls = message.tool_calls or []
        text_reply = (message.content or "").strip()

        logger.info(
            f"[OPENROUTER RESPONSE] loop={_loop_iter} | finish={choice.finish_reason} | "
            f"text_len={len(text_reply)} tool_calls={len(tool_calls)} | "
            f"tokens: in={response.usage.prompt_tokens} "
            f"out={response.usage.completion_tokens}"
        )

        
        assistant_msg: dict = {"role": "assistant", "content": message.content or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
        api_messages.append(assistant_msg)

        if text_reply:
            try:
                await send_message_fn(text_reply)
            except Exception as e:
                logger.error(f"Failed to send reply: {e}")

        if not tool_calls:
            if text_reply:
                new_messages = new_messages + [{"role": "model", "text": text_reply}]
            else:
                logger.warning(
                    f"[EMPTY RESPONSE] loop={_loop_iter} finish={choice.finish_reason}"
                )
                empty_msg = (
                    "Je n'ai pas pu traiter ce message. "
                    "Si c'est un message vocal, essayez de le renvoyer "
                    "ou décrivez votre demande par écrit."
                )
                await send_message_fn(empty_msg)
                new_messages = new_messages + [{"role": "model", "text": empty_msg}]
            break

        
        for tc in tool_calls:
            name = tc.function.name
            try:
                inputs = json.loads(tc.function.arguments)
            except Exception:
                inputs = {}
            logger.info(f"Executing tool: {name}")
            try:
                result_str, preview_text = await _execute_tool(
                    name, inputs, image_file_ids, user_id
                )
            except Exception as e:
                logger.error(f"Tool {name} failed: {e}")
                result_str, preview_text = f"Tool error: {e}", None

            if preview_text:
                try:
                    await send_message_fn(preview_text)
                    logger.info("Preview sent to Telegram successfully.")
                except Exception as e:
                    logger.error(f"Failed to send preview: {e}")

            if name in ("publish_facebook", "publish_linkedin", "publish_website"):
                msg = _format_publish_result(result_str, name)
                if msg:
                    try:
                        await send_message_fn(msg)
                    except Exception as e:
                        logger.error(f"Failed to send publish result: {e}")

            api_messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      result_str,
            })

    logger.info(
        f"[TURN END] user={user_id} | calls={_loop_iter + 1} | "
        f"duration={_time.time() - _turn_start:.2f}s | "
        f"history_out={len(new_messages)} msgs"
    )
    return new_messages
