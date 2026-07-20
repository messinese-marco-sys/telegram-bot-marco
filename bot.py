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

# iCloud CalDAV
ICLOUD_USERNAME = os.getenv("ICLOUD_USERNAME")
ICLOUD_APP_PASSWORD = os.getenv("ICLOUD_APP_PASSWORD")
CALDAV_CALENDAR = os.getenv("CALDAV_CALENDAR")
CALDAV_URL = "https://caldav.icloud.com"

# Notion
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_NOTES_PAGE_ID = os.getenv("NOTION_NOTES_PAGE_ID")
NOTION_TODO_PAGE_ID = os.getenv("NOTION_TODO_PAGE_ID")
NOTION_VERSION = "2022-06-28"

# Erinnerung: wie viele Minuten vor einem Termin
REMINDER_LEAD_MIN = int(os.getenv("REMINDER_LEAD_MIN", "60"))

TZ = pytz.timezone("Europe/Berlin")

DATA_FILE = "user_data.json"

conversation = []
MAX_HISTORY = 12

_cal_cache = {"time": 0, "events": []}
CAL_CACHE_SECONDS = 300

_caldav_principal = None


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"emails": [], "settings": {}, "reminded": [], "reminders": []}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ===== CALDAV (iCloud) =====
def get_caldav_principal():
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
    if isinstance(dtval, datetime):
        aware = dtval if dtval.tzinfo else TZ.localize(dtval)
        return aware.astimezone(TZ), False
    if isinstance(dtval, date):
        return TZ.localize(datetime(dtval.year, dtval.month, dtval.day, 0, 0)), True
    return None, False


def caldav_get_events(days=7):
    principal = get_caldav_principal()
    if principal is None:
        return None
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
    principal = get_caldav_principal()
    if principal is None:
        return False, "iCloud ist nicht verbunden."
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
        _cal_cache["time"] = 0
        return True, None
    except Exception as e:
        print(f"Termin eintragen fehlgeschlagen: {e}")
        return False, str(e)


# ===== ICS-FALLBACK =====
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
    now = _time.time()
    if not force and (now - _cal_cache["time"]) < CAL_CACHE_SECONDS:
        return _cal_cache["events"]

    events = caldav_get_events(days=days)
    if events is None:
        events = []
        for link in CALENDAR_LINKS:
            if link.strip() and link.strip().startswith("http"):
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


# ===== NOTION =====
def notion_append(page_id, block):
    if not (NOTION_TOKEN and page_id):
        return False, "Notion ist nicht konfiguriert (Token/Seiten-ID fehlt)."
    url = f"https://api.notion.com/v1/blocks/{page_id}/children"
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }
    try:
        r = requests.patch(url, headers=headers, json={"children": [block]}, timeout=15)
        if r.status_code in (200, 201):
            return True, None
        return False, f"{r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)


def add_todo(text):
    block = {"object": "block", "type": "to_do",
             "to_do": {"rich_text": [{"type": "text", "text": {"content": text}}], "checked": False}}
    return notion_append(NOTION_TODO_PAGE_ID, block)


def add_note(text):
    block = {"object": "block", "type": "bulleted_list_item",
             "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text}}]}}
    return notion_append(NOTION_NOTES_PAGE_ID, block)


# ===== PERSÖNLICHE ERINNERUNGEN =====
async def send_custom_reminder(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    await context.bot.send_message(chat_id=OWNER_ID, text=f"⏰ **Erinnerung:** {d['text']}",
                                   parse_mode='Markdown')
    data = load_data()
    data["reminders"] = [r for r in data.get("reminders", []) if r["id"] != d["id"]]
    save_data(data)


def schedule_reminder(job_queue, rid, text, when_dt):
    if when_dt.tzinfo is None:
        when_dt = TZ.localize(when_dt)
    if when_dt <= datetime.now(TZ):
        return False
    job_queue.run_once(send_custom_reminder, when=when_dt, data={"id": rid, "text": text})
    return True


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
        "Wenn Marco eine AKTION will, antworte NICHT in Prosa, sondern gib EXAKT eine einzige Zeile "
        "mit genau einem der folgenden Kommandos und JSON aus:\n"
        "- Termin/Kalender eintragen: CREATE_EVENT {\"title\": \"...\", \"start\": \"YYYY-MM-DDTHH:MM\", \"duration_min\": 60}\n"
        "- Todo/Aufgabe: CREATE_TODO {\"text\": \"...\"}\n"
        "- Notiz: CREATE_NOTE {\"text\": \"...\"}\n"
        "- Persönliche Erinnerung zu einer Uhrzeit (z. B. 'erinnere mich morgen 17 Uhr an X'): "
        "CREATE_REMINDER {\"text\": \"...\", \"when\": \"YYYY-MM-DDTHH:MM\"}\n"
        "Zeiten sind Lokalzeit (Berlin). Löse 'heute', 'morgen', Wochentage anhand des aktuellen Datums auf. "
        "Unterschied: CREATE_EVENT ist ein Kalendertermin, CREATE_REMINDER ist nur eine Nachricht zur Uhrzeit. "
        "Wenn Infos fehlen (Uhrzeit/Titel), frage stattdessen kurz nach. "
        "Bei normalen Fragen/Gesprächen antworte einfach normal in Prosa."
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


def parse_directive(answer_text):
    for key in ("CREATE_EVENT", "CREATE_TODO", "CREATE_NOTE", "CREATE_REMINDER"):
        m = re.search(key + r"\s*(\{.*\})", answer_text, re.DOTALL)
        if m:
            try:
                return key, json.loads(m.group(1))
            except Exception as e:
                return key, {"_error": str(e)}
    return None, None


def execute_directive(kind, payload, context):
    """Führt eine Aktion aus und gibt den Bestätigungstext zurück."""
    if "_error" in payload:
        return "⚠️ Das habe ich nicht ganz verstanden – sag es mir bitte nochmal etwas genauer."

    if kind == "CREATE_EVENT":
        try:
            title = payload["title"]
            start_dt = datetime.fromisoformat(payload["start"])
            duration = int(payload.get("duration_min", 60))
        except Exception:
            return "⚠️ Termin unklar – z. B. 'morgen 18 Uhr Zahnarzt'."
        ok, err = caldav_add_event(title, start_dt, duration)
        if ok:
            disp = TZ.localize(start_dt) if start_dt.tzinfo is None else start_dt.astimezone(TZ)
            return f"✅ Termin eingetragen: **{title}** am {disp.strftime('%a %d.%m. %H:%M')} Uhr."
        return f"⚠️ Konnte den Termin nicht eintragen: {err}"

    if kind == "CREATE_TODO":
        text = payload.get("text", "").strip()
        if not text:
            return "⚠️ Was soll das Todo sein?"
        ok, err = add_todo(text)
        return f"✅ Todo in Notion gespeichert: {text}" if ok else f"⚠️ Konnte das Todo nicht speichern: {err}"

    if kind == "CREATE_NOTE":
        text = payload.get("text", "").strip()
        if not text:
            return "⚠️ Was soll die Notiz sein?"
        ok, err = add_note(text)
        return f"📝 Notiz in Notion gespeichert: {text}" if ok else f"⚠️ Konnte die Notiz nicht speichern: {err}"

    if kind == "CREATE_REMINDER":
        text = payload.get("text", "").strip()
        try:
            when_dt = datetime.fromisoformat(payload["when"])
        except Exception:
            return "⚠️ Wann soll ich dich erinnern? z. B. 'morgen 17 Uhr'."
        if when_dt.tzinfo is None:
            when_dt = TZ.localize(when_dt)
        if when_dt <= datetime.now(TZ):
            return "⚠️ Diese Zeit liegt in der Vergangenheit."
        rid = str(uuid.uuid4())
        data = load_data()
        data.setdefault("reminders", []).append({"id": rid, "text": text, "when": when_dt.isoformat()})
        save_data(data)
        schedule_reminder(context.job_queue, rid, text, when_dt)
        return f"⏰ Erinnerung gesetzt: {when_dt.strftime('%a %d.%m. %H:%M')} Uhr – „{text}"

    return None


# ===== TÄGLICHE JOBS + TERMIN-ERINNERUNG =====
async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    events = get_events(force=True)
    await context.bot.send_message(chat_id=OWNER_ID, text=format_events(events),
                                   parse_mode='Markdown')


async def email_question(context: ContextTypes.DEFAULT_TYPE):
    message = "📧 **Guten Morgen!**\n\nSchreib mir deine wichtigsten Emails von gestern/heute, dann fasse ich sie zusammen."
    await context.bot.send_message(chat_id=OWNER_ID, text=message, parse_mode='Markdown')


async def upcoming_reminder(context: ContextTypes.DEFAULT_TYPE):
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

Schreib mir einfach – ich erledige das für dich. 🤖

Ich kann:
🧠 Auf deine Nachrichten antworten (mit Claude)
📅 Termine anzeigen & in deinen iPhone-Kalender eintragen
⏰ Dich vor Terminen erinnern & persönliche Erinnerungen setzen
✅ Todos in Notion speichern
📝 Notizen in Notion speichern

**Beispiele:**
„Trag mir morgen 18 Uhr Zahnarzt ein"
„Todo: Rechnung an Kaster schicken"
„Notiz: Idee für Fotobox-Angebot"
„Erinnere mich morgen 17 Uhr an den Anruf"

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
    kind, payload = parse_directive(answer)
    if kind is not None:
        reply = execute_directive(kind, payload, context)
        if conversation and conversation[-1]["role"] == "assistant":
            conversation[-1]["content"] = reply
        await update.message.reply_text(reply, parse_mode='Markdown')
    else:
        await update.message.reply_text(answer)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ Zugriff verweigert!")
        return
    message = """
📖 **Hilfe**

Schreib mir einfach, ich erledige es:
📅 „Trag mir Freitag 15 Uhr Friseur ein" → Kalender
✅ „Todo: Angebot schreiben" → Notion
📝 „Notiz: Idee XY" → Notion
⏰ „Erinnere mich morgen 17 Uhr an den Anruf" → Nachricht zur Uhrzeit
📋 /termine → deine Termine

Automatisch:
⏰ ~1 Std vor jedem Termin
⏰ 08:00 Email-Frage · 09:00 Termine des Tages

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


def reschedule_saved_reminders(job_queue):
    """Beim Start: gespeicherte Erinnerungen neu einplanen, abgelaufene entfernen."""
    data = load_data()
    now = datetime.now(TZ)
    kept = []
    for r in data.get("reminders", []):
        try:
            when_dt = datetime.fromisoformat(r["when"])
            if when_dt.tzinfo is None:
                when_dt = TZ.localize(when_dt)
        except Exception:
            continue
        if when_dt > now:
            schedule_reminder(job_queue, r["id"], r["text"], when_dt)
            kept.append(r)
    data["reminders"] = kept
    save_data(data)


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

    reschedule_saved_reminders(app.job_queue)

    print("✅ Bot läuft!")
    app.run_polling()


if __name__ == "__main__":
    main()
