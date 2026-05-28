import os
import json
import requests
from datetime import datetime, date
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.enums import TA_CENTER, TA_LEFT

# ── CONFIG ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_KEY']
DROPBOX_TOKEN = os.environ['DROPBOX_TOKEN']

today = date.today()
report_date = today.strftime('%d/%m/%Y')
filename = f"FSA_Records_{today.strftime('%Y-%m-%d')}.pdf"

# ── FETCH FROM SUPABASE ───────────────────────────────────────────────────────
headers = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json'
}

def fetch(table):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}?select=*", headers=headers)
    return r.json() if r.ok else []

intakes_raw = fetch('intakes')
deliveries_raw = fetch('deliveries')

intakes = [r['data'] for r in intakes_raw if r.get('data')]
daily_records = [r['data'] for r in deliveries_raw if r.get('data') and r['data'].get('_type') == 'daily']
deliveries = [r['data'] for r in deliveries_raw if r.get('data') and not r['data'].get('_type')]
pest_records = [r['data'] for r in deliveries_raw if r.get('data') and r['data'].get('_type') == 'pest']
production_records = [r['data'] for r in deliveries_raw if r.get('data') and r['data'].get('_type') == 'production']

# ── BUILD PDF ─────────────────────────────────────────────────────────────────
doc = SimpleDocTemplate(
    filename,
    pagesize=A4,
    rightMargin=20*mm, leftMargin=20*mm,
    topMargin=20*mm, bottomMargin=20*mm
)

styles = getSampleStyleSheet()
GREEN = colors.HexColor('#3a6b2a')
AMBER = colors.HexColor('#854f0b')
LIGHT_GREEN = colors.HexColor('#e8f4e3')
LIGHT_GREY = colors.HexColor('#f5f5f2')

h1 = ParagraphStyle('h1', fontSize=18, textColor=GREEN, fontName='Helvetica-Bold', spaceAfter=4)
h2 = ParagraphStyle('h2', fontSize=13, textColor=GREEN, fontName='Helvetica-Bold', spaceAfter=4, spaceBefore=12)
small = ParagraphStyle('small', fontSize=9, textColor=colors.grey)
normal = ParagraphStyle('normal', fontSize=10, spaceAfter=4)
centre = ParagraphStyle('centre', fontSize=10, alignment=TA_CENTER)

story = []

# Cover
story.append(Spacer(1, 10*mm))
story.append(Paragraph('Artisan by Robert', h1))
story.append(Paragraph('FSA Compliance Records', ParagraphStyle('sub', fontSize=13, textColor=AMBER, fontName='Helvetica-Bold', spaceAfter=2)))
story.append(Paragraph(f'FSA Licence: UK2820 &nbsp;&nbsp; Hook, Hampshire RG29 1HT', small))
story.append(Paragraph(f'Report generated: {report_date}', small))
story.append(HRFlowable(width='100%', thickness=1, color=GREEN, spaceAfter=12, spaceBefore=8))

# Summary counts
summary_data = [
    ['Intake records', str(len(intakes))],
    ['Daily records', str(len(daily_records))],
    ['Delivery records', str(len(deliveries))],
    ['Pest control checks', str(len(pest_records))],
    ['Production runs', str(len(production_records))],
]
summary_table = Table(summary_data, colWidths=[120*mm, 40*mm])
summary_table.setStyle(TableStyle([
    ('BACKGROUND', (0,0), (-1,-1), LIGHT_GREEN),
    ('TEXTCOLOR', (0,0), (0,-1), colors.HexColor('#1a1a1a')),
    ('TEXTCOLOR', (1,0), (1,-1), GREEN),
    ('FONTNAME', (1,0), (1,-1), 'Helvetica-Bold'),
    ('FONTSIZE', (0,0), (-1,-1), 10),
    ('ROWBACKGROUNDS', (0,0), (-1,-1), [LIGHT_GREEN, colors.white]),
    ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#b8d9b0')),
    ('LEFTPADDING', (0,0), (-1,-1), 8),
    ('RIGHTPADDING', (0,0), (-1,-1), 8),
    ('TOPPADDING', (0,0), (-1,-1), 6),
    ('BOTTOMPADDING', (0,0), (-1,-1), 6),
]))
story.append(summary_table)
story.append(Spacer(1, 8*mm))

# ── INTAKE RECORDS ────────────────────────────────────────────────────────────
story.append(Paragraph('Intake Records', h2))
story.append(HRFlowable(width='100%', thickness=0.5, color=GREEN, spaceAfter=6))

if intakes:
    intake_header = [['Batch Code', 'Date', 'Estate', 'Species', 'Items']]
    intake_rows = []
    for rec in sorted(intakes, key=lambda x: x.get('date',''), reverse=True):
        items_str = ', '.join([f"{i.get('qty','')} {i.get('unit','')} {i.get('species','')}" for i in rec.get('items',[])])
        intake_rows.append([
            rec.get('batchCode',''),
            rec.get('date',''),
            rec.get('estate', rec.get('estateId','')),
            rec.get('species',''),
            items_str[:50]
        ])
    intake_table = Table(intake_header + intake_rows, colWidths=[35*mm, 22*mm, 35*mm, 25*mm, 50*mm])
    intake_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), GREEN),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(intake_table)
else:
    story.append(Paragraph('No intake records found.', small))

story.append(Spacer(1, 8*mm))

# ── DAILY RECORDS ─────────────────────────────────────────────────────────────
story.append(Paragraph('Daily Records', h2))
story.append(HRFlowable(width='100%', thickness=0.5, color=GREEN, spaceAfter=6))

if daily_records:
    daily_header = [['Date', 'Day Type', 'Notes', 'Outstanding Tasks']]
    daily_rows = []
    for rec in sorted(daily_records, key=lambda x: x.get('date',''), reverse=True):
        open_tasks = [t['text'] for t in rec.get('todoList',[]) if not t.get('done')]
        tasks_str = ('; '.join(open_tasks))[:60] if open_tasks else 'None'
        notes = (rec.get('notes','') or rec.get('monitorNotes',''))[:60]
        daily_rows.append([
            rec.get('date',''),
            rec.get('dayTypeId','').replace('-',' ').title(),
            notes,
            tasks_str
        ])
    daily_table = Table(daily_header + daily_rows, colWidths=[22*mm, 35*mm, 65*mm, 45*mm])
    daily_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), GREEN),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(daily_table)
else:
    story.append(Paragraph('No daily records found.', small))

story.append(Spacer(1, 8*mm))

# ── DELIVERY RECORDS ──────────────────────────────────────────────────────────
story.append(Paragraph('Finished Product / Delivery Records', h2))
story.append(HRFlowable(width='100%', thickness=0.5, color=GREEN, spaceAfter=6))

if deliveries:
    del_header = [['Date', 'Batch', 'Destination', 'Notes']]
    del_rows = []
    for rec in sorted(deliveries, key=lambda x: x.get('date',''), reverse=True):
        del_rows.append([
            rec.get('date',''),
            rec.get('batchCode',''),
            rec.get('destination', rec.get('processor','')),
            (rec.get('notes',''))[:60]
        ])
    del_table = Table(del_header + del_rows, colWidths=[22*mm, 40*mm, 55*mm, 50*mm])
    del_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), GREEN),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
        ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')),
        ('LEFTPADDING', (0,0), (-1,-1), 4),
        ('RIGHTPADDING', (0,0), (-1,-1), 4),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(del_table)
else:
    story.append(Paragraph('No delivery records found.', small))

# ── PEST CONTROL RECORDS ──────────────────────────────────────────────────────
story.append(Paragraph('Pest Control Records', h2))
story.append(HRFlowable(width='100%', thickness=0.5, color=GREEN, spaceAfter=6))

# Standing reference: bait product (SDS) and station map — appears regardless of records
sds_url = 'https://artisanbyrobert.github.io/fsa-records/rat_bait_difen_blocks.pdf'
ref_data = [
    ['Rodenticide in use', Paragraph('VERTOX OKTABLOK II · brodifacoum 50ppm wax block · single-feed kill · PelGar International', small)],
    ['Safety Data Sheet', Paragraph(f'<link href="{sds_url}" color="#3a6b2a"><u>{sds_url}</u></link>', small)],
    ['Bait station map', Paragraph('7 numbered stations across sawmill + main workshop. Live map in web app: <link href="https://artisanbyrobert.github.io/fsa-records/" color="#3a6b2a"><u>artisanbyrobert.github.io/fsa-records</u></link> → Pest Control', small)],
]
ref_table = Table(ref_data, colWidths=[40*mm, 130*mm])
ref_table.setStyle(TableStyle([
    ('BACKGROUND', (0,0), (0,-1), LIGHT_GREEN),
    ('FONTNAME', (0,0), (0,-1), 'Helvetica-Bold'),
    ('FONTSIZE', (0,0), (-1,-1), 9),
    ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#b8d9b0')),
    ('LEFTPADDING', (0,0), (-1,-1), 6),
    ('RIGHTPADDING', (0,0), (-1,-1), 6),
    ('TOPPADDING', (0,0), (-1,-1), 5),
    ('BOTTOMPADDING', (0,0), (-1,-1), 5),
    ('VALIGN', (0,0), (-1,-1), 'TOP'),
]))
story.append(ref_table)
story.append(Spacer(1, 4*mm))

# Numbered station list (printable backup of the map)
station_default_names = ['Under alu roof sheet','By red cabinet','Behind bench','By smoker','Under saw bench','By french doors','Under vice']
station_list_data = [['#', 'Location']] + [[str(i+1), nm] for i, nm in enumerate(station_default_names)]
station_table = Table(station_list_data, colWidths=[15*mm, 155*mm])
station_table.setStyle(TableStyle([
    ('BACKGROUND', (0,0), (-1,0), GREEN),
    ('TEXTCOLOR', (0,0), (-1,0), colors.white),
    ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
    ('FONTSIZE', (0,0), (-1,-1), 9),
    ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
    ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')),
    ('LEFTPADDING', (0,0), (-1,-1), 5),
    ('RIGHTPADDING', (0,0), (-1,-1), 5),
    ('TOPPADDING', (0,0), (-1,-1), 4),
    ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ('ALIGN', (0,0), (0,-1), 'CENTER'),
]))
story.append(Paragraph('Bait station locations', ParagraphStyle('mini', fontSize=10, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=4)))
story.append(station_table)
story.append(Spacer(1, 6*mm))

# Per-check records
def _tick(b):
    return '✓' if b else '–'

if pest_records:
    for rec in sorted(pest_records, key=lambda x: x.get('date',''), reverse=True):
        story.append(Paragraph(f"Check date: {rec.get('date','—')}", ParagraphStyle('chk', fontSize=10, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=4, spaceBefore=6)))

        # Stations table for this check
        stns = rec.get('stations') or []
        if stns:
            stn_header = [['#', 'Location', 'Status', 'Notes']]
            stn_rows = []
            for idx, s in enumerate(stns):
                status_raw = s.get('status') or ''
                status = {'replaced':'Replaced bait', 'no_activity':'No activity'}.get(status_raw, '—')
                stn_rows.append([str(idx+1), s.get('name',''), status, (s.get('notes','') or '')[:50]])
            stn_tbl = Table(stn_header + stn_rows, colWidths=[10*mm, 60*mm, 35*mm, 65*mm])
            stn_tbl.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), GREEN),
                ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
                ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')),
                ('LEFTPADDING', (0,0), (-1,-1), 4),
                ('RIGHTPADDING', (0,0), (-1,-1), 4),
                ('TOPPADDING', (0,0), (-1,-1), 3),
                ('BOTTOMPADDING', (0,0), (-1,-1), 3),
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ]))
            story.append(stn_tbl)

        # Insectocutors
        ins = rec.get('insectocutors') or {}
        if ins:
            ins_header = [['Insectocutor', 'Cleaned/Sticky', 'Lamp', 'Starter']]
            ins_rows = []
            ins_map = [
                ('foyer', 'Foyer (zapper)', 'cleanout'),
                ('plucking_room', 'Plucking room (sticky)', 'sticky'),
                ('build_room', 'Build room (sticky)', 'sticky'),
            ]
            for key, label_txt, first_field in ins_map:
                a = ins.get(key) or {}
                ins_rows.append([label_txt, _tick(a.get(first_field)), _tick(a.get('lamp')), _tick(a.get('starter'))])
            ins_tbl = Table(ins_header + ins_rows, colWidths=[60*mm, 40*mm, 35*mm, 35*mm])
            ins_tbl.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), AMBER),
                ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
                ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')),
                ('LEFTPADDING', (0,0), (-1,-1), 4),
                ('RIGHTPADDING', (0,0), (-1,-1), 4),
                ('TOPPADDING', (0,0), (-1,-1), 3),
                ('BOTTOMPADDING', (0,0), (-1,-1), 3),
                ('ALIGN', (1,1), (-1,-1), 'CENTER'),
            ]))
            story.append(Spacer(1, 3*mm))
            story.append(ins_tbl)

        # General comment
        gc = rec.get('generalComment') or ''
        if gc:
            story.append(Spacer(1, 2*mm))
            story.append(Paragraph(f'<i>Notes: {gc[:200]}</i>', small))
else:
    story.append(Paragraph('No pest control checks recorded yet.', small))

story.append(Spacer(1, 8*mm))

# ── PRODUCTION RECORDS ────────────────────────────────────────────────────────
story.append(Paragraph('Production Records', h2))
story.append(HRFlowable(width='100%', thickness=0.5, color=GREEN, spaceAfter=6))

stage_type_names = {'defrost': 'Defrost', 'mince': 'Prep / wash / mince', 'stuff_hang': 'Stuffing & hanging'}

if production_records:
    for rec in sorted(production_records, key=lambda x: x.get('startDate', x.get('finishedDate', '')), reverse=True):
        # Header
        run_no = rec.get('runNumber') or '?'
        species_nm = rec.get('speciesName') or '—'
        batch = rec.get('batchCode') or '—'
        proc = rec.get('processCode') or '—'
        status_label = 'Finished' if rec.get('status') == 'finished' else 'In progress'
        fat_pct = rec.get('fatPercent')
        fat_str = f' · {fat_pct}% fat' if fat_pct else ''
        story.append(Paragraph(
            f"<b>{species_nm}</b> · run {run_no} from {batch} · process {proc} · {status_label}{fat_str}",
            ParagraphStyle('prh', fontSize=10, fontName='Helvetica-Bold', textColor=GREEN, spaceAfter=4, spaceBefore=6)
        ))
        if rec.get('finishedDate'):
            story.append(Paragraph(f"Started {rec.get('startDate','—')} · finished {rec.get('finishedDate','—')}", small))
        else:
            story.append(Paragraph(f"Started {rec.get('startDate','—')}", small))

        # Stages
        stages = rec.get('stages') or []
        if stages:
            stg_header = [['Date', 'Stage', 'Details', 'Notes']]
            stg_rows = []
            for s in stages:
                t = s.get('type','')
                details = ''
                if t == 'defrost':
                    if s.get('meatKg'):
                        details = f"{s.get('meatKg')} kg meat out"
                    else:
                        details = '(fat-only top-up)'
                elif t == 'mince':
                    details = f"meat {s.get('meatKg','?')}kg + fat {s.get('fatKg','?')}kg = {s.get('totalKg','?')}kg total"
                elif t == 'stuff_hang':
                    cnt = s.get('count','?')
                    sz = s.get('unitGrams')
                    details = f"{cnt} x {sz}g" if sz else f"{cnt} salami"
                stg_rows.append([
                    s.get('date','—'),
                    stage_type_names.get(t, t),
                    details,
                    (s.get('notes','') or '')[:60]
                ])
            stg_tbl = Table(stg_header + stg_rows, colWidths=[22*mm, 35*mm, 55*mm, 58*mm])
            stg_tbl.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), GREEN),
                ('TEXTCOLOR', (0,0), (-1,0), colors.white),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, LIGHT_GREY]),
                ('GRID', (0,0), (-1,-1), 0.3, colors.HexColor('#e0e0dc')),
                ('LEFTPADDING', (0,0), (-1,-1), 4),
                ('RIGHTPADDING', (0,0), (-1,-1), 4),
                ('TOPPADDING', (0,0), (-1,-1), 3),
                ('BOTTOMPADDING', (0,0), (-1,-1), 3),
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ]))
            story.append(stg_tbl)
        else:
            story.append(Paragraph('<i>No stages logged yet.</i>', small))
else:
    story.append(Paragraph('No production runs recorded yet.', small))

story.append(Spacer(1, 8*mm))

# Footer
story.append(Spacer(1, 8*mm))
story.append(HRFlowable(width='100%', thickness=0.5, color=colors.grey, spaceAfter=4))
story.append(Paragraph(f'Artisan by Robert · UK2820 · Generated {report_date} · Confidential FSA Records', small))

doc.build(story)
print(f"PDF generated: {filename}")

# ── UPLOAD TO DROPBOX ─────────────────────────────────────────────────────────
with open(filename, 'rb') as f:
    pdf_data = f.read()

dropbox_path = f'/FSA_Records_{today.strftime("%Y-%m-%d")}.pdf'
upload_headers = {
    'Authorization': f'Bearer {DROPBOX_TOKEN}',
    'Content-Type': 'application/octet-stream',
    'Dropbox-API-Arg': json.dumps({
        'path': dropbox_path,
        'mode': 'overwrite',
        'autorename': False,
        'mute': True
    })
}

r = requests.post('https://content.dropboxapi.com/2/files/upload', headers=upload_headers, data=pdf_data)
if r.ok:
    print(f"Uploaded to Dropbox: {dropbox_path}")
else:
    print(f"Dropbox upload failed: {r.text}")
    exit(1)
