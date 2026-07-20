import os
import re
import json
import uuid
import time as _time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
from datetime import datetime, timedelta, time, date
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from icalendar import Calendar, Event
import pytz
from anthropic import Anthropic

try:
    import caldav
except Exception:
    caldav = None

# ===== KONFIGURATION =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
CALENDAR_LINKS = os.getenv("CALENDAR_LINKS", "").split("|")

# Claude / Anthropic
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# iCloud CalDAV (zum Eintragen + zuverlässigen Lesen)
ICLOUD_USERNAME = os.getenv("ICLOUD_USERNAME")
ICLOUD_APP_PASSWORD = os.getenv("ICLOUD_APP_PASSWORD")
CALDAV_CALENDAR = os.getenv("CALDAV_CALENDAR")  # optional: Name des Zielkalenders
CALDAV_URL = "https://caldav.icloud.com"

# Erinnerung: wie viele Minuten vorher
REMINDER_LEAD_MIN = int(os.getenv("REMINDER_LEAD_MIN", "60"))

TZ = pytz.timezone("Europe/Berlin")

DATA_FILE = "user_data.json"

# Gesprächsverlauf im Speicher
conversation = []
MAX_HISTORY = 12

# Kalender-Cache
_cal_cache = {"time": 0, "events": []}
CAL_CACHE_SECONDS = 300  # 5 Minuten

_caldav_principal = None


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"emails": [], "settings": {}, "reminded": []}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ===== CALDAV (iCloud) =====
def get_caldav_principal():
    """Verbindung zu iCloud aufbauen (einmalig, gecacht)."""
    global _caldav_principal
    if _caldav_principal is not None:
        return _caldav_principal
    if not (caldav and ICLOUD_USERNAME and ICLOUD_APP_PASSWORD):
        return None
    try:
        client = caldav.DAVClient(url=CALDAV_URL, username=ICLOUD_USERNAME,
                                  password=ICLOUD_APP_PASSWORD)
        _caldav_principal = client.principal()
        return _caldav_principal
    except Exception as e:
        print(f"CalDAV-Verbindung fehlgeschlagen: {e}")
        return None


def pick_target_calendar(principal):
    """Wähle den Zielkalender zum Eintragen."""
    calendars = principal.calendars()
    if not calendars:
        return None
    if CALDAV_CALENDAR:
        for c in calendars:
            try:
                if (c.name or "").strip().lower() == CALDAV_CALENDAR.strip().lower():
                    return c
            except Exception:
                pass
    return calendars[0]


def _normalize_start(dtval):
    """Gibt (aware_datetime_or_None, all_day_bool) zurück."""
    if isinstance(dtval, datetime):
        aware = dtval if dtval.tzinfo else TZ.localize(dtval)
        return aware.astimezone(TZ), False
    if isinstance(dtval, date):
        # Ganztägig
        return TZ.localize(datetime(dtval.year, dtval.month, dtval.day, 0, 0)), True
    return None, False


def caldav_get_events(days=7):
    """Termine der nächsten N Tage über CalDAV holen."""
    principal = get_caldav_principal()
    if principal is None:
        return None  # Signal: CalDAV nicht verfügbar
    start = datetime.now(TZ)
    end = start + timedelta(days=days)
    events = []
    try:
        for cal in principal.calendars():
            try:
                results = cal.search(start=start, end=end, event=True, expand=True)
            except Exception:
                try:
                    results = cal.date_search(start=start, end=end)
                except Exception:
                    results = []
            for ev in results:
                try:
                    comp = ev.icalendar_component
                    dtstart = comp.get("dtstart")
                    if not dtstart:
                        continue
                    start_dt, all_day = _normalize_start(dtstart.dt)
                    if start_dt is None:
                        continue
                    events.append({
                        "title": str(comp.get("summary", "Kein Titel")),
                        "start": start_dt,
                        "all_day": all_day,
                        "uid": str(comp.get("uid", "")) + "|" + start_dt.isoformat(),
                    })
                except Exception:
                    continue
    except Exception as e:
        print(f"CalDAV-Suche fehlgeschlagen: {e}")
        return None
    events.sort(key=lambda x: x["start"])
    return events


def caldav_add_event(title, start_dt, duration_min=60):
    """Termin in iCloud eintragen. start_dt: naive oder aware datetime (Berlin)."""
    principal = get_caldav_principal()
    if principal is None:
        return False, "iCloud ist nicht verbunden (ICLOUD_USERNAME / ICLOUD_APP_PASSWORD fehlen)."
    cal = pick_target_calendar(principal)
    if cal is None:
        return False, "Kein Kalender gefunden."
    if start_dt.tzinfo is None:
        start_dt = TZ.localize(start_dt)
    end_dt = start_dt + timedelta(minutes=duration_min)

    c = Calendar()
    c.add("prodid", "-//telegram-bot-marco//DE")
    c.add("version", "2.0")
    ev = Event()
    ev.add("summary", title)
    ev.add("dtstart", start_dt)
    ev.add("dtend", end_dt)
    ev.add("dtstamp", datetime.now(TZ))
    ev["uid"] = str(uuid.uuid4()) + "@telegram-bot-marco"
    c.add_component(ev)
    try:
        cal.save_event(c.to_ical().decode("utf-8"))
        _cal_cache["time"] = 0  # Cache leeren, damit neuer Termin sichtbar wird
        return True, None
    except Exception as e:
        print(f"Termin eintragen fehlgeschlagen: {e}")
        return False, str(e)


# ===== ICS-FALLBACK (nur lesend) =====
def get_calendar_events_ics(cal_link, days=7):
    try:
        response = requests.get(cal_link, timeout=10)
        if response.status_code != 200:
            return []
        cal = Calendar.from_ical(response.content)
        events = []
        today = datetime.now(TZ)
        horizon = today + timedelta(days=days)
        for component in cal.walk():
            if component.name == "VEVENT":
                dtstart = component.get("dtstart")
                if not dtstart:
                    continue
                start_dt, all_day = _normalize_start(dtstart.dt)
                if start_dt is None:
                    continue
                if today.date() <= start_dt.date() <= horizon.date():
                    events.append({
                        "title": str(component.get("summary", "Kein Titel")),
                        "start": start_dt,
                        "all_day": all_day,
                        "uid": str(component.get("uid", "")) + "|" + start_dt.isoformat(),
                    })
        return events
    except Exception as e:
        print(f"Fehler beim Abrufen des Kalenders: {e}")
        return []


def get_events(days=7, force=False):
    """Zentrale Termin-Abfrage: erst CalDAV, sonst ICS-Fallback. Mit Cache."""
    now = _time.time()
    if not force and (now - _cal_cache["time"]) < CAL_CACHE_SECONDS:
        return _cal_cache["events"]

    events = caldav_get_events(days=days)
    if events is None:  # CalDAV nicht verfügbar -> ICS
        events = []
        for link in CALENDAR_LINKS:
            if link.strip():
                events.extend(get_calendar_events_ics(link, days=days))
        events.sort(key=lambda x: x["start"])

    _cal_cache["time"] = now
    _cal_cache["events"] = events
    return events


def format_events(events):
    if not events:
        return "📅 Keine Termine in den nächsten 7 Tagen."
    lines = ["📅 **Deine Termine (nächste 7 Tage):**\n"]
    for e in events:
        d = e["start"].strftime("%a %d.%m.")
        if e["all_day"]:
            lines.append(f"🗓 {d} (ganztägig) – {e['title']}")
        else:
            lines.append(f"🕐 {d} {e['start'].strftime('%H:%M')} – {e['title']}")
    return "\n".join(lines)


# ===== CLAUDE =====
def ask_claude(user_text):
    if anthropic_client is None:
        return ("⚠️ Ich bin noch nicht mit Claude verbunden. "
                "Bitte den ANTHROPIC_API_KEY in den Render-Einstellungen setzen.")

    events = get_events()
    if events:
        cal_context = "Termine der nächsten 7 Tage:\n" + "\n".join(
            f"- {e['start'].strftime('%Y-%m-%d %H:%M')} {e['title']}"
            + (" (ganztägig)" if e["all_day"] else "")
            for e in events
        )
    else:
        cal_context = "Aktuell keine Termine in den nächsten 7 Tagen."

    heute = datetime.now(TZ).strftime("%A, %d.%m.%Y %H:%M")

    system_prompt = (
        "Du bist Marcos persönlicher Assistent in Telegram. "
        "Du antwortest kurz, freundlich und auf Deutsch.\n"
        f"Aktuelles Datum/Uhrzeit (Europe/Berlin): {heute}.\n\n"
        f"{cal_context}\n\n"
        "WICHTIG – Termin eintragen: Wenn Marco dich bittet, einen Termin/Kalendereintrag "
        "hinzuzufügen (z. B. 'trag mir morgen 18 Uhr Zahnarzt ein'), antworte NICHT in Prosa, "
        "sondern gib EXAKT eine Zeile aus:\n"
        "CREATE_EVENT {\"title\": \"...\", \"start\": \"YYYY-MM-DDTHH:MM\", \"duration_min\": 60}\n"
        "Die Startzeit ist Lokalzeit (Berlin). Löse 'heute', 'morgen', Wochentage anhand des "
        "aktuellen Datums auf. Wenn die Uhrzeit oder der Titel fehlt, frage stattdessen kurz nach."
    )

    conversation.append({"role": "user", "content": user_text})
    del conversation[:-MAX_HISTORY]

    try:
        resp = anthropic_client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1000,
            system=system_prompt,
            messages=conversation,
        )
        answer = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        answer = answer or "(keine Antwort)"
        conversation.append({"role": "assistant", "content": answer})
        del conversation[:-MAX_HISTORY]
        return answer
    except Exception as e:
        print(f"Fehler bei Claude: {e}")
        if conversation and conversation[-1]["role"] == "user":
            conversation.pop()
        return f"⚠️ Fehler bei der Antwort: {e}"


def try_handle_create_event(answer_text):
    """Wenn Claude CREATE_EVENT ausgibt, Termin anlegen. Gibt Bestätigungstext oder None."""
    m = re.search(r"CREATE_EVENT\s*(\{.*\})", answer_text, re.DOTALL)
    if not m:
        return None
    try:
        payload = json.loads(m.group(1))
        title = payload["title"]
        start_dt = datetime.fromisoformat(payload["start"])
        duration = int(payload.get("duration_min", 60))
    except Exception as e:
        return f"⚠️ Ich konnte den Termin nicht verstehen ({e}). Sag es mir bitte nochmal, z. B. 'morgen 18 Uhr Zahnarzt'."

    ok, err = caldav_add_event(title, start_dt, duration)
    if ok:
        disp = TZ.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(TZ)
        return f"✅ Eingetragen: **{title}** am {disp.strftime('%a %d.%m. %H:%M')} Uhr."
    return f"⚠️ Konnte den Termin nicht eintragen: {err}"


# ===== TÄGLICHE JOBS + ERINNERUNG =====
async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    events = get_events(force=True)
    await context.bot.send_message(chat_id=OWNER_ID, text=format_events(events),
                                   parse_mode='Markdown')


async def email_question(context: ContextTypes.DEFAULT_TYPE):
    message = "📧 **Guten Morgen!**\n\nSchreib mir deine wichtigsten Emails von gestern/heute, dann fasse ich sie zusammen."
    await context.bot.send_message(chat_id=OWNER_ID, text=message, parse_mode='Markdown')


async def upcoming_reminder(context: ContextTypes.DEFAULT_TYPE):
    """Alle 10 Min: erinnert ~REMINDER_LEAD_MIN vor einem Termin."""
    events = get_events(days=2, force=True)
    now = datetime.now(TZ)
    data = load_data()
    reminded = set(data.get("reminded", []))
    changed = False

    for e in events:
        if e["all_day"]:
            continue
        minutes_until = (e["start"] - now).total_seconds() / 60.0
        if 0 < minutes_until <= REMINDER_LEAD_MIN and e["uid"] not in reminded:
            mins = int(round(minutes_until))
            if mins >= 55:
                vorlauf = "in ca. 1 Stunde"
            elif mins <= 5:
                vorlauf = "gleich"
            else:
                vorlauf = f"in ca. {mins} Minuten"
            msg = (f"⏰ **Erinnerung:** {vorlauf} hast du den Termin "
                   f"**{e['title']}** um {e['start'].strftime('%H:%M')} Uhr.")
            await context.bot.send_message(chat_id=OWNER_ID, text=msg, parse_mode='Markdown')
            reminded.add(e["uid"])
            changed = True

    if changed:
        data["reminded"] = list(reminded)[-200:]
        save_data(data)


# ===== BEFEHLE =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return
    message = """
✅ **Assistent aktiv!**

Ich bin dein persönlicher Assistent – schreib mir einfach. 🤖

Ich kann:
🧠 Auf deine Nachrichten antworten (mit Claude)
📅 Termine anzeigen & **neue Termine in deinen iPhone-Kalender eintragen**
⏰ Dich ~1 Stunde vor einem Termin erinnern
📧 Dich nach deinen wichtigsten Emails fragen

**Beispiele:**
„Trag mir morgen 18 Uhr Zahnarzt ein"
„Was habe ich diese Woche?"

**Befehle:** /start /termine /emails /help
"""
    await update.message.reply_text(message, parse_mode='Markdown')


async def termine(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return
    events = get_events(force=True)
    await update.message.reply_text(format_events(events), parse_mode='Markdown')


async def emails_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return
    data = load_data()
    if data.get("emails"):
        message = "📧 **Gespeicherte Nachrichten:**\n\n"
        for i, email in enumerate(data["emails"][-10:], 1):
            message += f"{i}. {email['text']}\n"
    else:
        message = "📧 Noch keine Nachrichten gespeichert."
    await update.message.reply_text(message, parse_mode='Markdown')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    user_text = update.message.text

    data = load_data()
    data.setdefault("emails", []).append({
        "text": user_text, "timestamp": datetime.now().isoformat()
    })
    save_data(data)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    answer = ask_claude(user_text)
    created = try_handle_create_event(answer)
    if created is not None:
        if conversation and conversation[-1]["role"] == "assistant":
            conversation[-1]["content"] = created
        await update.message.reply_text(created, parse_mode='Markdown')
    else:
        await update.message.reply_text(answer)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return
    message = """
📖 **Hilfe**

Schreib mir einfach – ich antworte als dein Assistent (Claude). 🤖

📅 **Termine eintragen:** z. B. „Trag mir Freitag 15 Uhr Friseur ein"
📋 **Termine ansehen:** /termine (oder „Was habe ich morgen?")
⏰ **Erinnerung:** Ich melde mich automatisch ~1 Stunde vor jedem Termin.

Automatisch:
⏰ 08:00 – Frage nach Emails
⏰ 09:00 – Termine des Tages

**Befehle:** /termine /emails /help
"""
    await update.message.reply_text(message, parse_mode='Markdown')


# ===== HEALTH + WACHHALTER =====
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
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        print("Kein RENDER_EXTERNAL_URL gesetzt – Wachhalter inaktiv.")
        return
    while True:
        _time.sleep(600)
        try:
            requests.get(url, timeout=15)
            print("Wachhalter-Ping ok")
        except Exception as e:
            print(f"Wachhalter-Ping fehlgeschlagen: {e}")


def main():
    threading.Thread(target=start_health_server, daemon=True).start()
    threading.Thread(target=keep_alive, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("termine", termine))
    app.add_handler(CommandHandler("emails", emails_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_daily(email_question, time=time(8, 0, tzinfo=TZ))
    app.job_queue.run_daily(daily_reminder, time=time(9, 0, tzinfo=TZ))
    app.job_queue.run_repeating(upcoming_reminder, interval=600, first=30)

    print("✅ Bot läuft!")
    app.run_polling()


if __name__ == "__main__":
    main()
