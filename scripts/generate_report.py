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
    4. BEFORE building the PDF, saves a full JSON snapshot of every Supabase
       record to Dropbox at:
       /FSA forms and records for emilys charcuterie/automated intake records/backups/
         supabase_latest.json          — newest snapshot, overwritten nightly
         supabase_<mon..sun>.json      — rolling 7-day history, self-overwriting
         supabase_month_YYYY-MM.json   — one archive per calendar month
       Fixed file count. No unbounded growth. No deletes ever issued.
       The backup runs FIRST on purpose: a PDF can be rebuilt from the data,
       the data cannot be rebuilt from the PDF.
    5. ALWAYS at exit — even on crash — pushes run_status.txt + generate_report_log.txt
       to artisanbyrobert/fsa-records/_status/ on GitHub (if GITHUB_TOKEN set)

  SUCCESS LOOKS LIKE:
    - generate_report_log.txt written next to this script with all ok lines
    - run_status.txt written next to this script with single line: GREEN
    - PDF file >5KB exists locally and in Dropbox
    - supabase_latest.json refreshed in Dropbox backups/ folder
    - _status/run_status.txt updated on GitHub showing GREEN + record counts
      + a "Backup:" line naming the snapshot size and row counts

  BACKUP SAFETY RULE:
    The backup will REFUSE to upload and leave the previous good files untouched
    if any Supabase table returns empty, or if fewer than MIN_DELIVERIES rows
    come back, or if the built snapshot fails to re-parse. A failed fetch must
    never be allowed to overwrite a good backup with an empty one. That case
    reports AMBER, not GREEN, so it is visible the next morning.

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

# SEASON_CODE lets a run target a past season - set it as a workflow input or an
# env var. Without it the script behaves exactly as before and uses today's date.
# Added 04/09/2026 so FSA_Records_202526.pdf could be rebuilt after commercial
# tasks were stripped out of generalTasks.
season_code = os.environ.get('SEASON_CODE', '').strip() or get_season_code(today)
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
periodic_cleans = [r['data'] for r in deliveries_raw if r.get('data') and r['data'].get('_type') == 'periodic_clean']
_log(f"  Records: intakes={len(intakes)}, daily={len(daily_records)}, deliveries={len(deliveries)}, pest={len(pest_records)}, prod={len(production_records)}, checks={len(daily_checks)}, venison={len(venison_runs)}")

# ── NIGHTLY DATA BACKUP ───────────────────────────────────────────────────────
# Added 01/08/2026. Runs here, before the PDF is built, deliberately: if the PDF
# build crashes later the snapshot has already been taken and uploaded.
#
# Retention is by fixed filename, not by deleting old files:
#   supabase_latest.json         overwritten every night
#   supabase_mon..sun.json       7 rolling days, each overwrites itself weekly
#   supabase_month_YYYY-MM.json  rewritten daily within the month, so the file
#                                left behind is that month's final state
# Steady state is 8 files plus one per month. Nothing is ever deleted.
MIN_DELIVERIES = 50          # sanity floor; live count was 211 on 01/08/2026
BACKUP_OK = False
BACKUP_MSG = "not attempted"
DBX_TOKEN = None

def _run_backup():
    global BACKUP_OK, BACKUP_MSG, DBX_TOKEN
    _log("Backup: building Supabase snapshot...")

    # GUARD 1 — a failed fetch returns [] and must never overwrite a good backup
    if not intakes_raw or not deliveries_raw or not config_raw:
        BACKUP_MSG = ("SKIPPED - a Supabase table returned empty "
                      f"(intakes={len(intakes_raw)}, deliveries={len(deliveries_raw)}, "
                      f"app_config={len(config_raw)}). Previous backups left untouched.")
        _log("!!! Backup " + BACKUP_MSG)
        return

    # GUARD 2 — short fetch means a partial read, not a real shrink
    if len(deliveries_raw) < MIN_DELIVERIES:
        BACKUP_MSG = (f"SKIPPED - only {len(deliveries_raw)} delivery rows fetched, "
                      f"floor is {MIN_DELIVERIES}. Previous backups left untouched.")
        _log("!!! Backup " + BACKUP_MSG)
        return

    snapshot = {
        'exportedAt': datetime.now().isoformat(),
        'source': 'generate_report.py nightly job',
        'warning': 'A backup is a SNAPSHOT. Live Supabase is always authoritative for writing.',
        'counts': {
            'intakes': len(intakes_raw),
            'deliveries': len(deliveries_raw),
            'app_config': len(config_raw),
        },
        'intakes': intakes_raw,
        'deliveries': deliveries_raw,
        'app_config': config_raw,
    }

    try:
        blob = json.dumps(snapshot, ensure_ascii=False, indent=1).encode('utf-8')
    except Exception as e:
        BACKUP_MSG = f"SKIPPED - could not serialise snapshot: {e}"
        _log("!!! Backup " + BACKUP_MSG)
        return

    # GUARD 3 — verify the bytes we are about to upload actually re-parse
    try:
        check = json.loads(blob.decode('utf-8'))
        assert len(check['intakes']) == len(intakes_raw)
        assert len(check['deliveries']) == len(deliveries_raw)
        assert len(check['app_config']) == len(config_raw)
    except Exception as e:
        BACKUP_MSG = f"SKIPPED - snapshot failed self-verification: {e}"
        _log("!!! Backup " + BACKUP_MSG)
        return

    _log(f"  Snapshot built and verified: {len(blob)} bytes")

    try:
        DBX_TOKEN = get_dropbox_token()
    except Exception as e:
        BACKUP_MSG = f"FAILED - could not get Dropbox token: {e}"
        _log("!!! Backup " + BACKUP_MSG)
        return

    base = '/FSA forms and records for emilys charcuterie/automated intake records/backups'
    targets = [
        ('latest',  f"{base}/supabase_latest.json"),
        ('weekday', f"{base}/supabase_{today.strftime('%a').lower()}.json"),
        ('month',   f"{base}/supabase_month_{today.strftime('%Y-%m')}.json"),
    ]

    written = []
    failed = []
    for label, path in targets:
        try:
            up_headers = {
                'Authorization': f'Bearer {DBX_TOKEN}',
                'Content-Type': 'application/octet-stream',
                'Dropbox-API-Arg': json.dumps({
                    'path': path, 'mode': 'overwrite', 'autorename': False, 'mute': True
                }),
            }
            rb = requests.post('https://content.dropboxapi.com/2/files/upload',
                               headers=up_headers, data=blob, timeout=120)
            if rb.ok:
                _log(f"  ok backup uploaded: {path}")
                written.append(label)
            else:
                _log(f"  !!! backup upload failed ({label}): HTTP {rb.status_code} {rb.text[:200]}")
                failed.append(f"{label} HTTP {rb.status_code}")
        except Exception as e:
            _log(f"  !!! backup upload error ({label}): {e}")
            failed.append(f"{label} {e}")

    # 'latest' is the one that must land. The other two are history.
    if 'latest' in written:
        BACKUP_OK = True
        BACKUP_MSG = (f"OK - {len(blob)} bytes, "
                      f"intakes={len(intakes_raw)}, deliveries={len(deliveries_raw)}, "
                      f"app_config={len(config_raw)}, files={'+'.join(written)}")
        if failed:
            BACKUP_MSG += f" (partial: {'; '.join(failed)})"
    else:
        BACKUP_MSG = f"FAILED - supabase_latest.json did not upload: {'; '.join(failed)}"
    _log("Backup: " + BACKUP_MSG)

try:
    _run_backup()
except Exception as _be:
    # A backup problem must never stop the FSA PDF being produced.
    BACKUP_MSG = f"FAILED - unexpected error: {_be}"
    _log("!!! Backup " + BACKUP_MSG)
# ── END NIGHTLY DATA BACKUP ───────────────────────────────────────────────────

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
    # Added 03/08/2026: these estates were printing their REAL names in the audit
    # PDF because they had no alias on file. Found via the delivery section.
    'hoddington':'Sam', 'wormsley':'Joe', 'stowell park':'James',
    'st clair':'Alex',
}
# Estates still with NO alias on file - their real name will print. Flagged to Robert
# 03/08/2026: g h sons, corbury estate, pevril, edward, derbyshire sporting,
# richard croft sporting.
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
# Without a family mapping, <b> inside a Paragraph has no bold face to switch to
# and reportlab silently renders it in the regular weight - every <b> tag in this
# report was doing nothing. Mapping bold to the SemiBold instance fixes them all.
# Found and fixed 06/09/2026.
if _HAS_EBG:
    try:
        pdfmetrics.registerFontFamily('EBG', normal='EBG', bold='EBGsb', italic='EBG', boldItalic='EBGsb')
    except Exception as _e:
        _log(f"font family mapping failed ({_e}) - bold tags will render regular")
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

# ── HACCP PLAN: PHEASANT MEAT INTAKE ──────────────────────────────────────────
# Sits at the START of the intake records, on its own page(s), with a trailing
# page break so it never runs on into the intake table.
_review_date = today.replace(year=today.year + 1).strftime('%d/%m/%Y')
_ht    = ParagraphStyle('hac_t',    fontName=DISPLAY, fontSize=22, textColor=GREEN,   alignment=1, leading=24, spaceAfter=2)
_hsub  = ParagraphStyle('hac_sub',  fontName=SERIF,   fontSize=11, textColor=GOLDLBL, alignment=1, spaceAfter=1)
_hmeta = ParagraphStyle('hac_meta', fontName=SERIF,   fontSize=9,  textColor=MUTE,    alignment=1, spaceAfter=10)
_hh    = ParagraphStyle('hac_h',    fontName=SERIFB,  fontSize=12, textColor=GREEN, spaceBefore=13, spaceAfter=3, keepWithNext=1)
_hb    = ParagraphStyle('hac_b',    fontName=SERIF,   fontSize=10, textColor=INK, leading=13.5, spaceAfter=4)
_hcell = ParagraphStyle('hac_cell', fontName=SERIF,   fontSize=9,  textColor=INK, leading=12)
_hctr  = ParagraphStyle('hac_ctr',  fontName=SERIFB,  fontSize=9,  textColor=GREEN, alignment=1, leading=12)
_hhdr  = ParagraphStyle('hac_hdr',  fontName=SERIFB,  fontSize=8.5,textColor=GREEN)
_hkey  = ParagraphStyle('hac_key',  fontName=SERIFB,  fontSize=9,  textColor=GREEN, leading=12)

def _hac_sec(title):
    story.append(Paragraph(title, _hh))
    _r = HRFlowable(width='100%', thickness=0.8, color=GOLD, spaceAfter=5); _r.keepWithNext = 1
    story.append(_r)

def _hac_table(rows, widths, header=True):
    t = Table(rows, colWidths=widths, repeatRows=(1 if header else 0))
    st = [('BACKGROUND', (0,0), (-1,0), SAGE[0]) if header else ('BACKGROUND',(0,0),(-1,0),colors.white),
          ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD),
          ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, ROWB]),
          ('LINEBELOW', (0,1), (-1,-1), 0.35, HAIR),
          ('GRID', (0,0), (-1,-1), 0.3, HAIR),
          ('LEFTPADDING', (0,0), (-1,-1), 6), ('RIGHTPADDING', (0,0), (-1,-1), 6),
          ('TOPPADDING', (0,0), (-1,-1), 5), ('BOTTOMPADDING', (0,0), (-1,-1), 5),
          ('VALIGN', (0,0), (-1,-1), 'TOP')]
    t.setStyle(TableStyle(st))
    return t

story.append(Paragraph('HACCP Plan &mdash; Pheasant Meat Intake', _ht))
story.append(HRFlowable(width='28%', thickness=1, color=GOLD, spaceAfter=6, spaceBefore=2, hAlign='CENTER'))
story.append(Paragraph('Goods-in control &nbsp;&middot;&nbsp; separate from the Salami process plan', _hsub))
story.append(Paragraph('Prepared by Robert Fry &nbsp;&middot;&nbsp; Revised ' + report_date + ' &nbsp;&middot;&nbsp; Review annually (next due ' + _review_date + ') &nbsp;&middot;&nbsp; FSA Licence UK2820', _hmeta))

_hac_sec('1 &nbsp; Scope of Study')
story.append(Paragraph('<b>Biological safety:</b> avoid introducing significant microbiological contamination onto any carcass, and reduce the potential for growth.', _hb))
story.append(Paragraph('<b>Physical &amp; chemical safety:</b> avoid introducing physical and chemical contaminants onto any carcass.', _hb))

_hac_sec('2 &nbsp; Process, Storage &amp; Customer')
story.append(Paragraph('<b>Process:</b> receive meat in vac bags from the processor, ready for making into salami.', _hb))
story.append(Paragraph('<b>Storage &amp; distribution:</b> either chilled for immediate use, or frozen for work later in the season.', _hb))
story.append(Paragraph('<b>Use &amp; customer:</b> current client base is hunts and estates &mdash; birds collected from each shoot, then returned as salami for their members\u2019 own consumption. Online shop for public sale developing mid-2026.', _hb))

_hac_sec('3 &nbsp; Process Flow Diagram')
_flow = [
  [Paragraph('Step', _hhdr), Paragraph('Process step', _hhdr), Paragraph('Key points', _hhdr)],
  [Paragraph('1', _hctr), Paragraph('Intake meat &nbsp;<font color="#18342A"><b>(CCP 1)</b></font>', _hcell),
     Paragraph('Take core temperature on arrival &mdash; probe <b>between packs</b> if frozen (&lt; -18\u00b0C), probe reading if chilled (&lt; 4\u00b0C). Check for damaged packaging. Remove all outer / shipping packaging at the door so only sealed vac packs pass through and outside contamination is not carried into storage.', _hcell)],
  [Paragraph('2', _hctr), Paragraph('Record intake &amp; assign batch number', _hcell),
     Paragraph('Enter delivery details on the intake record; the <b>app generates a unique batch number</b> (cannot be duplicated, reused or left blank). No meat stored or worked without a batch number.', _hcell)],
  [Paragraph('3', _hctr), Paragraph('Move to storage', _hcell),
     Paragraph('Chiller for imminent work, or freezer for work later in the season. Check airflow and fridge / freezer temperature against sensor. Store sealed vac packs only.', _hcell)],
  [Paragraph('End', _hctr), Paragraph('Hand-off', _hcell),
     Paragraph('Meat is held under its batch number in the estate pool, or passed directly to the Pheasant Salami process plan. Each intake keeps its own batch code; production runs draw from the pool and inherit the codes of the meat used.', _hcell)],
]
story.append(_hac_table(_flow, [24*mm, 78*mm, 165*mm]))

_hac_sec('4 &nbsp; Hazard Analysis')
_haz = [
  [Paragraph('Step', _hhdr), Paragraph('Process', _hhdr), Paragraph('Food safety hazard &amp; cause', _hhdr), Paragraph('Likely', _hhdr), Paragraph('Severity', _hhdr), Paragraph('Control measures', _hhdr)],
  [Paragraph('1', _hctr), Paragraph('Intake meat', _hcell), Paragraph('Bacterial growth from over-temperature meat; physical contamination from damaged packaging or from outer / shipping boxes carried into storage', _hcell), Paragraph('L', _hctr), Paragraph('H', _hctr), Paragraph('Core temp on arrival &mdash; between packs if frozen (&lt; -18\u00b0C), probe if chilled (&lt; 4\u00b0C). Check / reject damaged packaging. Remove outer packaging at the door. Log on intake record.', _hcell)],
  [Paragraph('2', _hctr), Paragraph('Record &amp; batch', _hcell), Paragraph('Loss of traceability &mdash; meat that cannot be traced to source, date and processor', _hcell), Paragraph('L', _hctr), Paragraph('H', _hctr), Paragraph('Enter details on intake record; app generates a unique batch number. No meat stored or worked without one.', _hcell)],
  [Paragraph('3', _hctr), Paragraph('Storage', _hcell), Paragraph('Bacterial growth from incorrect storage temperature; cross-contamination on handling', _hcell), Paragraph('L', _hctr), Paragraph('H', _hctr), Paragraph('Chiller or freezer as required. Check airflow and fridge / freezer temp against sensor. Store vac packs only.', _hcell)],
]
story.append(_hac_table(_haz, [16*mm, 34*mm, 74*mm, 16*mm, 20*mm, 107*mm]))

_hac_sec('5 &nbsp; CCP Determination')
_ccp = [
  [Paragraph('Step', _hhdr), Paragraph('Does this step reduce the hazard to an acceptable level?', _hhdr), Paragraph('Could contamination increase to unacceptable levels here?', _hhdr), Paragraph('Will a later step reduce it to an acceptable level?', _hhdr), Paragraph('CCP?', _hhdr), Paragraph('Justification', _hhdr)],
  [Paragraph('1 &nbsp; Intake meat', _hcell), Paragraph('Yes', _hctr), Paragraph('&mdash;', _hctr), Paragraph('&mdash;', _hctr), Paragraph('<b>CCP 1</b>', _hctr), Paragraph('The temperature check at the door is the control that confirms the meat is safe (chilled &lt; 4\u00b0C, frozen &lt; -18\u00b0C) and dirty outers are removed here. Contamination is controlled at this step.', _hcell)],
  [Paragraph('2 &nbsp; Record &amp; batch', _hcell), Paragraph('No', _hctr), Paragraph('No', _hctr), Paragraph('&mdash;', _hctr), Paragraph('No', _hctr), Paragraph('A traceability control, not a food-safety-hazard reduction. Recording details and assigning a batch number does not expose or grow contamination. (Critical traceability control all the same.)', _hcell)],
  [Paragraph('3 &nbsp; Storage', _hcell), Paragraph('No', _hctr), Paragraph('Yes', _hctr), Paragraph('Yes', _hctr), Paragraph('No', _hctr), Paragraph('Storage holds the meat; if the chiller / freezer ran warm, bacteria could grow over time, but later use &mdash; taken into the process, or frozen for future use &mdash; then controls it.', _hcell)],
]
story.append(_hac_table(_ccp, [40*mm, 16*mm, 16*mm, 16*mm, 24*mm, 155*mm]))

_hac_sec('6 &nbsp; CCP Summary &mdash; CCP 1, Meat Intake')
_sum = [
  [Paragraph('Field', _hhdr), Paragraph('Detail', _hhdr)],
  [Paragraph('Process step', _hkey), Paragraph('Step 1 &mdash; Meat intake', _hcell)],
  [Paragraph('CCP No.', _hkey), Paragraph('1', _hcell)],
  [Paragraph('Critical limit', _hkey), Paragraph('&lt; 4\u00b0C if chilled, or &lt; -18\u00b0C if frozen', _hcell)],
  [Paragraph('Monitoring', _hkey), Paragraph('Core temperature check on every delivery &mdash; probe reading if chilled, between-pack probe if frozen. Frequency: every intake. Responsibility: Robert.', _hcell)],
  [Paragraph('Records', _hkey), Paragraph('Intake record (app)', _hcell)],
  [Paragraph('Corrective action', _hkey), Paragraph('Reject or isolate any delivery outside the limit; record the deviation and the action taken. Responsibility: Robert.', _hcell)],
]
story.append(_hac_table(_sum, [50*mm, 217*mm]))

story.append(Spacer(1, 10))
story.append(Paragraph('Prepared and signed off by: Robert Fry &nbsp;&nbsp;&middot;&nbsp;&nbsp; Date ' + report_date + ' &nbsp;&nbsp;&middot;&nbsp;&nbsp; Next review: ' + _review_date,
    ParagraphStyle('hac_sign', fontName=SERIF, fontSize=9.5, textColor=INK)))
story.append(PageBreak())
# ── END HACCP PLAN ────────────────────────────────────────────────────────────

# ── RECORD PROVENANCE SUMMARY ─────────────────────────────────────────────────
# Every record states how it came to exist. Absent = written on the day.
# Four stamps: actual / reconstructed / estimated / inferred.
_PROV_LABEL = {
    'actual':        'Actual',
    'reconstructed': 'Reconstructed',
    'estimated':     'Estimated',
    'inferred':      'Inferred',
}
_PROV_MARK = {'actual': '', 'reconstructed': 'R', 'estimated': 'E', 'inferred': 'I'}
_PROV_MEANING = [
    ('Actual',        'Written on, or close to, the day the work was done. This is the normal case and carries no marking.'),
    ('Reconstructed', 'No record was made on the day. Entered later from standard operating practice, with the entry date, the person and any corroborating records stated.'),
    ('Estimated',     'Derived by calculation from something that was measured. Sound for costing; not used as evidence for a critical control point.'),
    ('Inferred',      'Taken from the recipe library or a comparable batch because no sheet exists. The weakest class, flagged for replacement.'),
]

def _prov(rec):
    """Provenance stamp of a record. Anything unmarked is an actual, on-the-day record."""
    if not isinstance(rec, dict):
        return 'actual'
    p = str(rec.get('provenance') or 'actual').strip().lower()
    return p if p in _PROV_LABEL else 'actual'

def _prov_mark(rec):
    return _PROV_MARK.get(_prov(rec), '')

_prov_pool = []
for _r in intakes:            _prov_pool.append(('Intake',           _r.get('batchCode') or _r.get('id',''), _r.get('date',''), _r))
for _r in daily_records:      _prov_pool.append(('Day record',       _r.get('batchCode') or '',              _r.get('date',''), _r))
for _r in daily_checks:       _prov_pool.append(('Opening/closing',  _r.get('batchCode') or '',              _r.get('date',''), _r))
for _r in production_records: _prov_pool.append(('Production',       _r.get('batchCode') or '',              _r.get('startDate',''), _r))
for _r in venison_runs:       _prov_pool.append(('Venison',          _r.get('batchCode') or '',              _r.get('date',''), _r))
for _r in pest_records:       _prov_pool.append(('Pest control',     '',                                     _r.get('date',''), _r))

_prov_counts = {'actual': 0, 'reconstructed': 0, 'estimated': 0, 'inferred': 0}
_prov_flagged = []
for _kind, _code, _dt, _r in _prov_pool:
    _p = _prov(_r)
    _prov_counts[_p] += 1
    if _p != 'actual':
        _prov_flagged.append((_kind, _code, _dt, _p, _r))
_prov_flagged.sort(key=lambda x: (x[2] or ''), reverse=True)
_prov_total = sum(_prov_counts.values())

_log(f"Building Record Provenance section ({_prov_total} records, {_prov_total - _prov_counts['actual']} flagged)")
add_section('Record Provenance',
    'How each record in this report came to exist. Records written on the day are shown as Actual and carry no marking. Anything entered later, calculated or taken from the library is stated openly below, with the date it was entered and what supports it. This section is published so that the status of every record is visible without having to be asked for.',
    new_page=False)

_pv_hdr  = ParagraphStyle('pvh',  fontSize=7.5, textColor=GREEN, fontName=SERIFB)
_pv_cell = ParagraphStyle('pvc',  fontName=SERIF, fontSize=8, leading=10.5)
_pv_num  = ParagraphStyle('pvn',  fontName=SERIF, fontSize=8, leading=10.5, alignment=2)

_rows = [[Paragraph('Class', _pv_hdr), Paragraph('Records', _pv_hdr), Paragraph('What it means', _pv_hdr)]]
for _lbl, _mean in _PROV_MEANING:
    _key = _lbl.lower()
    _rows.append([Paragraph(_lbl, _pv_cell), Paragraph(str(_prov_counts[_key]), _pv_num), Paragraph(_mean, _pv_cell)])
_rows.append([Paragraph('<b>Total records</b>', _pv_cell), Paragraph('<b>%d</b>' % _prov_total, _pv_num), Paragraph('', _pv_cell)])
_t = Table(_rows, colWidths=[34*mm, 22*mm, 211*mm], repeatRows=1)
_t.setStyle(TableStyle([
    ('BACKGROUND', (0,0), (-1,0), SAGE[0]),
    ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD),
    ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
    ('GRID', (0,0), (-1,-1), 0.35, HAIR),
    ('LEFTPADDING', (0,0), (-1,-1), 5), ('RIGHTPADDING', (0,0), (-1,-1), 5),
    ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ('VALIGN', (0,0), (-1,-1), 'TOP')]))
story.append(_t)
story.append(Spacer(1, 8))

if _prov_flagged:
    story.append(Paragraph('Records not written on the day', ParagraphStyle('pvsub', fontName=SERIFB, fontSize=10, textColor=GREEN, spaceAfter=4)))
    _rows = [[Paragraph('Date', _pv_hdr), Paragraph('Record', _pv_hdr), Paragraph('Batch', _pv_hdr),
              Paragraph('Class', _pv_hdr), Paragraph('Entered', _pv_hdr), Paragraph('By', _pv_hdr),
              Paragraph('Basis and corroborating records', _pv_hdr)]]
    for _kind, _code, _dt, _p, _r in _prov_flagged:
        _corr = _r.get('provenanceCorroboration') or []
        _note = clean(_r.get('provenanceNote') or 'No basis recorded.')
        if _corr:
            _note += '<br/><i>Corroborated by: ' + clean(', '.join([str(c) for c in _corr])) + '</i>'
        _rows.append([
            Paragraph(clean(_dt or '\u2014'), _pv_cell),
            Paragraph(clean(_kind), _pv_cell),
            Paragraph(clean(_code or '\u2014'), _pv_cell),
            Paragraph(_PROV_LABEL[_p], _pv_cell),
            Paragraph(clean(_r.get('provenanceDate') or '\u2014'), _pv_cell),
            Paragraph(clean(_r.get('provenanceBy') or '\u2014'), _pv_cell),
            Paragraph(_note, _pv_cell)])
    _t = Table(_rows, colWidths=[20*mm, 26*mm, 24*mm, 26*mm, 21*mm, 24*mm, 126*mm], repeatRows=1)
    _t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), SAGE[0]),
        ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
        ('GRID', (0,0), (-1,-1), 0.35, HAIR),
        ('LEFTPADDING', (0,0), (-1,-1), 5), ('RIGHTPADDING', (0,0), (-1,-1), 5),
        ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(_t)
else:
    story.append(Paragraph('Every record in this report was written on the day. Nothing has been reconstructed, calculated or inferred.', small))
story.append(Spacer(1, 6))
# ── END RECORD PROVENANCE ─────────────────────────────────────────────────────

add_section('Intake Records',
    'All raw meat brought in, by batch. Each batch carries its season code, intake date, source estate, species and weights. This is the start of the traceability chain — every finished product traces back to a batch here.',
    new_page=False)
intake_cell = ParagraphStyle('icell', fontName=SERIF, fontSize=8, leading=10.5)
intake_hdr = ParagraphStyle('ihdr', fontSize=7.5, textColor=GREEN, fontName=SERIFB)
if intakes:
    rows = [[Paragraph('Batch Code', intake_hdr), Paragraph('Date', intake_hdr), Paragraph('Estate', intake_hdr), Paragraph('Species', intake_hdr), Paragraph('Storage / Temp', intake_hdr), Paragraph('Items', intake_hdr)]]
    for rec in sorted(intakes, key=lambda x: (x.get('date') or ''), reverse=True):
        items_list = rec.get('items', [])
        items_str = '<br/>'.join([clean(f"{i.get('qty','')} {i.get('unit','')} {i.get('species','')}").strip() for i in items_list])
        species = ''
        if items_list:
            first = items_list[0]
            species = first.get('custom','') if first.get('species','') in ('Other','') else first.get('species','')
        # derive storage/temp display: frozen = < -18C, chilled = < 4C
        _storage_parts = []
        for _it in items_list:
            _st = (_it.get('storage') or '').strip().lower()
            _tmp = (_it.get('temp') or '').strip()
            if _st == 'frozen':
                _storage_parts.append('Frozen &lt; -18\u00b0C' + (' (' + _tmp + '\u00b0C recorded)' if _tmp else ''))
            elif _st == 'chilled':
                _storage_parts.append('Chilled &lt; 4\u00b0C' + (' (' + _tmp + '\u00b0C recorded)' if _tmp else ''))
            else:
                _storage_parts.append(clean(_st) if _st else '?')
        _storage_str = '<br/>'.join(dict.fromkeys(_storage_parts))  # deduplicate while preserving order
        rows.append([
            Paragraph(clean(rec.get('batchCode','')), intake_cell),
            Paragraph(rec.get('date',''), intake_cell),
            Paragraph(clean(get_estate(rec)), intake_cell),
            Paragraph(clean(species), intake_cell),
            Paragraph(_storage_str, intake_cell),
            Paragraph(items_str, intake_cell)
        ])
    t = Table(rows, colWidths=[28*mm, 18*mm, 36*mm, 20*mm, 34*mm, 91*mm], repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(t)
else:
    story.append(Paragraph('No intake records found.', small))

cell_style = ParagraphStyle('cell', fontName=SERIF, fontSize=8, leading=10.5)
header_style = ParagraphStyle('hdr', fontSize=7.5, textColor=GREEN, fontName=SERIFB)
# dates that had opening/closing checks recorded (standalone daily checks + mince days) = work days
_workday_dates = set()
_workday_batch = {}
for _c in daily_checks:
    if _c.get('date'):
        _workday_dates.add(_c.get('date'))
        if _c.get('batchCode'): _workday_batch[_c.get('date')] = _c.get('batchCode')
for _r in production_records:
    for _st in (_r.get('stages', []) or []):
        if _st.get('type') in ('mince','mix') and _st.get('date'):
            _workday_dates.add(_st.get('date'))
            _workday_batch.setdefault(_st.get('date'), _r.get('batchCode',''))
_daily_dates = set(r.get('date','') for r in daily_records)

# Monitor days and work days are recorded SEPARATELY, each with its own dates.
_monitor_rows = []
_work_rows = []
for rec in daily_records:
    dt = rec.get('date','')
    open_tasks = [t['text'] for t in rec.get('todoList',[]) if not t.get('done')]
    tasks_content = '<br/>'.join(['- ' + clean(t) for t in open_tasks]) if open_tasks else 'None'
    notes_content = clean(rec.get('notes','') or rec.get('monitorNotes','') or '') or '-'
    day_type = rec.get('dayTypeId','').replace('-',' ').title() or 'Monitor'
    if dt in _workday_dates:
        _work_rows.append((dt, _workday_batch.get(dt,'\u2014') or '\u2014', day_type, notes_content, tasks_content))
    else:
        _monitor_rows.append((dt, day_type, notes_content, tasks_content))
for dt in sorted(_workday_dates - _daily_dates, reverse=True):
    _work_rows.append((dt, _workday_batch.get(dt,'\u2014') or '\u2014', 'Work day',
                       '<i>Opening &amp; closing checks recorded \u2014 see Opening / Closing Checks &amp; Production Records.</i>', 'None'))
_monitor_rows.sort(key=lambda x: x[0], reverse=True)
_work_rows.sort(key=lambda x: x[0], reverse=True)

def _day_table(rows, widths):
    t = Table(rows, colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    return t

# ── MONITOR DAYS ──────────────────────────────────────────────────────────────
add_section('Monitor Days',
    'Non-production days, newest first. A <b>monitor / walkabout</b> day checks: dehumidifier emptied; insectocutors working; no pest ingress; temperature against the wall thermometer; cloud temperature monitoring running; and that salami and cuts are drying well. Work days are recorded separately in the next section.')
if _monitor_rows:
    rows = [[Paragraph('Date', header_style), Paragraph('Day Type', header_style), Paragraph('Notes', header_style), Paragraph('Outstanding Tasks', header_style)]]
    for dt, day_type, notes_content, tasks_content in _monitor_rows:
        rows.append([Paragraph(dt, cell_style), Paragraph(clean(day_type), cell_style), Paragraph(notes_content, cell_style), Paragraph(tasks_content, cell_style)])
    story.append(_day_table(rows, [20*mm, 42*mm, 91*mm, 74*mm]))
else:
    story.append(Paragraph('No monitor days recorded yet.', small))

# ── WORK DAYS ─────────────────────────────────────────────────────────────────
add_section('Work Days',
    'Production days, newest first \u2014 mince, stuffing, delivery or intake. Every work day carries full opening and closing hygiene checks; the detail is in the Opening Checks, Closing Checks, Equipment Clean-Down and Production Records sections.')
if _work_rows:
    rows = [[Paragraph('Date', header_style), Paragraph('Batch', header_style), Paragraph('Day Type', header_style), Paragraph('Notes', header_style), Paragraph('Outstanding Tasks', header_style)]]
    for dt, batch, day_type, notes_content, tasks_content in _work_rows:
        rows.append([Paragraph(dt, cell_style), Paragraph(clean(batch), cell_style), Paragraph(clean(day_type), cell_style), Paragraph(notes_content, cell_style), Paragraph(tasks_content, cell_style)])
    story.append(_day_table(rows, [20*mm, 24*mm, 34*mm, 89*mm, 60*mm]))
else:
    story.append(Paragraph('No work days recorded yet.', small))

# General quick-capture tasks (not tied to a record)
_open_general = [t for t in general_tasks if not t.get('done') and t.get('kind') != 'app']
_done_general = [t for t in general_tasks if t.get('done') and t.get('kind') != 'app']
if _open_general or _done_general:
    add_section('General Tasks',
        'Quick-capture jobs not tied to a specific day or batch (e.g. supplies to order). Open tasks are outstanding; done tasks show the date completed. App-development notes are excluded from this record.')
    grows = [['Task', 'Added', 'Status']]
    # Cells MUST be Paragraph objects. A raw string is drawn on one line and
    # overruns into the next column - that was the overlapping text on the
    # General Tasks page reported 03/08/2026.
    for t in _open_general:
        grows.append([Paragraph(clean(t.get('text','')), cell_style),
                      Paragraph(clean(str(t.get('addedDate',''))), cell_style),
                      Paragraph('Open', cell_style)])
    for t in _done_general:
        grows.append([Paragraph(clean(t.get('text','')), cell_style),
                      Paragraph(clean(str(t.get('addedDate',''))), cell_style),
                      Paragraph('Done ' + clean(str(t.get('doneDate',''))), cell_style)])
    gt = Table(grows, colWidths=[154*mm, 30*mm, 40*mm], repeatRows=1)
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
            if st.get('type') in ('mince','mix'):
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
    header = [Paragraph('Date', hdr_style), Paragraph('Batch', hdr_style), Paragraph('Src', hdr_style)] + [Paragraph(clean(lbl), hdr_style) for lbl in fixed_labels]
    rows = [header]
    for dt, batch, st in days:
        items = {i.get('text',''): i.get('done') for i in (st.get(key, []) or [])}
        row = [Paragraph(clean(dt), cell_style2), Paragraph(clean(batch), cell_style2),
               Paragraph(_prov_mark(st), ParagraphStyle('pvm', fontSize=7, alignment=1, fontName=SERIFB, textColor=GOLDLBL))]
        for lbl in fixed_labels:
            done = items.get(lbl)
            mark = '✓' if done else ('✗' if done is False else '–')
            row.append(Paragraph(mark, ParagraphStyle('mk', fontSize=8, alignment=1, textColor=(GREEN if done else (colors.HexColor('#a32d2d') if done is False else colors.grey)))))
        rows.append(row)
    n = len(fixed_labels)
    date_w, batch_w, src_w = 20*mm, 26*mm, 8*mm
    avail = 267*mm - date_w - batch_w - src_w
    col_w = [date_w, batch_w, src_w] + [avail / n] * n
    t = Table(rows, colWidths=col_w, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('FONTSIZE', (0,0), (-1,-1), 6),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
        ('GRID', (0,0), (-1,-1), 0.35, HAIR),
        ('LEFTPADDING', (0,0), (-1,-1), 2), ('RIGHTPADDING', (0,0), (-1,-1), 2),
        ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE')]))
    story.append(t)
    story.append(Spacer(1, 3))
    story.append(Paragraph('Src column: blank = record written on the day. '
                           'R = reconstructed later, E = estimated, I = inferred. '
                           'Full basis for every marked record is given in the Record Provenance section.', small))

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

# ── EQUIPMENT CLEAN-DOWN SECTION ──────────────────────────────────────────────
# The two machine deep-clean procedures (locked SOPs), each followed by a dated
# log of every completed clean-down. Dates come from dailychecks[].cleanDown.
_log("Building Equipment Clean-Down section")
add_section('Equipment Clean-Down',
    'Cleaning method and completion record for the mincer and sausage stuffer. The mincer is deep-cleaned on every mince day, the stuffer on every stuff day. Each completed clean-down is dated beneath its procedure.')

_CLEANDOWN_SOPS = [
    ('mincer', 'Mincer - Deep Clean',
     'Area: Dirty end / build room   Products: Ensure, Esteem, washup liquid, citric acid granules', [
        'Spray sink and draining board with Ensure alcohol sanitiser (sink already cleaned). Wait 30 sec contact, leave to air dry.',
        'Fill sink with hot water and 2 pumps washup liquid.',
        'Disassemble mincer: remove blade collar, mincing disk, cutting blade. Remove meat debris before putting into sink.',
        'Remove feed screw and separate the white nylon washer. Remove meat debris before putting into sink.',
        'Remove mincing housing from motor body. Remove meat debris before putting into sink.',
        'Wash all parts thoroughly, rinse, leave on drainer to drain.',
        'Empty and rinse out sink.',
        'Refill with water and 2 pumps Esteem sanitiser - minimum 30 sec contact time.',
        'Re-wash and rinse all parts, leave to drain.',
        'Spray all parts and leave to air dry for at least 30 sec.',
        'Load parts into water boiler, add citric acid granules - check pH below 4.0, temperature minimum 82 deg C.',
        'Soak minimum 5 minutes.',
        'Remove and leave to air dry.',
        'Cleaning cloths to washing machine 90 deg wash.',
     ]),
    ('stuffer', 'Sausage Stuffer - Deep Clean',
     'Area: Build room   Products: Ensure, Esteem, washup liquid', [
        'Spray sink and draining board with Ensure alcohol sanitiser (sink already cleaned). Wait 30 sec contact, leave to air dry.',
        'Fill sink with hot water and 2 pumps washup liquid.',
        'Disassemble stuffer: remove spout collar, spout, head plate. Remove meat debris before putting into sink.',
        'Remove pusher disk and edge rubber seal using the plastic square (so as not to damage the rubber). Remove meat debris before putting into sink.',
        'Spray inside machine body with Esteem, leave 30 sec.',
        'Rinse clean with a damp cloth.',
        'Spray inside body with Ensure alcohol spray and leave to air dry.',
        'Wash all parts thoroughly, rinse, leave on drainer to drain.',
        'Empty and rinse out sink.',
        'Refill with water and 2 pumps Esteem sanitiser - minimum 30 sec contact time.',
        'Re-wash and rinse all parts, leave to drain.',
        'Spray all parts and leave to air dry for at least 30 sec.',
        'Load all parts into blue tray, cover with water from the boiler - check temperature minimum 82 deg C.',
        'Soak minimum 5 minutes.',
        'Remove and leave to air dry.',
        'Cleaning cloths to washing machine 90 deg wash.',
     ]),
]

_cd_dates = {'mincer': [], 'stuffer': []}
for _c in daily_checks:
    _cd = _c.get('cleanDown') or {}
    _dt = _c.get('date', '')
    for _m in ('mincer', 'stuffer'):
        if _cd.get(_m) and _dt:
            _cd_dates[_m].append(_dt)
for _m in _cd_dates:
    _cd_dates[_m] = sorted(set(_cd_dates[_m]))

def _cd_fmt(iso):
    try:
        _y, _mo, _d = str(iso).split('-')
        _mn = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][int(_mo)-1]
        return f"{int(_d)} {_mn} {_y}"
    except Exception:
        return clean(str(iso))

_sop_h    = ParagraphStyle('cdsoph', fontName=SERIFB, fontSize=11,  textColor=GREEN,   spaceBefore=10, spaceAfter=1, keepWithNext=1)
_sop_meta = ParagraphStyle('cdsopm', fontName=SERIF,  fontSize=8.5, textColor=GOLDLBL, spaceAfter=5,  keepWithNext=1)
_sop_step = ParagraphStyle('cdsops', fontName=SERIF,  fontSize=9,   textColor=INK, leading=12, leftIndent=15, firstLineIndent=-12, spaceAfter=1)
_cd_hdr   = ParagraphStyle('cdhdr',  fontName=SERIFB, fontSize=8,   textColor=GREEN)
_cd_cell  = ParagraphStyle('cdcell', fontName=SERIF,  fontSize=8.5, leading=11)
_cd_tick  = ParagraphStyle('cdtick', fontName=SERIFB, fontSize=9,   textColor=GREEN)

for _cd_i, (_key, _title, _meta, _steps) in enumerate(_CLEANDOWN_SOPS):
    if _cd_i:                      # each machine's procedure starts on a clean page
        story.append(PageBreak())
    story.append(Paragraph(_title, _sop_h))
    story.append(Paragraph(clean(_meta), _sop_meta))
    for _i, _s in enumerate(_steps, 1):
        story.append(Paragraph(str(_i) + '.&nbsp;&nbsp;' + clean(_s), _sop_step))
    story.append(Spacer(1, 4))
    _dates = _cd_dates.get(_key, [])
    if _dates:
        _rows = [[Paragraph('Date completed', _cd_hdr), Paragraph('Clean-down done (per procedure)', _cd_hdr)]]
        for _d in _dates:
            _rows.append([Paragraph(_cd_fmt(_d), _cd_cell), Paragraph('\u2713', _cd_tick)])
        _ct = Table(_rows, colWidths=[40*mm, None])
        _ct.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), SAND[0]),
            ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD),
            ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8.5),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, ROWB]),
            ('GRID', (0,0), (-1,-1), 0.35, HAIR),
            ('LEFTPADDING', (0,0), (-1,-1), 5), ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
        story.append(_ct)
    else:
        story.append(Paragraph('No clean-downs recorded yet this season.', small))
    story.append(Spacer(1, 6))


# ── PERIODIC DEEP CLEANS ──────────────────────────────────────────────────────
# Whole-premises deep cleans, separate from the per-machine clean-downs above.
_log(f"Building Periodic Deep Clean section ({len(periodic_cleans)} records)")
story.append(PageBreak())
add_section('Periodic Deep Cleans',
    'Whole-premises deep cleans covering floors, fabric, fixed equipment and the winery. These are separate from the per-machine clean-downs, which run with each mince and stuff day.',
    new_page=False)

_pc_hdr  = ParagraphStyle('pchdr',  fontName=SERIFB, fontSize=8.5, textColor=GREEN)
_pc_cell = ParagraphStyle('pccell', fontName=SERIF,  fontSize=8.5, leading=11)

if periodic_cleans:
    _pcs = sorted(periodic_cleans, key=lambda x: x.get('date') or '', reverse=True)
    _rows = [[Paragraph('Date', _pc_hdr), Paragraph('Clean', _pc_hdr),
              Paragraph('Areas and equipment covered', _pc_hdr), Paragraph('Source', _pc_hdr)]]
    for _p in _pcs:
        _areas = []
        for _t in (_p.get('tasks') or []):
            _items = _t.get('items') or []
            _areas.append('<b>' + clean(str(_t.get('area',''))) + ':</b> ' +
                          clean(', '.join([str(i).split(' - ticked')[0] for i in _items])))
        _src = _PROV_LABEL.get(_prov(_p), 'Actual')
        _rows.append([
            Paragraph(_cd_fmt(_p.get('date','')), _pc_cell),
            Paragraph(clean(_p.get('name','Periodic deep clean')), _pc_cell),
            Paragraph('<br/>'.join(_areas) if _areas else clean(str(_p.get('note',''))), _pc_cell),
            Paragraph(_src, _pc_cell)])
    _pt = Table(_rows, colWidths=[24*mm, 44*mm, 168*mm, 31*mm], repeatRows=1)
    _pt.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), SAGE[0]),
        ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
        ('GRID', (0,0), (-1,-1), 0.35, HAIR),
        ('LEFTPADDING', (0,0), (-1,-1), 5), ('RIGHTPADDING', (0,0), (-1,-1), 5),
        ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    story.append(_pt)
    story.append(Spacer(1, 5))
    for _p in _pcs:
        _extra = _p.get('instructionsLeft') or []
        if _extra:
            story.append(Paragraph('<b>' + _cd_fmt(_p.get('date','')) + '</b> &mdash; notes on the sheet: ' +
                                   clean('; '.join([str(x) for x in _extra])), small))
else:
    story.append(Paragraph('No periodic deep cleans recorded yet this season.', small))
story.append(Spacer(1, 6))
# ── END PERIODIC DEEP CLEANS ──────────────────────────────────────────────────

# ── HACCP PLAN: PHEASANT / PARTRIDGE SALAMI ───────────────────────────────────
# Sits at the START of the Production section, on its own page(s), with a
# trailing page break so it never runs on into the production records.
# Section 10 (Verification & Review) is deliberately omitted until the
# outstanding lab / water / swab tests are completed.
story.append(PageBreak())
story.append(Paragraph('HACCP Plan &mdash; Pheasant &amp; Partridge Salami', _ht))
story.append(HRFlowable(width='28%', thickness=1, color=GOLD, spaceAfter=6, spaceBefore=2, hAlign='CENTER'))
story.append(Paragraph('Dried fermented salami &nbsp;&middot;&nbsp; separate from the Meat Intake plan', _hsub))
story.append(Paragraph('Prepared by Robert Fry &nbsp;&middot;&nbsp; Revised ' + report_date + ' &nbsp;&middot;&nbsp; Review annually (next due ' + _review_date + ') &nbsp;&middot;&nbsp; FSA Licence UK2820', _hmeta))

_hac_sec('1 &nbsp; Scope of Study')
story.append(Paragraph('Dried, uncooked, fermented salami made from wild game birds &mdash; principally pheasant and partridge &mdash; blended with organic pork fat, salted, cased and air dried to shelf stability.', _hb))
story.append(Paragraph('The plan begins where frozen game meat is drawn from storage and ends at vac-packed finished product held in ambient storage awaiting delivery.', _hb))
story.append(Paragraph('<b>Not covered here:</b> meat intake (separate plan, in force from 26 July 2026), and venison prosciutto, cured loin and fillet, and pastrami (separate plans to follow).', _hb))

_hac_sec('2 &nbsp; Product Description')
_pd = [
  [Paragraph('Item', _hhdr), Paragraph('Detail', _hhdr)],
  [Paragraph('Product', _hkey), Paragraph('Dried fermented salami. Ready to eat, not cooked.', _hcell)],
  [Paragraph('Meat', _hkey), Paragraph('Wild pheasant or partridge, previously frozen.', _hcell)],
  [Paragraph('Fat', _hkey), Paragraph('Organic pork fat from a known farm via its own abattoir. Diced and stored in salt; alcohol washed before mince, which removes the storage salt, so no salt adjustment is required.', _hcell)],
  [Paragraph('Curing agent', _hkey), Paragraph('Salt only, at 2.5% of combined meat and fat weight. <b>No nitrates or nitrites are used.</b>', _hcell)],
  [Paragraph('Starter culture', _hkey), Paragraph('Flora Italiana, added at the blend stage (S3). Sugar in the recipe is the substrate for the culture, not a flavouring.', _hcell)],
  [Paragraph('Alcohol', _hkey), Paragraph('Estate-produced wine above 13% ABV. A processing aid only &mdash; applied to the surface, then drained. It does not remain in the mix and is not an ingredient of the finished product.', _hcell)],
  [Paragraph('Casing', _hkey), Paragraph('45 mm Devro collagen, or 65&ndash;68 mm beef.', _hcell)],
  [Paragraph('Target wet weight', _hkey), Paragraph('230&ndash;260 g in 45 mm &nbsp;&middot;&nbsp; 400&ndash;530 g in 65&ndash;68 mm. Recorded per piece at stuffing.', _hcell)],
  [Paragraph('Shelf stability', _hkey), Paragraph('Achieved by reduction of water activity to 0.82 or below.', _hcell)],
  [Paragraph('Packaging &amp; storage', _hkey), Paragraph('Vacuum packed, then held in ambient storage below 18&deg;C.', _hcell)],
]
story.append(_hac_table(_pd, [42*mm, 225*mm]))

_hac_sec('3 &nbsp; Intended Use &amp; Consumers')
story.append(Paragraph('Ready to eat without further cooking. Supplied to shooting estates for consumption by estate members and their guests, and to private individuals. No current client resells to the public, and the product is not currently placed on the open retail market.', _hb))
story.append(Paragraph('<b>Vulnerable groups:</b> the product is not marketed to infants, the elderly, pregnant women or the immunocompromised, but as a ready-to-eat cured product it may be consumed by them. The controls in this plan are set on that basis.', _hb))

_hac_sec('4 &nbsp; Basis of the Plan &mdash; Why Cold, and Why No Nitrate')
story.append(Paragraph('<b>No nitrate.</b> Salt is the sole curing agent by choice. Nitrate is therefore not available as a hurdle against pathogen growth during curing.', _hb))
story.append(Paragraph('<b>Cold rather than ambient.</b> Standard salami is fermented at ambient, roughly 18&ndash;24&deg;C. Pheasant and partridge are effectively poultry and carry a significantly higher bacterial load than pork or beef, principally <i>Salmonella</i> and <i>Campylobacter</i>. Fermenting these at ambient without nitrate would allow those organisms to multiply during the critical early drying phase.', _hb))
story.append(Paragraph('<b>The control.</b> Product is held below 4&deg;C from intake through the entire drying process, until water activity 0.82 is reached. This cold chain replaces the nitrate-and-ambient pathway. It is a deliberate, documented and more conservative approach, appropriate to the species used. Once 0.82 is reached the product is shelf stable and moves to ambient below 18&deg;C.', _hb))
story.append(Paragraph('<b>The second hurdle.</b> Flora Italiana starter culture is added at the blend stage. It metabolises the added sugar and lowers pH through the cure, continuing once the product moves to ambient where the culture is most active. A falling pH is a recognised hurdle against <i>Listeria monocytogenes</i>, the organism of most concern in a chilled ready-to-eat product.', _hb))
story.append(Paragraph('<b>Stated limitation.</b> pH is not currently measured or recorded. The hurdle is real and the mechanism is standard, but until pH is measured it is described here rather than monitored, and the plan does not depend on it. Water activity remains the primary control and the plan stands without the pH claim.', _hb))

_hac_sec('5 &nbsp; Process Flow Diagram')
_sflow = [
  [Paragraph('Step', _hhdr), Paragraph('Process step', _hhdr), Paragraph('Temperature', _hhdr), Paragraph('Key points', _hhdr)],
  [Paragraph('S1', _hctr), Paragraph('Draw from storage, defrost', _hcell), Paragraph('0&ndash;4&deg;C', _hctr),
     Paragraph('Frozen meat transferred to chiller, approximately 3 days. <b>No ambient defrost is permitted.</b> Fat is held separately and joins at S2.', _hcell)],
  [Paragraph('S2', _hctr), Paragraph('Alcohol wash, mince, salt', _hcell), Paragraph('below 4&deg;C', _hctr),
     Paragraph('Wash meat in alcohol above 13% ABV, minimum 60 seconds contact, then drain (garlic blitzed into the alcohol first if the recipe requires it). Wash the diced salted fat the same way. Mince meat and fat together to recipe plate sizes. Add salt at 2.5% of combined weight. Record all contributing batch codes. Decide the number of children from the defrost size and the client order; each child takes its own code.', _hcell)],
  [Paragraph('S3', _hctr), Paragraph('Make blend per child', _hcell), Paragraph('below 4&deg;C', _hctr),
     Paragraph('Add order: sugar, then Flora Italiana starter culture, then garlic if used, then the remaining dry powders. Each ingredient ticked as it goes in. Blend mixed into the rested meat and fat.', _hcell)],
  [Paragraph('S4', _hctr), Paragraph('Rest mixed blend', _hcell), Paragraph('below 4&deg;C', _hctr),
     Paragraph('Standard 24 hours, may be shortened under time pressure. Quality control, not a safety step &mdash; it improves texture and filling consistency.', _hcell)],
  [Paragraph('S5', _hctr), Paragraph('Stuff casings, hang', _hcell), Paragraph('below 4&deg;C', _hctr),
     Paragraph('Stuffer deep-cleaned per SOP. Stuff to target wet weight and record it &mdash; this is the baseline the 40% weight loss is measured from. Apply the internal hanging label (batch code, alias, species, flavour, hang date) and hang on rails.', _hcell)],
  [Paragraph('<b>S6</b>', _hctr), Paragraph('<b>Air dry to shelf stability &nbsp;<font color="#18342A">(CCP 1)</font></b>', _hcell), Paragraph('<b>below 4&deg;C throughout</b>', _hctr),
     Paragraph('Cold chain maintained for the whole drying period. Dry until water activity is 0.82 or below <b>and</b> weight loss is 40% or more. Water activity meter is the primary measure, weight loss the secondary. Mould washed off with vinegar as required.', _hcell)],
  [Paragraph('S7', _hctr), Paragraph('Move to ambient storage', _hcell), Paragraph('below 18&deg;C', _hctr),
     Paragraph('Quality and presentation control, not a CCP. The 18&deg;C ceiling is set by pheasant fat, which is very soft and begins to liquefy around 20&deg;C, seeping into the vac pack and appearing oily. No minimum temperature.', _hcell)],
  [Paragraph('S8', _hctr), Paragraph('Vac pack, label, store', _hcell), Paragraph('ambient below 18&deg;C', _hctr),
     Paragraph('Vac pack the finished pieces. Apply the customer label: estate name, species, flavour, ingredients in descending weight order, allergens in bold, batch code and best before. Ambient storage pending delivery.', _hcell)],
  [Paragraph('S9', _hctr), Paragraph('Lab test, positive release', _hcell), Paragraph('&mdash;', _hctr),
     Paragraph('<b>Future requirement, not currently applicable.</b> Required before any onward sale to the public. All product currently goes to estate members for their own consumption.', _hcell)],
]
story.append(_hac_table(_sflow, [16*mm, 46*mm, 27*mm, 178*mm]))

_hac_sec('6 &nbsp; Hazard Analysis')
story.append(Paragraph('Hazard types: <b>B</b> biological &nbsp;&middot;&nbsp; <b>C</b> chemical &nbsp;&middot;&nbsp; <b>P</b> physical.', _hb))
_shaz = [
  [Paragraph('Step', _hhdr), Paragraph('Food safety hazard &amp; cause', _hhdr), Paragraph('Significant', _hhdr), Paragraph('Control measures', _hhdr), Paragraph('CCP?', _hhdr)],
  [Paragraph('S1', _hctr), Paragraph('<b>B</b> &mdash; growth of <i>Salmonella</i>, <i>Campylobacter</i> and <i>Listeria</i> if the meat is defrosted warm', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Chiller defrost at 0&ndash;4&deg;C only; ambient defrost prohibited. Chiller temperature monitored on the daily checks.', _hcell), Paragraph('No &mdash; prerequisite', _hcell)],
  [Paragraph('S1', _hctr), Paragraph('<b>P</b> &mdash; shot, bone fragment or feather carried through from processing', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Controlled at meat intake under the separate plan, and by visual inspection at mince.', _hcell), Paragraph('No &mdash; prerequisite', _hcell)],
  [Paragraph('S2', _hctr), Paragraph('<b>B</b> &mdash; surface bacteria on meat and fat redistributed through the whole batch by mincing', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Alcohol wash above 13% ABV, minimum 60 seconds contact, applied to meat and fat <b>before</b> mincing, then drained. Reduces the surface load entering the mince. See note 6.1.', _hcell), Paragraph('No &mdash; hurdle, not a kill step', _hcell)],
  [Paragraph('S2', _hctr), Paragraph('<b>B</b> &mdash; bacterial growth during handling', _hcell), Paragraph('Yes', _hctr),
     Paragraph('All work held below 4&deg;C throughout.', _hcell), Paragraph('No &mdash; prerequisite', _hcell)],
  [Paragraph('S2', _hctr), Paragraph('<b>C</b> &mdash; salt weighed incorrectly, weakening the cure', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Salt calculated at 2.5% of combined meat and fat weight by the app and recorded on the production record.', _hcell), Paragraph('No &mdash; verified at S6', _hcell)],
  [Paragraph('S2', _hctr), Paragraph('<b>B / P</b> &mdash; cross-contamination from the mincer', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Mincer deep-cleaned to the SOP and the clean recorded.', _hcell), Paragraph('No &mdash; prerequisite', _hcell)],
  [Paragraph('S3', _hctr), Paragraph('<b>C</b> &mdash; undeclared allergen in a spice blend', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Recipe held centrally in the app; ingredients ticked individually as added; allergens carried through to the customer label in bold. FSA allergen chart used in the manual paperwork.', _hcell), Paragraph('No &mdash; prerequisite', _hcell)],
  [Paragraph('S3', _hctr), Paragraph('<b>B</b> &mdash; <i>Listeria monocytogenes</i> survival in a chilled ready-to-eat product', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Flora Italiana starter culture added here, with sugar as its substrate, lowering pH through the cure. An additional hurdle alongside salt, cold chain and water activity. See note 6.2.', _hcell), Paragraph('No &mdash; hurdle, not monitored', _hcell)],
  [Paragraph('S3', _hctr), Paragraph('<b>C</b> &mdash; starter culture omitted, or ingredients added out of order', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Add order enforced tick-by-tick in the app: sugar, enzyme, garlic, then the remaining powders.', _hcell), Paragraph('No &mdash; recipe control', _hcell)],
  [Paragraph('S4', _hctr), Paragraph('<b>B</b> &mdash; growth during the rest period', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Held below 4&deg;C. A texture and filling-consistency step, not a safety step.', _hcell), Paragraph('No', _hcell)],
  [Paragraph('S5', _hctr), Paragraph('<b>B</b> &mdash; contamination from the stuffer', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Stuffer deep-cleaned to the SOP before use and the clean recorded.', _hcell), Paragraph('No &mdash; prerequisite', _hcell)],
  [Paragraph('S5', _hctr), Paragraph('<b>B</b> &mdash; piece weight above target, so drying is slower than assumed', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Target wet weight per casing size; every piece weighed at stuffing and the wet weight recorded as the baseline for the 40% weight loss.', _hcell), Paragraph('No &mdash; input to CCP 1', _hcell)],
  [Paragraph('<b>S6</b>', _hctr), Paragraph('<b>B</b> &mdash; survival or growth of <i>Salmonella</i>, <i>Campylobacter</i>, <i>Listeria</i> and <i>Staph. aureus</i>, and toxin formation, in a ready-to-eat product cured without nitrate', _hcell), Paragraph('<b>Yes</b>', _hctr),
     Paragraph('<b>Cold chain below 4&deg;C throughout drying, together with reduction of water activity to 0.82 or below.</b>', _hcell), Paragraph('<b>YES &mdash; CCP 1</b>', _hcell)],
  [Paragraph('S6', _hctr), Paragraph('<b>B</b> &mdash; surface mould growth', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Washed with vinegar as required. A corrective action, not routine. Excessive mould is flagged and investigated as poor upstream handling at estate or processor level.', _hcell), Paragraph('No &mdash; corrective action', _hcell)],
  [Paragraph('S6', _hctr), Paragraph('<b>B</b> &mdash; spore-forming organisms (<i>Clostridium</i>, <i>Bacillus</i>), which the alcohol wash does not affect', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Controlled by water activity reduction and the cold chain, that is by CCP 1.', _hcell), Paragraph('Covered by CCP 1', _hcell)],
  [Paragraph('S7', _hctr), Paragraph('<b>C</b> &mdash; pheasant fat softens and seeps, product appears oily', _hcell), Paragraph('No &mdash; quality', _hcell),
     Paragraph('Upper limit 18&deg;C; pheasant fat begins to liquefy around 20&deg;C. No minimum temperature.', _hcell), Paragraph('No', _hcell)],
  [Paragraph('S8', _hctr), Paragraph('<b>C</b> &mdash; incorrect allergen declaration reaching the customer', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Customer label generated from the stored recipe, allergens rendered bold, batch code carried on every label.', _hcell), Paragraph('No &mdash; prerequisite', _hcell)],
  [Paragraph('S8', _hctr), Paragraph('<b>B</b> &mdash; contamination during packing', _hcell), Paragraph('Yes', _hctr),
     Paragraph('Product is already shelf stable at this point. Clean handling to the SOP.', _hcell), Paragraph('No', _hcell)],
]
story.append(_hac_table(_shaz, [15*mm, 74*mm, 24*mm, 122*mm, 32*mm]))

_hac_sec('6.1 &nbsp; Note &mdash; Why the Alcohol Wash Is Not a CCP')
story.append(Paragraph('The wash reduces the bacterial load on the <b>surface of intact pieces</b>. It cannot reach organisms already within the muscle, and it does not act on spores. It is applied before mincing precisely because mincing would redistribute surface organisms throughout the batch, where a surface treatment can no longer reach them.', _hb))
story.append(Paragraph('It is therefore a genuine hurdle, and it is monitored by contact time, but it is <b>not claimed as a kill step and not designated a CCP</b>. The step that delivers safety is S6.', _hb))
_bac = [
  [Paragraph('Organism reduced by the wash', _hhdr), Paragraph('Why it is relevant', _hhdr)],
  [Paragraph('<i>Salmonella</i> spp.', _hkey), Paragraph('Primary concern with game birds. Surface contamination from gutting and processing.', _hcell)],
  [Paragraph('<i>Campylobacter jejuni</i>', _hkey), Paragraph('Highly alcohol-sensitive. Common on game bird carcasses.', _hcell)],
  [Paragraph('<i>E. coli</i>, including O157', _hkey), Paragraph('Surface contamination from gut contact during dressing.', _hcell)],
  [Paragraph('<i>Listeria monocytogenes</i>', _hkey), Paragraph('Present in the processing environment and on raw meat surfaces.', _hcell)],
  [Paragraph('<i>Staphylococcus aureus</i>', _hkey), Paragraph('Skin and handling contamination.', _hcell)],
  [Paragraph('<i>Pseudomonas</i> spp.', _hkey), Paragraph('Spoilage organism. Reducing it extends shelf life as well as improving safety.', _hcell)],
  [Paragraph('Not affected', _hkey), Paragraph('Bacterial spores (<i>Clostridium</i>, <i>Bacillus</i>), controlled instead by CCP 1. Viruses, not relevant to this product.', _hcell)],
]
story.append(_hac_table(_bac, [62*mm, 205*mm]))
story.append(Spacer(1, 4))
story.append(Paragraph('These are meat-associated organisms. They are not specific to poultry.', _hb))

_hac_sec('6.2 &nbsp; Note &mdash; The Starter Culture as a Hurdle')
story.append(Paragraph('Flora Italiana is added at S3 and metabolises the added sugar, lowering pH through the cure and on into ambient storage at S7. A falling pH is a recognised hurdle against <i>Listeria monocytogenes</i>.', _hb))
story.append(Paragraph('It is treated here as a <b>supporting hurdle, not a control</b>, for one reason: pH is not currently measured or recorded. A hurdle that is not monitored cannot be verified. If pH measurement is introduced &mdash; a reading at hang and a reading at release, recorded on the production record &mdash; this becomes an evidenced hurdle and this note can be rewritten accordingly.', _hb))

_hac_sec('7 &nbsp; CCP Determination')
story.append(Paragraph('Applied to each significant hazard. <b>Q1</b> is a control measure in place? &nbsp;<b>Q2</b> is this step designed specifically to eliminate or reduce the hazard to an acceptable level? &nbsp;<b>Q3</b> could contamination occur or increase to unacceptable levels here? &nbsp;<b>Q4</b> will a later step eliminate or reduce it to an acceptable level?', _hb))
_sccp = [
  [Paragraph('Step', _hhdr), Paragraph('Q1', _hhdr), Paragraph('Q2', _hhdr), Paragraph('Q3', _hhdr), Paragraph('Q4', _hhdr), Paragraph('Outcome &amp; justification', _hhdr)],
  [Paragraph('S1 &nbsp; Defrost', _hcell), Paragraph('Yes', _hctr), Paragraph('No', _hctr), Paragraph('Yes', _hctr), Paragraph('Yes &mdash; S6', _hctr),
     Paragraph('Not a CCP. Growth is possible if the chiller runs warm, but S6 subsequently controls it. Managed by chiller monitoring as a prerequisite.', _hcell)],
  [Paragraph('S2 &nbsp; Wash, mince, salt', _hcell), Paragraph('Yes', _hctr), Paragraph('No', _hctr), Paragraph('Yes', _hctr), Paragraph('Yes &mdash; S6', _hctr),
     Paragraph('Not a CCP. The alcohol wash reduces surface load but does not eliminate the hazard, and does not reach spores or organisms within the muscle.', _hcell)],
  [Paragraph('S3 &nbsp; Make blend', _hcell), Paragraph('Yes', _hctr), Paragraph('No', _hctr), Paragraph('No', _hctr), Paragraph('&mdash;', _hctr),
     Paragraph('Not a CCP. The starter culture contributes a pH hurdle but is not monitored; allergen accuracy is a prerequisite control.', _hcell)],
  [Paragraph('S4 &nbsp; Rest', _hcell), Paragraph('Yes', _hctr), Paragraph('No', _hctr), Paragraph('No', _hctr), Paragraph('&mdash;', _hctr),
     Paragraph('Not a CCP. Held below 4&deg;C; a quality step affecting texture and filling consistency.', _hcell)],
  [Paragraph('S5 &nbsp; Stuff and hang', _hcell), Paragraph('Yes', _hctr), Paragraph('No', _hctr), Paragraph('Yes', _hctr), Paragraph('Yes &mdash; S6', _hctr),
     Paragraph('Not a CCP, but the recorded stuffed wet weight is an essential input to the CCP 1 weight-loss measure.', _hcell)],
  [Paragraph('<b>S6 &nbsp; Air dry</b>', _hcell), Paragraph('<b>Yes</b>', _hctr), Paragraph('<b>Yes</b>', _hctr), Paragraph('<b>Yes</b>', _hctr), Paragraph('<b>No later step</b>', _hctr),
     Paragraph('<b>CCP 1.</b> The only step at which a significant hazard is reduced to an acceptable level, and no subsequent step will do so. Sole critical control point.', _hcell)],
  [Paragraph('S7 &nbsp; Ambient storage', _hcell), Paragraph('Yes', _hctr), Paragraph('No', _hctr), Paragraph('No', _hctr), Paragraph('&mdash;', _hctr),
     Paragraph('Not a CCP. Product is already shelf stable; the temperature limit is a presentation control set by the behaviour of pheasant fat.', _hcell)],
  [Paragraph('S8 &nbsp; Pack and label', _hcell), Paragraph('Yes', _hctr), Paragraph('No', _hctr), Paragraph('No', _hctr), Paragraph('&mdash;', _hctr),
     Paragraph('Not a CCP. Allergen declaration accuracy is a prerequisite labelling control.', _hcell)],
]
story.append(_hac_table(_sccp, [40*mm, 14*mm, 14*mm, 14*mm, 24*mm, 161*mm]))

_hac_sec('8 &nbsp; CCP Summary &mdash; CCP 1, Air Drying to Shelf Stability')
_ssum = [
  [Paragraph('Field', _hhdr), Paragraph('Detail', _hhdr)],
  [Paragraph('Process step', _hkey), Paragraph('S6 &mdash; air drying', _hcell)],
  [Paragraph('CCP No.', _hkey), Paragraph('1 &mdash; the only critical control point in this plan', _hcell)],
  [Paragraph('Hazard', _hkey), Paragraph('Survival or growth of vegetative pathogens and spore-forming organisms in a ready-to-eat, uncooked product cured without nitrate.', _hcell)],
  [Paragraph('Critical limit', _hkey), Paragraph('Water activity <b>0.82 or below</b>, <b>and</b> weight loss of <b>40% or more</b> from the recorded stuffed wet weight. Both must be met.', _hcell)],
  [Paragraph('Supporting limit', _hkey), Paragraph('Product temperature below 4&deg;C for the whole of the drying period.', _hcell)],
  [Paragraph('Monitoring', _hkey), Paragraph('Primary: water activity meter, read on a representative piece from the batch. Secondary: weight loss calculated against the stuffed wet weight recorded at S5. Frequency: weekly during drying, and both measures confirmed before the batch leaves drying. Responsibility: Robert.', _hcell)],
  [Paragraph('Records', _hkey), Paragraph('Production record in the app, carried into this nightly audit report.', _hcell)],
  [Paragraph('Corrective action', _hkey), Paragraph('If either limit is not met, <b>continue drying</b> and re-measure. The batch is not released from CCP 1 and does not proceed to S7 or S8 until both limits are met. Responsibility: Robert.', _hcell)],
  [Paragraph('Corrective action &mdash; mould', _hkey), Paragraph('Wash with vinegar as required and record. Excessive mould is investigated as an upstream handling issue at estate or processor level, not simply treated.', _hcell)],
  [Paragraph('Validation', _hkey), Paragraph('Water activity of 0.82 is the recognised threshold below which <i>Staphylococcus aureus</i> growth ceases and below which the product is shelf stable without refrigeration. The 40% weight loss figure is the practical, physically measurable corroboration of that reduction.', _hcell)],
]
story.append(_hac_table(_ssum, [45*mm, 222*mm]))

_hac_sec('9 &nbsp; Prerequisite Programmes This Plan Relies On')
for _pq in [
  'Meat Intake HACCP plan, in force from 26 July 2026.',
  'Equipment clean-down SOPs for the mincer and the stuffer, each recorded per use.',
  'Daily opening and closing checks, including chiller and drying room temperatures.',
  'Pest control programme, including bait stations and insectocutor.',
  'Personal hygiene and staff training.',
  'Traceability: every contributing batch code recorded on the production record at mince; the batch code carried on internal hanging labels and on every customer label.',
  'Allergen control: recipes stored centrally, allergens declared in bold on customer labels, FSA allergen chart used in the manual paperwork.',
  'Cleaning water supply and environmental monitoring.',
]:
    story.append(Paragraph('&bull;&nbsp;&nbsp;' + _pq, _hb))

_last = []
_last.append(Paragraph('10 &nbsp; Notes for the Next Revision', _hh))
_last.append(HRFlowable(width='100%', thickness=0.8, color=GOLD, spaceAfter=5))
_last.append(Paragraph('<b>pH measurement.</b> Flora Italiana is added at S3 and is expected to lower pH through the cure, giving a hurdle against <i>Listeria monocytogenes</i>. pH is not currently measured. Introducing a reading at hang and at release would turn a described effect into an evidenced hurdle.', _hb))
_last.append(Paragraph('<b>Salt trial.</b> Batch 202627-20 was made at 2.3% salt rather than the 2.5% stated in this plan, and is under review at six weeks. If 2.3% is adopted, section 2 and the S2 hazard analysis must both be updated and the change justified.', _hb))
_last.append(Paragraph('<b>Positive release.</b> S9 is not applicable while all product goes to estate members for their own consumption. It becomes a requirement before any client resells to the public.', _hb))
_last.append(Spacer(1, 10))
_last.append(Paragraph('Prepared and signed off by: Robert Fry &nbsp;&nbsp;&middot;&nbsp;&nbsp; Date ' + report_date + ' &nbsp;&nbsp;&middot;&nbsp;&nbsp; Next review: ' + _review_date,
    ParagraphStyle('hac_sign2', fontName=SERIF, fontSize=9.5, textColor=INK)))
# keep the closing notes and the sign-off on one page so the signature is never orphaned
story.append(KeepTogether(_last))

# (no trailing PageBreak — add_section('Production Records') supplies the page break)
# ── END SALAMI HACCP PLAN ─────────────────────────────────────────────────────

# ── PRODUCTION SECTION ────────────────────────────────────────────────────────
_log(f"Building Production section ({len(production_records)} records)")
add_section('Production Records',
    'Each production run from a batch: the children (divisions) it was split into, each child\'s recipe with ingredient amounts and the date each was added, and the day-by-day stages worked (defrost, fat calc, recipe, mince, stuffing). This is the full make-record for traceability.')

if production_records:
    _prod_first = True
    for rec in sorted(production_records, key=lambda x: (x.get('startDate') or x.get('processCode') or ''), reverse=True):
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
                        irows.append([Paragraph(clean(ln.get('name','')), cell_style),
                                      Paragraph(amt_str, cell_style),
                                      Paragraph(added, cell_style)])
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
                elif stage == 'mix':
                    cl = st_rec.get('childLetter')
                    ch = next((c for c in (rec.get('children',[]) or []) if c.get('letter') == cl), None) if cl else None
                    nm = (ch.get('recipe') or {}).get('name') if ch else None
                    who = nm if nm else (f"Child {cl}" if cl else 'child')
                    detail = f"{who} · " + ('blend mixed into meat' if st_rec.get('mixDone') else 'blend not yet mixed')
                    skipped = [o.get('text','') for o in (st_rec.get('opening',[]) or []) if not o.get('done')]
                    skipped += [o.get('text','') for o in (st_rec.get('closing',[]) or []) if not o.get('done')]
                    if skipped: detail += ' · SKIPPED: ' + '; '.join(skipped)
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
                stage_label = {'fatcalc':'Fat Calculator','saltcalc':'Salt Calculator','stuff_hang':'Stuffing & Hanging','mince':'Mince Day','mix':'Mix into Meat','defrost':'Defrost'}.get(stage, stage.replace('_',' ').title())
                detail_cell = Paragraph(clean(detail), ParagraphStyle('sdc', fontSize=7, leading=9)) if detail else ''
                notes_cell = Paragraph(clean(st_rec.get('notes','')), ParagraphStyle('snc', fontSize=7, leading=9)) if st_rec.get('notes') else ''
                srows.append([dstr, stage_label, detail_cell, notes_cell])
            pt = Table(srows, colWidths=[22*mm, 32*mm, 60*mm, 110*mm], repeatRows=1)
            pt.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), SAGE[0]), ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD), ('TEXTCOLOR', (0,0), (-1,0), GREEN), ('FONTNAME', (0,0), (-1,0), SERIFB), ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8), ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]), ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4), ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
            story.append(pt)
else:
    story.append(Paragraph('No production runs recorded yet.', small))

# ── HACCP PLANS: VENISON ──────────────────────────────────────────────────────
# Added 06/09/2026. Sits at the START of the venison section, ahead of the
# breakdown records, mirroring how the intake plan sits ahead of intake records.
# Source of truth is ref_haccp_venison_plan in Supabase; VENISON_HACCP_MD below
# is a copy of it. If the Supabase record changes, change this too.
import re as _re

VENISON_HACCP_MD = r"""**Private estate kill for the estate's own use. Not for sale to the public.**

**Legal basis.** Assimilated Regulation (EC) 852/2004 Article 5 - procedures based on the seven HACCP principles, proportionate to the business. Assimilated Regulation (EC) 853/2004 Annex III Section IV - wild game meat: Chapter II paragraph 5 sets the chilling limit for large wild game at not more than 7 °C throughout the meat; the 4 °C figure applies to small wild game under Chapter III paragraph 4.

## 0. How this document is organised

Four documents in one file, because the process is one process and splitting it would repeat the same prerequisites four times.

| Part | Covers | Critical control points |
|---|---|---|
| **Plan A** | Venison intake and breakdown — collection, transport, primals, boning, trim segregation, private-use diversion | CCP 1 — temperature at receipt |
| **Plan B** | Whole muscle — prosciutto, cured loin, fillet, bresaola | CCP 2 — air dry to shelf stability |
| **Plan C** | Pastrami — cooked, chilled, sliced, frozen | CCP 3 — cook · CCP 4 — chill |
| **Annex S** | Venison entering the existing salami plan | Salami CCP 1, unchanged |

Every product made from venison starts in Plan A and then follows exactly one of B, C or Annex S. The process flow diagram (the venison process flow diagram held with this plan) is the single-page picture of the same thing and forms part of this document.

**Four products, four plans, no fourth salami plan.** Venison salami is the same process as pheasant salami with a different species and a different intake route. It is handled as an annex to the salami plan rather than a separate plan, which is the proportionate approach under 852/2004 Article 5.

---

# PLAN A — Venison intake and breakdown

## A1. Scope

Covers wild deer — fallow, muntjac, roe, red — from the point of collection from the estate larder to the point at which boned, trimmed, segregated meat enters Plan B, Plan C or Annex S.

Begins: at the estate chilled larder, at collection.
Ends: at boned primals and segregated trim, held at 3 °C, ready for salting.

**Not covered:** the killing, gralloching and initial examination, which are the responsibility of the estate's trained person; and everything downstream of boning, which is Plan B, Plan C or Annex S.

## A2. Product description

| | |
|---|---|
| Species | Wild fallow, muntjac, roe and red deer — large wild game |
| Source | Shooting estates, own kill, examined by the estate's trained person |
| Condition at collection | Whole carcass, skinned, gralloched, hung in a chilled insect-proof larder |
| Transport | Chill box with frozen 2 L water bottles, Robert's own vehicle |
| Primals produced | Forequarter (shoulders, neck, ribs), saddle (loins, fillets), haunch (legs, rump) |
| Trim | Taken from all three primals, segregated for salami |
| Waste | All venison fat, bone, neck trauma meat and rejected meat |
| Holding after breakdown | 3 °C, working temperature below 4 °C throughout |

## A3. Intended use and consumers

Venison entering this plan becomes ready-to-eat cured product supplied to shooting estates for consumption by estate members and their guests, and to private individuals. It is not currently placed on the open retail market.

Roughly 99 per cent of venison handled is estate own-kill returning to the estate that shot it. Any public-facing sale route runs via a game dealer or via Vicars Game, not direct.

**Vulnerable groups:** the product is not marketed to infants, the elderly, pregnant women or the immunocompromised, but as a ready-to-eat cured product it may be eaten by them. Controls are set accordingly.

## A4. The three things an inspector will ask first

**1. Where does the carcass come from, and who declared it fit?**
Estate own-kill, examined in the field by the estate's trained person under 853/2004 Annex III Section IV Chapter I. A numbered trained person's declaration must accompany the body, giving date, time and place of killing. No declaration, no acceptance — that is the first line of the intake record.

**2. Is the legal route into UK 2820 settled?**
**No, and this plan says so plainly.** Estate own-kill venison entering an approved establishment, processed, and returned to the estate that supplied it, is a scope question that has to be answered by the EHO in writing. It is open, it is recorded in `ref_haccp_gap_analysis`, and this plan is drafted on the assumption that it will be resolved before any product covered by it is sold rather than returned. Nothing in Plans A to C depends on the answer; the scope line does.

**3. Why 7 °C at the larder and not 4 °C?**
7 °C is the legal chilling figure for large wild game — 853/2004 Annex III Section IV Chapter II paragraph 5. The 4 °C figure belongs to small wild game, Chapter III paragraph 4, and applies to the pheasant and partridge side of this business, not to deer. Once the carcass is inside UK 2820 the house limit is tighter than the law: primals hang at 3 °C and work is done below 4 °C.

The **insect-proof enclosure** at the estate larder is a separate requirement from the temperature and is not satisfied by it. Flies are controlled by the enclosure; bacteria by the chill.

## A5. Process flow

| Step | Process | Temperature |
|---|---|---|
| A1 | Estate larder — carcass skinned, hung, insect-proof enclosure | 7 °C or below |
| **A2** | **Acceptance and loading — declaration checked, carcass temperature taken and recorded — CCP 1** | **7 °C or below throughout the meat** |
| A3 | Transport — chill box with frozen 2 L bottles, no heaping | 7 °C or below |
| A4 | Arrival at UK 2820 — temperature recorded again, carcass hung | 3 °C |
| A5 | Breakdown into primals — forequarter, saddle, haunch | below 4 °C |
| A6 | Boning — aitchbone out of the haunch, legs boned, shoulders boned and rolled | below 4 °C |
| A7 | Trim segregation — trim from all three primals to the salami route; venison fat, bone, damaged and rejected meat out | below 4 °C |
| A8 | Alcohol wash — every whole chunk washed in alcohol above 13 per cent ABV, then drained | below 4 °C |

Product then leaves this plan: whole muscle to **Plan B**, boned shoulders to **Plan C**, trim to **Annex S**.

## A6. Hazard analysis

Hazard types: **B** biological · **C** chemical · **P** physical.

| Step | Hazard | Significant? | Control measure | CCP? |
|---|---|---|---|---|
| A1 larder | **B** Growth of *Salmonella*, *E. coli*, *Listeria* on the carcass surface if the larder is warm or overloaded | Yes | Estate larder held at 7 °C or below. Temperature seen and recorded at collection. Carcass rejected if warm. | Controlled at A2 |
| A1 | **B/P** Fly strike, larvae, contamination by vermin | Yes | Insect-proof enclosure at the larder — a condition of collection, checked on every visit and recorded | No — prerequisite (supplier condition) |
| A1 | **B** Poor field hygiene — gut spillage, delayed gralloch | Yes | Trained person's declaration required. Gralloch and skin condition inspected at collection; carcass rejected if contaminated. | No — prerequisite (supplier standard) |
| A1 | **C** Taint from tobacco smoke during gutting, skinning or de-heading | Yes (quality and taint) | Standing collection briefing to estates: no smoking while handling the carcass | No — supplier briefing |
| **A2 acceptance** | **B Growth and toxin formation, principally *Staphylococcus aureus*, following temperature abuse before or during collection** | **Yes** | **Carcass temperature probed and recorded at acceptance. Legal limit 7 °C throughout the meat.** | **CCP 1 — see A8** |
| A2 | **B** Unfit animal accepted — disease, abnormal behaviour, environmental contamination | Yes | Numbered trained person's declaration checked and filed against the intake record. No declaration, no acceptance. | No — prerequisite (documented intake check) |
| A3 transport | **B** Temperature rise in transit | Yes | Insulated chill box with frozen 2 L bottles, no heaping, direct journey. Temperature recorded again on arrival at A4. | Covered by CCP 1 |
| A4 arrival | **B** Growth during hanging | Yes | Butchery fridge at 3 °C, monitored on the daily checks | No — prerequisite (chiller monitoring) |
| A5 breakdown | **B** Cross-contamination from hide, hair, saw or hands onto exposed muscle | Yes | Skinned carcass only; clean-down SOP for saw, knives and surfaces; hand hygiene; work below 4 °C | No — prerequisite (clean-down and hygiene SOPs) |
| A5 | **P** Bone dust and saw fragments in exposed meat | Yes | Visual inspection at boning; wiped surfaces; damaged surface trimmed away | No — prerequisite |
| A6 boning | **C** Lead residue around the wound channel from expanding ammunition | Yes | Wound channel and bruised tissue cut out generously and binned at A7, not trimmed into salami. Head-shot animals checked for neck trauma. | No — corrective at A7 |
| A6 | **P** Shot, bullet fragment, hair, bone chip | Yes | Visual inspection of every piece at boning; damaged tissue removed | No — prerequisite |
| A7 segregation | **B** Damaged, bloodshot or rejected meat entering product | Yes | Bones, neck trauma meat, rejected meat and all venison fat diverted to the private-use freezer. Never into product. Diversion is recorded. | No — segregation control |
| A7 | **B** Trim held warm while primals are worked | Yes | Trim boxed and returned to 3 °C between primals; work below 4 °C | No — prerequisite |
| A8 alcohol wash | **B** Surface bacteria carried forward into the cure or the mince | Yes | Wash in alcohol above 13 per cent ABV, minimum 60 seconds contact, applied before mincing or salting, then drained | No — hurdle, not a kill step. See A7.1 |
| A8 | **C** Alcohol residue as an undeclared ingredient | No | Processing aid, drained off, not an ingredient of the finished product | No |
| all | **B** *Trichinella* | No | Not a hazard in deer. Applies to wild boar and other porcine species, which this establishment does not handle. | No |
| all | **B** Chronic wasting disease / TSE in cervids | Not currently | Not present in GB deer. Monitored nationally. Any suspicion is an APHA notification, not a HACCP control. | No |

### A6.1 Why the alcohol wash is not a CCP

Identical reasoning to the salami plan. The wash reduces load on the **surface of intact pieces**, cannot reach organisms inside the muscle, and does nothing to spores. It is applied before mincing precisely because mincing would spread surface organisms through the batch where a surface treatment can no longer reach them. Real hurdle, monitored at 60 seconds, **not claimed as a kill step**.

## A7. CCP determination

Questions applied at each step: (Q1) is a control measure in place? (Q2) is this step specifically designed to eliminate or reduce the hazard to an acceptable level? (Q3) could contamination occur or increase to unacceptable levels here? (Q4) will a later step eliminate or reduce it to an acceptable level?

| Step | Q1 | Q2 | Q3 | Q4 | Outcome |
|---|---|---|---|---|---|
| A1 larder | Yes | No | Yes | No — see below | Supplier condition, evidenced at A2 |
| **A2 acceptance** | **Yes** | **Yes — this is the step that rejects abused meat** | **Yes** | **No — no later step destroys pre-formed toxin** | **CCP 1** |
| A3 transport | Yes | No | Yes | Yes — recorded again at A4 | Not a CCP |
| A4 arrival | Yes | No | Yes | Yes — B, C or Annex S | Not a CCP, prerequisite |
| A5 breakdown | Yes | No | Yes | Yes — B, C or Annex S | Not a CCP, prerequisite |
| A6 boning | Yes | No | Yes | Partly — physical hazards removed here | Not a CCP, prerequisite |
| A7 segregation | Yes | No | Yes | No | Not a CCP, segregation control |
| A8 alcohol wash | Yes | No — reduces, does not eliminate | Yes | Yes — B, C or Annex S | Not a CCP |

**Why acceptance is a CCP and not simply a prerequisite.** Drying (CCP 2) and cooking (CCP 3) both act on live organisms. Neither destroys *Staphylococcus aureus* enterotoxin, which is heat-stable and survives both. If a carcass has been held warm long enough for toxin to form, no later step in any of these plans will make it safe. Acceptance is therefore the last point at which that hazard can be controlled, which is exactly the test in Q4.

## A8. CCP summary

**CCP 1 — A2, temperature at acceptance**

| | |
|---|---|
| **Hazard** | Growth of vegetative pathogens and formation of heat-stable *Staphylococcus aureus* enterotoxin following temperature abuse at the estate or in transit |
| **Critical limit** | **7 °C or below throughout the meat**, measured at the thickest part of the haunch |
| **Second limit** | Numbered trained person's declaration present, complete and matching the carcass |
| **Monitoring** | Probe thermometer into the deepest muscle of the haunch, every carcass, at acceptance. Reading written on the intake record with the estate, date and batch code. |
| **Second reading** | Repeated on arrival at UK 2820 and recorded, as evidence the cold chain held in transit |
| **Frequency** | Every carcass, every collection |
| **Who** | Robert Fry |
| **Records** | Intake record in the FSA app, carried into the nightly audit PDF |
| **Corrective action — above 7 °C** | Do not load. If discovered on arrival, quarantine and reject. Rejected carcasses go to the private-use freezer route or to disposal — **never into product** — and the rejection is recorded with the reason. |
| **Corrective action — no declaration** | Do not accept. Contact the estate. Meat may not be accepted retrospectively on a declaration written after the event. |
| **Corrective action — repeated failure** | Estate larder standard reviewed with the estate before the next collection. Two failures from one estate in a season triggers a written condition of supply. |
| **Verification** | Probe accuracy checked against iced water and boiling water at a stated interval; intake records reviewed against production records at each seasonal audit |
| **Validation** | 7 °C is the statutory chilling limit for large wild game under 853/2004 Annex III Section IV Chapter II paragraph 5. *S. aureus* growth and toxin production are negligible below 7 °C on intact muscle. |

---

# PLAN B — Whole muscle: prosciutto, cured loin, fillet, bresaola

## B1. Scope

Dried, uncooked, whole-muscle cured meat: venison prosciutto from the boned haunch, cured loin and fillet from the saddle, and beef bresaola.

Begins at boned whole muscle leaving Plan A. Ends at vac-packed finished product held in ambient storage awaiting delivery.

**Scope note — bresaola.** Bresaola is made from farmed beef, not wild game. The premises approval is for wild game. The process below is identical, but the scope question is open with the EHO and is listed in section B10. Do not read this plan as an assertion that farmed beef is within approval.

## B2. Product description

| | |
|---|---|
| Product | Dried, uncooked, whole-muscle cured meat, ready to eat |
| Meat | Wild deer (haunch, loin, fillet); farmed beef for bresaola |
| Curing agent | **Salt only** |
| Salt rate | **2.5 per cent** of the raw weight into cure — confirmed 6 September 2026 |
| **Nitrates / nitrites** | **None used** |
| Starter culture | **None.** Flora Italia is used in salami only, never in whole muscle. |
| Alcohol | Above 13 per cent ABV, applied at A8 as a processing aid, drained off, not an ingredient |
| Casing | Prosciutto: collagen wrap and net. Loin and fillet: bare on the rack. |
| Press | Prosciutto only — spring form press, 5 days at 3 °C |
| Shelf stability | Water activity reduction, target 40 per cent weight loss |
| Packaging | Vacuum packed |
| Storage after packing | Ambient, below 18 °C |

## B3. Intended use and consumers

As Plan A section A3. Ready to eat without cooking.

## B4. The thing an inspector will ask first

**Salt only, no nitrate, no starter culture, months on the rack — what is actually keeping this safe?**

One answer: **water activity**. Salt at 2.5 per cent draws water out during the press or vac stage; air drying takes the rest out until the product will not support pathogen growth. Whole muscle has one advantage over salami — it is intact. Contamination is on the surface, not distributed through the mass, and the surface is the part that dries first and hardest.

Two things this plan does **not** claim:

- It does not claim the alcohol wash is a kill step. It is a surface hurdle. See A6.1.
- It does not claim a pH hurdle. There is no starter culture in whole muscle, so there is no acidification and none is claimed.

**Cold, not ambient.** Whole muscle is dried in the same chilled conditions as the salami. The reasoning in the salami plan applies with less force here — deer are not poultry and carry a lower load than pheasant — but the same room is used and the same cold chain is recorded.

## B5. Process flow

| Step | Process | Temperature |
|---|---|---|
| B1 | Whole muscle received from Plan A, washed in alcohol above 13 per cent ABV, drained | below 4 °C |
| B2 | Salt at 2.5 per cent of raw weight, applied by hand, weight into cure recorded | below 4 °C |
| B3a | **Prosciutto:** spring form press, 5 days, drained | 3 °C |
| B3b | **Loin, fillet, bresaola:** vac packed, 5 days | 3 °C |
| B4 | Remove from press or vac, drain, collagen wrap and net (prosciutto only), tag with batch code | below 4 °C |
| **B5** | **Air dry on the rack until shelf stable — CCP 2** | **below 4 °C throughout** |
| B6 | Move to ambient storage | below 18 °C |
| B7 | Vac pack, customer label, store | ambient, below 18 °C |

Removal from press or vac is 5 days from the salting date. Weight into cure is the baseline the 40 per cent loss is measured from and must be recorded at B2.

## B6. Hazard analysis

| Step | Hazard | Significant? | Control measure | CCP? |
|---|---|---|---|---|
| B1 receive and wash | **B** Surface bacteria on the muscle | Yes | Alcohol wash above 13 per cent ABV, 60 seconds minimum, drained. Hurdle, not a kill step. | No — see A6.1 |
| B2 salt | **C** Under-salting weakens the cure and slows water loss | Yes | Salt calculated as a percentage of the weighed raw piece by the app; both weights recorded on the production record | No — recipe control, verified at CCP 2 |
| B2 | **B** Growth during salting and handling | Yes | Work below 4 °C, minimal handling time | No — prerequisite |
| B3a press | **B** Growth in the press; brine pooling around the meat | Yes | Held at 3 °C. Drained at B4. Spring form press cleaned down per SOP between batches. | No — prerequisite |
| B3b vac | **B** Anaerobic growth in a vac pack, principally *Clostridium botulinum* | Yes | 3 °C for 5 days only. Salt present from B2. Chilled, short, and followed by drying. Vac is a curing stage, not storage. | No — controlled by time, temperature and salt |
| B4 wrap and net | **B/P** Contamination from wrap, net, hands or bench | Yes | Clean handling per SOP; single-use collagen wrap; batch code tag applied here | No — prerequisite |
| **B5 air dry** | **B Survival or growth of *Salmonella*, *Listeria monocytogenes*, *E. coli*, *Staph. aureus* in a ready-to-eat product cured without nitrate** | **Yes** | **Cold chain below 4 °C throughout drying, plus reduction of water activity to the point the product will not support growth** | **CCP 2** |
| B5 | **B** Surface mould during drying | Yes | Wipe with vinegar as required and record. Excessive mould is investigated as an upstream handling problem, not simply wiped off. | No — corrective action |
| B5 | **B** Case hardening — the outside dries and seals, the centre stays wet, so weight loss reads correct while the core is not stable | Yes | Drying progress tracked from stuffing or from weight into cure, not estimated. Test-string method applied per batch. Water activity read on a cut face, not on the crust. | Covered by CCP 2 |
| B6 ambient | **quality** Fat softening, oiliness | No — quality | Limit 18 °C | No |
| B7 pack and label | **C** Incorrect allergen or species declaration reaching the customer | Yes | Customer label generated from the stored recipe; allergens in bold; batch code on every label | No — prerequisite (labelling control) |

## B7. CCP determination

| Step | Q1 | Q2 | Q3 | Q4 | Outcome |
|---|---|---|---|---|---|
| B1 wash | Yes | No — reduces, does not eliminate | Yes | Yes — B5 | Not a CCP |
| B2 salt | Yes | No | No | Yes — B5 | Not a CCP, recipe control |
| B3a/b press or vac | Yes | No | Yes | Yes — B5 | Not a CCP, prerequisite |
| B4 wrap | Yes | No | Yes | Yes — B5 | Not a CCP, prerequisite |
| **B5 air dry** | **Yes** | **Yes** | **Yes** | **No later step exists** | **CCP 2** |
| B6 ambient | Yes | No | No | — | Not a CCP |
| B7 pack | Yes | No | No | — | Not a CCP, prerequisite |

## B8. CCP summary

**CCP 2 — B5, air drying to shelf stability**

| | |
|---|---|
| **Hazard** | Survival or growth of vegetative pathogens in a ready-to-eat, uncooked, whole-muscle product cured with salt alone |
| **Critical limit** | Water activity **0.82 or below**, **and** weight loss of **40 per cent or more** from the recorded weight into cure. Both must be met. |
| **Supporting limit** | Product temperature below 4 °C throughout drying |
| **Primary monitoring** | Water activity meter, read on a cut face of a representative piece — not on the crust |
| **Secondary monitoring** | Weight loss against the weight into cure recorded at B2. Weighed weekly. Test-string method used to track drying rate through the batch. |
| **Frequency** | Weekly during drying; both measures confirmed before the batch leaves the rack |
| **Who** | Robert Fry |
| **Records** | Production record in the FSA app, carried into the nightly audit PDF |
| **Corrective action** | Either limit not met: **keep drying**, re-measure. The batch does not proceed to B6 or B7 until both limits are met. |
| **Corrective action — over-dried** | A quality fault, not a safety fault. Recorded in the quality register and reviewed; product remains safe. |
| **Corrective action — mould** | Wipe with vinegar and record. Recurrent mould is investigated upstream. |
| **Verification** | Laboratory testing of finished batches — salt and water activity on the same samples. Water activity meter calibration checked per manufacturer's instruction. |
| **Validation** | 0.82 is the recognised threshold below which *Staphylococcus aureus* growth ceases and below which the product is shelf stable without refrigeration. 40 per cent weight loss is the physically measurable corroboration. **Validation is incomplete until the lab panel returns salt and water activity together — see the appendix.** |

### B8.1 What the 40 per cent actually means

**40 per cent weight loss is not the same thing as water activity 0.82, and this plan does not treat it as if it were.**

Water activity is the free water available to bacteria, on a scale where pure water is 1.00. Weight loss is how much water has left the piece. They move together, but there is no fixed conversion between them, because water activity depends on what is dissolved in the water that remains as well as on how much remains. At the same 40 per cent loss:

- a piece salted at 2.5 per cent finishes with roughly 4.2 per cent salt in the remaining mass, which drags water activity down further than the drying alone would
- a fatty piece loses less water for the same weight loss, because fat carries almost no water — so weight loss overstates the drying
- an unevenly dried or case-hardened piece can read 40 per cent overall while the core is still well above 0.82

Published figures for salt-cured, air-dried whole muscle put 0.82 somewhere in the region of 30 to 40 per cent loss at these salt levels — which is why 40 per cent is a **conservative** working figure rather than an equivalence. Typical values for orientation only: 0.90 to 0.92 around 20 per cent loss, 0.85 to 0.88 around 30 per cent, 0.80 to 0.84 around 40 per cent. These are indications, not limits, and no batch is released on them.

**How the two limits are used, therefore:**

| | |
|---|---|
| Water activity 0.82 | The **safety limit**. It is the thing that makes the product shelf stable, and it is measured directly. |
| 40 per cent weight loss | The **operating check**. It is measured every week, on every batch, at no cost, and it tells Robert whether drying is on track long before a meter is worth reaching for. |

A batch that has hit 40 per cent but reads above 0.82 keeps drying. A batch that reads 0.82 but has lost less than 40 per cent is measured again on a fresh cut face before anything is released — that pattern is the signature of case hardening, not of an early finish.

**The pairing is what makes the plan defensible.** The lab panel now requested — salt, water activity and pH on the same samples — is what turns the relationship between the two from a reasonable assumption into this establishment's own evidence, for these products at these salt rates.

---

# PLAN C — Pastrami

## C1. Scope

Cooked, chilled, sliced, vac-packed and frozen pastrami from boned and rolled venison shoulder.

Begins at boned shoulders leaving Plan A. Ends at sliced product in frozen storage.

**This is the only cooked product in the business, and it has the only kill step.** Everything else on these pages is controlled by drying. Pastrami is controlled by heat, and then by how fast it comes back down.

## C2. Product description

| | |
|---|---|
| Product | Cooked, cured, sliced meat — ready to eat |
| Meat | Venison shoulder, boned and rolled |
| Salt rate | 2.2 per cent |
| Seasoning | Cracked black pepper, coriander seed |
| **Nitrates / nitrites** | **None used** — the product is grey-brown, not pink, by design |
| Cook | Water boiler with digital control, 3 hours timed from the unit reaching 82 °C from cold |
| Chill | Straight from the boiler into an ice plunge bath, then 3 °C fridge overnight |
| Packaging | Sliced, vacuum packed |
| Storage | Frozen |

## C3. Intended use and consumers

Ready to eat without further cooking, from frozen storage, thawed by the client. As Plan A section A3.

## C4. The thing an inspector will ask first

**Two questions, and they are the two CCPs.**

**Did it get hot enough?** The boiler is set at 82 °C and the 3 hours is timed from the moment the unit reaches 82 °C from cold — not from when the meat went in. That is a conservative way to run it, but the plan cannot claim a core temperature that has never been measured. **The one-off validation probe has not yet been done.** Until it is, CCP 3 has a limit and a monitoring method but no validation record, and this plan says so rather than implying otherwise.

**Did it cool fast enough?** A cooked, nitrate-free meat is a growth medium. The hazard after cooking is spore-forming organisms — *Clostridium perfringens* and *Bacillus cereus* — which survive the cook and germinate in the danger zone as the product cools. That is why the pastrami now goes straight from the boiler into an ice plunge rather than being left to cool. Frozen 2 L bottles are kept in stock for the purpose.

## C5. Process flow

| Step | Process | Temperature |
|---|---|---|
| C1 | Shoulders received from Plan A, boned and rolled, alcohol washed | below 4 °C |
| C2 | Salt at 2.2 per cent, cracked black pepper and coriander seed applied | below 4 °C |
| C3 | Cure, vac packed | 3 °C |
| **C4** | **Cook — water boiler, 3 hours timed from the unit reaching 82 °C from cold — CCP 3** | **82 °C water** |
| **C5** | **Chill — straight into ice plunge bath, then 3 °C fridge overnight — CCP 4** | **63 °C to below 8 °C within 90 minutes** |
| C6 | Slice | below 4 °C |
| C7 | Vac pack, label, freeze | frozen |

## C6. Hazard analysis

| Step | Hazard | Significant? | Control measure | CCP? |
|---|---|---|---|---|
| C1 receive | **B** Surface bacteria | Yes | Alcohol wash; work below 4 °C | Covered by CCP 3 |
| C2 salt and season | **C** Under-salting | Yes | Salt calculated on the weighed piece, recorded | No — recipe control |
| C2 | **C** Undeclared allergen — mustard, spice blends | Yes | Recipe held in the app, ingredients ticked individually, allergens bold on the label | No — prerequisite (labelling) |
| C3 cure | **B** Growth during the cure | Yes | 3 °C, vac packed, time limited | No — prerequisite |
| **C4 cook** | **B Survival of *Salmonella*, *Listeria monocytogenes*, *E. coli* including O157** | **Yes** | **Time and temperature in the boiler, digitally controlled, 3 hours from 82 °C** | **CCP 3** |
| C4 | **B** *Staph. aureus* enterotoxin already present before cooking | Yes | Heat-stable — not destroyed here. Controlled upstream by CCP 1. | Covered by CCP 1 |
| **C5 chill** | **B Germination and growth of *Clostridium perfringens* and *Bacillus cereus* spores surviving the cook, during cooling** | **Yes** | **Ice plunge immediately out of the boiler, then 3 °C fridge** | **CCP 4** |
| C6 slice | **B** Recontamination of a cooked ready-to-eat product with *Listeria monocytogenes* from the slicer, board or hands | Yes | Slicer and boards cleaned and disinfected before use on cooked product; cooked product never handled on a surface used for raw that shift; product below 4 °C while sliced | No — prerequisite, but the highest-consequence prerequisite in this plan. See C6.1 |
| C7 pack and freeze | **B** Growth between slicing and freezing | Yes | Packed and into the freezer without delay | No — prerequisite |
| C7 | **C** Wrong label, wrong batch, missing allergen | Yes | Label generated from the stored recipe with the batch code | No — prerequisite |

### C6.1 The raw-to-cooked boundary

Everything before C4 is raw. Everything after it is ready to eat, cooked, and has no further kill step. Cross-contamination at C6 puts *Listeria* into a product that will be eaten without cooking.

This is handled as a prerequisite because it is a hygiene and separation control rather than a measurable limit, but it carries the same weight as a CCP in practice: **cooked pastrami is never sliced on a surface, board or slicer that has handled raw meat that day without a full clean-down and disinfection first, and the clean-down is recorded.**

## C7. CCP determination

| Step | Q1 | Q2 | Q3 | Q4 | Outcome |
|---|---|---|---|---|---|
| C1 receive | Yes | No | Yes | Yes — C4 | Not a CCP |
| C2 salt | Yes | No | No | Yes — C4 | Not a CCP |
| C3 cure | Yes | No | Yes | Yes — C4 | Not a CCP |
| **C4 cook** | **Yes** | **Yes** | **Yes** | **No** | **CCP 3** |
| **C5 chill** | **Yes** | **Yes** | **Yes** | **No** | **CCP 4** |
| C6 slice | Yes | No | Yes | **No later step** | Not a CCP — prerequisite, see C6.1 |
| C7 pack and freeze | Yes | No | Yes | No | Not a CCP, prerequisite |

## C8. CCP summary

**CCP 3 — C4, cook**

| | |
|---|---|
| **Hazard** | Survival of vegetative pathogens in a ready-to-eat cooked product |
| **Critical limit** | Core temperature of the thickest piece reaching **70 °C held for 2 minutes**, or an equivalent time-temperature combination |
| **Operating limit** | Boiler at 82 °C, 3 hours timed from the unit reaching 82 °C from cold |
| **Monitoring** | Boiler digital controller — temperature and start time recorded on the production record every cook |
| **Verification probe** | Calibrated probe into the core of the thickest piece at the end of the cook |
| **Frequency** | Every cook |
| **Who** | Robert Fry |
| **Records** | Production record in the FSA app |
| **Corrective action** | Limit not reached: continue cooking and re-probe. Product is not removed from the boiler until the core limit is met. If the boiler fails mid-cook, the batch is chilled immediately and treated as raw; it is not held warm while the fault is investigated. |
| **Verification** | Probe calibration checked against iced water and boiling water at a stated interval |
| **Validation** | **NOT YET DONE.** 70 °C for 2 minutes is the standard UK cooking equivalence for ready-to-eat meat. The one-off core probe at the end of a 3-hour cook has not been carried out, so the operating limit of 3 hours at 82 °C is not yet evidenced as delivering it. Until that probe is done and recorded, CCP 3 is monitored but unvalidated. |

**CCP 4 — C5, chill**

| | |
|---|---|
| **Hazard** | Germination and growth of *Clostridium perfringens* and *Bacillus cereus* spores surviving the cook |
| **Critical limit** | **63 °C down to below 8 °C within 90 minutes**, then to 3 °C in the fridge |
| **Method** | Straight out of the boiler into an ice plunge bath. Frozen 2 L bottles held in stock at all times so the bath can always be made. |
| **Monitoring** | Probe reading of a pack at the start of the plunge and at 90 minutes, recorded |
| **Frequency** | Every cook |
| **Who** | Robert Fry |
| **Records** | Production record in the FSA app |
| **Corrective action** | Above 8 °C at 90 minutes: add ice, re-probe. If the product has been between 8 °C and 63 °C for more than 4 hours in total, it is not fit for sale as a ready-to-eat product. Recorded and diverted. |
| **Verification** | Chill curve re-probed at 30, 60 and 90 minutes annually and after any change to bath size or batch size |
| **Validation** | **NOT YET DONE.** The 30 / 60 / 90 minute probe run that establishes the chill curve for this bath and this batch size has not been carried out. Until it is, CCP 4 is monitored but unvalidated. |

---

# ANNEX S — Venison in the salami plan

Venison salami is made by the process already documented in the Pheasant / Partridge Salami HACCP plan. This annex records only where venison differs. **No separate salami plan exists or is needed.**

| | Pheasant / partridge salami | Venison salami |
|---|---|---|
| Intake route | Processor — Vicars Game, Oaklands Park, Willo, Lincolnshire Game | Estate own-kill, via **Plan A** of this document |
| Species hazard | Poultry-type load — *Salmonella*, *Campylobacter* — high | Deer — lower surface load, no poultry-specific organisms |
| Meat entering the mince | Whole birds broken down | **Trim from all three primals** (Plan A step A7), washed and drained |
| Fat | Organic pork fat | Organic pork fat — **venison fat is never used, it is always waste** |
| Salt | Per salami plan | **2.2 per cent of the wet mix** from 4 September 2026 |
| Starter culture | Flora Italia | Flora Italia — unchanged |
| CCP | CCP 1, air dry, water activity 0.82 or below and 40 per cent loss | **Identical** |
| Finished salt | Per salami plan | 3.7 per cent at 40 per cent loss |

**Consequences for the salami plan.** Two amendments are needed to it, both already open:

1. **Salt.** The plan text says 2.5 per cent. The season rule is 2.2 per cent and the recipe library was updated on 5 September 2026. Section 2 and the S2 hazard analysis of the salami plan are out of date. Task `gt_haccp_salt_update` is open.
2. **Species and scope.** Section 1 of the salami plan scopes it to wild game birds. It needs a line admitting venison, referring to this annex and to Plan A for the intake route.

**pH.** The salami plan describes the Flora Italia starter as a supporting hurdle that could not be relied on because pH was never measured. pH has now been added to the standing lab panel, on the salami samples delivered in the week of 31 August 2026. When those results come back, that section of the salami plan can be rewritten from a described effect to an evidenced hurdle.

Neither amendment changes CCP 1 of the salami plan. Water activity and weight loss are unaffected by species or by a 0.3 per cent salt change.

---

# APPENDIX 1 — Prerequisite programmes these plans rely on

- Meat intake HACCP plan, in force from 26 July 2026 (birds)
- Equipment clean-down SOPs — saw, knives, mincer, stuffer, slicer, spring form press, boiler, ice bath — each recorded per use
- Daily opening and closing checks, including chiller, drying room and freezer temperatures
- Pest control programme, including bait stations and insectocutor; insectocutor tubes and sticky boards replaced annually
- Personal hygiene and staff training, including the part-time helper
- Supplier conditions on estates: chilled insect-proof larder at 7 °C or below, trained person's declaration, no smoking during carcass handling
- Traceability: batch code from intake through production to every label; contributing batch codes recorded at mince
- Allergen control: recipes held centrally; allergens in bold on customer labels
- Water supply testing and environmental swabbing
- Waste and animal by-product route for bone, fat and rejected meat

# APPENDIX 2 — Verification and review

| Activity | Frequency | Status |
|---|---|---|
| Pastrami core temperature probe at end of cook | Once, then annually | **OUTSTANDING — CCP 3 unvalidated until done** |
| Pastrami chill probe at 30 / 60 / 90 minutes | Once, then annually | **OUTSTANDING — CCP 4 unvalidated until done** |
| Drained brine weighed on one prosciutto batch | Once | **OUTSTANDING — retained salt unknown until done** |
| Lab panel: salt, **water activity and pH** on the same samples | Requested | **IN HAND — salami samples delivered week of 31 August 2026, water activity and pH added to the standing panel. Results awaited.** |
| Batch laboratory testing | Minimum 2 batches | **OVERDUE** (carried from the salami plan) |
| Annual water supply test | Annually | **OVERDUE** |
| Environmental wall swab | Annually | **OVERDUE** |
| Probe and water activity meter calibration | Per manufacturer's instruction | To confirm |
| Plan review | Annually or on process change | Next: September 2027 |

**On the four outstanding validations.** They are one-off jobs, each takes under a day, and until they are done Plans B and C describe controls that are monitored but not evidenced. An inspector will accept a plan that says "not yet validated, here is the date it will be". An inspector will not accept a plan that states a validated limit that was never measured. Nothing in this document claims a measurement that has not been taken.

**Ask the lab which salt method it uses.** Mohr titration is unreliable on cured meat.

# APPENDIX 3 — Open items

1. **EHO scope — own-kill route.** Estate own-kill venison entering UK 2820 and returning to the supplying estate. Needs an answer in writing. Robert's call on when to raise it.
2. **EHO scope — farmed beef.** Bresaola from beef in a wild game approval. Same conversation.
3. **Salt meter.** Deferred until the lab panel returns.
4. **Nduja.** Not covered by any plan. Written once the first batch is finished and there is something to describe.
5. **Salami plan amendments.** Salt 2.2 per cent, and venison admitted to scope — see Annex S.
6. **Water activity on the rack.** Case hardening means a crust reading is not a core reading. Method for taking the reading on a cut face should be written into the drying SOP.

---

# APPENDIX 4 — Carcass handover declaration

**Private estate kill for the estate's own use. Not for sale to the public.**

**In force from Thursday 10 September 2026, first used on the Wilton Estate collection.**

## Why this sheet exists

While reviewing the HACCP for the coming 2026/27 season, and having only just started taking estate own-kill venison for the estate's own use, a number of unknowns were highlighted. Every plan in this document depends on work done at the estate before the carcass is ever seen: how quickly it was gralloched, whether the gut leaked, whether the larder was fly-proof and cold, whether anyone smoked while handling it. None of that was recorded. It was assumed.

The assumption is not unreasonable — but it is not evidence, and it is not a control. Gamekeepers and hunters are trained by their peers, and that training is built around game that will be **cooked**. Nothing made here is cooked. There is no kill step in prosciutto, cured loin, bresaola or salami, and pastrami is cooked long after the decisions that matter have already been made in the field.

So this establishment asks for its own declaration, in addition to the trained person's declaration required by law, and it is completed and signed at the point of collection.

## What the sheet says at the top

> Nothing we make from this carcass is cooked. There is no cooking step to make it safe. It is salted, dried and eaten as it is. That means we depend on you, in the field and in the larder, for the parts we cannot control. Tick each line honestly. A blank is not a problem — it tells us what to trim or reject. A wrong tick is.

## What is declared

| # | Declared by the hunter or gamekeeper | Why it is asked |
|---|---|---|
| 1 | Gralloched as soon as possible after the shot | The longer stomach and intestines stay in, the more gut bacteria move into the meat |
| 2 | No gut contents or bladder leaked onto the inside of the carcass | If it has, we can trim — but only if we know where |
| 3 | Carcass kept clear of droppings, soil, dogs and wildlife | Anything on the outside ends up on the meat when it is skinned |
| 4 | Head shot, or neck clear of blood trauma | Head shot preferred; a clean neck means the neck muscle can be used rather than binned |
| 5 | Shot damage and bruising limited, and noted if not | Bruised and bloodshot meat cannot be cured, and we need to know before cutting |
| 6 | Hung in a fly-proof larder or chiller, no flies on the carcass | One fly laying eggs in a wound ruins a carcass. The enclosure is a separate control from the chilling |
| 7 | Larder holding below 7 °C, temperature read and written on the sheet | The legal figure for large wild game. Warm meat grows bacteria that curing cannot remove |
| 8 | If skinned: skinned inside the fly-proof chiller or prep room | Not outside, not in the yard, not in a vehicle |
| 9 | Knives, saw, hooks and surfaces cleaned before and after | Cross-contamination from the previous carcass is invisible and travels |
| 10 | No smoking while gutting, de-heading, skinning or handling | Nicotine goes into the flesh and stays there |
| 11 | Nothing abnormal before or after the shot — behaviour, condition, viscera | If anything looked wrong the sheet is not signed; the estate rings Robert instead |
| 12 | Trained person's declaration completed and with the carcass | Legal requirement under 853/2004 Annex III Section IV, and the paperwork this establishment is audited on |

The sheet also records the carcass details (estate, species, sex, date and time of kill, tag number, larder temperature, shot placement), a free-text line for anything else the estate wants to tell us, and the signature and trained person number of whoever hands the carcass over.

## What this establishment records on the same sheet

Carcass temperature probed at collection, accepted or rejected, batch code, who collected it, signature and date. That is the monitoring record for **CCP 1**, and it now sits on the same piece of paper as the estate's own declaration.

## How it is used

| | |
|---|---|
| Frequency | One sheet per carcass, every collection, from 10 September 2026 |
| Completed by | The estate hunter or gamekeeper, at the larder, before loading |
| Countersigned by | Robert Fry, at collection |
| Held with | The intake record for that batch code |
| A blank or unticked line | Not a rejection in itself. It directs the inspection of the carcass and may lead to extra trimming, or to rejection under CCP 1 |
| Repeated failure from one estate | Reviewed with the estate before the next collection, per the CCP 1 corrective action |

**Status.** Paper form, version 1, in use from the Wilton Estate collection on 10 September 2026. It will be reviewed after the first few collections and reworded where lines are queried or left blank. It is not built into the app.

---

*Drafted 6 September 2026 from the process confirmed in `ref_haccp_venison_design`. Status: DRAFT. Not approved. Not in the nightly audit PDF.*
"""

_hh3 = ParagraphStyle('hac_h3', fontName=SERIFB, fontSize=10, textColor=GOLDLBL,
                      spaceBefore=9, spaceAfter=2, keepWithNext=1)
_hquote = ParagraphStyle('hac_quote', fontName=SERIF, fontSize=10, textColor=GREEN,
                         leading=14, spaceBefore=3, spaceAfter=5, leftIndent=10, rightIndent=10,
                         borderPadding=4)
_hbul = ParagraphStyle('hac_bul', fontName=SERIF, fontSize=10, textColor=INK,
                       leading=13.5, spaceAfter=3, leftIndent=8)

def _md_inline(t):
    t = t.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    t = _re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', t)
    t = _re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', t)
    t = _re.sub(r'`([^`]+)`', r'<font name="Courier">\1</font>', t)
    return t

def _md_table(block):
    """Markdown table -> reportlab table. An all-blank header row means a
    key/value table: no header band, first column rendered as the key."""
    rows = [[c.strip() for c in ln.strip().strip('|').split('|')] for ln in block]
    head, body = rows[0], rows[2:]
    keyval = not any(head)
    ncol = max(len(r) for r in rows)
    body = [r + [''] * (ncol - len(r)) for r in body]
    widths_raw = []
    for i in range(ncol):
        longest = max([len(r[i]) for r in body] + ([0] if keyval else [len(head[i]) if i < len(head) else 0]))
        widths_raw.append(max(longest, 6))
    total = float(sum(widths_raw))
    avail = 267.0
    widths = [max(16.0, avail * w / total) for w in widths_raw]
    scale = avail / sum(widths)
    widths = [w * scale * mm for w in widths]
    out = []
    if not keyval:
        out.append([Paragraph(_md_inline(c), _hhdr) for c in head])
    for r in body:
        cells = []
        for i, c in enumerate(r):
            style = _hkey if (keyval and i == 0) else _hcell
            cells.append(Paragraph(_md_inline(c), style))
        out.append(cells)
    # Header tables split cleanly because the header row repeats. Long key/value
    # tables have no header, so a page break can strand a single row on an empty
    # page - they are chunked and each chunk kept together instead.
    if keyval and len(out) > 8:
        parts = []
        for j in range(0, len(out), 5):
            parts.append(KeepTogether(_hac_table(out[j:j+5], widths, header=False)))
        return parts
    return [_hac_table(out, widths, header=not keyval)]

def _md_flow(md):
    """Render the plan markdown into the story. Supports # ## ### headings,
    paragraphs, bullet and numbered lists, and pipe tables."""
    lines = md.split('\n')
    i = 0
    first_title = False   # every '# ' plan heading starts a fresh page
    while i < len(lines):
        ln = lines[i].rstrip()
        if not ln.strip() or ln.strip() == '---':
            i += 1
            continue
        if ln.startswith('|'):
            block = []
            while i < len(lines) and lines[i].lstrip().startswith('|'):
                block.append(lines[i])
                i += 1
            if len(block) >= 2:
                for _fl in _md_table(block):
                    story.append(_fl)
                story.append(Spacer(1, 4))
            continue
        if ln.startswith('### '):
            story.append(Paragraph(_md_inline(ln[4:]), _hh3))
        elif ln.startswith('## '):
            _hac_sec(_md_inline(ln[3:]))
        elif ln.startswith('# '):
            if not first_title:
                story.append(PageBreak())
            first_title = False
            story.append(Paragraph(_md_inline(ln[2:]), _ht))
            story.append(HRFlowable(width='28%', thickness=1, color=GOLD,
                                    spaceAfter=6, spaceBefore=2, hAlign='CENTER'))
        elif ln.lstrip().startswith('> '):
            story.append(Paragraph(_md_inline(ln.lstrip()[2:]), _hquote))
        elif ln.lstrip().startswith('- '):
            story.append(Paragraph('\u2022&nbsp;&nbsp;' + _md_inline(ln.lstrip()[2:]), _hbul))
        elif _re.match(r'^\d+\.\s', ln.strip()):
            story.append(Paragraph(_md_inline(ln.strip()), _hbul))
        else:
            story.append(Paragraph(_md_inline(ln.strip()), _hb))
        i += 1

story.append(PageBreak())
story.append(Paragraph('HACCP Plans &mdash; Venison', _ht))
story.append(HRFlowable(width='28%', thickness=1, color=GOLD, spaceAfter=6, spaceBefore=2, hAlign='CENTER'))
story.append(Paragraph('Intake and breakdown &nbsp;&middot;&nbsp; whole muscle &nbsp;&middot;&nbsp; pastrami &nbsp;&middot;&nbsp; salami annex', _hsub))
story.append(Paragraph('Prepared by Robert Fry &nbsp;&middot;&nbsp; Revised ' + report_date + ' &nbsp;&middot;&nbsp; Review annually (next due ' + _review_date + ') &nbsp;&middot;&nbsp; FSA Licence UK2820', _hmeta))
_md_flow(VENISON_HACCP_MD)
story.append(Spacer(1, 10))
story.append(Paragraph('Prepared and signed off by: Robert Fry &nbsp;&nbsp;&middot;&nbsp;&nbsp; Date ' + report_date + ' &nbsp;&nbsp;&middot;&nbsp;&nbsp; Next review: ' + _review_date,
    ParagraphStyle('ven_hac_sign', fontName=SERIF, fontSize=9.5, textColor=INK)))
story.append(PageBreak())
# ── END HACCP PLANS: VENISON ──────────────────────────────────────────────────

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
    for run in sorted(venison_runs, key=lambda r: (r.get('date') or ''), reverse=True):
        ven_alias = run.get('alias','') or to_alias(run.get('estate',''))
        title = ("Venison Breakdown — " + str(run.get('batchCode', '(no batch)')) + " · " + str(ven_alias)).strip(' ·')
        # Subtitle must state the true provenance. Bought-in meat is NOT a private
        # kill - saying so on an FSA record misstates provenance. Fixed 06/08/2026.
        _end_use = "Processed for the estate\u2019s own consumption \u2014 not for sale to the public."
        if run.get('sourceType') == 'bought_in':
            _sup = str(run.get('supplier', '') or 'an approved supplier')
            _fsa = str(run.get('supplierFSA', '') or '')
            _srcline = ("Meat bought in from " + _sup + (" (" + _fsa + ")" if _fsa else "")
                        + ", an FSA-approved establishment. " + _end_use)
        else:
            _srcline = "Private kill. " + _end_use
        add_section(title, _srcline)
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

            # ── DRYING LOG (added 06/08/2026) ─────────────────────────────────
            # Every weighing taken during drying, oldest first, with loss against
            # the start weight and the forecast finish computed at that point.
            # This is the record that lets the forecast get better over time.
            _dlog = lane.get('dryingLog') or []
            if _dlog:
                story.append(Spacer(1, 5*mm))
                story.append(Paragraph('Drying log', ven_lane_h))
                story.append(Spacer(1, 2*mm))
                _dd = [[Paragraph(c, ven_hdr) for c in
                        ['Weighed', 'Component', 'Start (g)', 'Now (g)', 'Lost (g)', 'Loss %', 'Scale']]]
                for _e in sorted(_dlog, key=lambda x: str(x.get('date',''))):
                    for _rd in _e.get('readings', []):
                        _s = _vg(_rd.get('startG')); _n = _vg(_rd.get('grams'))
                        _lp = _rd.get('lossPct')
                        _lp = f'{float(_lp):.1f}%' if _lp not in (None, '') else (
                              f'{100*(_s-_n)/_s:.1f}%' if _s else '\u2014')
                        _dd.append([Paragraph(clean(str(_e.get('date',''))), ven_cell),
                                    Paragraph(clean(str(_rd.get('component',''))), ven_cell),
                                    Paragraph(_vfmt(_s), ven_cell), Paragraph(_vfmt(_n), ven_cell),
                                    Paragraph(_vfmt(_s-_n) if _s else '\u2014', ven_cell),
                                    Paragraph(_lp, ven_cell),
                                    Paragraph(clean(str(_e.get('scale',''))), ven_cell)])
                _dt = Table(_dd, colWidths=[22*mm, 74*mm, 24*mm, 24*mm, 24*mm, 20*mm, 38*mm], repeatRows=1)
                _dt.setStyle(TableStyle([
                    ('BACKGROUND', (0,0), (-1,0), SAGE[0]),
                    ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD),
                    ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8.5),
                    ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
                    ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4),
                    ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                    ('VALIGN', (0,0), (-1,-1), 'TOP')]))
                story.append(_dt)
                for _e in sorted(_dlog, key=lambda x: str(x.get('date',''))):
                    if _e.get('note'):
                        story.append(Paragraph('<b>' + clean(str(_e.get('date',''))) + ':</b> '
                                               + clean(str(_e['note'])), ven_stat))

            # ── FORECAST ──────────────────────────────────────────────────────
            _fc = lane.get('forecast') or {}
            _tg = _fc.get('targets') or {}
            if _tg:
                story.append(Spacer(1, 5*mm))
                story.append(Paragraph('Forecast finish', ven_lane_h))
                story.append(Spacer(1, 2*mm))
                _fd = [[Paragraph(c, ven_hdr) for c in
                        ['Component', 'Target loss', 'Finishes at (g)', 'Still to lose (g)', 'Expected ready']]]
                for _pk in sorted(_tg.keys()):
                    _label = _pk.replace('pct', '%')
                    for _cname, _v in (_tg[_pk] or {}).items():
                        if not _v: continue
                        _when = _v.get('projectedDate') or _v.get('status') or '\u2014'
                        _lin = _v.get('projectedDateLinear')
                        if _lin and _lin != _v.get('projectedDate'):
                            _when = str(_when) + ' (earliest ' + str(_lin) + ')'
                        _fd.append([Paragraph(clean(str(_cname)), ven_cell),
                                    Paragraph(_label, ven_cell),
                                    Paragraph(_vfmt(_v.get('targetG')), ven_cell),
                                    Paragraph(_vfmt(_v.get('gramsToGo')) if _v.get('gramsToGo') else '\u2014', ven_cell),
                                    Paragraph(clean(str(_when)), ven_cell)])
                if len(_fd) > 1:
                    _ft = Table(_fd, colWidths=[74*mm, 24*mm, 30*mm, 30*mm, 68*mm], repeatRows=1)
                    _ft.setStyle(TableStyle([
                        ('BACKGROUND', (0,0), (-1,0), SAGE[0]),
                        ('LINEABOVE', (0,0), (-1,0), 0.8, GOLD), ('LINEBELOW', (0,0), (-1,0), 0.8, GOLD),
                        ('FONTNAME', (0,1), (-1,-1), SERIF), ('FONTSIZE', (0,0), (-1,-1), 8.5),
                        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
                        ('GRID', (0,0), (-1,-1), 0.35, HAIR), ('LEFTPADDING', (0,0), (-1,-1), 4),
                        ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                        ('VALIGN', (0,0), (-1,-1), 'TOP')]))
                    story.append(_ft)
                    story.append(Paragraph(
                        'Projected from the weighings above. Dates assume drying continues to slow as it has so far; '
                        'the earliest date is the straight-line case. Weight loss is a guide, not the test \u2014 confirm '
                        'texture by hand before packing as finished.', ven_stat))

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
    _bait_stations = []
    for rec in pest_records:
        for _stn in (rec.get('stations', []) or []):
            _nm = clean(_stn.get('name',''))
            if _nm and _nm not in _bait_stations: _bait_stations.append(_nm)
    if _bait_stations:
        _legend = '  \u00b7  '.join(f'{i+1}: {nm}' for i, nm in enumerate(_bait_stations))
        _bait_legend = Paragraph('Stations \u2014 ' + _legend, ParagraphStyle('blg', fontName=SERIF, fontSize=8, textColor=MUTE, leading=11, spaceAfter=4))
        _bh = ParagraphStyle('bh', fontName=SERIFB, fontSize=8, textColor=TEAL[1], alignment=1)
        _bhl = ParagraphStyle('bhl', fontName=SERIFB, fontSize=8, textColor=TEAL[1])
        _bc = ParagraphStyle('bc', fontName=SERIF, fontSize=8, leading=10, alignment=1)
        _bd = ParagraphStyle('bd', fontName=SERIF, fontSize=8, leading=10)
        brows = [[Paragraph('Date', _bhl)] + [Paragraph(str(i+1), _bh) for i in range(len(_bait_stations))]]
        for rec in sorted(pest_records, key=lambda x: (x.get('date') or ''), reverse=True):
            _stt = {clean(s2.get('name','')): clean(s2.get('status','')) for s2 in (rec.get('stations', []) or [])}
            if not _stt: continue
            brows.append([Paragraph(clean(rec.get('date','')), _bd)] + [Paragraph(_stt.get(nm, '\u2013'), _bc) for nm in _bait_stations])
        _nst = len(_bait_stations); _dw = 24*mm; _cw = (267*mm - _dw)/_nst
        bt = Table(brows, colWidths=[_dw] + [_cw]*_nst, repeatRows=1)
        bt.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),TEAL[0]), ('LINEABOVE',(0,0),(-1,0),0.8,GOLD), ('LINEBELOW',(0,0),(-1,0),0.8,GOLD), ('FONTSIZE',(0,0),(-1,-1),8), ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,ROWB]), ('GRID',(0,0),(-1,-1),0.35,HAIR), ('ALIGN',(1,0),(-1,-1),'CENTER'), ('LEFTPADDING',(0,0),(-1,-1),4), ('RIGHTPADDING',(0,0),(-1,-1),4), ('TOPPADDING',(0,0),(-1,-1),5), ('BOTTOMPADDING',(0,0),(-1,-1),5), ('VALIGN',(0,0),(-1,-1),'MIDDLE')]))
        story.append(KeepTogether([Paragraph('Bait Station Checks', ParagraphStyle('rbh', fontSize=11, fontName=SERIFB, textColor=GREEN, spaceAfter=2, spaceBefore=2)), _bait_legend, bt]))

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
        _ins_heading = Paragraph('Insectocutor Checks', ParagraphStyle('insh', fontSize=11, fontName=SERIFB, textColor=GREEN, spaceAfter=4, spaceBefore=2))
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
        for rec in sorted(pest_records, key=lambda x: (x.get('date') or ''), reverse=True):
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
        _ins_legend = Paragraph('St = sticky board \u00b7 Cl = cleanout \u00b7 La = lamp \u00b7 Sr = starter &nbsp;\u00b7&nbsp; \u2713 done \u00b7 \u2013 not done / not recorded', small)
        story.append(KeepTogether([_ins_heading, it, _ins_legend]))
else:
    story.append(Paragraph('No pest control checks recorded yet.', small))

add_section('Finished Product / Delivery Records',
    'Finished salami, prosciutto and other products dispatched, by batch and destination. Completes the traceability chain from intake through production to the customer.')
if deliveries:
    rows = [['Date', 'Batch', 'Destination', 'Products dispatched', 'Notes']]
    for rec in sorted(deliveries, key=lambda x: (x.get('date') or ''), reverse=True):
        # The app writes batchCodes (a LIST) and clientId. Older code here read
        # batchCode / destination / processor, which the app never writes, so
        # every delivery printed with three empty columns. Fixed 03/08/2026.
        _bc = rec.get('batchCodes') or rec.get('batchCode') or ''
        if isinstance(_bc, list):
            _bc = ', '.join([str(x) for x in _bc if x])
        _dest = ''
        _cid = rec.get('clientId') or ''
        if _cid and estates.get(_cid):
            _dest = to_alias(estates.get(_cid))
        if not _dest:
            _dest = get_estate(rec)
        if not _dest or _dest == '—':
            _dest = clean(str(rec.get('destination', rec.get('processor','')) or ''))
        _fl = []
        for f in (rec.get('flavours') or []):
            nm = str(f.get('name','') or '').strip()
            if not nm:
                continue
            q = str(f.get('qty','') or '').strip()
            u = str(f.get('unit','') or '').strip()
            _fl.append(f"{nm} {q} {u}".strip() if q else nm)
        rows.append([Paragraph(clean(str(rec.get('date',''))), cell_style),
                     Paragraph(clean(str(_bc)), cell_style),
                     Paragraph(clean(str(_dest)), cell_style),
                     Paragraph(clean('; '.join(_fl)), cell_style),
                     Paragraph(clean(str(rec.get('notes','') or '')), cell_style)])
    t = Table(rows, colWidths=[20*mm, 42*mm, 34*mm, 66*mm, 65*mm], repeatRows=1)
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
access_token = DBX_TOKEN if DBX_TOKEN else get_dropbox_token()
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
    _level = "GREEN" if BACKUP_OK else "AMBER"
    _reason = (f"PDF {filename} ({len(pdf_data)} bytes) uploaded successfully. "
               f"Records: intakes={len(intakes)}, daily={len(daily_records)}, "
               f"deliveries={len(deliveries)}, pest={len(pest_records)}, "
               f"prod={len(production_records)}\nBackup: {BACKUP_MSG}")
    if not BACKUP_OK:
        _reason += "\nAMBER because the PDF is fine but the data backup did not land."
    _write_status(_level, _reason)
    _log(f"=== Run completed ({_level}) ===")
else:
    err = f"Dropbox upload failed: HTTP {r.status_code} {r.text[:200]}"
    _log(f"!!! {err}")
    _write_status("RED", err)
    print(f"Dropbox upload failed: {r.text}")
    exit(1)
