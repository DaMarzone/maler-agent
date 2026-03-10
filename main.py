"""
Maler-Agent — Telegram AI Angebots-Generator
============================================
Voraussetzungen (pip install):
  python-telegram-bot==20.7
  anthropic
  gspread
  google-auth
  requests

Umgebungsvariablen (in Railway als Environment Variables setzen):
  TELEGRAM_TOKEN        → Dein Telegram Bot Token
  ANTHROPIC_API_KEY     → Dein Anthropic API Key
  GROQ_API_KEY          → Dein Groq API Key
  GOOGLE_SHEET_ID       → Die Spreadsheet-ID aus der Google Sheets URL
  GOOGLE_CREDENTIALS    → Inhalt der service_account.json (als JSON-String)
"""

import os
import json
import logging
import requests
import tempfile
from datetime import datetime, timedelta

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Konfiguration aus Umgebungsvariablen ─────────────────────────────────────
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GROQ_API_KEY      = os.environ["GROQ_API_KEY"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]   # JSON-String

# ── Google Sheets verbinden ───────────────────────────────────────────────────
def get_sheet_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

# ── Schritt 1: Sprachnachricht transkribieren (Groq Whisper) ──────────────────
def transkribiere_audio(file_bytes: bytes, filename: str) -> str:
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {"file": (filename, file_bytes, "audio/ogg")}
    data = {"model": "whisper-large-v3", "language": "de", "response_format": "text"}
    r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    r.raise_for_status()
    return r.text.strip()

# ── Schritt 2: Google Sheets Daten lesen ─────────────────────────────────────
def lese_sheets_daten():
    gc = get_sheet_client()
    wb = gc.open_by_key(GOOGLE_SHEET_ID)

    # Betriebsstamm — Zeile 2 (horizontal)
    stamm_sheet = wb.worksheet("Betriebsstamm_Agent")
    zeile2 = stamm_sheet.row_values(2)
    betrieb = {
        "betriebsname":         zeile2[0] if len(zeile2) > 0 else "",
        "stundensatz":          zeile2[1] if len(zeile2) > 1 else "",
        "mwst":                 zeile2[2] if len(zeile2) > 2 else "",
        "gewinnaufschlag":      zeile2[3] if len(zeile2) > 3 else "",
        "mindestauftrag":       zeile2[4] if len(zeile2) > 4 else "",
        "anfahrtspauschale":    zeile2[5] if len(zeile2) > 5 else "",
    }

    # Leistungen — alle Zeilen ab Zeile 2
    leistungen_sheet = wb.worksheet("🔧 Leistungen")
    leistungen_rows = leistungen_sheet.get_all_values()[1:]  # ohne Header
    leistungen_text = "\n".join(["|".join(row) for row in leistungen_rows if any(row)])

    # Materialpreise — alle Zeilen ab Zeile 2
    material_sheet = wb.worksheet("🎨 Materialpreise")
    material_rows = material_sheet.get_all_values()[1:]  # ohne Header
    material_text = "\n".join(["|".join(row) for row in material_rows if any(row)])

    return betrieb, leistungen_text, material_text

# ── Schritt 3: Claude API — Angebot berechnen ────────────────────────────────
def erstelle_angebot_json(anfrage: str, betrieb: dict, leistungen: str, material: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""ANFRAGE: {anfrage}

BETRIEBSSTAMM:
Betriebsname: {betrieb['betriebsname']}
Stundensatz (netto): {betrieb['stundensatz']} EUR
Gewinnaufschlag: {betrieb['gewinnaufschlag']} %
MwSt-Satz: {betrieb['mwst']} %
Anfahrtspauschale: {betrieb['anfahrtspauschale']} EUR
Mindestauftragswert: {betrieb['mindestauftrag']} EUR

LEISTUNGSKATALOG (Format: ID|Art|Kategorie|Einheit|Std_h|Material-ID|Schwierigkeit):
{leistungen}

MATERIALPREISE (Format: ID|Bezeichnung|Kategorie|Einheit|EK-Preis|VK-Preis):
{material}

Antworte NUR mit diesem exakten JSON-Format, keine anderen Felder:
{{
  "betreff": "...",
  "einleitungstext": "...",
  "gesamtstunden": 0.0,
  "arbeitskosten": 0.0,
  "materialkosten": 0.0,
  "anfahrt": 0.0,
  "zwischensumme_netto": 0.0,
  "gewinnaufschlag_betrag": 0.0,
  "angebotspreis_netto": 0.0,
  "mwst_betrag": 0.0,
  "brutto": 0.0,
  "positionen": [
    {{
      "pos_nr": 1,
      "leistungs_id": "L001",
      "beschreibung": "...",
      "einheit": "m²",
      "menge": 0.0,
      "gesamtstunden": 0.0,
      "materialkosten": 0.0,
      "arbeitskosten": 0.0,
      "positionspreis_netto": 0.0
    }}
  ]
}}

Beginne mit {{ und ende mit }}."""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        system=(
            "Du bist ein Kalkulationsassistent fuer Malerbetriebe. "
            "Antworte NUR mit einem reinen JSON-Objekt. "
            "Beginne deine Antwort direkt mit { und ende mit }. "
            "Keine Backticks, kein ```json, keine Erklaerungen, kein Markdown. "
            "Nur das JSON-Objekt selbst."
        ),
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    # Backtick-Schutz falls Claude sich nicht daran hält
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    log.info(f"CLAUDE_RAW: {raw[:500]}")
    return json.loads(raw)

# ── Schritt 4: In Google Sheets schreiben ─────────────────────────────────────
def speichere_angebot(angebot: dict) -> int:
    gc = get_sheet_client()
    wb = gc.open_by_key(GOOGLE_SHEET_ID)
    heute = datetime.now().strftime("%d.%m.%Y")
    gueltig = (datetime.now() + timedelta(days=30)).strftime("%d.%m.%Y")

    # Angebote-Sheet — Kopfzeile
    angebote_sheet = wb.worksheet("📄 Angebote")
    kopf = [
        "",                                      # A — Angebots-ID (leer, auto)
        "",                                      # B — Angebotsnummer
        "",                                      # C — Kunden-ID
        "",                                      # D — Objekt-ID
        "Entwurf",                               # E — Status
        heute,                                   # F — Erstellt am
        "",                                      # G — Gesendet am
        gueltig,                                 # H — Gültig bis
        "",                                      # I — Angenommen am
        angebot.get("gesamtstunden", ""),        # J
        angebot.get("arbeitskosten", ""),        # K
        angebot.get("materialkosten", ""),       # L
        angebot.get("anfahrt", ""),              # M
        angebot.get("zwischensumme_netto", ""),  # N
        angebot.get("gewinnaufschlag_betrag",""),# O
        angebot.get("angebotspreis_netto", ""),  # P
        angebot.get("mwst_betrag", ""),          # Q
        angebot.get("brutto", ""),               # R
        angebot.get("einleitungstext", ""),      # S — Notizen Agent
    ]
    angebote_sheet.append_row(kopf, value_input_option="USER_ENTERED")
    angebots_zeile = len(angebote_sheet.get_all_values())  # Zeilennummer

    # Angebotspositionen-Sheet
    positionen_sheet = wb.worksheet("📝 Angebotspositionen")
    for pos in angebot.get("positionen", []):
        zeile = [
            "",                               # A — Position-ID
            angebots_zeile,                   # B — Angebots-ID (Zeilennummer)
            pos.get("pos_nr", ""),            # C
            pos.get("leistungs_id", ""),      # D
            pos.get("beschreibung", ""),      # E
            pos.get("einheit", ""),           # F
            pos.get("menge", ""),             # G
            "",                               # H
            "",                               # I
            pos.get("gesamtstunden", ""),     # J
            "",                               # K
            "",                               # L
            pos.get("materialkosten", ""),    # M
            pos.get("arbeitskosten", ""),     # N
            pos.get("positionspreis_netto",""),# O
        ]
        positionen_sheet.append_row(zeile, value_input_option="USER_ENTERED")

    return angebots_zeile

# ── Telegram Handler ──────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    anfrage_text = ""

    # Audio- oder Sprachnachricht
    if update.message.voice or update.message.audio:
        await update.message.reply_text("🎤 Sprachnachricht empfangen, transkribiere...")
        try:
            file_obj = update.message.voice or update.message.audio
            tg_file = await context.bot.get_file(file_obj.file_id)
            audio_bytes = await tg_file.download_as_bytearray()
            anfrage_text = transkribiere_audio(bytes(audio_bytes), "audio.ogg")
            await update.message.reply_text(f"📝 Transkription: {anfrage_text}")
        except Exception as e:
            log.error(f"Transkriptionsfehler: {e}")
            await update.message.reply_text(f"❌ Transkriptionsfehler: {e}")
            return

    # Textnachricht
    elif update.message.text:
        anfrage_text = update.message.text
    else:
        await update.message.reply_text("❓ Bitte sende Text oder eine Sprachnachricht.")
        return

    await update.message.reply_text("⚙️ Erstelle Angebot, einen Moment...")

    try:
        betrieb, leistungen, material = lese_sheets_daten()
        angebot = erstelle_angebot_json(anfrage_text, betrieb, leistungen, material)
        zeile = speichere_angebot(angebot)
        jahr = datetime.now().strftime("%Y")
        ang_nr = f"ANG-{jahr}-{zeile}"

        antwort = (
            f"✅ Angebot erstellt — Status: Entwurf\n\n"
            f"Betreff: {angebot.get('betreff', '-')}\n\n"
            f"Nr.: {ang_nr}\n\n"
            f"Netto:  {angebot.get('angebotspreis_netto', '-')} EUR\n"
            f"Brutto: {angebot.get('brutto', '-')} EUR\n\n"
            f"Zum Freigeben antworte mit:\n"
            f"freigeben {ang_nr}"
        )
        await update.message.reply_text(antwort)

    except json.JSONDecodeError as e:
        log.error(f"JSON-Fehler: {e}")
        await update.message.reply_text(
            "❌ Claude hat kein gültiges JSON zurückgegeben. "
            "Bitte Anfrage präziser formulieren."
        )
    except Exception as e:
        log.error(f"Fehler: {e}", exc_info=True)
        await update.message.reply_text(f"❌ Fehler: {e}")

# ── Start ─────────────────────────────────────────────────────────────────────
def main():
    log.info("Maler-Agent startet...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
