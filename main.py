"""
Maler-Agent — Telegram AI Angebots-Generator
Fixes: Betriebsadresse im PDF, kein "Vorname von X", doppelte Anrede weg, robusteres JSON-Parsing
"""

import os
import json
import logging
import requests
import re
from datetime import datetime, timedelta
from io import BytesIO

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_RIGHT

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GOOGLE_SHEET_ID    = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]

gespraech = {}

# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_sheet_client():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    return gspread.authorize(creds)

def lese_sheets_daten():
    gc = get_sheet_client()
    wb = gc.open_by_key(GOOGLE_SHEET_ID)
    stamm = wb.worksheet("Betriebsstamm_Agent").row_values(2)
    betrieb = {
        "betriebsname":      stamm[0] if len(stamm) > 0 else "",
        "stundensatz":       stamm[1] if len(stamm) > 1 else "",
        "mwst":              stamm[2] if len(stamm) > 2 else "",
        "gewinnaufschlag":   stamm[3] if len(stamm) > 3 else "",
        "mindestauftrag":    stamm[4] if len(stamm) > 4 else "",
        "anfahrtspauschale": stamm[5] if len(stamm) > 5 else "",
        "strasse":           stamm[6] if len(stamm) > 6 else "",
        "plz":               stamm[7] if len(stamm) > 7 else "",
        "ort":               stamm[8] if len(stamm) > 8 else "",
        "telefon":           stamm[9] if len(stamm) > 9 else "",
        "email":             stamm[10] if len(stamm) > 10 else "",
    }
    leistungen_rows = wb.worksheet("🔧 Leistungen").get_all_values()[1:]
    leistungen_text = "\n".join(["|".join(r) for r in leistungen_rows if any(r)])
    material_rows = wb.worksheet("🎨 Materialpreise").get_all_values()[1:]
    material_text = "\n".join(["|".join(r) for r in material_rows if any(r)])
    return betrieb, leistungen_text, material_text

def speichere_angebot(angebot: dict) -> int:
    gc = get_sheet_client()
    wb = gc.open_by_key(GOOGLE_SHEET_ID)
    heute = datetime.now().strftime("%d.%m.%Y")
    gueltig = (datetime.now() + timedelta(days=30)).strftime("%d.%m.%Y")
    angebote_sheet = wb.worksheet("📄 Angebote")
    angebote_sheet.append_row([
        "", "", "", "", "Entwurf", heute, "", gueltig, "",
        angebot.get("gesamtstunden",""), angebot.get("arbeitskosten",""),
        angebot.get("materialkosten",""), angebot.get("anfahrt",""),
        angebot.get("zwischensumme_netto",""), angebot.get("gewinnaufschlag_betrag",""),
        angebot.get("angebotspreis_netto",""), angebot.get("mwst_betrag",""),
        angebot.get("brutto",""), angebot.get("einleitungstext",""),
    ], value_input_option="USER_ENTERED")
    zeile = len(angebote_sheet.get_all_values())
    positionen_sheet = wb.worksheet("📝 Angebotspositionen")
    for pos in angebot.get("positionen", []):
        positionen_sheet.append_row([
            "", zeile, pos.get("pos_nr",""), pos.get("leistungs_id",""),
            pos.get("beschreibung",""), pos.get("einheit",""), pos.get("menge",""),
            "", "", pos.get("gesamtstunden",""), "", "",
            pos.get("materialkosten",""), pos.get("arbeitskosten",""),
            pos.get("positionspreis_netto",""),
        ], value_input_option="USER_ENTERED")
    return zeile

# ── Groq Transkription ────────────────────────────────────────────────────────
def transkribiere_audio(file_bytes: bytes, filename: str) -> str:
    r = requests.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        files={"file": (filename, file_bytes, "audio/ogg")},
        data={"model": "whisper-large-v3", "language": "de", "response_format": "text"},
        timeout=60
    )
    r.raise_for_status()
    return r.text.strip()

# ── JSON robust parsen ────────────────────────────────────────────────────────
def parse_json_robust(raw: str) -> dict:
    # Backticks entfernen
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    # Erstes { bis letztes } extrahieren
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end+1]
    log.info(f"CLAUDE_RAW: {raw[:300]}")
    return json.loads(raw)

# ── Claude API ────────────────────────────────────────────────────────────────
def frage_claude(verlauf: list, betrieb: dict, leistungen: str, material: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system = f"""Du bist ein Kalkulationsassistent fuer Malerbetriebe.

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

WICHTIG - GEDAECHTNIS:
Lies den GESAMTEN Gespraechsverlauf. Extrahiere alle bereits genannten Informationen.
Frage NIEMALS nach etwas das bereits erwaehnt wurde.

WICHTIG - NAMEN:
Wenn ein vollstaendiger Name genannt wird (z.B. "Kai Weber", "Max Mustermann"), ist dieser vollstaendig.
Frage NIEMALS nach dem Vornamen wenn bereits ein vollstaendiger Name (Vorname + Nachname) vorliegt.
Frage NIEMALS nach dem Nachnamen wenn bereits ein vollstaendiger Name vorliegt.

PFLICHTFELDER:
GRUPPE A - Auftragsdaten: mind. eine Leistung mit Mengenangabe (m2, Stueck, lfd. m)
GRUPPE B - Kundendaten: vollstaendiger Name, Strasse + Hausnummer, PLZ, Ort

VORGEHEN:
1. Extrahiere alle bekannten Infos aus dem gesamten Verlauf
2. Pruefe welche Pflichtfelder noch fehlen
3. Fehlende Infos: Frage ALLE fehlenden Punkte einer Gruppe auf einmal
   - Erst Gruppe A (Auftragsdaten), dann Gruppe B (Kundendaten)
4. Alle Infos vorhanden: Erstelle Angebot

EINLEITUNGSTEXT:
Schreibe NUR den Haupttext des Anschreibens (z.B. "gerne unterbreiten wir Ihnen...").
KEINE Anrede (kein "Sehr geehrte/r...") - die Anrede wird separat gesetzt.
KEIN Schluss (kein "Mit freundlichen Gruessen") - der Schluss wird separat gesetzt.

ANTWORTFORMAT:

Falls Informationen fehlen:
{{"status": "rueckfrage", "frage": "Deine Rueckfrage mit ALLEN fehlenden Punkten"}}

Falls alle Infos vorhanden:
{{"status": "angebot", "kunde_name": "...", "kunde_strasse": "...", "kunde_plz": "...", "kunde_ort": "...", "betreff": "...", "einleitungstext": "...", "gesamtstunden": 0.0, "arbeitskosten": 0.0, "materialkosten": 0.0, "anfahrt": 0.0, "zwischensumme_netto": 0.0, "gewinnaufschlag_betrag": 0.0, "angebotspreis_netto": 0.0, "mwst_betrag": 0.0, "brutto": 0.0, "positionen": [{{"pos_nr": 1, "leistungs_id": "L001", "beschreibung": "...", "einheit": "m2", "menge": 0.0, "gesamtstunden": 0.0, "materialkosten": 0.0, "arbeitskosten": 0.0, "positionspreis_netto": 0.0}}]}}

Antworte NUR mit dem JSON. Keine Backticks, kein Markdown."""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        system=system,
        messages=verlauf
    )
    return parse_json_robust(message.content[0].text)

# ── PDF erstellen ─────────────────────────────────────────────────────────────
def erstelle_pdf(angebot: dict, betrieb: dict, ang_nr: str, heute: str, gueltig: str) -> BytesIO:
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=2*cm, rightMargin=2*cm,
                            topMargin=2*cm, bottomMargin=2*cm)
    W = 17 * cm
    normal = ParagraphStyle("n", fontSize=9, leading=13)
    bold   = ParagraphStyle("b", fontSize=9, leading=13, fontName="Helvetica-Bold")
    small  = ParagraphStyle("s", fontSize=8, leading=11, textColor=colors.grey)
    right  = ParagraphStyle("r", fontSize=9, leading=13, alignment=TA_RIGHT)
    story  = []

    # ── Kopfzeile: Betrieb links | Angebotsdaten rechts ──
    betrieb_info = (
        f"<b>{betrieb['betriebsname']}</b><br/>"
        f"{betrieb.get('strasse','')}<br/>"
        f"{betrieb.get('plz','')} {betrieb.get('ort','')}<br/>"
        f"Tel: {betrieb.get('telefon','')}<br/>"
        f"{betrieb.get('email','')}"
    )
    angebots_info = (
        f"Angebotsnummer: {ang_nr}<br/>"
        f"Datum: {heute}<br/>"
        f"Gueltig bis: {gueltig}"
    )
    kopf = Table([
        [Paragraph(betrieb_info, bold), Paragraph(angebots_info, normal)],
    ], colWidths=[W*0.55, W*0.45])
    kopf.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("BOTTOMPADDING",(0,0),(-1,-1),4),
    ]))
    story.append(kopf)
    story.append(Spacer(1, 0.8*cm))

    # ── Empfaengeradresse ──
    story.append(Paragraph(f"<b>{angebot.get('kunde_name','')}</b>", bold))
    story.append(Paragraph(angebot.get("kunde_strasse",""), normal))
    story.append(Paragraph(f"{angebot.get('kunde_plz','')} {angebot.get('kunde_ort','')}", normal))
    story.append(Spacer(1, 0.7*cm))

    # ── Betreff ──
    story.append(Paragraph(f"<b>Angebot - {angebot.get('betreff','')}</b>",
                 ParagraphStyle("h", fontSize=12, leading=16, fontName="Helvetica-Bold", spaceAfter=6)))
    story.append(Spacer(1, 0.3*cm))

    # ── Anrede + Einleitungstext (nur einmal) ──
    kunde_name = angebot.get("kunde_name","")
    nachname = kunde_name.split()[-1] if kunde_name else ""
    story.append(Paragraph(f"Sehr geehrter Herr/Frau {nachname},", normal))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(angebot.get("einleitungstext",""), normal))
    story.append(Spacer(1, 0.5*cm))

    # ── Positionstabelle ──
    story.append(Paragraph("<b>Leistungsuebersicht</b>", bold))
    story.append(Spacer(1, 0.2*cm))
    pos_rows = [[
        Paragraph("<b>Pos.</b>", bold),
        Paragraph("<b>Leistungsbeschreibung</b>", bold),
        Paragraph("<b>Einheit</b>", bold),
        Paragraph("<b>Menge</b>", bold),
        Paragraph("<b>Preis (EUR)</b>", bold),
    ]]
    for pos in angebot.get("positionen", []):
        pos_rows.append([
            Paragraph(str(pos.get("pos_nr","")), normal),
            Paragraph(pos.get("beschreibung",""), normal),
            Paragraph(pos.get("einheit",""), normal),
            Paragraph(str(pos.get("menge","")), normal),
            Paragraph(f"{float(pos.get('positionspreis_netto',0)):.2f}", right),
        ])
    pos_table = Table(pos_rows, colWidths=[1*cm, W*0.5, 2*cm, 2*cm, 2.5*cm])
    pos_table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#2E5D8E")),
        ("TEXTCOLOR",(0,0),(-1,0),colors.white),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F5F5F5")]),
        ("GRID",(0,0),(-1,-1),0.5,colors.HexColor("#DDDDDD")),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
        ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
    ]))
    story.append(pos_table)
    story.append(Spacer(1, 0.5*cm))

    # ── Summentabelle ──
    mwst_satz = betrieb.get("mwst","19")
    summen_data = [
        [Paragraph("Zwischensumme:", normal), Paragraph(f"{float(angebot.get('zwischensumme_netto',0)):.2f} EUR", right)],
        [Paragraph("Anfahrt:", normal), Paragraph(f"{float(angebot.get('anfahrt',0)):.2f} EUR", right)],
        [Paragraph("Gewinnaufschlag:", normal), Paragraph(f"{float(angebot.get('gewinnaufschlag_betrag',0)):.2f} EUR", right)],
        [Paragraph("Nettobetrag:", normal), Paragraph(f"{float(angebot.get('angebotspreis_netto',0)):.2f} EUR", right)],
        [Paragraph(f"zzgl. {mwst_satz}% MwSt:", normal), Paragraph(f"{float(angebot.get('mwst_betrag',0)):.2f} EUR", right)],
        [Paragraph("<b>Gesamtbetrag brutto:</b>", bold), Paragraph(f"<b>{float(angebot.get('brutto',0)):.2f} EUR</b>", bold)],
    ]
    summen_table = Table(summen_data, colWidths=[W*0.7, W*0.3])
    summen_table.setStyle(TableStyle([
        ("ALIGN",(1,0),(1,-1),"RIGHT"),
        ("LINEABOVE",(0,-1),(-1,-1),1,colors.black),
        ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
    ]))
    story.append(summen_table)
    story.append(Spacer(1, 0.7*cm))

    # ── Konditionen ──
    story.append(Paragraph("<b>Konditionen & Hinweise</b>", bold))
    story.append(Spacer(1, 0.2*cm))
    k_table = Table([
        [Paragraph("Zahlungsziel:", bold), Paragraph("14 Tage nach Rechnungsstellung", normal)],
        [Paragraph("Gueltigkeit:", bold), Paragraph(f"Dieses Angebot ist gueltig bis zum {gueltig}.", normal)],
        [Paragraph("Gewaehrleistung:", bold), Paragraph("5 Jahre gemaess BGB 634a.", normal)],
    ], colWidths=[3.5*cm, W-3.5*cm])
    k_table.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
    ]))
    story.append(k_table)
    story.append(Spacer(1, 0.7*cm))

    # ── Schluss ──
    story.append(Paragraph("Mit freundlichen Gruessen", normal))
    story.append(Spacer(1, 1.2*cm))
    story.append(Paragraph(f"<b>{betrieb['betriebsname']}</b>", bold))
    if betrieb.get("strasse"):
        story.append(Paragraph(f"{betrieb['strasse']}, {betrieb['plz']} {betrieb['ort']}", small))
    if betrieb.get("telefon"):
        story.append(Paragraph(f"Tel: {betrieb['telefon']} | {betrieb.get('email','')}", small))

    doc.build(story)
    buf.seek(0)
    return buf

# ── Telegram Handler ──────────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    nutzer_text = ""

    if update.message.voice or update.message.audio:
        await update.message.reply_text("🎤 Transkribiere...")
        try:
            file_obj = update.message.voice or update.message.audio
            tg_file = await context.bot.get_file(file_obj.file_id)
            audio_bytes = await tg_file.download_as_bytearray()
            nutzer_text = transkribiere_audio(bytes(audio_bytes), "audio.ogg")
            await update.message.reply_text(f"📝 {nutzer_text}")
        except Exception as e:
            log.error(f"Transkriptionsfehler: {e}")
            await update.message.reply_text(f"❌ Transkriptionsfehler: {e}")
            return
    elif update.message.text:
        nutzer_text = update.message.text
    else:
        await update.message.reply_text("❓ Bitte sende Text oder Sprachnachricht.")
        return

    if chat_id not in gespraech:
        gespraech[chat_id] = []
    gespraech[chat_id].append({"role": "user", "content": nutzer_text})

    await update.message.reply_text("⚙️ Pruefe Anfrage...")

    try:
        betrieb, leistungen, material = lese_sheets_daten()
        antwort = frage_claude(gespraech[chat_id], betrieb, leistungen, material)

        if antwort.get("status") == "rueckfrage":
            frage = antwort.get("frage", "Kannst du die Anfrage praezisieren?")
            gespraech[chat_id].append({"role": "assistant", "content": frage})
            await update.message.reply_text(f"❓ {frage}")
            return

        if antwort.get("status") == "angebot":
            zeile = speichere_angebot(antwort)
            jahr = datetime.now().strftime("%Y")
            ang_nr = f"ANG-{jahr}-{zeile}"
            heute = datetime.now().strftime("%d.%m.%Y")
            gueltig = (datetime.now() + timedelta(days=30)).strftime("%d.%m.%Y")
            gespraech[chat_id] = []

            await update.message.reply_text(
                f"✅ Angebot erstellt — Status: Entwurf\n\n"
                f"Betreff: {antwort.get('betreff','-')}\n\n"
                f"Nr.: {ang_nr}\n\n"
                f"Netto:  {antwort.get('angebotspreis_netto','-')} EUR\n"
                f"Brutto: {antwort.get('brutto','-')} EUR\n\n"
                f"Zum Freigeben antworte mit:\nfreigeben {ang_nr}"
            )
            await update.message.reply_text("📄 Erstelle PDF...")
            pdf_buf = erstelle_pdf(antwort, betrieb, ang_nr, heute, gueltig)
            await context.bot.send_document(
                chat_id=chat_id,
                document=pdf_buf,
                filename=f"{ang_nr}.pdf",
                caption=f"📄 Angebot {ang_nr}"
            )
            return

        await update.message.reply_text("❌ Unerwartete Antwort.")

    except json.JSONDecodeError as e:
        log.error(f"JSON-Fehler: {e}")
        gespraech[chat_id] = []
        await update.message.reply_text("❌ Antwort konnte nicht verarbeitet werden. Bitte neu starten.")
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
