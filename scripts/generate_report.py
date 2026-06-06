"""
═══════════════════════════════════════════════════════════════════════════════
  generate_report.py — FSA Compliance Records nightly PDF generator
  Artisan by Robert (UK2820) · Built per Deployment Protocol v1
═══════════════════════════════════════════════════════════════════════════════

  CONTRACT (Rule 1)
  -----------------
  EXPECTS (env vars):
    Required:
      SUPABASE_URL          — your Supabase project URL
      SUPABASE_KEY          — Supabase API key
      DROPBOX_APP_KEY       — Dropbox app key
      DROPBOX_APP_SECRET    — Dropbox app secret
      DROPBOX_REFRESH_TOKEN — Dropbox refresh token (long-lived)
    Optional:
      GITHUB_TOKEN          — fine-grained PAT with write access to
                              artisanbyrobert/fsa-records repo.
                              When set, every run pushes status + log to the
                              repo's _status/ folder so Claude can fetch the
                              outcome on next session without you uploading
                              anything. Without it, the script still works
                              locally — you'd just need to upload files manually.

  DOES:
    1. Pulls intakes, deliveries (incl. daily/pest/production), app_config from Supabase
    2. Builds A4-landscape PDF with: intake records, daily records, deliveries,
       pest control checks, production runs
    3. Uploads PDF to Dropbox at:
       /FSA forms and records for emilys charcuterie/automated intake records/
       FSA_Records_<season_code>.pdf
       (e.g. FSA_Records_202526.pdf for Sept 2025 - Aug 2026 game season.
       One growing PDF per season, overwritten nightly — no 365-file proliferation.)
    4. ALWAYS at exit — even on crash — pushes run_status.txt + generate_report_log.txt
       to artisanbyrobert/fsa-records/_status/ on GitHub (if GITHUB_TOKEN set)

  SUCCESS LOOKS LIKE:
    - generate_report_log.txt written next to this script with all ok lines
    - run_status.txt written next to this script with single line: GREEN
    - PDF file >5KB exists locally and in Dropbox
    - _status/run_status.txt updated on GitHub showing GREEN + record counts

  ON FAILURE:
    - run_status.txt contains RED + plain-English reason
    - generate_report_log.txt has full traceback
    - GitHub _status/ also updated with the failure (so Claude sees it next session)
    - Script exits with code 1 (Task Scheduler will log)

  REMOTE STATUS CHECK URL (Claude fetches this on "check status"):
    https://raw.githubusercontent.com/artisanbyrobert/fsa-records/main/_status/run_status.txt
═══════════════════════════════════════════════════════════════════════════════
"""

import os
import sys
import json
import base64
import atexit
import traceback
import requests
from datetime import date, datetime

# ── DIAGNOSTIC LOGGING + STATUS REPORT (Rules 3 + 4) ──────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__)) if '__file__' in dir() else os.getcwd()
LOG_FILE = os.path.join(SCRIPT_DIR, 'generate_report_log.txt')
STATUS_FILE = os.path.join(SCRIPT_DIR, 'run_status.txt')

def _log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try: print(line)
    except: pass
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + "\n")
    except: pass

def _write_status(level, reason=""):
    """level: GREEN / AMBER / RED. reason: plain-English explanation."""
    try:
        with open(STATUS_FILE, 'w', encoding='utf-8') as f:
            f.write(f"{level}\n")
            f.write(f"Last run: {datetime.now().isoformat()}\n")
            if reason: f.write(f"Reason: {reason}\n")
    except: pass

# Reset log file at start of each run
try:
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write(f"=== Run started {datetime.now().isoformat()} ===\n")
        f.write(f"Python: {sys.version.splitlines()[0]}\n")
        f.write(f"Working dir: {os.getcwd()}\n")
        f.write(f"Script dir: {SCRIPT_DIR}\n\n")
except Exception as e:
    print(f"Warning: could not init log: {e}")

_write_status("AMBER", "Run in progress")
_log("Script starting")

# ── GITHUB STATUS PUSH (Rule 4 — full loop) ───────────────────────────────────
# Registered via atexit so it runs on normal exit, sys.exit(), and after exceptions.
# This is what lets Claude fetch the outcome on the next session with no input from Robert.
def _push_status_to_github():
    gh_token = os.environ.get('GITHUB_TOKEN')
    if not gh_token:
        print("  [status-push] GITHUB_TOKEN not set — skipping remote status push.")
        print("  [status-push] To enable: add GITHUB_TOKEN to Windows env vars with a")
        print("  [status-push] fine-grained PAT (write access to artisanbyrobert/fsa-records).")
        return

    owner = 'artisanbyrobert'
    repo = 'fsa-records'
    gh_headers = {
        'Authorization': f'Bearer {gh_token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }

    for local_path, remote_path in [
        (STATUS_FILE, '_status/run_status.txt'),
        (LOG_FILE,    '_status/generate_report_log.txt'),
    ]:
        try:
            with open(local_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            print(f"  [status-push] Could not read {local_path}: {e}")
            continue

        api_url = f'https://api.github.com/repos/{owner}/{repo}/contents/{remote_path}'
        sha = None
        try:
            getr = requests.get(api_url, headers=gh_headers, timeout=15)
            if getr.ok:
                sha = getr.json().get('sha')
        except Exception:
            pass  # file likely doesn't exist yet (first run) — that's fine, sha stays None

        body = {
            'message': f'Auto: status from nightly run {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            'content': base64.b64encode(content.encode('utf-8')).decode('ascii'),
            'branch': 'main'
        }
        if sha:
            body['sha'] = sha

        try:
            putr = requests.put(api_url, headers=gh_headers, json=body, timeout=30)
            if putr.ok:
                print(f"  [status-push] ok pushed {remote_path}")
            else:
                print(f"  [status-push] !! push of {remote_path} failed: HTTP {putr.status_code} {putr.text[:200]}")
        except Exception as e:
            print(f"  [status-push] !! push of {remote_path} failed: {e}")

atexit.register(_push_status_to_github)

# Capture any uncaught exception to log file + status (Rule 3)
def _excepthook(exc_type, exc_value, exc_tb):
    err_msg = f"{exc_type.__name__}: {exc_value}"
    _log(f"!!! UNCAUGHT ERROR: {err_msg}")
    tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write("\n--- Traceback ---\n")
            f.write(tb_str)
    except: pass
    _write_status("RED", err_msg)
    sys.__excepthook__(exc_type, exc_value, exc_tb)
sys.excepthook = _excepthook

# ── PRE-FLIGHT CHECKS (Rule 2) ────────────────────────────────────────────────
_log("Pre-flight: checking env vars...")
_required = ['SUPABASE_URL', 'SUPABASE_KEY', 'DROPBOX_APP_KEY', 'DROPBOX_APP_SECRET', 'DROPBOX_REFRESH_TOKEN']
_missing = [k for k in _required if k not in os.environ]
if _missing:
    msg = f"Missing env vars: {', '.join(_missing)}. Set them in Windows System Environment Variables."
    _log(f"!!! {msg}")
    _write_status("RED", msg)
    sys.exit(1)
for _k in _required:
    _log(f"  ok {_k} present ({len(os.environ[_k])} chars)")

SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_KEY']
DROPBOX_APP_KEY = os.environ['DROPBOX_APP_KEY']
DROPBOX_APP_SECRET = os.environ['DROPBOX_APP_SECRET']
DROPBOX_REFRESH_TOKEN = os.environ['DROPBOX_REFRESH_TOKEN']

today = date.today()
report_date = today.strftime('%d/%m/%Y')

# Seasonal filename — one growing PDF per game season instead of 365 dated files.
# Game season: starts September of year Y, ends August of Y+1. Code: YYYYYY.
def get_season_code(d):
    if d.month >= 9:
        open_year = d.year
    else:
        open_year = d.year - 1
    close_yy = str(open_year + 1)[-2:]
    return f"{open_year}{close_yy}"

season_code = get_season_code(today)
filename = f"FSA_Records_{season_code}.pdf"
_log(f"Season code: {season_code}  →  filename: {filename}")
_log(f"Target filename: {filename}")

def get_dropbox_token():
    _log("Refreshing Dropbox access token...")
    r = requests.post('https://api.dropbox.com/oauth2/token', data={
        'grant_type': 'refresh_token',
        'refresh_token': DROPBOX_REFRESH_TOKEN,
        'client_id': DROPBOX_APP_KEY,
        'client_secret': DROPBOX_APP_SECRET,
    }, timeout=30)
    _log(f"  Dropbox token HTTP {r.status_code}")
    if r.ok:
        _log("  ok token refreshed")
        return r.json()['access_token']
    raise Exception(f"Failed to get Dropbox token: {r.text}")

headers = {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}', 'Content-Type': 'application/json'}

def fetch(table):
    _log(f"  Fetching table: {table}")
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?select=*", headers=headers, timeout=30)
    _log(f"    HTTP {r.status_code} ({len(r.text)} bytes)")
    return r.json() if r.ok else []

_log("Fetching from Supabase...")
intakes_raw = fetch('intakes')
deliveries_raw = fetch('deliveries')
config_raw = fetch('app_config')

intakes = [r['data'] for r in intakes_raw if r.get('data')]
daily_records = [r['data'] for r in deliveries_raw if r.get('data') and r['data'].get('_type') == 'daily']
deliveries = [r['data'] for r in deliveries_raw if r.get('data') and not r['data'].get('_type')]
pest_records = [r['data'] for r in deliveries_raw if r.get('data') and r['data'].get('_type') == 'pest']
production_records = [r['data'] for r in deliveries_raw if r.get('data') and r['data'].get('_type') == 'production']
daily_checks = [r['data'] for r in deliveries_raw if r.get('data') and r['data'].get('_type') == 'dailychecks']
venison_runs = [r['data'] for r in deliveries_raw if r.get('data') and r['data'].get('_type') == 'venison']
_log(f"  Records: intakes={len(intakes)}, daily={len(daily_records)}, deliveries={len(deliveries)}, pest={len(pest_records)}, prod={len(production_records)}, checks={len(daily_checks)}, venison={len(venison_runs)}")

estates = {}
for row in config_raw:
    if row.get('key') == 'estates':
        d = row.get('data')
        # Handle if data is a JSON string
        if isinstance(d, str):
            try: d = json.loads(d)
            except: d = []
        if isinstance(d, list):
            for e in d:
                if isinstance(e, dict):
                    eid = e.get('id','')
                    ename = e.get('name','')
                    if eid and ename:
                        estates[eid] = ename
print(f"Estates loaded: {len(estates)} — {estates}")

general_tasks = []
for row in config_raw:
    if row.get('key') == 'generalTasks':
        d = row.get('data')
        if isinstance(d, str):
            try: d = json.loads(d)
            except: d = []
        if isinstance(d, list):
            general_tasks = d

# Estate -> published ALIAS. Real estate names are confidential and must NEVER
# appear in the FSA audit PDF. The alias is what gets published; the real estate
# name lives only in the private commercial records.
ESTATE_ALIASES = {
    'coombe manor':'Jem', 'audley end':'Aimee',
    'cold aston':'Gary', 'cold aston - gary':'Gary',
    'belvoir':'Caroline', 'belvoir castle':'Caroline',
    'lees court':'Elizabeth',
}
def to_alias(name):
    if not name: return name
    key = str(name).strip().lower()
    for real, alias in ESTATE_ALIASES.items():
        if real in key: return alias
    return name  # no alias on file yet -> real name still shows (flagged to Robert)

def get_estate(rec):
    # A record may already carry a published alias
    if rec.get('alias'): return rec['alias']
    # Try direct estate name first
    name = rec.get('estate','') or rec.get('estateName','')
    if name and len(name) < 30 and not name.startswith('ey'): return to_alias(name)
    # Try lookup by ID
    eid = rec.get('estateId','') or rec.get('estate','')
    looked_up = estates.get(eid, '')
    if looked_up: return to_alias(looked_up)
    # Return whatever we have
    return eid or '—'

def clean(text):
    if not text: return ''
    # Replace common special chars that break PDF rendering
    text = str(text)
    text = text.replace('•', '-').replace('’', "'").replace('‘', "'")
    text = text.replace('“', '"').replace('”', '"')
    text = text.replace('–', '-').replace('—', '-')
    text = text.replace('·', '-').replace('‣', '-')
    # Remove any other non-ASCII characters
    text = text.encode('ascii', 'ignore').decode('ascii')
    return text.strip()

def prettify_name(raw):
    # Turn a raw id like 'build_room' / 'plucking-room' into 'Build Room'.
    if not raw: return ''
    s = str(raw).replace('_', ' ').replace('-', ' ').strip()
    return ' '.join(w.capitalize() for w in s.split())

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, PageBreak, KeepTogether
from reportlab.graphics.shapes import Drawing, Rect, Circle, Line, String
from reportlab.graphics import renderPDF
from reportlab.pdfgen import canvas as _canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import os as _os
_FONTDIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'fonts')
def _regfont(name, fn):
    try:
        pdfmetrics.registerFont(TTFont(name, _os.path.join(_FONTDIR, fn))); return True
    except Exception as _e:
        _log(f"font {name} load failed ({_e}) — falling back to Helvetica"); return False
_HAS_EBG  = _regfont('EBG',  'EBGaramond-Regular.ttf')
_regfont('EBGsb','EBGaramond-SemiBold.ttf')
_regfont('Cor',  'Cormorant-SemiBold.ttf')
# graceful fallback so a missing font never breaks the nightly run
SERIF   = 'EBG'   if _HAS_EBG else 'Helvetica'
SERIFB  = 'EBGsb' if _HAS_EBG else 'Helvetica-Bold'
DISPLAY = 'Cor'   if 'Cor' in pdfmetrics.getRegisteredFontNames() else SERIFB

doc = SimpleDocTemplate(filename, pagesize=landscape(A4), rightMargin=15*mm, leftMargin=15*mm, topMargin=24*mm, bottomMargin=16*mm)
# ── Luxury palette (estate house style — no solid colour bars anywhere) ──────
IVORY   = colors.HexColor('#FBF8F1')
GREEN   = colors.HexColor('#18342A')   # deep racing green (display + accents)
GOLD    = colors.HexColor('#C9A86A')   # antique gold (rules)
GOLDLBL = colors.HexColor('#8A6D2F')   # gold label text
INK     = colors.HexColor('#2C2A26')   # body ink
MUTE    = colors.HexColor('#7A736A')   # muted captions
HAIR    = colors.HexColor('#E3D9C4')   # hairline rule
ROWB    = colors.HexColor('#F6F1E6')   # alt row tint on ivory
LIGHT_GREY = ROWB
# soft per-section header tints (fill, dark text) — pale, never saturated
SAGE  = (colors.HexColor('#E7EDDF'), GREEN)
SAND  = (colors.HexColor('#F1E8D6'), GOLDLBL)
SLATE = (colors.HexColor('#E5ECF1'), colors.HexColor('#3C5A73'))
TEAL  = (colors.HexColor('#E3EDE8'), colors.HexColor('#1F6E56'))
ROSE  = (colors.HexColor('#F1E6E8'), colors.HexColor('#7A3B4C'))
LIGHT_GREEN = SAGE[0]
AMBER = GOLDLBL
h1 = ParagraphStyle('h1', fontName=DISPLAY, fontSize=30, textColor=GREEN, leading=33, spaceAfter=4, alignment=1)
h2 = ParagraphStyle('h2', fontName=DISPLAY, fontSize=19, textColor=GREEN, leading=21, spaceAfter=2, spaceBefore=4, keepWithNext=1)
small = ParagraphStyle('small', fontName=SERIF, fontSize=9, textColor=MUTE)
desc_style = ParagraphStyle('desc', fontName=SERIF, fontSize=9.5, textColor=INK, spaceAfter=8, leading=13)

def hdr_cells(labels, tint):
    # pale tinted header cells with dark text — returned as Paragraphs
    fill, txt = tint
    st = ParagraphStyle('hc', fontName=SERIFB, fontSize=8, leading=10, textColor=txt)
    return [Paragraph(clean(str(l)), st) for l in labels]

def lux_table_style(tint, nrows, total_row=None):
    # hairline grid, gold rule top+below header, soft alt rows, serif body, NO solid bar
    fill, txt = tint
    cmds = [
        ('BACKGROUND', (0,0), (-1,0), fill),
        ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8.5),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, ROWB]),
        ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD),
        ('LINEBELOW', (0,1), (-1,-1), 0.35, HAIR),
        ('LEFTPADDING', (0,0), (-1,-1), 6), ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 5), ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('VALIGN', (0,0), (-1,-1), 'TOP')]
    if total_row is not None:
        cmds.append(('BACKGROUND', (0,total_row), (-1,total_row), SAND[0]))
        cmds.append(('LINEABOVE', (0,total_row), (-1,total_row), 0.6, GOLD))
    return TableStyle(cmds)

story = []
_first_section = [True]

def add_section(title, description=None, new_page=True):
    # Each major section starts on its own page and grows downward over the season.
    # An optional description explains what the section records.
    if new_page and not _first_section[0]:
        story.append(PageBreak())
    _first_section[0] = False
    story.append(Paragraph(title, h2))
    hr = HRFlowable(width='100%', thickness=1, color=GOLD, spaceAfter=6)
    hr.keepWithNext = 1
    story.append(hr)
    if description:
        story.append(Paragraph(description, desc_style))
# Season window — boundary matches the app's batch-coding rule (rolls over 1 March),
# so consecutive seasons abut with no overlap. e.g. 202526 -> 1 Mar 2025 to 28 Feb 2026.
_MONTHS = ['January','February','March','April','May','June','July','August','September','October','November','December']
def _season_window(code):
    try:
        oy = int(str(code)[:4])
    except Exception:
        return ('', '')
    import calendar as _cal
    end_last = _cal.monthrange(oy+1, 2)[1]
    return (f'1 March {oy}', f'{end_last} February {oy+1}')
_sw_start, _sw_end = _season_window(season_code)
_season_label = season_code[:4] + ' / ' + season_code[4:] if season_code and len(season_code)==6 else str(season_code)

_sub   = ParagraphStyle('sub',   fontName=SERIF,  fontSize=13, textColor=GOLDLBL, alignment=1, leading=16, spaceBefore=2)
_meta  = ParagraphStyle('meta',  fontName=SERIF,  fontSize=10, textColor=MUTE,    alignment=1, leading=14)
story.append(Spacer(1, 38*mm))
story.append(Paragraph('Artisan by Robert', h1))
story.append(Spacer(1, 2*mm))
story.append(HRFlowable(width='42%', thickness=1, color=GOLD, spaceAfter=6, spaceBefore=2, hAlign='CENTER'))
story.append(Paragraph('Food Safety &amp; Compliance Records', _sub))
story.append(Paragraph('Season ' + _season_label, _sub))
if _sw_start:
    story.append(Paragraph(_sw_start + ' &nbsp;\u2013&nbsp; ' + _sw_end, _meta))
story.append(Spacer(1, 8*mm))
story.append(Paragraph('FSA Licence UK2820 &nbsp;\u00b7&nbsp; Hook, Hampshire RG29 1HT &nbsp;\u00b7&nbsp; Generated ' + report_date, _meta))
story.append(Spacer(1, 14*mm))
summary_rows = [hdr_cells(['Record','Held'], SAND)]
for _lbl,_n in [('Intake records',len(intakes)),('Daily records',len(daily_records)),('Production runs',len(production_records)),('Pest control checks',len(pest_records)),('Finished product / deliveries',len(deliveries))]:
    summary_rows.append([_lbl, str(_n)])
summary_table = Table(summary_rows, colWidths=[150*mm, 50*mm], hAlign='CENTER')
summary_table.setStyle(lux_table_style(SAND, len(summary_rows)))
story.append(summary_table)
story.append(PageBreak())

add_section('Intake Records',
    'All raw meat brought in, by batch. Each batch carries its season code, intake date, source estate, species and weights. This is the start of the traceability chain — every finished product traces back to a batch here.',
    new_page=False)
intake_cell = ParagraphStyle('icell', fontName=SERIF, fontSize=8, leading=10.5)
intake_hdr = ParagraphStyle('ihdr', fontSize=7.5, textColor=GREEN, fontName=SERIFB)
if intakes:
    rows = [[Paragraph('Batch Code', intake_hdr), Paragraph('Date', intake_hdr), Paragraph('Estate', intake_hdr), Paragraph('Species', intake_hdr), Paragraph('Items', intake_hdr)]]
    for rec in sorted(intakes, key=lambda x: x.get('date',''), reverse=True):
        items_list = rec.get('items', [])
        items_str = '<br/>'.join([clean(f"{i.get('qty','')} {i.get('unit','')} {i.get('species','')}").strip() for i in items_list])
        species = ''
        if items_list:
            first = items_list[0]
            species = first.get('custom','') if first.get('species','') in ('Other','') else first.get('species','')
        rows.append([
            Paragraph(clean(rec.get('batchCode','')), intake_cell),
            Paragraph(rec.get('date',''), intake_cell),
            Paragraph(clean(get_estate(rec)), intake_cell),
            Paragraph(clean(species), intake_cell),
            Paragraph(items_str, intake_cell)
        ])
    t = Table(rows, colWidths=[30*mm, 20*mm, 40*mm, 22*mm, 115*mm], repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(t)
else:
    story.append(Paragraph('No intake records found.', small))

add_section('Daily Records',
    'Day-by-day log, newest first. A <b>Monitor / walkabout</b> day checks: dehumidifier emptied; insectocutors working; no pest ingress; temperature against the wall thermometer; cloud temperature monitoring running; and that salami and cuts are drying well. A <b>Work day</b> (mince, stuffing, delivery or intake) instead has full opening and closing hygiene checks recorded \u2014 those days appear here marked \u201cWork day\u201d and the detail is in the Opening Checks, Closing Checks and Production Records sections.')
cell_style = ParagraphStyle('cell', fontName=SERIF, fontSize=8, leading=10.5)
header_style = ParagraphStyle('hdr', fontSize=7.5, textColor=GREEN, fontName=SERIFB)
# dates that had opening/closing checks recorded (standalone daily checks + mince days) = work days
_workday_dates = set()
for _c in daily_checks:
    if _c.get('date'): _workday_dates.add(_c.get('date'))
for _r in production_records:
    for _st in (_r.get('stages', []) or []):
        if _st.get('type') == 'mince' and _st.get('date'): _workday_dates.add(_st.get('date'))
_daily_dates = set(r.get('date','') for r in daily_records)
# build a combined, de-duplicated day list: real daily records + synthetic work-day rows
_day_rows = []
for rec in daily_records:
    dt = rec.get('date','')
    open_tasks = [t['text'] for t in rec.get('todoList',[]) if not t.get('done')]
    tasks_content = '<br/>'.join(['- ' + clean(t) for t in open_tasks]) if open_tasks else 'None'
    notes_content = clean(rec.get('notes','') or rec.get('monitorNotes','') or '') or '-'
    day_type = rec.get('dayTypeId','').replace('-',' ').title() or 'Monitor'
    if dt in _workday_dates:
        notes_content = (notes_content + ' ' if notes_content != '-' else '') + '<i>Work day \u2014 see Opening / Closing Checks &amp; Production Records.</i>'
        day_type = day_type + ' (work day)'
    _day_rows.append((dt, day_type, notes_content, tasks_content))
for dt in sorted(_workday_dates - _daily_dates, reverse=True):
    _day_rows.append((dt, 'Work day', '<i>Opening &amp; closing checks recorded \u2014 see Opening / Closing Checks &amp; Production Records.</i>', 'None'))
_day_rows.sort(key=lambda x: x[0], reverse=True)
if _day_rows:
    rows = [[Paragraph('Date', header_style), Paragraph('Day Type', header_style), Paragraph('Notes', header_style), Paragraph('Outstanding Tasks', header_style)]]
    for dt, day_type, notes_content, tasks_content in _day_rows:
        rows.append([Paragraph(dt, cell_style), Paragraph(clean(day_type), cell_style), Paragraph(notes_content, cell_style), Paragraph(tasks_content, cell_style)])
    t = Table(rows, colWidths=[20*mm, 42*mm, 91*mm, 74*mm], repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(t)
else:
    story.append(Paragraph('No daily records found.', small))

# General quick-capture tasks (not tied to a record)
_open_general = [t for t in general_tasks if not t.get('done') and t.get('kind') != 'app']
_done_general = [t for t in general_tasks if t.get('done') and t.get('kind') != 'app']
if _open_general or _done_general:
    add_section('General Tasks',
        'Quick-capture jobs not tied to a specific day or batch (e.g. supplies to order). Open tasks are outstanding; done tasks show the date completed. App-development notes are excluded from this record.')
    grows = [['Task', 'Added', 'Status']]
    for t in _open_general:
        grows.append([clean(t.get('text','')), clean(str(t.get('addedDate',''))), 'Open'])
    for t in _done_general:
        grows.append([clean(t.get('text','')), clean(str(t.get('addedDate',''))), 'Done ' + clean(str(t.get('doneDate','')))])
    gt = Table(grows, colWidths=[130*mm, 45*mm, 49*mm], repeatRows=1)
    gt.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('TEXTCOLOR', (0,0), (-1,0), GREEN), ('FONTNAME', (0,0), (-1,0), SERIFB), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(gt)

# ── MINCE-DAY HYGIENE MATRICES ────────────────────────────────────────────────
# Gather every mince day across all production runs and present opening / closing
# checks as date-row matrices (one column per fixed check). Designed to grow to
# 365 rows over a season; the column header repeats on each new page.
def _gather_mince_days():
    days = []
    for rec in production_records:
        batch = rec.get('batchCode', '')
        for st in (rec.get('stages', []) or []):
            if st.get('type') == 'mince':
                days.append((st.get('date', ''), batch, st))
    days.sort(key=lambda x: x[0], reverse=True)
    return days

def _check_matrix(days, key, fixed_labels, section_title, section_desc):
    add_section(section_title, section_desc)
    if not days:
        story.append(Paragraph('No mince days recorded yet.', small))
        return
    # Use a stable column order from the fixed label list; shorten labels for headers
    hdr_style = ParagraphStyle('mxh', fontSize=6, textColor=GREEN, fontName=SERIFB, leading=7)
    cell_style2 = ParagraphStyle('mxc', fontSize=7, leading=8)
    header = [Paragraph('Date', hdr_style), Paragraph('Batch', hdr_style)] + [Paragraph(clean(lbl), hdr_style) for lbl in fixed_labels]
    rows = [header]
    for dt, batch, st in days:
        items = {i.get('text',''): i.get('done') for i in (st.get(key, []) or [])}
        row = [Paragraph(clean(dt), cell_style2), Paragraph(clean(batch), cell_style2)]
        for lbl in fixed_labels:
            done = items.get(lbl)
            mark = '✓' if done else ('✗' if done is False else '–')
            row.append(Paragraph(mark, ParagraphStyle('mk', fontSize=8, alignment=1, textColor=(GREEN if done else (colors.HexColor('#a32d2d') if done is False else colors.grey)))))
        rows.append(row)
    n = len(fixed_labels)
    date_w, batch_w = 20*mm, 26*mm
    avail = 267*mm - date_w - batch_w
    col_w = [date_w, batch_w] + [avail / n] * n
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('FONTSIZE', (0,0), (-1,-1), 6),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
        ('GRID', (0,0), (-1,-1), 0.35, HAIR),
        ('LEFTPADDING', (0,0), (-1,-1), 2), ('RIGHTPADDING', (0,0), (-1,-1), 2),
        ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE')]))
    story.append(t)

_mince_days = _gather_mince_days()
# Standalone daily-check records (decoupled from mince day) join the same matrices.
_standalone_checks = [(c.get('date', ''), '\u2014', c) for c in daily_checks]
_check_days = _mince_days + _standalone_checks
_check_days.sort(key=lambda x: x[0], reverse=True)
# Derive the fixed label sets from the data (fall back to first day's labels)
_open_labels = []
_close_labels = []
for _, _, _st in _check_days:
    for i in (_st.get('opening', []) or []):
        if i.get('text') and i['text'] not in _open_labels: _open_labels.append(i['text'])
    for i in (_st.get('closing', []) or []):
        if i.get('text') and i['text'] not in _close_labels: _close_labels.append(i['text'])

_check_matrix(_check_days, 'opening', _open_labels,
    'Opening Checks',
    'Start-of-day hygiene and equipment checks for every work day (mince days and standalone daily checks). Each row is one day; a tick confirms the step was done, a cross means it was skipped that day. New page continues with the same column headers.')
_check_matrix(_check_days, 'closing', _close_labels,
    'Closing Checks',
    'End-of-day clean-down and shutdown checks for every work day (3-stage clean, UV cabinet, heaters off, etc.). Each row is one day; tick = done, cross = skipped.')

# ── PRODUCTION SECTION ────────────────────────────────────────────────────────
_log(f"Building Production section ({len(production_records)} records)")
add_section('Production Records',
    'Each production run from a batch: the children (divisions) it was split into, each child\'s recipe with ingredient amounts and the date each was added, and the day-by-day stages worked (defrost, fat calc, recipe, mince, stuffing). This is the full make-record for traceability.')

if production_records:
    _prod_first = True
    for rec in sorted(production_records, key=lambda x: x.get('startDate', x.get('processCode','')), reverse=True):
        if not _prod_first:
            story.append(PageBreak())
        _prod_first = False
        proc = rec.get('processCode','—')
        # If the process code is a YYYYMMDD date, show it as DD/MM/YYYY (nicer on the audit doc)
        proc_str = str(proc)
        if proc_str.isdigit() and len(proc_str) == 8:
            proc = f"{proc_str[6:8]}/{proc_str[4:6]}/{proc_str[0:4]}"
        batch = rec.get('batchCode','—')
        species = rec.get('speciesName','') or rec.get('species','')
        status = rec.get('status','in_progress')
        fat_pct = rec.get('fatPercent','') or rec.get('fatPct','')
        alias = rec.get('alias','')
        header = f"<b>{clean(species)}" + (f" · {clean(alias)}" if alias else "") + f" · Batch {clean(batch)} · Process {proc} · {status}"
        if fat_pct: header += f" · fat {fat_pct}%"
        header += "</b>"
        story.append(Paragraph(header, ParagraphStyle('prh', fontSize=10, fontName=SERIFB, textColor=GREEN, spaceAfter=3, spaceBefore=8, keepWithNext=1)))
        # Children (divisions) for this run
        children = rec.get('children', []) or []
        if children:
            crows = [['Child', 'Meat', 'Fat', 'Total', 'Recipe']]
            for c in children:
                rcp = c.get('recipe') or {}
                rcp_name = clean(rcp.get('name', '')) if isinstance(rcp, dict) else ''
                crows.append([
                    'Child ' + clean(str(c.get('code',''))),
                    f"{c.get('meatKg','')}kg",
                    f"{c.get('fatKg','')}kg",
                    f"{c.get('totalKg','')}kg",
                    rcp_name or '-'
                ])
            ct = Table(crows, colWidths=[45*mm, 25*mm, 25*mm, 25*mm, 104*mm], repeatRows=1)
            ct.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), SAND[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('TEXTCOLOR', (0,0), (-1,0), GREEN), ('FONTNAME', (0,0), (-1,0), SERIFB), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
            story.append(ct)
            story.append(Spacer(1, 2*mm))
            # Per-child recipe ingredient breakdown (full traceability)
            for c in children:
                rcp = c.get('recipe') or {}
                lines = rcp.get('lines', []) if isinstance(rcp, dict) else []
                if lines:
                    story.append(Paragraph('<b>Child ' + clean(str(c.get('code',''))) + ' — ' + clean(rcp.get('name','')) + '</b>', ParagraphStyle('rch', fontSize=8, fontName=SERIFB, textColor=GOLDLBL, spaceAfter=2, spaceBefore=4, keepWithNext=1)))
                    irows = [['Ingredient', 'Amount', 'Added']]
                    for ln in lines:
                        amt = ln.get('amount')
                        unit = ln.get('unit','') or 'g'
                        if ln.get('type') == 'asneeded' or amt is None or amt == '':
                            amt_str = 'as needed'
                        else:
                            amt_str = f"{amt} {unit}".strip()
                        added = clean(str(ln.get('addedDate',''))) or '-'
                        irows.append([clean(ln.get('name','')), amt_str, added])
                    it = Table(irows, colWidths=[110*mm, 64*mm, 50*mm], repeatRows=1)
                    it.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('TEXTCOLOR', (0,0), (-1,0), GREEN), ('FONTNAME', (0,0), (-1,0), SERIFB), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8), ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 2), ('BOTTOMPADDING', (0,0), (-1,-1), 2)]))
                    story.append(it)
                    story.append(Spacer(1, 2*mm))
        stages = rec.get('stages',[]) or []
        if stages:
            if children:
                story.append(PageBreak())
            story.append(Paragraph('Days worked on this run', ParagraphStyle('dwh', fontName=SERIFB, fontSize=9, textColor=GOLDLBL, spaceAfter=3, spaceBefore=2, keepWithNext=1)))
            srows = [['Date', 'Stage', 'Details', 'Notes']]
            for st_rec in stages:
                stage = st_rec.get('type','')
                dstr = st_rec.get('date','')
                detail = ''
                if stage == 'defrost':
                    mk = st_rec.get('meatKg','')
                    detail = f"meat {mk}kg" if mk else 'fat-only top-up day'
                elif stage == 'fatcalc':
                    mk = st_rec.get('meatKg','')
                    fk = st_rec.get('fatKg','')
                    tk = st_rec.get('totalKg','')
                    fp = st_rec.get('fatPercent','')
                    detail = f"meat {mk}kg + fat {fk}kg = {tk}kg total ({fp}% fat)" if tk else ''
                elif stage == 'saltcalc':
                    sg = st_rec.get('saltGrams','')
                    sp = st_rec.get('saltPercent','')
                    tk = st_rec.get('totalKg','')
                    detail = f"{sg}g salt ({sp}% of {tk}kg mix)" if sg else ''
                elif stage == 'mince':
                    cw = st_rec.get('childWork', {}) or {}
                    if cw:
                        parts = []
                        for letter in sorted(cw.keys()):
                            w = cw[letter]
                            steps = []
                            if w.get('minced'): steps.append('minced')
                            if w.get('salted'): steps.append(f"salt {w.get('saltGrams','')}g")
                            if w.get('mixed'): steps.append('mixed')
                            parts.append(f"{letter}: {', '.join(steps) if steps else 'pending'}")
                        # flag any skipped hygiene checks for the audit trail
                        skipped = [o.get('text','') for o in (st_rec.get('opening',[]) or []) if not o.get('done')]
                        skipped += [c.get('text','') for c in (st_rec.get('closing',[]) or []) if not c.get('done')]
                        detail = ' · '.join(parts)
                        if not st_rec.get('prepDone'): detail = 'PREP NOT TICKED · ' + detail
                        if skipped: detail += ' · SKIPPED: ' + '; '.join(skipped)
                    else:
                        # legacy mince rows
                        mk = st_rec.get('meatKg','')
                        fk = st_rec.get('fatKg','')
                        tk = st_rec.get('totalKg','')
                        if fk and tk:
                            detail = f"meat {mk}kg + fat {fk}kg = {tk}kg total"
                        else:
                            detail = f"meat {mk}kg minced" if mk else 'minced'
                elif stage == 'stuff_hang':
                    n = st_rec.get('count','')
                    ug = st_rec.get('unitGrams','')
                    bits = []
                    cl = st_rec.get('childLetter')
                    if cl:
                        ch = next((c for c in (rec.get('children',[]) or []) if c.get('letter') == cl), None)
                        nm = (ch.get('recipe') or {}).get('name') if ch else None
                        bits.append(nm if nm else f"Child {cl}")
                    if n:
                        bits.append(f"{n} x {ug}g" if ug else f"{n} salami")
                    if st_rec.get('skinSize'):
                        bits.append(f"{st_rec.get('skinSize')} skins")
                    if st_rec.get('finishDate'):
                        bits.append(f"est. ready {st_rec.get('finishDate')}")
                    detail = ' · '.join(bits)
                stage_label = {'fatcalc':'Fat Calculator','saltcalc':'Salt Calculator','stuff_hang':'Stuffing & Hanging','mince':'Mince Day','defrost':'Defrost'}.get(stage, stage.replace('_',' ').title())
                detail_cell = Paragraph(clean(detail), ParagraphStyle('sdc', fontSize=7, leading=9)) if detail else ''
                notes_cell = Paragraph(clean(st_rec.get('notes','')), ParagraphStyle('snc', fontSize=7, leading=9)) if st_rec.get('notes') else ''
                srows.append([dstr, stage_label, detail_cell, notes_cell])
            pt = Table(srows, colWidths=[22*mm, 32*mm, 60*mm, 110*mm], repeatRows=1)
            pt.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('TEXTCOLOR', (0,0), (-1,0), GREEN), ('FONTNAME', (0,0), (-1,0), SERIFB), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
            story.append(pt)
else:
    story.append(Paragraph('No production runs recorded yet.', small))

# ── Venison Breakdown ───────────────────────────────────────────────────────
VEN_ORDER = ['prosciutto', 'curedloin', 'salami', 'pastrami']
ven_cell = ParagraphStyle('vcell', fontName=SERIF, fontSize=8.5, leading=11)
ven_cell_b = ParagraphStyle('vcellb', fontSize=8.5, leading=11, fontName=SERIFB)
ven_hdr = ParagraphStyle('vhdr', fontSize=8, textColor=GREEN, fontName=SERIFB)
ven_stat = ParagraphStyle('vstat', fontSize=9, textColor=colors.HexColor('#444'), spaceBefore=2, spaceAfter=4, keepWithNext=1)
ven_mince = ParagraphStyle('vmince', fontSize=9, textColor=colors.HexColor('#444'), fontName='Helvetica-Bold', spaceBefore=3, spaceAfter=6)
ven_lane_h = ParagraphStyle('vlh', fontSize=15, textColor=GREEN, fontName=DISPLAY, spaceBefore=10, spaceAfter=2, keepWithNext=1)

def _vg(n):
    try: return float(n)
    except (TypeError, ValueError): return 0.0
def _vfmt(n):
    n = _vg(n)
    return f'{int(round(n)):,}' if n else '—'

if venison_runs:
    for run in sorted(venison_runs, key=lambda r: r.get('date', ''), reverse=True):
        ven_alias = run.get('alias','') or to_alias(run.get('estate',''))
        title = ("Venison Breakdown — " + str(run.get('batchCode', '(no batch)')) + " · " + str(ven_alias)).strip(' ·')
        add_section(title, 'Private kill — processed for the estate\u2019s own consumption.')
        lanes = sorted(run.get('lanes', []), key=lambda l: VEN_ORDER.index(l['key']) if l.get('key') in VEN_ORDER else 99)
        for lane in lanes:
            is_salami_frozen = (lane.get('calc') == 'salami' and lane.get('frozen'))
            heading = 'Venison \u2014 diced meat (for salami)' if is_salami_frozen else str(lane.get('name', 'Lane'))
            story.append(Paragraph(heading, ven_lane_h))
            if is_salami_frozen:
                st = "Trimmed and diced, pre-salted and frozen on " + str(lane.get('frozenDate', '')) + " \u2014 to defrost, add fat and mince into salami later"
            elif lane.get('frozen'):
                st = "Status: FROZEN (held) since " + str(lane.get('frozenDate', '')) + " \u2014 to defrost and continue later"
            elif lane.get('cureDate'):
                st = "Status: curing \u00b7 into cure " + str(lane.get('cureDate', ''))
            else:
                st = "Status: in progress"
            story.append(Paragraph(st, ven_stat))
            show_salt = bool(lane.get('salt'))
            is_salami = lane.get('calc') == 'salami'
            data = [[Paragraph(c, ven_hdr) for c in ['Component', 'Meat kept (g)', 'Bone / trim (g)', 'Loss %', 'Salt 2.5% (g)']]]
            sum_meat = 0.0; sum_bone = 0.0
            for c in lane.get('components', []):
                meat = _vg(c.get('meat')); bone = _vg(c.get('bone'))
                sum_meat += meat; sum_bone += bone
                loss = (bone / (meat + bone) * 100) if (meat + bone) > 0 else 0
                salt = (f'{meat * 0.025:.1f}' if show_salt else '\u2014')
                data.append([Paragraph(str(c.get('name', '')), ven_cell), Paragraph(_vfmt(meat), ven_cell),
                             Paragraph(_vfmt(bone), ven_cell), Paragraph(f'{loss:.1f}%', ven_cell), Paragraph(salt, ven_cell)])
            total_idx = None
            if is_salami:
                my = _vg(lane.get('minceYield'))
                if my:
                    trim = my - sum_meat
                    if trim > 0.5:
                        data.append([Paragraph('Trim (from leg &amp; loin prep)', ven_cell), Paragraph(_vfmt(trim), ven_cell),
                                     Paragraph('\u2014', ven_cell), Paragraph('\u2014', ven_cell), Paragraph('\u2014', ven_cell)])
                    salt_total = my * 0.025 if lane.get('frozen') else my * 1.25 * 0.025
                    label = 'Total diced (incl. trim)' if lane.get('frozen') else 'Total minced (incl. trim)'
                    data.append([Paragraph(label, ven_cell_b), Paragraph(_vfmt(my), ven_cell_b),
                                 Paragraph(_vfmt(sum_bone), ven_cell_b), Paragraph('', ven_cell_b), Paragraph(f'{salt_total:.0f}', ven_cell_b)])
                    total_idx = len(data) - 1
            t = Table(data, colWidths=[120*mm, 28*mm, 28*mm, 22*mm, 28*mm], repeatRows=1)
            tstyle = [('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8.5),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.35, HAIR),
                ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4), ('VALIGN', (0,0), (-1,-1), 'TOP')]
            if total_idx is not None:
                tstyle.append(('BACKGROUND', (0, total_idx), (-1, total_idx), LIGHT_GREEN))
                tstyle.append(('LINEABOVE', (0, total_idx), (-1, total_idx), 0.6, GREEN))
            t.setStyle(TableStyle(tstyle))
            story.append(t)

# ── PEST CONTROL SECTION ──────────────────────────────────────────────────────
_log(f"Building Pest Control section ({len(pest_records)} records)")
add_section('Pest Control Records',
    'Rodenticide details, the bait-station map, monthly bait-station checks, and insectocutor (fly-killer) checks. Insectocutor checks are shown as a matrix — one row per check date — so the record grows cleanly over the season.')

# Standing reference info
ref_style = ParagraphStyle('ref', fontSize=8, textColor=colors.HexColor('#444'), leading=11, spaceAfter=4)
story.append(Paragraph('<b>Rodenticide in use:</b> VERTOX OKTABLOK II (Brodifacoum 50ppm). SDS available at <link href="https://artisanbyrobert.github.io/fsa-records/rat_bait_difen_blocks.pdf" color="blue">artisanbyrobert.github.io/fsa-records/rat_bait_difen_blocks.pdf</link>', ref_style))
station_names = ['Under alu roof sheet (top, by sawmill)', 'By red cabinet', 'Behind bench', 'By smoker', 'Under saw bench', 'By french doors', 'Under vice']
station_lines = '  •  '.join([f'{i+1}: {n}' for i, n in enumerate(station_names)])
story.append(Paragraph(f'<b>Bait stations:</b> {station_lines}', ref_style))
story.append(Spacer(1, 4*mm))

# ── Bait station map (drawn) ──────────────────────────────────────────────────
def build_bait_map():
    # Workshop layout, scaled to fit the page. Mirrors the in-app SVG map.
    sc = 0.62  # scale factor
    W, H = 680*sc, 620*sc
    d = Drawing(W, H)
    def sx(x): return x*sc
    def sy(y): return H - y*sc  # flip Y (reportlab origin bottom-left)
    rust = colors.HexColor('#d35400'); rustedge = colors.HexColor('#a04200')
    grey = colors.HexColor('#888888'); dark = colors.HexColor('#1a1a1a'); mid = colors.HexColor('#444444')
    def box(x,y,w,h):
        d.add(Rect(sx(x), sy(y+h), w*sc, h*sc, strokeColor=grey, strokeWidth=0.8, fillColor=None))
    def txt(x,y,s,size=11,col=mid,anchor='start',bold=False):
        st = String(sx(x), sy(y), s, fontSize=size*sc*1.4, fillColor=col, textAnchor=anchor)
        st.fontName = 'Helvetica-Bold' if bold else 'Helvetica'
        d.add(st)
    def station(x,y,num):
        d.add(Circle(sx(x), sy(y), 14*sc, fillColor=rust, strokeColor=rustedge, strokeWidth=1))
        s = String(sx(x), sy(y)-4*sc, str(num), fontSize=13*sc*1.4, fillColor=colors.white, textAnchor='middle'); s.fontName='Helvetica-Bold'; d.add(s)
    def lead(x1,y1,x2,y2):
        d.add(Line(sx(x1), sy(y1), sx(x2), sy(y2), strokeColor=colors.HexColor('#aaaaaa'), strokeWidth=0.5, strokeDashArray=[2,2]))
    # outer
    txt(60,28,'Bait station map',14,dark,bold=True)
    txt(620,28,'Revised 28 Oct 2024',11,grey,anchor='end')
    box(240,78,200,56); txt(340,107,'Sawmill',13,dark,'middle',True)
    station(340,170,1); lead(354,170,448,186); txt(454,190,'Under alu roof sheet',11,mid)
    box(50,220,580,370); txt(62,238,'Main workshop',11,grey)
    box(82,265,64,22); txt(114,280,'Vice',11,mid,'middle')
    station(114,322,7); lead(128,322,198,322); txt(204,326,'Under vice',11,mid)
    box(390,262,160,78); txt(470,306,'Tractor',13,dark,'middle',True)
    d.add(Line(sx(82),sy(372),sx(600),sy(372),strokeColor=grey,strokeWidth=1)); txt(86,365,'Workbench',10,grey)
    box(82,394,108,36); txt(150,416,'Red cabinet',11,mid,'middle')
    station(105,412,2); lead(119,412,220,412); txt(226,416,'By red cabinet',11,mid)
    station(588,390,3); lead(588,404,588,428); txt(588,442,'Behind bench',11,mid,'middle')
    d.add(Line(sx(82),sy(490),sx(600),sy(490),strokeColor=grey,strokeWidth=1)); txt(86,483,'Workbench',10,grey)
    box(82,512,78,30); txt(121,530,'Smoker',11,mid,'middle')
    station(188,527,4); lead(202,527,232,527); txt(238,531,'By smoker',11,mid)
    box(320,528,110,58); txt(375,559,'Boiler tank',11,mid,'middle')
    station(375,510,5); lead(375,496,498,470); txt(504,474,'Under saw bench',11,mid)
    d.add(Line(sx(600),sy(490),sx(600),sy(570),strokeColor=grey,strokeWidth=1,strokeDashArray=[4,3])); txt(610,478,'French doors',10,grey)
    station(572,540,6); lead(572,554,572,578); txt(572,592,'By french doors',11,mid,'middle')
    return d

story.append(Paragraph('<b>Bait station map</b> — workshop layout (revised 28 Oct 2024)', ParagraphStyle('bmh', fontSize=9, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=4)))
try:
    story.append(build_bait_map())
except Exception as _e:
    _log(f"bait map draw failed: {_e}")
story.append(Spacer(1, 6*mm))

# Pest checks — date-row matrices (bait stations + insectocutors), matched pair
if pest_records:
    story.append(Paragraph('Bait Station Checks', ParagraphStyle('rbh', fontSize=11, fontName=SERIFB, textColor=GREEN, spaceAfter=2, spaceBefore=2)))
    _bait_stations = []
    for rec in pest_records:
        for _stn in (rec.get('stations', []) or []):
            _nm = clean(_stn.get('name',''))
            if _nm and _nm not in _bait_stations: _bait_stations.append(_nm)
    if _bait_stations:
        _legend = '  \u00b7  '.join(f'{i+1}: {nm}' for i, nm in enumerate(_bait_stations))
        story.append(Paragraph('Stations \u2014 ' + _legend, ParagraphStyle('blg', fontName=SERIF, fontSize=8, textColor=MUTE, leading=11, spaceAfter=4)))
        _bh = ParagraphStyle('bh', fontName=SERIFB, fontSize=8, textColor=TEAL[1], alignment=1)
        _bhl = ParagraphStyle('bhl', fontName=SERIFB, fontSize=8, textColor=TEAL[1])
        _bc = ParagraphStyle('bc', fontName=SERIF, fontSize=8, leading=10, alignment=1)
        _bd = ParagraphStyle('bd', fontName=SERIF, fontSize=8, leading=10)
        brows = [[Paragraph('Date', _bhl)] + [Paragraph(str(i+1), _bh) for i in range(len(_bait_stations))]]
        for rec in sorted(pest_records, key=lambda x: x.get('date',''), reverse=True):
            _stt = {clean(s2.get('name','')): clean(s2.get('status','')) for s2 in (rec.get('stations', []) or [])}
            if not _stt: continue
            brows.append([Paragraph(clean(rec.get('date','')), _bd)] + [Paragraph(_stt.get(nm, '\u2013'), _bc) for nm in _bait_stations])
        _nst = len(_bait_stations); _dw = 24*mm; _cw = (267*mm - _dw)/_nst
        bt = Table(brows, colWidths=[_dw] + [_cw]*_nst, repeatRows=1)
        bt.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),TEAL[0]), ('LINEABOVE',(0,0),(-1,0),0.8,GOLD), ('LINEBELOW',(0,0),(-1,0),0.8,GOLD), ('FONTSIZE',(0,0),(-1,-1),8), ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,ROWB]), ('GRID',(0,0),(-1,-1),0.35,HAIR), ('ALIGN',(1,0),(-1,-1),'CENTER'), ('LEFTPADDING',(0,0),(-1,-1),4), ('RIGHTPADDING',(0,0),(-1,-1),4), ('TOPPADDING',(0,0),(-1,-1),5), ('BOTTOMPADDING',(0,0),(-1,-1),5), ('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
        story.append(bt)

    def _itick(val):
        if isinstance(val, bool): return '\u2713' if val else '\u2013'
        v = str(val).strip().lower()
        if v in ('true','yes','done','changed','y'): return '\u2713'
        if v in ('false','no','n',''): return '\u2013'
        return clean(str(val))
    ins_locs = []
    for rec in pest_records:
        for loc in (rec.get('insectocutors', {}) or {}).keys():
            if loc not in ins_locs: ins_locs.append(loc)
    if ins_locs:
        story.append(Spacer(1, 5*mm))
        story.append(Paragraph('Insectocutor Checks', ParagraphStyle('insh', fontSize=11, fontName=SERIFB, textColor=GREEN, spaceAfter=4, spaceBefore=2)))
        _sub = ['St','Cl','La','Sr']
        _roomh = ParagraphStyle('irh', fontName=SERIFB, fontSize=8.5, textColor=TEAL[1], alignment=1)
        _subh = ParagraphStyle('ish', fontName=SERIFB, fontSize=7.5, textColor=TEAL[1], alignment=1)
        _idh = ParagraphStyle('idh', fontName=SERIFB, fontSize=8, textColor=TEAL[1])
        _mkd = ParagraphStyle('mkd', fontName='Helvetica', fontSize=9.5, textColor=GREEN, alignment=1)
        _mkn = ParagraphStyle('mkn', fontName='Helvetica', fontSize=9.5, textColor=colors.HexColor('#B9B1A4'), alignment=1)
        def _mkp(v): return Paragraph('\u2713', _mkd) if v == '\u2713' else (Paragraph('\u2013', _mkn) if v in ('\u2013','') else Paragraph(clean(v), _mkn))
        _h1 = [Paragraph('Date', _idh)]
        for loc in ins_locs: _h1 += [Paragraph(clean(prettify_name(loc)), _roomh), '', '', '']
        _h2 = [''] + [Paragraph(x, _subh) for _ in ins_locs for x in _sub]
        idata = [_h1, _h2]
        for rec in sorted(pest_records, key=lambda x: x.get('date',''), reverse=True):
            ins = rec.get('insectocutors', {}) or {}
            if not ins: continue
            r = [Paragraph(clean(rec.get('date','')), ParagraphStyle('idd', fontName=SERIF, fontSize=8, leading=10))]
            for loc in ins_locs:
                d = ins.get(loc, {})
                if isinstance(d, dict):
                    vals = [_itick(d.get('sticky','')), _itick(d.get('cleanout','')), '\u2713' if d.get('lamp') else '\u2013', '\u2713' if d.get('starter') else '\u2013']
                else:
                    vals = ['\u2013','\u2013','\u2013','\u2013']
                r += [_mkp(v) for v in vals]
            idata.append(r)
        _n = len(ins_locs); _dw2 = 24*mm; _cw2 = (267*mm - _dw2)/(4*_n)
        it = Table(idata, colWidths=[_dw2] + [_cw2]*(4*_n), repeatRows=2)
        _isty = [('BACKGROUND',(0,0),(-1,1),TEAL[0]), ('TEXTCOLOR',(0,0),(-1,1),TEAL[1]),
            ('SPAN',(0,0),(0,1)),
            ('LINEABOVE',(0,0),(-1,0),0.8,GOLD), ('LINEBELOW',(0,1),(-1,1),0.8,GOLD),
            ('ROWBACKGROUNDS',(0,2),(-1,-1),[colors.white,ROWB]),
            ('LINEBELOW',(0,2),(-1,-1),0.35,HAIR),
            ('FONTSIZE',(0,0),(-1,-1),8), ('ALIGN',(1,0),(-1,-1),'CENTER'), ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('TOPPADDING',(0,0),(-1,-1),4), ('BOTTOMPADDING',(0,0),(-1,-1),4),
            ('LEFTPADDING',(0,0),(-1,-1),2), ('RIGHTPADDING',(0,0),(-1,-1),2)]
        for _r in range(_n):
            _c0 = 1 + 4*_r
            _isty.append(('SPAN',(_c0,0),(_c0+3,0)))
            _isty.append(('LINEAFTER',(_c0+3,0),(_c0+3,-1),0.4,HAIR))
        it.setStyle(TableStyle(_isty))
        story.append(it)
        story.append(Paragraph('St = sticky board \u00b7 Cl = cleanout \u00b7 La = lamp \u00b7 Sr = starter &nbsp;\u00b7&nbsp; \u2713 done \u00b7 \u2013 not done / not recorded', small))
else:
    story.append(Paragraph('No pest control checks recorded yet.', small))

add_section('Finished Product / Delivery Records',
    'Finished salami, prosciutto and other products dispatched, by batch and destination. Completes the traceability chain from intake through production to the customer.')
if deliveries:
    rows = [['Date', 'Batch', 'Destination', 'Notes']]
    for rec in sorted(deliveries, key=lambda x: x.get('date',''), reverse=True):
        rows.append([rec.get('date',''), rec.get('batchCode',''), rec.get('destination', rec.get('processor','')), rec.get('notes','')[:60]])
    t = Table(rows, colWidths=[20*mm, 45*mm, 72*mm, 90*mm], repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('TEXTCOLOR', (0,0), (-1,0), GREEN), ('FONTNAME', (0,0), (-1,0), SERIFB), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8.5), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(t)
else:
    story.append(Paragraph('No delivery records found.', small))

class NumberedCanvas(_canvas.Canvas):
    def __init__(self, *args, **kwargs):
        _canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_pages = []
    def showPage(self):
        self._saved_pages.append(dict(self.__dict__))
        self._startPage()
    def save(self):
        total = len(self._saved_pages)
        for state in self._saved_pages:
            self.__dict__.update(state)
            self._draw_furniture(total)
            _canvas.Canvas.showPage(self)
        _canvas.Canvas.save(self)
    def _draw_furniture(self, total):
        w, hh = landscape(A4)
        self.setFont(DISPLAY, 15); self.setFillColor(GREEN)
        self.drawString(15*mm, hh - 12*mm, 'Artisan by Robert')
        self.setFont(SERIF, 8.5); self.setFillColor(MUTE)
        self.drawString(15*mm, hh - 16*mm, 'FSA Compliance Records  ' + _season_label)
        self.setFont(SERIFB, 8.5); self.setFillColor(GOLDLBL)
        self.drawRightString(w - 15*mm, hh - 12*mm, 'Licence UK2820')
        self.setFont(SERIF, 8.5); self.setFillColor(MUTE)
        self.drawRightString(w - 15*mm, hh - 16*mm, 'Hook, Hampshire RG29 1HT')
        self.setStrokeColor(GOLD); self.setLineWidth(0.8)
        self.line(15*mm, hh - 18.5*mm, w - 15*mm, hh - 18.5*mm)
        self.setFont(SERIF, 8.5); self.setFillColor(MUTE)
        self.drawCentredString(w / 2, 9*mm, f'Page {self._pageNumber} of {total} pages')

def _page_bg(canvas, doc):
    canvas.saveState(); canvas.setFillColor(IVORY)
    canvas.rect(0, 0, *landscape(A4), fill=1, stroke=0); canvas.restoreState()

story.append(Spacer(1, 8*mm))
story.append(HRFlowable(width='100%', thickness=0.8, color=GOLD, spaceAfter=4))
story.append(Paragraph('Artisan by Robert &nbsp;\u00b7&nbsp; UK2820 &nbsp;\u00b7&nbsp; Generated ' + report_date + ' &nbsp;\u00b7&nbsp; Confidential FSA Records', small))
_log(f"Building PDF ({len(story)} story elements)")
doc.build(story, onFirstPage=_page_bg, onLaterPages=_page_bg, canvasmaker=NumberedCanvas)
_log(f"ok PDF generated: {filename} ({os.path.getsize(filename)} bytes)")
print(f"PDF generated: {filename}")

_log("Reading PDF for Dropbox upload...")
access_token = get_dropbox_token()
with open(filename, 'rb') as f:
    pdf_data = f.read()

dropbox_path = f'/FSA forms and records for emilys charcuterie/automated intake records/FSA_Records_{season_code}.pdf'
_log(f"Uploading to Dropbox: {dropbox_path}")
upload_headers = {'Authorization': f'Bearer {access_token}', 'Content-Type': 'application/octet-stream', 'Dropbox-API-Arg': json.dumps({'path': dropbox_path, 'mode': 'overwrite', 'autorename': False, 'mute': True})}
r = requests.post('https://content.dropboxapi.com/2/files/upload', headers=upload_headers, data=pdf_data, timeout=60)
_log(f"  Dropbox HTTP {r.status_code}")
if r.ok:
    _log(f"ok Uploaded to Dropbox: {dropbox_path}")
    print(f"Uploaded to Dropbox: {dropbox_path}")
    # ── SUCCESS — write GREEN status (Rule 4) ─────────────────────────────────
    _write_status("GREEN", f"PDF {filename} ({len(pdf_data)} bytes) uploaded successfully. Records: intakes={len(intakes)}, daily={len(daily_records)}, deliveries={len(deliveries)}, pest={len(pest_records)}, prod={len(production_records)}")
    _log("=== Run completed successfully ===")
else:
    err = f"Dropbox upload failed: HTTP {r.status_code} {r.text[:200]}"
    _log(f"!!! {err}")
    _write_status("RED", err)
    print(f"Dropbox upload failed: {r.text}")
    exit(1)
