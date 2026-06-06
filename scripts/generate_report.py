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

doc = SimpleDocTemplate(filename, pagesize=landscape(A4), rightMargin=15*mm, leftMargin=15*mm, topMargin=24*mm, bottomMargin=16*mm)
GREEN = colors.HexColor('#3a6b2a')
AMBER = colors.HexColor('#854f0b')
LIGHT_GREEN = colors.HexColor('#e8f4e3')
LIGHT_GREY = colors.HexColor('#f5f5f2')
h1 = ParagraphStyle('h1', fontSize=18, textColor=GREEN, fontName='Helvetica-Bold', spaceAfter=4)
h2 = ParagraphStyle('h2', fontSize=15, textColor=GREEN, fontName='Helvetica-Bold', spaceAfter=4, spaceBefore=4, keepWithNext=1)
small = ParagraphStyle('small', fontSize=9, textColor=colors.grey)
desc_style = ParagraphStyle('desc', fontSize=9, textColor=colors.HexColor('#666'), spaceAfter=8, leading=12)

story = []
_first_section = [True]

def add_section(title, description=None, new_page=True):
    # Each major section starts on its own page and grows downward over the season.
    # An optional description explains what the section records.
    if new_page and not _first_section[0]:
        story.append(PageBreak())
    _first_section[0] = False
    story.append(Paragraph(title, h2))
    hr = HRFlowable(width='100%', thickness=1, color=GREEN, spaceAfter=6)
    hr.keepWithNext = 1
    story.append(hr)
    if description:
        story.append(Paragraph(description, desc_style))
story.append(Paragraph('Artisan by Robert', h1))
story.append(Paragraph('FSA Compliance Records', ParagraphStyle('sub', fontSize=13, textColor=AMBER, fontName='Helvetica-Bold', spaceAfter=2)))
story.append(Paragraph(f'FSA Licence: UK2820    Hook, Hampshire RG29 1HT', small))
story.append(Paragraph(f'Report generated: {report_date}', small))
story.append(HRFlowable(width='100%', thickness=1, color=GREEN, spaceAfter=12, spaceBefore=8))

summary_data = [['Intake records', str(len(intakes))], ['Daily records', str(len(daily_records))], ['Delivery records', str(len(deliveries))], ['Pest control checks', str(len(pest_records))], ['Production runs', str(len(production_records))]]
summary_table = Table(summary_data, colWidths=[160*mm, 60*mm])
summary_table.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,-1), LIGHT_GREEN), ('FONTSIZE', (0,0), (-1,-1), 10), ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#b8d9b0')), ('LEFTPADDING', (0,0), (-1,-1), 8), ('TOPPADDING', (0,0), (-1,-1), 6), ('BOTTOMPADDING', (0,0), (-1,-1), 6)]))
story.append(summary_table)
story.append(PageBreak())

add_section('Intake Records',
    'All raw meat brought in, by batch. Each batch carries its season code, intake date, source estate, species and weights. This is the start of the traceability chain — every finished product traces back to a batch here.',
    new_page=False)
intake_cell = ParagraphStyle('icell', fontSize=7, leading=10)
intake_hdr = ParagraphStyle('ihdr', fontSize=7, textColor=colors.white, fontName='Helvetica-Bold')
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
    t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), GREEN), ('FONTSIZE', (0,0), (-1,-1), 7), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(t)
else:
    story.append(Paragraph('No intake records found.', small))

add_section('Daily Records',
    'Day-by-day log of monitoring, walkabouts and ad-hoc work. Each row is one day: the type of day, free-text notes on what was observed or done, and any outstanding tasks raised that day. Newest first.')
cell_style = ParagraphStyle('cell', fontSize=7, leading=10)
header_style = ParagraphStyle('hdr', fontSize=7, textColor=colors.white, fontName='Helvetica-Bold')
if daily_records:
    rows = [[Paragraph('Date', header_style), Paragraph('Day Type', header_style), Paragraph('Notes', header_style), Paragraph('Outstanding Tasks', header_style)]]
    for rec in sorted(daily_records, key=lambda x: x.get('date',''), reverse=True):
        open_tasks = [t['text'] for t in rec.get('todoList',[]) if not t.get('done')]
        tasks_content = '<br/>'.join(['- ' + clean(t) for t in open_tasks]) if open_tasks else 'None'
        notes_content = clean(rec.get('notes','') or rec.get('monitorNotes','') or '') or '-'
        rows.append([
            Paragraph(rec.get('date',''), cell_style),
            Paragraph(rec.get('dayTypeId','').replace('-',' ').title(), cell_style),
            Paragraph(notes_content, cell_style),
            Paragraph(tasks_content, cell_style)
        ])
    t = Table(rows, colWidths=[20*mm, 38*mm, 85*mm, 84*mm], repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), GREEN), ('FONTSIZE', (0,0), (-1,-1), 7), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
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
    gt.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), GREEN), ('TEXTCOLOR', (0,0), (-1,0), colors.white), ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 7), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(gt)

add_section('Finished Product / Delivery Records',
    'Finished salami, prosciutto and other products dispatched, by batch and destination. Completes the traceability chain from intake through production to the customer.')
if deliveries:
    rows = [['Date', 'Batch', 'Destination', 'Notes']]
    for rec in sorted(deliveries, key=lambda x: x.get('date',''), reverse=True):
        rows.append([rec.get('date',''), rec.get('batchCode',''), rec.get('destination', rec.get('processor','')), rec.get('notes','')[:60]])
    t = Table(rows, colWidths=[20*mm, 45*mm, 72*mm, 90*mm], repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), GREEN), ('TEXTCOLOR', (0,0), (-1,0), colors.white), ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 8), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(t)
else:
    story.append(Paragraph('No delivery records found.', small))

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

# Rat bait checks — keep per-date detail (status varies per check)
if pest_records:
    story.append(Paragraph('<b>Bait Station Checks</b>', ParagraphStyle('rbh', fontSize=10, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=4, spaceBefore=2)))
    for rec in sorted(pest_records, key=lambda x: x.get('date',''), reverse=True):
        date_str = rec.get('date','')
        stations = rec.get('stations', []) or []
        if stations:
            story.append(Paragraph(f'Check date: {date_str}', ParagraphStyle('pdh', fontSize=9, fontName='Helvetica-Bold', textColor=colors.HexColor('#444'), spaceAfter=2, spaceBefore=4, keepWithNext=1)))
            srows = [['#', 'Location', 'Status', 'Notes']]
            for i, s in enumerate(stations):
                srows.append([str(i+1), clean(s.get('name','')), clean(s.get('status','')), clean(s.get('notes',''))[:60]])
            st = Table(srows, colWidths=[12*mm, 75*mm, 35*mm, 145*mm], repeatRows=1)
            st.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), GREEN), ('TEXTCOLOR', (0,0), (-1,0), colors.white), ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 7), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
            story.append(st)

    # Insectocutor checks as a date-row matrix
    def _itick(val):
        if isinstance(val, bool): return '✓' if val else '–'
        s = str(val).strip().lower()
        if s in ('true','yes','done','changed','y'): return '✓'
        if s in ('false','no','n','',): return '–'
        return clean(str(val))
    # collect locations seen across all records (stable column order)
    ins_locs = []
    for rec in pest_records:
        for loc in (rec.get('insectocutors', {}) or {}).keys():
            if loc not in ins_locs: ins_locs.append(loc)
    if ins_locs:
        story.append(Spacer(1, 4*mm))
        story.append(Paragraph('<b>Insectocutor Checks</b> — one row per check date; for each unit: Sticky / Cleanout / Lamp / Starter', ParagraphStyle('insh', fontSize=9, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=4, spaceBefore=2)))
        hdr_style = ParagraphStyle('ih', fontSize=6, textColor=colors.white, fontName='Helvetica-Bold', leading=7)
        # build two-row header: location spanning 4 sub-columns each
        sub = ['St','Cl','La','Sr']
        header = [Paragraph('Date', hdr_style)]
        for loc in ins_locs:
            header.append(Paragraph(clean(prettify_name(loc)) + '<br/>St·Cl·La·Sr', hdr_style))
        irows = [header]
        cell2 = ParagraphStyle('ic', fontSize=7, leading=8, alignment=1)
        for rec in sorted(pest_records, key=lambda x: x.get('date',''), reverse=True):
            ins = rec.get('insectocutors', {}) or {}
            if not ins: continue
            row = [Paragraph(clean(rec.get('date','')), ParagraphStyle('id', fontSize=7, leading=8))]
            for loc in ins_locs:
                data = ins.get(loc, {})
                if isinstance(data, dict):
                    cell = ' '.join([_itick(data.get('sticky','')), _itick(data.get('cleanout','')), '✓' if data.get('lamp') else '–', '✓' if data.get('starter') else '–'])
                else:
                    cell = '– – – –'
                row.append(Paragraph(cell, cell2))
            irows.append(row)
        nloc = len(ins_locs)
        date_w = 22*mm; loc_w = (267*mm - date_w) / nloc
        it = Table(irows, colWidths=[date_w] + [loc_w]*nloc, repeatRows=1)
        it.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), GREEN), ('FONTSIZE', (0,0), (-1,-1), 6), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 2), ('RIGHTPADDING', (0,0), (-1,-1), 2), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'MIDDLE')]))
        story.append(it)
        story.append(Paragraph('St = sticky board · Cl = cleanout · La = lamp · Sr = starter. ✓ done · – not done/not recorded.', small))
else:
    story.append(Paragraph('No pest control checks recorded yet.', small))

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
    hdr_style = ParagraphStyle('mxh', fontSize=6, textColor=colors.white, fontName='Helvetica-Bold', leading=7)
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
        ('BACKGROUND', (0,0), (-1,0), GREEN), ('FONTSIZE', (0,0), (-1,-1), 6),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')),
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
        story.append(Paragraph(header, ParagraphStyle('prh', fontSize=10, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=3, spaceBefore=6, keepWithNext=1)))
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
            ct.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), AMBER), ('TEXTCOLOR', (0,0), (-1,0), colors.white), ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 7), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
            story.append(ct)
            story.append(Spacer(1, 2*mm))
            # Per-child recipe ingredient breakdown (full traceability)
            for c in children:
                rcp = c.get('recipe') or {}
                lines = rcp.get('lines', []) if isinstance(rcp, dict) else []
                if lines:
                    story.append(Paragraph('<b>Child ' + clean(str(c.get('code',''))) + ' — ' + clean(rcp.get('name','')) + '</b>', ParagraphStyle('rch', fontSize=8, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=2, spaceBefore=3, keepWithNext=1)))
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
                    it.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), LIGHT_GREEN), ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 7), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 2), ('BOTTOMPADDING', (0,0), (-1,-1), 2)]))
                    story.append(it)
                    story.append(Spacer(1, 2*mm))
        stages = rec.get('stages',[]) or []
        if stages:
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
            pt.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), GREEN), ('TEXTCOLOR', (0,0), (-1,0), colors.white), ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 7), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
            story.append(pt)
else:
    story.append(Paragraph('No production runs recorded yet.', small))

# ── Venison Breakdown ───────────────────────────────────────────────────────
VEN_ORDER = ['prosciutto', 'curedloin', 'salami', 'pastrami']
ven_cell = ParagraphStyle('vcell', fontSize=8, leading=10)
ven_cell_b = ParagraphStyle('vcellb', fontSize=8, leading=10, fontName='Helvetica-Bold')
ven_hdr = ParagraphStyle('vhdr', fontSize=8, textColor=colors.white, fontName='Helvetica-Bold')
ven_stat = ParagraphStyle('vstat', fontSize=9, textColor=colors.HexColor('#444'), spaceBefore=2, spaceAfter=4, keepWithNext=1)
ven_mince = ParagraphStyle('vmince', fontSize=9, textColor=colors.HexColor('#444'), fontName='Helvetica-Bold', spaceBefore=3, spaceAfter=6)
ven_lane_h = ParagraphStyle('vlh', fontSize=13, textColor=GREEN, fontName='Helvetica-Bold', spaceBefore=10, spaceAfter=2, keepWithNext=1)

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
            tstyle = [('BACKGROUND', (0,0), (-1,0), GREEN), ('FONTSIZE', (0,0), (-1,-1), 8),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')),
                ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4), ('VALIGN', (0,0), (-1,-1), 'TOP')]
            if total_idx is not None:
                tstyle.append(('BACKGROUND', (0, total_idx), (-1, total_idx), LIGHT_GREEN))
                tstyle.append(('LINEABOVE', (0, total_idx), (-1, total_idx), 0.6, GREEN))
            t.setStyle(TableStyle(tstyle))
            story.append(t)

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
        self.setFont('Helvetica-Bold', 9); self.setFillColor(GREEN)
        self.drawString(15*mm, hh - 12*mm, 'Artisan by Robert')
        self.setFont('Helvetica', 8); self.setFillColor(colors.HexColor('#666'))
        self.drawString(15*mm, hh - 16*mm, 'FSA Compliance Records 2025/26')
        self.setFont('Helvetica', 8); self.setFillColor(AMBER)
        self.drawRightString(w - 15*mm, hh - 12*mm, 'Licence UK2820')
        self.setFillColor(colors.HexColor('#666'))
        self.drawRightString(w - 15*mm, hh - 16*mm, 'Generated ' + str(report_date))
        self.setStrokeColor(GREEN); self.setLineWidth(1)
        self.line(15*mm, hh - 18*mm, w - 15*mm, hh - 18*mm)
        self.setFont('Helvetica', 8); self.setFillColor(colors.HexColor('#888'))
        self.drawCentredString(w / 2, 10*mm, f'Page {self._pageNumber} of {total} pages')

story.append(Spacer(1, 8*mm))
story.append(HRFlowable(width='100%', thickness=0.5, color=colors.grey, spaceAfter=4))
story.append(Paragraph(f'Artisan by Robert · UK2820 · Generated {report_date} · Confidential FSA Records', small))
_log(f"Building PDF ({len(story)} story elements)")
doc.build(story, canvasmaker=NumberedCanvas)
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
