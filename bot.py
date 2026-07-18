import os
import json
import requests
from datetime import datetime, timedelta, time
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from icalendar import Calendar
import pytz

# ===== KONFIGURATION =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CALENDAR_LINKS = os.getenv("CALENDAR_LINKS", "").split("|")

# Datenbank für Emails
DATA_FILE = "user_data.json"

def load_data():
    """Lade gespeicherte Daten"""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"emails": [], "settings": {}}

def save_data(data):
    """Speichere Daten"""
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_calendar_events(cal_link):
    """Hole Termine aus iCal-Link"""
    try:
        response = requests.get(cal_link, timeout=10)
        if response.status_code != 200:
            return []

        cal = Calendar.from_ical(response.content)
        events = []

        today = datetime.now(pytz.UTC)
        tomorrow = today + timedelta(days=1)

        for component in cal.walk():
            if component.name == "VEVENT":
                event_start = component.get('dtstart')
                if event_start:
                    dt = event_start.dt
                    if isinstance(dt, datetime):
                        dt_aware = dt.replace(tzinfo=pytz.UTC) if dt.tzinfo is None else dt.astimezone(pytz.UTC)
                        # Nur heutige und morgige Termine
                        if today.date() <= dt_aware.date() <= tomorrow.date():
                            events.append({
                                "title": str(component.get('summary', 'Kein Titel')),
                                "time": dt_aware.strftime("%H:%M"),
                                "date": dt_aware.strftime("%d.%m.%Y")
                            })

        return sorted(events, key=lambda x: x["time"])
    except Exception as e:
        print(f"Fehler beim Abrufen des Kalenders: {e}")
        return []

async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Täglich um 9 Uhr: Termine-Erinnerung"""
    all_events = []

    for link in CALENDAR_LINKS:
        if link.strip():
            events = get_calendar_events(link)
            all_events.extend(events)

    if all_events:
        message = "📅 **Deine Termine heute & morgen:**\n\n"
        for event in all_events:
            message += f"🕐 {event['time']} - {event['title']}\n"
    else:
        message = "📅 Keine Termine für heute und morgen."

    await context.bot.send_message(chat_id=OWNER_ID, text=message, parse_mode='Markdown')

async def email_question(context: ContextTypes.DEFAULT_TYPE):
    """Täglich um 8 Uhr: Email-Frage"""
    message = "📧 **Guten Morgen!**\n\nSchreib mir deine wichtigsten Emails von gestern/heute, dann fasse ich sie zusammen."
    await context.bot.send_message(chat_id=OWNER_ID, text=message, parse_mode='Markdown')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start-Kommando"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return

    message = """
✅ **Assistent aktiv!**

Ich kann für dich:
📅 Täglich Termine aus deinem Kalender erinnern
📧 Dich nach deinen wichtigsten Emails fragen
💾 Alles speichern

**Befehle:**
/start - Diese Nachricht
/termine - Zeige heute Termine
/emails - Zeige gespeicherte Emails
/help - Hilfe
"""
    await update.message.reply_text(message, parse_mode='Markdown')

async def termine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeige aktuelle Termine"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return

    all_events = []
    for link in CALENDAR_LINKS:
        if link.strip():
            events = get_calendar_events(link)
            all_events.extend(events)

    if all_events:
        message = "📅 **Deine nächsten Termine:**\n\n"
        for event in all_events:
            message += f"🕐 {event['time']} - {event['title']}\n"
    else:
        message = "📅 Keine anstehenden Termine."

    await update.message.reply_text(message, parse_mode='Markdown')

async def emails_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeige gespeicherte Emails"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return

    data = load_data()
    if data["emails"]:
        message = "📧 **Gespeicherte Emails:**\n\n"
        for i, email in enumerate(data["emails"][-10:], 1):  # Letzte 10
            message += f"{i}. {email['text']}\n"
    else:
        message = "📧 Noch keine Emails gespeichert."

    await update.message.reply_text(message, parse_mode='Markdown')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verarbeite Nachrichten (Emails speichern)"""
    if update.effective_user.id != OWNER_ID:
        return

    data = load_data()
    data["emails"].append({
        "text": update.message.text,
        "timestamp": datetime.now().isoformat()
    })
    save_data(data)

    await update.message.reply_text("✅ Nachricht gespeichert!")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hilfe"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return

    message = """
📖 **Hilfe**

Der Bot läuft automatisch:
⏰ **08:00 Uhr** - Fragt nach Emails
⏰ **09:00 Uhr** - Zeigt deine Termine

**Manuelle Befehle:**
/termine - Zeige Termine jetzt
/emails - Zeige gespeicherte Emails
/help - Diese Hilfe

**Setup:**
Kalender-Links: 3 iCloud Kalender
Bot Token: Telegram

Fragen? Schreib einfach eine Nachricht!
"""
    await update.message.reply_text(message, parse_mode='Markdown')

def main():
    """Starte den Bot"""
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("termine", termine))
    app.add_handler(CommandHandler("emails", emails_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # Nachrichten verarbeiten
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Tägliche Tasks
    tz = pytz.timezone("Europe/Berlin")
    app.job_queue.run_daily(email_question, time=time(8, 0, tzinfo=tz))
    app.job_queue.run_daily(daily_reminder, time=time(9, 0, tzinfo=tz))

    print("✅ Bot läuft!")
    app.run_polling()

if __name__ == "__main__":
    main()
