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
_log(f"  Records: intakes={len(intakes)}, daily={len(daily_records)}, deliveries={len(deliveries)}, pest={len(pest_records)}, prod={len(production_records)}")

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

def get_estate(rec):
    # Try direct estate name first
    name = rec.get('estate','') or rec.get('estateName','')
    if name and len(name) < 30 and not name.startswith('ey'): return name
    # Try lookup by ID
    eid = rec.get('estateId','') or rec.get('estate','')
    looked_up = estates.get(eid, '')
    if looked_up: return looked_up
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

from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable

doc = SimpleDocTemplate(filename, pagesize=landscape(A4), rightMargin=15*mm, leftMargin=15*mm, topMargin=15*mm, bottomMargin=15*mm)
GREEN = colors.HexColor('#3a6b2a')
AMBER = colors.HexColor('#854f0b')
LIGHT_GREEN = colors.HexColor('#e8f4e3')
LIGHT_GREY = colors.HexColor('#f5f5f2')
h1 = ParagraphStyle('h1', fontSize=18, textColor=GREEN, fontName='Helvetica-Bold', spaceAfter=4)
h2 = ParagraphStyle('h2', fontSize=13, textColor=GREEN, fontName='Helvetica-Bold', spaceAfter=4, spaceBefore=12, keepWithNext=1)
small = ParagraphStyle('small', fontSize=9, textColor=colors.grey)

story = []

def add_section(title):
    # Section heading + divider that stay glued to the data beneath them, so a
    # heading is never stranded at the bottom of a page away from its table.
    story.append(Spacer(1, 8*mm))
    story.append(Paragraph(title, h2))
    hr = HRFlowable(width='100%', thickness=0.5, color=GREEN, spaceAfter=6)
    hr.keepWithNext = 1
    story.append(hr)

story.append(Spacer(1, 10*mm))
story.append(Paragraph('Artisan by Robert', h1))
story.append(Paragraph('FSA Compliance Records', ParagraphStyle('sub', fontSize=13, textColor=AMBER, fontName='Helvetica-Bold', spaceAfter=2)))
story.append(Paragraph(f'FSA Licence: UK2820    Hook, Hampshire RG29 1HT', small))
story.append(Paragraph(f'Report generated: {report_date}', small))
story.append(HRFlowable(width='100%', thickness=1, color=GREEN, spaceAfter=12, spaceBefore=8))

summary_data = [['Intake records', str(len(intakes))], ['Daily records', str(len(daily_records))], ['Delivery records', str(len(deliveries))], ['Pest control checks', str(len(pest_records))], ['Production runs', str(len(production_records))]]
summary_table = Table(summary_data, colWidths=[160*mm, 60*mm])
summary_table.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,-1), LIGHT_GREEN), ('FONTSIZE', (0,0), (-1,-1), 10), ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#b8d9b0')), ('LEFTPADDING', (0,0), (-1,-1), 8), ('TOPPADDING', (0,0), (-1,-1), 6), ('BOTTOMPADDING', (0,0), (-1,-1), 6)]))
story.append(summary_table)

add_section('Intake Records')
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

add_section('Daily Records')
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

add_section('Finished Product / Delivery Records')
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
add_section('Pest Control Records')

# Standing reference info
ref_style = ParagraphStyle('ref', fontSize=8, textColor=colors.HexColor('#444'), leading=11, spaceAfter=4)
story.append(Paragraph('<b>Rodenticide in use:</b> VERTOX OKTABLOK II (Brodifacoum 50ppm). SDS available at <link href="https://artisanbyrobert.github.io/fsa-records/rat_bait_difen_blocks.pdf" color="blue">artisanbyrobert.github.io/fsa-records/rat_bait_difen_blocks.pdf</link>', ref_style))
station_names = ['Under alu roof sheet (top, by sawmill)', 'By red cabinet', 'Behind bench', 'By smoker', 'Under saw bench', 'By french doors', 'Under vice']
station_lines = '  •  '.join([f'{i+1}: {n}' for i, n in enumerate(station_names)])
story.append(Paragraph(f'<b>Bait stations:</b> {station_lines}', ref_style))
story.append(Spacer(1, 4*mm))

if pest_records:
    for rec in sorted(pest_records, key=lambda x: x.get('date',''), reverse=True):
        date_str = rec.get('date','')
        story.append(Paragraph(f'<b>Check date: {date_str}</b>', ParagraphStyle('pdh', fontSize=10, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=3, spaceBefore=6, keepWithNext=1)))
        # Stations table
        stations = rec.get('stations', []) or []
        if stations:
            story.append(Paragraph('<b>Rat Bait</b>', ParagraphStyle('rbh', fontSize=9, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=2, spaceBefore=4, keepWithNext=1)))
            srows = [['#', 'Location', 'Status', 'Notes']]
            for i, s in enumerate(stations):
                srows.append([str(i+1), clean(s.get('name','')), clean(s.get('status','')), clean(s.get('notes',''))[:60]])
            st = Table(srows, colWidths=[12*mm, 75*mm, 30*mm, 100*mm], repeatRows=1)
            st.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), GREEN), ('TEXTCOLOR', (0,0), (-1,0), colors.white), ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 7), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
            story.append(st)
        # Insectocutors
        insects = rec.get('insectocutors', {}) or {}
        if insects:
            def _tick(val):
                # True/'true'/'yes' -> check; False/None/'' -> dash; any other text shown as-is
                if isinstance(val, bool):
                    return '✓' if val else '–'
                s = str(val).strip()
                if s == '' or s is None:
                    return '–'
                if s.lower() in ('true', 'yes', 'done', 'changed', 'y'):
                    return '✓'
                if s.lower() in ('false', 'no', 'n'):
                    return '–'
                return clean(s)
            irows = [['Location', 'Sticky', 'Cleanout', 'Lamp', 'Starter', 'Notes']]
            for loc, data in insects.items():
                if isinstance(data, dict):
                    irows.append([clean(loc), _tick(data.get('sticky','')), _tick(data.get('cleanout','')), '✓' if data.get('lamp') else '–', '✓' if data.get('starter') else '–', clean(data.get('notes',''))[:50]])
            if len(irows) > 1:
                story.append(Spacer(1, 2*mm))
                story.append(Paragraph('<b>Insectocutors</b>', ParagraphStyle('insh', fontSize=9, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=2, spaceBefore=4, keepWithNext=1)))
                it = Table(irows, colWidths=[35*mm, 25*mm, 25*mm, 15*mm, 18*mm, 99*mm], repeatRows=1)
                it.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), GREEN), ('TEXTCOLOR', (0,0), (-1,0), colors.white), ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 7), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
                story.append(it)
        gn = rec.get('generalNotes','') or rec.get('notes','')
        if gn:
            story.append(Paragraph(f'<i>Notes: {clean(gn)[:200]}</i>', small))
else:
    story.append(Paragraph('No pest control checks recorded yet.', small))

# ── PRODUCTION SECTION ────────────────────────────────────────────────────────
_log(f"Building Production section ({len(production_records)} records)")
add_section('Production Records')

if production_records:
    for rec in sorted(production_records, key=lambda x: x.get('startDate', x.get('processCode','')), reverse=True):
        proc = rec.get('processCode','—')
        # If the process code is a YYYYMMDD date, show it as DD/MM/YYYY (nicer on the audit doc)
        proc_str = str(proc)
        if proc_str.isdigit() and len(proc_str) == 8:
            proc = f"{proc_str[6:8]}/{proc_str[4:6]}/{proc_str[0:4]}"
        batch = rec.get('batchCode','—')
        species = rec.get('speciesName','') or rec.get('species','')
        status = rec.get('status','in_progress')
        fat_pct = rec.get('fatPercent','') or rec.get('fatPct','')
        header = f"<b>{clean(species)} · Batch {clean(batch)} · Process {proc} · {status}"
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
                    mk = st_rec.get('meatKg','')
                    # legacy rows may still carry fat; show it if present, else just the mince
                    fk = st_rec.get('fatKg','')
                    tk = st_rec.get('totalKg','')
                    if fk and tk:
                        detail = f"meat {mk}kg + fat {fk}kg = {tk}kg total"
                    else:
                        detail = f"meat {mk}kg minced" if mk else 'minced'
                elif stage == 'stuff_hang':
                    n = st_rec.get('count','')
                    ug = st_rec.get('unitGrams','')
                    detail = f"{n} x {ug}g" if ug else f"{n} salami"
                stage_label = {'fatcalc':'Fat Calculator','saltcalc':'Salt Calculator','stuff_hang':'Stuffing & Hanging','mince':'Prep, Wash & Mince','defrost':'Defrost'}.get(stage, stage.replace('_',' ').title())
                srows.append([dstr, stage_label, detail, clean(st_rec.get('notes',''))[:60]])
            pt = Table(srows, colWidths=[22*mm, 32*mm, 60*mm, 110*mm], repeatRows=1)
            pt.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), GREEN), ('TEXTCOLOR', (0,0), (-1,0), colors.white), ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), ('FONTSIZE', (0,0), (-1,-1), 7), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
            story.append(pt)
else:
    story.append(Paragraph('No production runs recorded yet.', small))

story.append(Spacer(1, 8*mm))
story.append(HRFlowable(width='100%', thickness=0.5, color=colors.grey, spaceAfter=4))
story.append(Paragraph(f'Artisan by Robert · UK2820 · Generated {report_date} · Confidential FSA Records', small))
_log(f"Building PDF ({len(story)} story elements)")
doc.build(story)
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
