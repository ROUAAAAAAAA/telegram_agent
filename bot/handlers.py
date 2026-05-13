"""
bot/handlers.py — Telegram handlers for the agentic content bot.

Every message type (text, voice, photo, document) funnels into run_agent_turn().
Photos are ALWAYS processed immediately — the model sees the image via vision,
understands it, and stores that context in conversation history.
image_file_ids are persisted in session and attached at publish time.
"""

import logging

from telegram import Update, Bot
from telegram.ext import Application, MessageHandler, ContextTypes, filters

from pipeline.session import (
    get_or_create_session,
    add_pending_media,
    append_messages,
    clear_pending_media,
    delete_session,
)
from pipeline.ai import run_agent_turn
from config import settings

logger = logging.getLogger(__name__)



def _split(text: str, limit: int = 4000) -> list[str]:
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


async def _send(bot: Bot, chat_id: int, text: str):
    for chunk in _split(text):
        await bot.send_message(chat_id=chat_id, text=chunk)




async def _run_turn(
    bot: Bot,
    user_id: str,
    chat_id: int,
    user_text: str | None,
):
    """
    Run one agent turn:
    - Reads full session (messages, pending_media, image_file_ids, publish_ready)
    - Passes everything to run_agent_turn
    - Persists updated history; image_file_ids survive until /reset
    """
    session = get_or_create_session(user_id)
    pending = session.get("pending_media", [])
    img_ids = session.get("image_file_ids", [])
    messages = session.get("messages", [])

    async def send(text: str):
        await _send(bot, chat_id, text)

    try:
        await bot.send_chat_action(chat_id=chat_id, action="typing")
        updated_messages = await run_agent_turn(
            user_text=user_text,
            pending_media=pending,
            messages=messages,
            send_message_fn=send,
            image_file_ids=img_ids,
            user_id=user_id,
            session=session,
        )
    except Exception as e:
        logger.error(f"Agent turn failed for {user_id}: {e}", exc_info=True)
        await _send(bot, chat_id, "Something went wrong — try again.")
        clear_pending_media(user_id)  
        return

    clear_pending_media(user_id)
    append_messages(user_id, updated_messages[len(messages):])



async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    add_pending_media(user_id, "voice", update.message.voice.file_id)
    await _run_turn(
        bot=context.bot,
        user_id=user_id,
        chat_id=update.effective_chat.id,
        user_text=None,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Photos are ALWAYS processed immediately via a full agent turn.
    The model sees the image (vision), understands its content, and
    that context lives in message history for all future turns.
    The file_id is also saved to session.image_file_ids so it gets
    attached when publishing later.
    """
    user_id = str(update.effective_user.id)
    file_id = update.message.photo[-1].file_id
    add_pending_media(user_id, "image", file_id)  

    caption = (update.message.caption or "").strip() or None

    
    await _run_turn(
        bot=context.bot,
        user_id=user_id,
        chat_id=update.effective_chat.id,
        user_text=caption,  
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Same as handle_photo but for files sent as documents (full resolution)."""
    doc = update.message.document
    if not doc.mime_type or not doc.mime_type.startswith("image/"):
        await update.message.reply_text(
            "please send photos or image files."
        )
        return

    user_id = str(update.effective_user.id)
    add_pending_media(user_id, "image", doc.file_id)

    caption = (update.message.caption or "").strip() or None
    await _run_turn(
        bot=context.bot,
        user_id=user_id,
        chat_id=update.effective_chat.id,
        user_text=caption,
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = (update.message.text or "").strip()

   
    if text.lower() in ("/reset", "/start", "/flush"):
        delete_session(user_id)
        await update.message.reply_text(" What do you want to post?")
        return

    if text.lower() == "/debug":
        session = get_or_create_session(user_id)
        debug_info = (
            f" Session debug\n"
            f"publish_ready: {session.get('publish_ready', False)}\n"
            f"image_count: {len(session.get('image_file_ids', []))}\n"
            f"pending_media: {len(session.get('pending_media', []))}\n"
            f"messages: {len(session.get('messages', []))}\n"
            f"last_preview caption: {(session.get('last_preview') or {}).get('caption', 'none')}"
        )
        await update.message.reply_text(debug_info)
        return

    if text.lower() == "/images":
        session = get_or_create_session(user_id)
        ids = session.get("image_file_ids", [])
        if ids:
            await update.message.reply_text(
                f" {len(ids)} image(s) attached to this session.\nIDs: {', '.join(ids)}"
            )
        else:
            await update.message.reply_text("🖼 No images in this session yet.")
        return

   
    await _run_turn(
        bot=context.bot,
        user_id=user_id,
        chat_id=update.effective_chat.id,
        user_text=text,
    )



def build_application() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(MessageHandler(filters.VOICE,        handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO,        handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT,         handle_text))
    return app
