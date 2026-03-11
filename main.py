"""
Maler-Agent — Telegram AI Angebots-Generator
Mit Onboarding, Menü, Stammdaten-Verwaltung
"""

import os
import re
import json
import logging
import requests
from datetime import datetime, timedelta
from io import BytesIO

import anthropic
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_RIGHT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GOOGLE_SHEET_ID    = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDENTIALS = os.environ["GOOGLE_CREDENTIALS"]

# ── Zustandsspeicher pro Chat ─────────────────────────────────────────────────
gespraech      = {}   # { chat_id: [verlauf] }
letztes_angebot = {}  # { chat_id: "ANG-2026-X" }
chat_modus     = {}   # { chat_id: "normal" | "onboarding" | "stammdaten_aendern" }
onboarding_data = {}  # { chat_id: { schritt: int, daten: {} } }

# Standardwerte für Express-Onboarding
EXPRESS_DEFAULTS = {
    "stundensatz":       "55",
    "mwst":              "19",
    "gewinnaufschlag":   "10",
    "mindestauftrag":    "150",
    "anfahrtspauschale": "25",
    "strasse":           "",
    "plz":               "",
    "ort":               "",
    "telefon":           "",
    "email":             "",
}

# Onboarding-Felder der Reihe nach
ONBOARDING_FELDER = [
    ("betriebsname",      "Wie lautet der Name deines Betriebs?"),
    ("stundensatz",       "Was ist dein Stundensatz in EUR (netto)? z.B. 55"),
    ("mwst",              "Welcher MwSt-Satz gilt? (normal: 19)"),
    ("gewinnaufschlag",   "Welchen Gewinnaufschlag möchtest du in %? z.B. 10"),
    ("mindestauftrag",    "Was ist dein Mindestauftragswert in EUR? z.B. 150"),
    ("anfahrtspauschale", "Wie hoch ist deine Anfahrtspauschale in EUR? z.B. 25"),
    ("strasse",           "Straße und Hausnummer deines Betriebs?"),
    ("plz",               "PLZ deines Betriebs?"),
    ("ort",               "Ort deines Betriebs?"),
    ("telefon",           "Telefonnummer deines Betriebs?"),
    ("email",             "E-Mail-Adresse deines Betriebs?"),
]

# ── Schriftart ────────────────────────────────────────────────────────────────
def registriere_schriften():
    for pfad, name in [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",      "DejaVu"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu-Bold"),
    ]:
        if os.path.exists(pfad):
            pdfmetrics.registerFont(TTFont(name, pfad))
    return os.path.exists("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")

UMLAUT_OK   = registriere_schriften()
FONT_NORMAL = "DejaVu"      if UMLAUT_OK else "Helvetica"
FONT_BOLD   = "DejaVu-Bold" if UMLAUT_OK else "Helvetica-Bold"

# ── Zahlenformatierung ────────────────────────────────────────────────────────
def eur(wert) -> str:
    try:
        return f"{float(wert):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (ValueError, TypeError):
        return str(wert)

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
        "betriebsname":      stamm[0]  if len(stamm) > 0  else "",
        "stundensatz":       stamm[1]  if len(stamm) > 1  else "",
        "mwst":              stamm[2]  if len(stamm) > 2  else "",
        "gewinnaufschlag":   stamm[3]  if len(stamm) > 3  else "",
        "mindestauftrag":    stamm[4]  if len(stamm) > 4  else "",
        "anfahrtspauschale": stamm[5]  if len(stamm) > 5  else "",
        "strasse":           stamm[6]  if len(stamm) > 6  else "",
        "plz":               stamm[7]  if len(stamm) > 7  else "",
        "ort":               stamm[8]  if len(stamm) > 8  else "",
        "telefon":           stamm[9]  if len(stamm) > 9  else "",
        "email":             stamm[10] if len(stamm) > 10 else "",
    }
    leistungen_rows = wb.worksheet("🔧 Leistungen").get_all_values()[1:]
    leistungen_text = "\n".join(["|".join(r) for r in leistungen_rows if any(r)])

    # Materialpreise: VK-Preis aus EK × (1 + Aufschlag) selbst berechnen
    # Spalten: ID|Bezeichnung|Kategorie|Einheit|EK-Preis|Aufschlag(%)|VK-Formel|Verbrauch
    material_zeilen = []
    for r in wb.worksheet("🎨 Materialpreise").get_all_values()[1:]:
        if not any(r):
            continue
        try:
            ek   = float(str(r[4]).replace(",", ".")) if len(r) > 4 and r[4] else 0
            aufschlag = float(str(r[5]).replace(",", ".").replace("%","")) if len(r) > 5 and r[5] else 0
            vk   = round(ek * (1 + aufschlag), 4)
            verbrauch = r[7] if len(r) > 7 else ""
            material_zeilen.append(f"{r[0]}|{r[1]}|{r[2]}|{r[3]}|{ek}|{aufschlag}|{vk}|{verbrauch}")
        except:
            material_zeilen.append("|".join(r))
    material_text = "\n".join(material_zeilen)

    # Schwierigkeitsfaktoren laden
    sf_rows = wb.worksheet("📊 Schwierigkeitsfaktoren").get_all_values()[1:]
    sf_text = "\n".join(["|".join(r) for r in sf_rows if any(r)])

    return betrieb, leistungen_text, material_text, sf_text

def ist_onboarding_noetig() -> bool:
    """Prüft ob Betriebsstamm noch leer ist."""
    try:
        betrieb, _, _, _ = lese_sheets_daten()
        return not betrieb.get("betriebsname", "").strip()
    except:
        return True

def schreibe_stammdaten(daten: dict):
    gc = get_sheet_client()
    wb = gc.open_by_key(GOOGLE_SHEET_ID)
    sheet = wb.worksheet("Betriebsstamm_Agent")
    felder = ["betriebsname","stundensatz","mwst","gewinnaufschlag",
              "mindestauftrag","anfahrtspauschale","strasse","plz","ort","telefon","email"]
    werte = [daten.get(f, "") for f in felder]
    sheet.update("A2:K2", [werte])

def speichere_angebot(angebot: dict, ang_nr: str) -> int:
    gc = get_sheet_client()
    wb = gc.open_by_key(GOOGLE_SHEET_ID)
    heute = datetime.now().strftime("%d.%m.%Y")
    gueltig = (datetime.now() + timedelta(days=30)).strftime("%d.%m.%Y")
    angebote_sheet = wb.worksheet("📄 Angebote")
    angebote_sheet.append_row([
        "", ang_nr, "", "", "Entwurf", heute, "", gueltig, "",
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

def freigabe_angebot(ang_nr: str) -> bool:
    gc = get_sheet_client()
    wb = gc.open_by_key(GOOGLE_SHEET_ID)
    sheet = wb.worksheet("📄 Angebote")
    zellen = sheet.findall(ang_nr)
    if not zellen:
        return False
    for zelle in zellen:
        sheet.update_cell(zelle.row, 5, "Freigegeben")
    return True

def speichere_feedback(chat_id: int, typ: str, nachricht: str):
    """Schreibt Feedback/Problem in den Sheet-Tab Feedback."""
    gc = get_sheet_client()
    wb = gc.open_by_key(GOOGLE_SHEET_ID)
    sheet = wb.worksheet("📬 Feedback")
    heute = datetime.now().strftime("%d.%m.%Y %H:%M")
    sheet.append_row([heute, str(chat_id), typ, nachricht, "offen"])

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
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end+1]
    log.info(f"CLAUDE_RAW: {raw[:300]}")
    return json.loads(raw)

# ── Claude API ────────────────────────────────────────────────────────────────
def frage_claude(verlauf: list, betrieb: dict, leistungen: str, material: str, sf: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    system = f"""Du bist ein Kalkulationsassistent für Malerbetriebe.

BETRIEBSSTAMM:
Betriebsname: {betrieb['betriebsname']}
Stundensatz (netto): {betrieb['stundensatz']} EUR
Gewinnaufschlag: {betrieb['gewinnaufschlag']} %
MwSt-Satz: {betrieb['mwst']} %
Anfahrtspauschale: {betrieb['anfahrtspauschale']} EUR
Mindestauftragswert: {betrieb['mindestauftrag']} EUR

LEISTUNGSKATALOG (Format: ID|Art|Kategorie|Einheit|Std_h|Material-ID|SF-Basis):
{leistungen}

MATERIALPREISE (Format: ID|Bezeichnung|Kategorie|Einheit|EK-Preis|Aufschlag|VK-Preis|Verbrauch_pro_m2):
Verwende immer den VK-Preis (Spalte 7) für die Kalkulation, NICHT den EK-Preis.
{material}

SCHWIERIGKEITSFAKTOREN (Format: ID|Bezeichnung|Multiplikator|Beschreibung):
Wähle den passenden Schwierigkeitsfaktor anhand der Auftragsbeschreibung.
Standard (SF01, Multiplikator 1.0) wenn keine besonderen Umstände erwähnt werden.
Der Faktor wird auf die Stunden einer Position angewendet.
{sf}

KALKULATIONSREGELN:
1. Stunden = Menge × Std_h_aus_Katalog × Schwierigkeitsfaktor
2. Arbeitskosten = Stunden × Stundensatz
3. Materialkosten = Menge × Verbrauch_pro_m2 × VK-Preis (aus Materialpreise-Sheet)
4. Positionspreis = Arbeitskosten + Materialkosten
5. Zwischensumme = Summe aller Positionspreise + Anfahrt
6. Gewinnaufschlag_Betrag = Zwischensumme × (Gewinnaufschlag% / 100)
7. Angebotspreis_netto = Zwischensumme + Gewinnaufschlag_Betrag
8. MwSt = Angebotspreis_netto × (MwSt% / 100)
9. Brutto = Angebotspreis_netto + MwSt

WICHTIG - GEDÄCHTNIS:
Lies den GESAMTEN Gesprächsverlauf. Extrahiere alle bereits genannten Informationen.
Frage NIEMALS nach etwas das bereits erwähnt wurde.

WICHTIG - NAMEN:
Wenn ein vollständiger Name genannt wird (z.B. "Kai Weber"), ist dieser vollständig.
Frage NIEMALS nach dem Vornamen wenn bereits ein vollständiger Name vorliegt.

PFLICHTFELDER:
GRUPPE A - Auftragsdaten: mind. eine Leistung mit Mengenangabe (m², Stück, lfd. m)
GRUPPE B - Kundendaten: vollständiger Name, Anrede (Herr/Frau), Straße + Hausnummer, PLZ, Ort

VORGEHEN:
1. Extrahiere alle bekannten Infos aus dem gesamten Verlauf
2. Prüfe welche Pflichtfelder noch fehlen
3. Fehlende Infos: Frage ALLE fehlenden Punkte einer Gruppe auf einmal
4. Alle Infos vorhanden: Erstelle Angebot

ANREDE: Leite "Herr" oder "Frau" aus dem Namen ab wenn möglich. Nur bei Unklarheit nachfragen.

EINLEITUNGSTEXT: NUR Haupttext, keine Anrede, kein Schluss.

ANTWORTFORMAT:
Falls Informationen fehlen:
{{"status": "rueckfrage", "frage": "..."}}

Falls alle Infos vorhanden:
{{"status": "angebot", "kunde_name": "...", "kunde_anrede": "Herr", "kunde_strasse": "...", "kunde_plz": "...", "kunde_ort": "...", "betreff": "...", "einleitungstext": "...", "gesamtstunden": 0.0, "arbeitskosten": 0.0, "materialkosten": 0.0, "anfahrt": 0.0, "zwischensumme_netto": 0.0, "gewinnaufschlag_betrag": 0.0, "angebotspreis_netto": 0.0, "mwst_betrag": 0.0, "brutto": 0.0, "positionen": [{{"pos_nr": 1, "leistungs_id": "L001", "beschreibung": "...", "einheit": "m²", "menge": 0.0, "gesamtstunden": 0.0, "materialkosten": 0.0, "arbeitskosten": 0.0, "positionspreis_netto": 0.0}}]}}

Antworte NUR mit dem JSON. Keine Backticks, kein Markdown."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
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
    normal = ParagraphStyle("n", fontSize=9,  leading=13, fontName=FONT_NORMAL)
    bold   = ParagraphStyle("b", fontSize=9,  leading=13, fontName=FONT_BOLD)
    small  = ParagraphStyle("s", fontSize=8,  leading=11, fontName=FONT_NORMAL, textColor=colors.grey)
    right  = ParagraphStyle("r", fontSize=9,  leading=13, fontName=FONT_NORMAL, alignment=TA_RIGHT)
    h1     = ParagraphStyle("h", fontSize=12, leading=16, fontName=FONT_BOLD,   spaceAfter=6)
    story  = []

    betrieb_info = (f"<b>{betrieb['betriebsname']}</b><br/>"
                    f"{betrieb.get('strasse','')}<br/>"
                    f"{betrieb.get('plz','')} {betrieb.get('ort','')}<br/>"
                    f"Tel: {betrieb.get('telefon','')}<br/>"
                    f"{betrieb.get('email','')}")
    angebots_info = (f"Angebotsnummer: {ang_nr}<br/>"
                     f"Datum: {heute}<br/>"
                     f"Gültig bis: {gueltig}")
    kopf = Table([[Paragraph(betrieb_info, bold), Paragraph(angebots_info, normal)]],
                 colWidths=[W*0.55, W*0.45])
    kopf.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("BOTTOMPADDING",(0,0),(-1,-1),4)]))
    story.append(kopf)
    story.append(Spacer(1, 0.8*cm))

    story.append(Paragraph(f"<b>{angebot.get('kunde_name','')}</b>", bold))
    story.append(Paragraph(angebot.get("kunde_strasse",""), normal))
    story.append(Paragraph(f"{angebot.get('kunde_plz','')} {angebot.get('kunde_ort','')}", normal))
    story.append(Spacer(1, 0.7*cm))

    story.append(Paragraph(f"<b>Angebot – {angebot.get('betreff','')}</b>", h1))
    story.append(Spacer(1, 0.3*cm))

    anrede  = angebot.get("kunde_anrede", "Herr/Frau")
    nachname = angebot.get("kunde_name","").split()[-1] if angebot.get("kunde_name") else ""
    story.append(Paragraph(f"Sehr geehrter {anrede} {nachname},", normal))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph(angebot.get("einleitungstext",""), normal))
    story.append(Spacer(1, 0.5*cm))

    story.append(Paragraph("<b>Leistungsübersicht</b>", bold))
    story.append(Spacer(1, 0.2*cm))
    pos_rows = [[
        Paragraph("<b>Pos.</b>", bold),
        Paragraph("<b>Leistungsbeschreibung</b>", bold),
        Paragraph("<b>Einheit</b>", bold),
        Paragraph("<b>Menge</b>", bold),
        Paragraph("<b>EP (EUR)</b>", bold),
        Paragraph("<b>GP (EUR)</b>", bold),
    ]]
    for pos in angebot.get("positionen", []):
        menge = float(pos.get("menge", 0) or 0)
        gp    = float(pos.get("positionspreis_netto", 0) or 0)
        ep    = round(gp / menge, 2) if menge else 0
        pos_rows.append([
            Paragraph(str(pos.get("pos_nr","")), normal),
            Paragraph(pos.get("beschreibung",""), normal),
            Paragraph(pos.get("einheit",""), normal),
            Paragraph(str(pos.get("menge","")), normal),
            Paragraph(eur(ep), right),
            Paragraph(eur(gp), right),
        ])
    pos_table = Table(pos_rows, colWidths=[1*cm, W*0.42, 1.8*cm, 1.6*cm, 2.1*cm, 2.1*cm])
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

    mwst_satz = betrieb.get("mwst","19")
    summen_data = [
        [Paragraph("Anfahrt:", normal), Paragraph(f"{eur(angebot.get('anfahrt',0))} EUR", right)],
        [Paragraph("Nettobetrag:", normal), Paragraph(f"{eur(angebot.get('angebotspreis_netto',0))} EUR", right)],
        [Paragraph(f"zzgl. {mwst_satz}% MwSt:", normal), Paragraph(f"{eur(angebot.get('mwst_betrag',0))} EUR", right)],
        [Paragraph("<b>Gesamtbetrag brutto:</b>", bold), Paragraph(f"<b>{eur(angebot.get('brutto',0))} EUR</b>", bold)],
    ]
    summen_table = Table(summen_data, colWidths=[W*0.7, W*0.3])
    summen_table.setStyle(TableStyle([
        ("ALIGN",(1,0),(1,-1),"RIGHT"),
        ("LINEABOVE",(0,-1),(-1,-1),1,colors.black),
        ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
    ]))
    story.append(summen_table)
    story.append(Spacer(1, 0.7*cm))

    story.append(Paragraph("<b>Konditionen & Hinweise</b>", bold))
    story.append(Spacer(1, 0.2*cm))
    k_table = Table([
        [Paragraph("Zahlungsziel:", bold), Paragraph("14 Tage nach Rechnungsstellung", normal)],
        [Paragraph("Gültigkeit:", bold), Paragraph(f"Dieses Angebot ist gültig bis zum {gueltig}.", normal)],
        [Paragraph("Gewährleistung:", bold), Paragraph("5 Jahre gemäß BGB §634a.", normal)],
    ], colWidths=[3.5*cm, W-3.5*cm])
    k_table.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),
                                  ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3)]))
    story.append(k_table)
    story.append(Spacer(1, 0.7*cm))
    story.append(Paragraph("Mit freundlichen Grüßen", normal))
    story.append(Spacer(1, 1.2*cm))
    story.append(Paragraph(f"<b>{betrieb['betriebsname']}</b>", bold))
    if betrieb.get("strasse"):
        story.append(Paragraph(f"{betrieb['strasse']}, {betrieb['plz']} {betrieb['ort']}", small))
    if betrieb.get("telefon"):
        story.append(Paragraph(f"Tel: {betrieb['telefon']} | {betrieb.get('email','')}", small))

    doc.build(story)
    buf.seek(0)
    return buf

# ── Menü ──────────────────────────────────────────────────────────────────────
MENU_TEXT = """📋 *Hauptmenü*

1️⃣ Angebot erstellen
2️⃣ Stammdaten ansehen
3️⃣ Stammdaten ändern
4️⃣ Hilfe / Problem melden

Antworte einfach mit der Nummer oder tippe dein Anliegen."""

async def zeige_menu(update: Update):
    await update.message.reply_text(MENU_TEXT, parse_mode="Markdown")

async def zeige_stammdaten(update: Update):
    try:
        betrieb, _, _, _ = lese_sheets_daten()
        text = (
            f"📋 *Deine Stammdaten*\n\n"
            f"🏢 Betrieb: {betrieb.get('betriebsname','-')}\n"
            f"💶 Stundensatz: {betrieb.get('stundensatz','-')} EUR\n"
            f"📊 MwSt: {betrieb.get('mwst','-')} %\n"
            f"📈 Gewinnaufschlag: {betrieb.get('gewinnaufschlag','-')} %\n"
            f"🚗 Anfahrt: {betrieb.get('anfahrtspauschale','-')} EUR\n"
            f"💰 Mindestauftrag: {betrieb.get('mindestauftrag','-')} EUR\n\n"
            f"📍 {betrieb.get('strasse','-')}, {betrieb.get('plz','-')} {betrieb.get('ort','-')}\n"
            f"📞 {betrieb.get('telefon','-')}\n"
            f"📧 {betrieb.get('email','-')}"
        )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler beim Laden der Stammdaten: {e}")

# ── Onboarding ────────────────────────────────────────────────────────────────
async def starte_onboarding(update: Update, chat_id: int):
    chat_modus[chat_id] = "onboarding_wahl"
    onboarding_data[chat_id] = {"schritt": 0, "daten": {}}
    await update.message.reply_text(
        "👋 Willkommen beim Maler-Agent!\n\n"
        "Wie möchtest du starten?\n\n"
        "⚡ *1 — Express-Start*\n"
        "Nur Betriebsname eingeben, Rest wird mit Standardwerten gefüllt:\n"
        "• Stundensatz: 55 €/Std\n"
        "• Gewinnaufschlag: 15 %\n"
        "• MwSt: 19 %\n"
        "• Anfahrt: 25 €\n"
        "• Mindestauftrag: 150 €\n"
        "_(Alle Werte kannst du später jederzeit ändern)_\n\n"
        "📋 *2 — Vollständiges Setup*\n"
        "Alle 11 Felder Schritt für Schritt eingeben (~2 Min)\n\n"
        "Antworte mit *1* oder *2*:",
        parse_mode="Markdown"
    )

async def verarbeite_onboarding(update: Update, chat_id: int, text: str):
    # Wahl: Express oder Vollständig
    if chat_modus.get(chat_id) == "onboarding_wahl":
        if text.strip() == "1":
            # Express: nur Betriebsname fragen
            chat_modus[chat_id] = "onboarding_express"
            await update.message.reply_text(
                "⚡ *Express-Start*\n\nWie lautet der Name deines Betriebs?",
                parse_mode="Markdown"
            )
        elif text.strip() == "2":
            # Vollständiges Setup
            chat_modus[chat_id] = "onboarding"
            await update.message.reply_text(
                f"📋 Vollständiges Setup\n\n"
                f"Frage 1 von {len(ONBOARDING_FELDER)}: {ONBOARDING_FELDER[0][1]}"
            )
        else:
            await update.message.reply_text("Bitte antworte mit *1* (Express) oder *2* (Vollständig).", parse_mode="Markdown")
        return

    # Express: nur Betriebsname
    if chat_modus.get(chat_id) == "onboarding_express":
        betriebsname = text.strip()
        daten = {"betriebsname": betriebsname, **EXPRESS_DEFAULTS}
        await update.message.reply_text("⏳ Speichere Stammdaten...")
        try:
            schreibe_stammdaten(daten)
            chat_modus[chat_id] = "normal"
            onboarding_data.pop(chat_id, None)
            await update.message.reply_text(
                f"🎉 *{betriebsname}* ist startklar!\n\n"
                f"Standardwerte sind gesetzt. Du kannst sofort Angebote erstellen.\n"
                f"Passe deine Daten jederzeit über *Menü → 3* an.",
                parse_mode="Markdown"
            )
            await zeige_menu(update)
        except Exception as e:
            await update.message.reply_text(f"❌ Fehler beim Speichern: {e}")
        return

    state = onboarding_data[chat_id]
    schritt = state["schritt"]
    feld = ONBOARDING_FELDER[schritt][0]
    state["daten"][feld] = text
    schritt += 1
    state["schritt"] = schritt

    if schritt < len(ONBOARDING_FELDER):
        naechste_frage = ONBOARDING_FELDER[schritt][1]
        await update.message.reply_text(
            f"✅ Gespeichert!\n\nFrage {schritt + 1} von {len(ONBOARDING_FELDER)}: {naechste_frage}"
        )
    else:
        # Alle Felder gesammelt → ins Sheet schreiben
        await update.message.reply_text("⏳ Speichere Stammdaten...")
        try:
            schreibe_stammdaten(state["daten"])
            chat_modus[chat_id] = "normal"
            onboarding_data.pop(chat_id, None)
            await update.message.reply_text(
                f"🎉 Einrichtung abgeschlossen!\n\n"
                f"Betrieb *{state['daten'].get('betriebsname','')}* ist jetzt bereit.\n\n"
                f"Du kannst jetzt Angebote erstellen — einfach losschreiben oder:",
                parse_mode="Markdown"
            )
            await zeige_menu(update)
        except Exception as e:
            await update.message.reply_text(f"❌ Fehler beim Speichern: {e}")

# ── Stammdaten ändern (Einzelfeld via Claude) ────────────────────────────────
STAMMDATEN_FELDER_MAP = {
    "betriebsname": "A2", "stundensatz": "B2", "mwst": "C2",
    "gewinnaufschlag": "D2", "mindestauftrag": "E2", "anfahrtspauschale": "F2",
    "strasse": "G2", "plz": "H2", "ort": "I2", "telefon": "J2", "email": "K2",
}

def erkenne_stammdaten_aenderung(text: str, betrieb: dict) -> list | None:
    """Nutzt Claude um zu erkennen welche Felder geändert werden sollen (auch mehrere)."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""Der Nutzer möchte Stammdaten ändern. Erkenne ALLE Felder die geändert werden sollen.

Aktuelle Stammdaten:
{json.dumps(betrieb, ensure_ascii=False)}

Verfügbare Felder: betriebsname, stundensatz, mwst, gewinnaufschlag, mindestauftrag, anfahrtspauschale, strasse, plz, ort, telefon, email

Nutzernachricht: "{text}"

Antworte NUR mit JSON-Array: [{{"feld": "feldname", "wert": "neuer_wert"}}, ...]
Falls keine eindeutige Änderung erkennbar: []"""
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip()
        # Array aus Antwort extrahieren
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end != -1:
            return json.loads(raw[start:end+1])
        return []
    except:
        return []

async def starte_stammdaten_aendern(update: Update, chat_id: int):
    chat_modus[chat_id] = "stammdaten_aendern"
    betrieb_text = ""
    try:
        betrieb, _, _, _ = lese_sheets_daten()
        betrieb_text = (
            f"• Betriebsname: {betrieb.get('betriebsname','-')}\n"
            f"• Stundensatz: {betrieb.get('stundensatz','-')} EUR\n"
            f"• MwSt: {betrieb.get('mwst','-')} %\n"
            f"• Gewinnaufschlag: {betrieb.get('gewinnaufschlag','-')} %\n"
            f"• Mindestauftrag: {betrieb.get('mindestauftrag','-')} EUR\n"
            f"• Anfahrt: {betrieb.get('anfahrtspauschale','-')} EUR\n"
            f"• Straße: {betrieb.get('strasse','-')}\n"
            f"• PLZ: {betrieb.get('plz','-')}\n"
            f"• Ort: {betrieb.get('ort','-')}\n"
            f"• Telefon: {betrieb.get('telefon','-')}\n"
            f"• E-Mail: {betrieb.get('email','-')}"
        )
    except:
        pass
    await update.message.reply_text(
        f"✏️ *Stammdaten ändern*\n\n"
        f"Aktuelle Werte:\n{betrieb_text}\n\n"
        f"Sag mir einfach was du ändern möchtest, z.B.:\n"
        f"_'Stundensatz auf 60€'_ oder _'E-Mail auf neu@firma.de'_",
        parse_mode="Markdown"
    )

async def verarbeite_stammdaten_aendern(update: Update, chat_id: int, text: str):
    await update.message.reply_text("⏳ Verarbeite Änderung...")
    try:
        betrieb, _, _, _ = lese_sheets_daten()
        ergebnis = erkenne_stammdaten_aenderung(text, betrieb)
        if not ergebnis:
            await update.message.reply_text(
                "❓ Ich konnte keine eindeutige Änderung erkennen.\n"
                "Bitte formuliere es so: _'Stundensatz auf 60'_",
                parse_mode="Markdown"
            )
            return
        if not ergebnis:
            await update.message.reply_text(
                "❓ Ich konnte keine eindeutige Änderung erkennen.\n"
                "Bitte formuliere es so: _'Stundensatz auf 60'_",
                parse_mode="Markdown"
            )
            return
        gc = get_sheet_client()
        sheet = gc.open_by_key(GOOGLE_SHEET_ID).worksheet("Betriebsstamm_Agent")
        bestaetigung = []
        for item in ergebnis:
            feld = item.get("feld")
            wert = item.get("wert")
            zelle = STAMMDATEN_FELDER_MAP.get(feld)
            if not zelle:
                continue
            # Zellkoordinaten aus Notation (z.B. "B2" → row=2, col=2)
            col = ord(zelle[0]) - ord("A") + 1
            row = int(zelle[1:])
            sheet.update_cell(row, col, wert)
            bestaetigung.append(f"• *{feld}* → {wert}")
        chat_modus[chat_id] = "normal"
        if bestaetigung:
            await update.message.reply_text(
                "✅ Folgende Änderungen wurden gespeichert:\n" + "\n".join(bestaetigung),
                parse_mode="Markdown"
            )
        await zeige_menu(update)
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler: {e}")

# ── Feedback-Flow ────────────────────────────────────────────────────────────
TYP_MAP = {"1": "Problem", "2": "Idee", "3": "Frage"}

async def verarbeite_feedback(update: Update, chat_id: int, text: str):
    modus = chat_modus.get(chat_id)

    if modus == "feedback_typ":
        typ = TYP_MAP.get(text.strip(), None)
        if not typ:
            await update.message.reply_text("Bitte antworte mit 1 (Problem), 2 (Idee) oder 3 (Frage).")
            return
        chat_modus[chat_id] = f"feedback_{typ}"
        await update.message.reply_text(
            f"Okay, *{typ}* — beschreibe es kurz in einer Nachricht:",
            parse_mode="Markdown"
        )
        return

    # Nachricht empfangen → speichern
    for prefix in ["feedback_Problem", "feedback_Idee", "feedback_Frage"]:
        if modus == prefix:
            typ = prefix.replace("feedback_", "")
            try:
                speichere_feedback(chat_id, typ, text)
                chat_modus[chat_id] = "normal"
                await update.message.reply_text(
                    f"✅ Danke! Dein {typ} wurde gespeichert und wird geprüft."
                )
                await zeige_menu(update)
            except Exception as e:
                await update.message.reply_text(f"❌ Fehler beim Speichern: {e}")
            return

# ── /start Handler ────────────────────────────────────────────────────────────
async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if ist_onboarding_noetig():
        await starte_onboarding(update, chat_id)
    else:
        await update.message.reply_text("👋 Willkommen zurück!")
        await zeige_menu(update)

# ── Hauptnachricht Handler ────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    nutzer_text = ""

    # Text oder Sprache
    if update.message.voice or update.message.audio:
        await update.message.reply_text("🎤 Transkribiere...")
        try:
            file_obj = update.message.voice or update.message.audio
            tg_file = await context.bot.get_file(file_obj.file_id)
            audio_bytes = await tg_file.download_as_bytearray()
            nutzer_text = transkribiere_audio(bytes(audio_bytes), "audio.ogg")
            await update.message.reply_text(f"📝 {nutzer_text}")
        except Exception as e:
            await update.message.reply_text(f"❌ Transkriptionsfehler: {e}")
            return
    elif update.message.text:
        nutzer_text = update.message.text
    else:
        await update.message.reply_text("❓ Bitte sende Text oder Sprachnachricht.")
        return

    # ── Onboarding läuft ──
    modus = chat_modus.get(chat_id, "normal")
    if modus in ("onboarding", "onboarding_wahl", "onboarding_express"):
        await verarbeite_onboarding(update, chat_id, nutzer_text)
        return
    if modus and modus.startswith("feedback"):
        await verarbeite_feedback(update, chat_id, nutzer_text)
        return
    if modus == "stammdaten_aendern":
        await verarbeite_stammdaten_aendern(update, chat_id, nutzer_text)
        return

    # ── Erster Start ohne /start ──
    if chat_id not in chat_modus and ist_onboarding_noetig():
        await starte_onboarding(update, chat_id)
        return

    # ── Menü-Trigger ──
    text_lower = nutzer_text.lower().strip()
    if text_lower in ["menü", "menu", "/menu", "hilfe", "help"] or text_lower in ["1","2","3","4"]:
        if text_lower in ["1", "angebot erstellen"]:
            await update.message.reply_text("💬 Beschreibe einfach das Angebot — Kunde, Leistung, Fläche.")
            return
        elif text_lower in ["2", "stammdaten ansehen"]:
            await zeige_stammdaten(update)
            return
        elif text_lower in ["3", "stammdaten ändern"]:
            await starte_stammdaten_aendern(update, chat_id)
            return
        elif text_lower in ["4", "hilfe", "help"]:
            chat_modus[chat_id] = "feedback_typ"
            await update.message.reply_text(
                "🆘 *Hilfe & Feedback*\n\n"
                "Was möchtest du melden?\n\n"
                "1️⃣ Problem / Fehler\n"
                "2️⃣ Idee / Verbesserungsvorschlag\n"
                "3️⃣ Frage\n\n"
                "Antworte mit 1, 2 oder 3:",
                parse_mode="Markdown"
            )
            return
        else:
            await zeige_menu(update)
            return

    # ── Freigabe ──
    _freigabe_match = re.search(r'freigeben\s+(ANG-\d{4}-\d+)', nutzer_text, re.IGNORECASE)
    _freigabe_wort  = re.search(r'freigeben|freigabe|passt so|bitte freigeben', nutzer_text, re.IGNORECASE)
    if _freigabe_match:
        ang_nr_f = _freigabe_match.group(1).upper()
    elif _freigabe_wort and chat_id in letztes_angebot:
        ang_nr_f = letztes_angebot[chat_id]
    else:
        ang_nr_f = None

    if ang_nr_f:
        await update.message.reply_text("⏳ Setze Status auf Freigegeben...")
        try:
            if freigabe_angebot(ang_nr_f):
                await update.message.reply_text(f"✅ Angebot {ang_nr_f} wurde freigegeben.")
            else:
                await update.message.reply_text(f"❌ Angebot {ang_nr_f} nicht gefunden.")
        except Exception as e:
            await update.message.reply_text(f"❌ Fehler: {e}")
        return

    # ── Angebot erstellen ──
    if chat_id not in gespraech:
        gespraech[chat_id] = []
    gespraech[chat_id].append({"role": "user", "content": nutzer_text})
    await update.message.reply_text("⚙️ Prüfe Anfrage...")

    try:
        betrieb, leistungen, material, sf = lese_sheets_daten()
        antwort = frage_claude(gespraech[chat_id], betrieb, leistungen, material, sf)

        if antwort.get("status") == "rueckfrage":
            frage = antwort.get("frage","Kannst du die Anfrage präzisieren?")
            gespraech[chat_id].append({"role": "assistant", "content": frage})
            await update.message.reply_text(f"❓ {frage}")
            return

        if antwort.get("status") == "angebot":
            jahr  = datetime.now().strftime("%Y")
            heute = datetime.now().strftime("%d.%m.%Y")
            gueltig = (datetime.now() + timedelta(days=30)).strftime("%d.%m.%Y")
            # Platzhalter speichern um Zeile zu bekommen, dann Nummer generieren
            zeile  = speichere_angebot(antwort, "")
            ang_nr = f"ANG-{jahr}-{zeile}"
            # Nummer nachschreiben
            gc = get_sheet_client()
            gc.open_by_key(GOOGLE_SHEET_ID).worksheet("📄 Angebote").update_cell(zeile, 2, ang_nr)
            gespraech[chat_id] = []
            letztes_angebot[chat_id] = ang_nr

            await update.message.reply_text(
                f"✅ Angebot erstellt — Status: Entwurf\n\n"
                f"Betreff: {antwort.get('betreff','-')}\n\n"
                f"Nr.: {ang_nr}\n\n"
                f"Netto:  {eur(antwort.get('angebotspreis_netto',0))} EUR\n"
                f"Brutto: {eur(antwort.get('brutto',0))} EUR\n\n"
                f"🔧 Kalkulation: {betrieb.get('stundensatz','-')} €/Std · "
                f"Aufschlag {betrieb.get('gewinnaufschlag','-')}% · "
                f"Anfahrt {betrieb.get('anfahrtspauschale','-')} €\n\n"
                f"Zum Freigeben antworte mit: freigeben\n\n"
                f"⚠️ Entwürfe werden nach 90 Tagen automatisch gelöscht. "
                f"Freigegebene Angebote werden 10 Jahre aufbewahrt."
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
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("menu", lambda u, c: zeige_menu(u)))
    app.add_handler(MessageHandler(filters.ALL, handle_message))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
