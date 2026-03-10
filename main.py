"""
Maler-Agent — Telegram AI Angebots-Generator
Mit Rückfragen-Funktion bei unvollständigen Informationen
"""

import os
import json
import logging
import requests
from datetime import datetime, timedelta

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── Konfiguration ─────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GOOGLE_SHEET_ID    = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]

# ── Gesprächsspeicher (pro Chat-ID) ───────────────────────────────────────────
# Format: { chat_id: [ {"role": "user"/"assistant", "content": "..."}, ... ] }
gespraech = {}

# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_sheet_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

# ── Groq Transkription ────────────────────────────────────────────────────────
def transkribiere_audio(file_bytes: bytes, filename: str) -> str:
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    files = {"file": (filename, file_bytes, "audio/ogg")}
    data = {"model": "whisper-large-v3", "language": "de", "response_format": "text"}
    r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    r.raise_for_status()
    return r.text.strip()

# ── Google Sheets lesen ───────────────────────────────────────────────────────
def lese_sheets_daten():
    gc = get_sheet_client()
    wb = gc.open_by_key(GOOGLE_SHEET_ID)

    stamm_sheet = wb.worksheet("Betriebsstamm_Agent")
    zeile2 = stamm_sheet.row_values(2)
    betrieb = {
        "betriebsname":      zeile2[0] if len(zeile2) > 0 else "",
        "stundensatz":       zeile2[1] if len(zeile2) > 1 else "",
        "mwst":              zeile2[2] if len(zeile2) > 2 else "",
        "gewinnaufschlag":   zeile2[3] if len(zeile2) > 3 else "",
        "mindestauftrag":    zeile2[4] if len(zeile2) > 4 else "",
        "anfahrtspauschale": zeile2[5] if len(zeile2) > 5 else "",
    }

    leistungen_sheet = wb.worksheet("🔧 Leistungen")
    leistungen_rows = leistungen_sheet.get_all_values()[1:]
    leistungen_text = "\n".join(["|".join(row) for row in leistungen_rows if any(row)])

    material_sheet = wb.worksheet("🎨 Materialpreise")
    material_rows = material_sheet.get_all_values()[1:]
    material_text = "\n".join(["|".join(row) for row in material_rows if any(row)])

    return betrieb, leistungen_text, material_text

# ── Claude API ────────────────────────────────────────────────────────────────
def frage_claude(verlauf: list, betrieb: dict, leistungen: str, material: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system = f"""Du bist ein Kalkulationsassistent für Malerbetriebe.

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

DEINE AUFGABE:
Prüfe ob die Anfrage vollständig genug ist um ein Angebot zu erstellen.

Für ein vollständiges Angebot werden MINDESTENS benötigt:
- Mindestens eine konkrete Leistung (z.B. streichen, tapezieren)
- Mengenangabe (z.B. m², Stück, lfd. m) für jede Leistung

NICHT zwingend nötig (schätze wenn fehlend):
- Kundenname (verwende "Kunde" wenn unbekannt)
- Adresse
- Altbau/Neubau (nimm Standard-Schwierigkeit 1.0)

ANTWORTFORMAT — NUR eines dieser zwei JSON-Formate:

Falls Infos FEHLEN:
{{
  "status": "rueckfrage",
  "frage": "Deine konkrete Rückfrage an den Nutzer"
}}

Falls alle Infos VORHANDEN:
{{
  "status": "angebot",
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

Antworte NUR mit dem JSON-Objekt. Keine Backticks, kein Markdown, keine Erklärungen."""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        system=system,
        messages=verlauf
    )

    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    log.info(f"CLAUDE_RAW: {raw[:300]}")
    return json.loads(raw)

# ── Angebot in Sheets speichern ───────────────────────────────────────────────
def speichere_angebot(angebot: dict) -> int:
    gc = get_sheet_client()
    wb = gc.open_by_key(GOOGLE_SHEET_ID)
    heute = datetime.now().strftime("%d.%m.%Y")
    gueltig = (datetime.now() + timedelta(days=30)).strftime("%d.%m.%Y")

    angebote_sheet = wb.worksheet("📄 Angebote")
    kopf = [
        "", "", "", "",
        "Entwurf", heute, "", gueltig, "",
        angebot.get("gesamtstunden", ""),
        angebot.get("arbeitskosten", ""),
        angebot.get("materialkosten", ""),
        angebot.get("anfahrt", ""),
        angebot.get("zwischensumme_netto", ""),
        angebot.get("gewinnaufschlag_betrag", ""),
        angebot.get("angebotspreis_netto", ""),
        angebot.get("mwst_betrag", ""),
        angebot.get("brutto", ""),
        angebot.get("einleitungstext", ""),
    ]
    angebote_sheet.append_row(kopf, value_input_option="USER_ENTERED")
    angebots_zeile = len(angebote_sheet.get_all_values())

    positionen_sheet = wb.worksheet("📝 Angebotspositionen")
    for pos in angebot.get("positionen", []):
        zeile = [
            "", angebots_zeile,
            pos.get("pos_nr", ""),
            pos.get("leistungs_id", ""),
            pos.get("beschreibung", ""),
            pos.get("einheit", ""),
            pos.get("menge", ""),
            "", "",
            pos.get("gesamtstunden", ""),
            "", "",
            pos.get("materialkosten", ""),
            pos.get("arbeitskosten", ""),
            pos.get("positionspreis_netto", ""),
        ]
        positionen_sheet.append_row(zeile, value_input_option="USER_ENTERED")

    return angebots_zeile

# ── Telegram Handler ──────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    nutzer_text = ""

    # Audio transkribieren
    if update.message.voice or update.message.audio:
        await update.message.reply_text("🎤 Sprachnachricht empfangen, transkribiere...")
        try:
            file_obj = update.message.voice or update.message.audio
            tg_file = await context.bot.get_file(file_obj.file_id)
            audio_bytes = await tg_file.download_as_bytearray()
            nutzer_text = transkribiere_audio(bytes(audio_bytes), "audio.ogg")
            await update.message.reply_text(f"📝 Transkription: {nutzer_text}")
        except Exception as e:
            log.error(f"Transkriptionsfehler: {e}")
            await update.message.reply_text(f"❌ Transkriptionsfehler: {e}")
            return
    elif update.message.text:
        nutzer_text = update.message.text
    else:
        await update.message.reply_text("❓ Bitte sende Text oder eine Sprachnachricht.")
        return

    # Gesprächsverlauf aktualisieren
    if chat_id not in gespraech:
        gespraech[chat_id] = []
    gespraech[chat_id].append({"role": "user", "content": nutzer_text})

    await update.message.reply_text("⚙️ Prüfe Anfrage...")

    try:
        betrieb, leistungen, material = lese_sheets_daten()
        antwort = frage_claude(gespraech[chat_id], betrieb, leistungen, material)

        # Rückfrage
        if antwort.get("status") == "rueckfrage":
            frage = antwort.get("frage", "Kannst du die Anfrage präzisieren?")
            gespraech[chat_id].append({"role": "assistant", "content": frage})
            await update.message.reply_text(f"❓ {frage}")
            return

        # Angebot erstellen
        if antwort.get("status") == "angebot":
            zeile = speichere_angebot(antwort)
            jahr = datetime.now().strftime("%Y")
            ang_nr = f"ANG-{jahr}-{zeile}"

            # Gesprächsverlauf nach erfolgreichem Angebot zurücksetzen
            gespraech[chat_id] = []

            bestaetigung = (
                f"✅ Angebot erstellt — Status: Entwurf\n\n"
                f"Betreff: {antwort.get('betreff', '-')}\n\n"
                f"Nr.: {ang_nr}\n\n"
                f"Netto:  {antwort.get('angebotspreis_netto', '-')} EUR\n"
                f"Brutto: {antwort.get('brutto', '-')} EUR\n\n"
                f"Zum Freigeben antworte mit:\n"
                f"freigeben {ang_nr}"
            )
            await update.message.reply_text(bestaetigung)
            return

        # Unbekannte Antwort
        await update.message.reply_text("❌ Unerwartete Antwort vom Agenten.")

    except json.JSONDecodeError as e:
        log.error(f"JSON-Fehler: {e}")
        gespraech[chat_id] = []
        await update.message.reply_text(
            "❌ Claude hat kein gültiges JSON zurückgegeben. "
            "Bitte Anfrage neu formulieren."
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
