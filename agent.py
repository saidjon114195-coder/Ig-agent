"""
Instagram agent: kontent tayyorlaydi -> Telegramda tasdiqlatadi -> Instagramga joylaydi.
Sms va kommentlarga umuman tegmaydi (bunday kod yo'q).
"""
import asyncio
import datetime as dt
import json
import logging
import os
import time
import urllib.parse
import uuid
from zoneinfo import ZoneInfo

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ig-agent")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OWNER_ID = int(os.environ["TELEGRAM_OWNER_ID"])  # faqat siz boshqara olasiz
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
IG_USER_ID = os.getenv("IG_USER_ID", "")
IG_TOKEN = os.getenv("IG_ACCESS_TOKEN", "")
IG_HOST = os.getenv("IG_API_HOST", "https://graph.instagram.com")  # Instagram Login tokeni uchun
NICHE = os.getenv("NICHE", "Mening biznesim haqida")
LANGUAGE = os.getenv("LANGUAGE", "o'zbek (lotin)")
POST_TIME = os.getenv("DRAFT_TIME", "09:00")  # har kuni shu vaqtda yangi draft keladi
TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))
MODEL = "claude-sonnet-4-6"

claude = Anthropic(api_key=ANTHROPIC_KEY)

SYSTEM = f"""Sen Instagram kontent menejerisan.
Soha/brend: {NICHE}
Til: {LANGUAGE}
Har safar BITTA yangi post tayyorla. Oldingi mavzularni takrorlama.
Faqat JSON qaytar, boshqa hech narsa yozma:
{{"topic": "...", "caption": "... (hook, qimmatli matn, CTA, 5-8 hashtag)",
 "image_prompt": "rasm uchun inglizcha tavsif, matnsiz, vertikal 4:5"}}"""


# ---------- Kontent yaratish ----------
def generate_draft(history: list[str]) -> dict:
    avoid = "; ".join(history[-20:]) or "yo'q"
    msg = claude.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=SYSTEM,
        messages=[{"role": "user", "content": f"Oldingi mavzular: {avoid}. Yangi post tayyorla."}],
    )
    text = msg.content[0].text.strip().replace("```json", "").replace("```", "")
    data = json.loads(text)
    # Rasm: ochiq URL bo'lishi shart (Instagram shuni talab qiladi).
    # Xohlasangiz bu qismni boshqa rasm generatoriga almashtiring.
    prompt = urllib.parse.quote(data["image_prompt"])
    data["image_url"] = (
        f"https://image.pollinations.ai/prompt/{prompt}"
        f"?width=1080&height=1350&nologo=true&seed={uuid.uuid4().int % 100000}"
    )
    return data


# ---------- Instagramga joylash ----------
def publish_to_instagram(image_url: str, caption: str) -> str:
    if not (IG_USER_ID and IG_TOKEN):
        raise RuntimeError("IG_USER_ID / IG_ACCESS_TOKEN sozlanmagan")
    base = f"{IG_HOST}/v21.0/{IG_USER_ID}"
    r = requests.post(
        f"{base}/media",
        data={"image_url": image_url, "caption": caption, "access_token": IG_TOKEN},
        timeout=90,
    )
    r.raise_for_status()
    container = r.json()["id"]

    for _ in range(12):  # konteyner tayyor bo'lishini kutamiz
        s = requests.get(
            f"{IG_HOST}/v21.0/{container}",
            params={"fields": "status_code", "access_token": IG_TOKEN},
            timeout=30,
        ).json()
        if s.get("status_code") == "FINISHED":
            break
        if s.get("status_code") == "ERROR":
            raise RuntimeError(f"Instagram konteyner xatosi: {s}")
        time.sleep(5)

    r = requests.post(
        f"{base}/media_publish",
        data={"creation_id": container, "access_token": IG_TOKEN},
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["id"]


# ---------- Telegram ----------
def owner_only(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == OWNER_ID


async def send_new_draft(context: ContextTypes.DEFAULT_TYPE):
    history = context.bot_data.setdefault("history", [])
    drafts = context.bot_data.setdefault("drafts", {})
    try:
        d = await asyncio.to_thread(generate_draft, history)
    except Exception as e:
        log.exception("draft xatosi")
        await context.bot.send_message(OWNER_ID, f"Draft yaratishda xato: {e}")
        return
    did = uuid.uuid4().hex[:8]
    drafts[did] = d
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Tasdiqlash", callback_data=f"ok:{did}"),
                InlineKeyboardButton("🔄 Qayta", callback_data=f"redo:{did}"),
                InlineKeyboardButton("❌ Rad", callback_data=f"no:{did}"),
            ]
        ]
    )
    caption = f"📝 {d['topic']}\n\n{d['caption']}"
    try:
        await context.bot.send_photo(OWNER_ID, d["image_url"], caption=caption[:1024], reply_markup=kb)
    except Exception:
        # rasm yuklanmasa matnni o'zini yuboramiz
        await context.bot.send_message(OWNER_ID, caption[:4000], reply_markup=kb)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not owner_only(update):
        return
    action, did = q.data.split(":")
    drafts = context.bot_data.setdefault("drafts", {})
    d = drafts.get(did)
    if not d:
        await q.message.reply_text("Bu draft eskirgan.")
        return
    await q.edit_message_reply_markup(reply_markup=None)

    if action == "ok":
        try:
            post_id = await asyncio.to_thread(publish_to_instagram, d["image_url"], d["caption"])
            context.bot_data.setdefault("history", []).append(d["topic"])
            await q.message.reply_text(f"✅ Instagramga joylandi (id: {post_id})")
        except Exception as e:
            log.exception("joylash xatosi")
            await q.message.reply_text(f"Joylashda xato: {e}")
    elif action == "redo":
        await q.message.reply_text("Yangisini tayyorlayapman...")
        await send_new_draft(context)
    else:
        context.bot_data.setdefault("history", []).append(d["topic"])
        await q.message.reply_text("Rad etildi.")
    drafts.pop(did, None)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if owner_only(update):
        await update.message.reply_text("Tayyorlayapman...")
        await send_new_draft(context)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if owner_only(update):
        await update.message.reply_text(
            f"Salom! Har kuni {POST_TIME} da yangi post yuboraman.\n/new - hozir yangi draft"
        )
    else:
        await update.message.reply_text("Bu shaxsiy bot.")


def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CallbackQueryHandler(on_button))
    h, m = map(int, POST_TIME.split(":"))
    app.job_queue.run_daily(send_new_draft, time=dt.time(h, m, tzinfo=TZ))
    app.run_polling()


if __name__ == "__main__":
    main()
