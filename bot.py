import os
import json
import time as _time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from datetime import datetime, timedelta, time
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from icalendar import Calendar
import pytz
from anthropic import Anthropic

# ===== KONFIGURATION =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CALENDAR_LINKS = os.getenv("CALENDAR_LINKS", "").split("|")

# Claude / Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# Datenbank für Nachrichten / Verlauf
DATA_FILE = "user_data.json"

# Kurzer Gesprächsverlauf im Speicher (wird bei Neustart geleert)
conversation = []  # Liste aus {"role": "user"/"assistant", "content": "..."}
MAX_HISTORY = 12   # letzte 12 Nachrichten als Kontext

# Einfacher Kalender-Cache, damit nicht bei jeder Nachricht neu geladen wird
_cal_cache = {"time": 0, "events": []}
CAL_CACHE_SECONDS = 600  # 10 Minuten


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
    """Hole Termine aus iCal-Link (heute + morgen)"""
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


def get_all_events(force=False):
    """Alle Termine aus allen Kalendern, mit 10-Min-Cache."""
    now = _time.time()
    if not force and (now - _cal_cache["time"]) < CAL_CACHE_SECONDS:
        return _cal_cache["events"]

    all_events = []
    for link in CALENDAR_LINKS:
        if link.strip():
            all_events.extend(get_calendar_events(link))
    all_events.sort(key=lambda x: (x["date"], x["time"]))
    _cal_cache["time"] = now
    _cal_cache["events"] = all_events
    return all_events


def ask_claude(user_text):
    """Schicke die Nachricht an Claude und gib die Antwort zurück."""
    if anthropic_client is None:
        return ("⚠️ Ich bin noch nicht mit Claude verbunden. "
                "Bitte den ANTHROPIC_API_KEY in den Render-Einstellungen setzen.")

    # Kalender-Kontext für den heutigen Tag mitgeben
    events = get_all_events()
    if events:
        cal_context = "Aktuelle Termine (heute & morgen):\n" + "\n".join(
            f"- {e['date']} {e['time']} {e['title']}" for e in events
        )
    else:
        cal_context = "Aktuell keine Termine in den nächsten zwei Tagen."

    heute = datetime.now(pytz.timezone("Europe/Berlin")).strftime("%A, %d.%m.%Y %H:%M")

    system_prompt = (
        "Du bist Marcos persönlicher Assistent in Telegram. "
        "Du antwortest kurz, freundlich und auf Deutsch. "
        "Du hilfst bei Terminen, E-Mail-Zusammenfassungen, Notizen, Ideen und Alltagsfragen. "
        f"Aktuelles Datum/Uhrzeit: {heute}.\n\n{cal_context}"
    )

    # Verlauf aktualisieren
    conversation.append({"role": "user", "content": user_text})
    del conversation[:-MAX_HISTORY]

    try:
        resp = anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1000,
            system=system_prompt,
            messages=conversation,
        )
        answer = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        answer = answer.strip() or "(keine Antwort)"
        conversation.append({"role": "assistant", "content": answer})
        del conversation[:-MAX_HISTORY]
        return answer
    except Exception as e:
        print(f"Fehler bei Claude: {e}")
        # fehlerhaften User-Turn wieder entfernen, damit der Verlauf sauber bleibt
        if conversation and conversation[-1]["role"] == "user":
            conversation.pop()
        return f"⚠️ Fehler bei der Antwort: {e}"


async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Täglich um 9 Uhr: Termine-Erinnerung"""
    all_events = get_all_events(force=True)
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

Ich bin dein persönlicher Assistent – schreib mir einfach, ich antworte dir direkt. 🤖

Ich kann außerdem:
📅 Täglich Termine aus deinem Kalender erinnern
📧 Dich nach deinen wichtigsten Emails fragen & sie zusammenfassen
💾 Nachrichten speichern

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

    all_events = get_all_events(force=True)
    if all_events:
        message = "📅 **Deine nächsten Termine:**\n\n"
        for event in all_events:
            message += f"🕐 {event['time']} - {event['title']}\n"
    else:
        message = "📅 Keine anstehenden Termine."
    await update.message.reply_text(message, parse_mode='Markdown')


async def emails_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeige gespeicherte Emails/Nachrichten"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return

    data = load_data()
    if data["emails"]:
        message = "📧 **Gespeicherte Nachrichten:**\n\n"
        for i, email in enumerate(data["emails"][-10:], 1):
            message += f"{i}. {email['text']}\n"
    else:
        message = "📧 Noch keine Nachrichten gespeichert."
    await update.message.reply_text(message, parse_mode='Markdown')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Freie Nachrichten: Claude antwortet (und wir loggen die Nachricht)."""
    if update.effective_user.id != OWNER_ID:
        return

    user_text = update.message.text

    # Nachricht im Verlauf/Log speichern
    data = load_data()
    data["emails"].append({
        "text": user_text,
        "timestamp": datetime.now().isoformat()
    })
    save_data(data)

    # "Tippt..." anzeigen, während Claude denkt
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    answer = ask_claude(user_text)
    await update.message.reply_text(answer)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hilfe"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return

    message = """
📖 **Hilfe**

Schreib mir einfach eine Nachricht – ich antworte dir als dein persönlicher Assistent (mit Claude). 🤖

Der Bot läuft automatisch:
⏰ **08:00 Uhr** - Fragt nach Emails
⏰ **09:00 Uhr** - Zeigt deine Termine

**Manuelle Befehle:**
/termine - Zeige Termine jetzt
/emails - Zeige gespeicherte Nachrichten
/help - Diese Hilfe
"""
    await update.message.reply_text(message, parse_mode='Markdown')


# ===== HEALTH-SERVER + WACHHALTER (KEEP-ALIVE) =====
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def start_health_server():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), HealthHandler).serve_forever()


def keep_alive():
    """Pingt die eigene Render-URL alle 10 Minuten, damit der Free-Dienst nicht einschläft."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        print("Kein RENDER_EXTERNAL_URL gesetzt – Wachhalter inaktiv.")
        return
    while True:
        _time.sleep(600)  # 10 Minuten
        try:
            requests.get(url, timeout=15)
            print("Wachhalter-Ping ok")
        except Exception as e:
            print(f"Wachhalter-Ping fehlgeschlagen: {e}")


def main():
    """Starte den Bot"""
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("termine", termine))
    app.add_handler(CommandHandler("emails", emails_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    # Freie Nachrichten -> Claude
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Tägliche Tasks
    tz = pytz.timezone("Europe/Berlin")
    app.job_queue.run_daily(email_question, time=time(8, 0, tzinfo=tz))
    app.job_queue.run_daily(daily_reminder, time=time(9, 0, tzinfo=tz))

    print("✅ Bot läuft!")
    app.run_polling()


if __name__ == "__main__":
    main()
