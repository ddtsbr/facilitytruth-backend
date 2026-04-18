import os
import json
import uuid
import threading
import requests
import anthropic
import sendgrid
from sendgrid.helpers.mail import Mail
from flask import Flask, request, jsonify
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
import pdfplumber
import pytesseract
from PIL import Image
import io
import base64

app = Flask(__name__)

# ── Environment variables ──────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY")
SENDGRID_API_KEY    = os.environ.get("SENDGRID_API_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
DATABASE_URL        = os.environ.get("DATABASE_URL")
FROM_EMAIL          = "support@facilitytruth.com"
NOTIFY_EMAIL        = os.environ.get("NOTIFY_EMAIL", "support@facilitytruth.com")
CMS_API_BASE        = "https://data.cms.gov/provider-data/api/1/datastore/query"

# ── Database helpers ───────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    """Create jobs table if it doesn't exist."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            customer_name TEXT,
            customer_email TEXT,
            facility_name TEXT,
            facility_state TEXT,
            contract_text TEXT,
            cms_data JSONB,
            state_data JSONB,
            report_pdf BYTEA,
            status_contract TEXT DEFAULT 'pending',
            status_cms TEXT DEFAULT 'pending',
            status_state TEXT DEFAULT 'pending',
            status_report TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW(),
            delivered_at TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def create_job(job_id, customer_name, customer_email, facility_name, facility_state):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO jobs (id, customer_name, customer_email, facility_name, facility_state)
        VALUES (%s, %s, %s, %s, %s)
    """, (job_id, customer_name, customer_email, facility_name, facility_state))
    conn.commit()
    cur.close()
    conn.close()

def update_job(job_id, **kwargs):
    if not kwargs:
        return
    conn = get_db()
    cur = conn.cursor()
    sets = ", ".join([f"{k} = %s" for k in kwargs.keys()])
    values = list(kwargs.values()) + [job_id]
    cur.execute(f"UPDATE jobs SET {sets} WHERE id = %s", values)
    conn.commit()
    cur.close()
    conn.close()

def get_job(job_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None

# ── PDF / OCR extraction ───────────────────────────────────────────────
def extract_text_from_pdf(pdf_bytes):
    """Try direct text extraction first, fall back to OCR if needed."""
    text = ""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
    except Exception as e:
        print(f"pdfplumber error: {e}")

    # If we got meaningful text, return it
    if len(text.strip()) > 200:
        return text.strip()

    # Fall back to OCR for scanned documents
    print("Direct extraction yielded little text — falling back to OCR")
    return extract_text_via_ocr(pdf_bytes)

def extract_text_via_ocr(pdf_bytes):
    """Use Google Document AI or Tesseract for scanned PDFs."""
    google_api_key = os.environ.get("GOOGLE_DOCUMENT_AI_KEY")

    if google_api_key:
        return extract_via_google_document_ai(pdf_bytes, google_api_key)
    else:
        return extract_via_tesseract(pdf_bytes)

def extract_via_google_document_ai(pdf_bytes, api_key):
    """Use Google Document AI for high-quality OCR."""
    try:
        project_id = os.environ.get("GOOGLE_PROJECT_ID")
        processor_id = os.environ.get("GOOGLE_PROCESSOR_ID")
        url = f"https://documentai.googleapis.com/v1/projects/{project_id}/locations/us/processors/{processor_id}:process?key={api_key}"

        encoded = base64.b64encode(pdf_bytes).decode("utf-8")
        payload = {
            "rawDocument": {
                "content": encoded,
                "mimeType": "application/pdf"
            }
        }
        response = requests.post(url, json=payload, timeout=60)
        data = response.json()
        return data.get("document", {}).get("text", "")
    except Exception as e:
        print(f"Google Document AI error: {e}")
        return extract_via_tesseract(pdf_bytes)

def extract_via_tesseract(pdf_bytes):
    """Tesseract OCR fallback."""
    try:
        from pdf2image import convert_from_bytes
        images = convert_from_bytes(pdf_bytes, dpi=200)
        text = ""
        for img in images:
            text += pytesseract.image_to_string(img) + "\n"
        return text.strip()
    except Exception as e:
        print(f"Tesseract error: {e}")
        return ""

# ── CMS Care Compare API ───────────────────────────────────────────────
def fetch_cms_data(facility_name, facility_state):
    """Fetch facility data from Medicare Care Compare API."""
    try:
        # Search for the facility
        params = {
            "resource_id": "ims-listing",
            "filters[0][property]": "provname",
            "filters[0][value]": facility_name,
            "filters[0][operator]": "LIKE",
            "filters[1][property]": "state",
            "filters[1][value]": facility_state,
            "limit": 5
        }

        # Try the NH Compare dataset (nursing home / assisted living)
        url = "https://data.cms.gov/provider-data/api/1/datastore/query/4pq5-n9py/0"
        response = requests.get(url, params={
            "filter[provname][value]": facility_name.upper(),
            "filter[state][value]": facility_state.upper(),
            "size": 5
        }, timeout=30)

        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            if results:
                facility = results[0]
                return {
                    "found": True,
                    "provider_name": facility.get("provname", facility_name),
                    "address": facility.get("address", ""),
                    "city": facility.get("city", ""),
                    "state": facility.get("state", facility_state),
                    "overall_rating": facility.get("overall_rating", "N/A"),
                    "health_inspection_rating": facility.get("health_inspection_rating", "N/A"),
                    "staffing_rating": facility.get("staffing_rating", "N/A"),
                    "quality_measure_rating": facility.get("quality_measure_rating", "N/A"),
                    "total_penalties": facility.get("total_penalties", 0),
                    "penalty_amount": facility.get("penalty_amount", 0),
                    "cms_id": facility.get("provnum", "")
                }

        return {"found": False, "provider_name": facility_name}

    except Exception as e:
        print(f"CMS API error: {e}")
        return {"found": False, "provider_name": facility_name, "error": str(e)}

# ── State inspection data ──────────────────────────────────────────────
COVERED_STATES = ["CA", "TX", "FL", "NY", "PA", "IL", "OH", "GA", "NC", "MI"]

def fetch_state_inspection_data(facility_name, facility_state, cms_id=""):
    """Fetch state inspection report summary where available."""
    state = facility_state.upper()

    if state not in COVERED_STATES:
        return {
            "available": False,
            "state": state,
            "message": f"State inspection data is not yet available for {state}. Your report includes Medicare data only."
        }

    # For covered states, use CMS inspection data as the source
    # In production this would query state-specific databases
    try:
        url = f"https://data.cms.gov/provider-data/api/1/datastore/query/4pq5-n9py/0"
        response = requests.get(url, params={
            "filter[provnum][value]": cms_id,
            "size": 1
        }, timeout=30)

        if response.status_code == 200:
            data = response.json()
            results = data.get("results", [])
            if results:
                facility = results[0]
                return {
                    "available": True,
                    "state": state,
                    "total_citations": facility.get("tot_defic", 0),
                    "health_citations": facility.get("h_tot_defic", 0),
                    "fire_citations": facility.get("f_tot_defic", 0),
                    "complaint_citations": facility.get("tot_comp_defic", 0),
                    "inspection_cycle": facility.get("cycle_1_tot_defic", 0),
                    "last_inspection_date": facility.get("survey_date", ""),
                }

        return {"available": True, "state": state, "total_citations": 0}

    except Exception as e:
        print(f"State inspection error: {e}")
        return {"available": False, "state": state, "error": str(e)}

# ── Claude contract analysis ───────────────────────────────────────────
def analyze_contract(contract_text, facility_name, facility_state):
    """Run the contract through Claude for red flag analysis."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are an expert analyst specializing in assisted living contracts. You are reviewing a contract for {facility_name} in {facility_state} on behalf of a family placing a parent in assisted living.

Your job is to identify red flags, watch items, and negotiable clauses — and explain each one in plain English that a non-lawyer can understand.

CRITICAL INSTRUCTIONS:
1. Cite every finding by its EXACT article and section number from the contract (e.g., "Article IV, Section B")
2. Quote the exact problematic language in quotation marks
3. Explain what it means in plain English
4. Explain why it matters to the family
5. Provide a specific negotiation script or action item
6. If a section cannot be found or text is unclear, note it explicitly

Respond ONLY with a valid JSON object. No markdown, no explanation, just raw JSON.

{{
  "overall_risk": "Low" | "Moderate" | "High",
  "risk_explanation": "2-3 sentence summary of overall contract risk",
  "red_flags": [
    {{
      "title": "Short descriptive title",
      "section_reference": "Article X, Section Y",
      "clause_text": "exact quoted text from contract",
      "plain_english": "what this means in plain English",
      "why_it_matters": "why this is a problem for the family",
      "action": "specific thing to ask for or do"
    }}
  ],
  "watch_items": [
    {{
      "title": "Short descriptive title",
      "section_reference": "Article X, Section Y",
      "clause_text": "exact quoted text",
      "plain_english": "what this means",
      "action": "what to ask or clarify"
    }}
  ],
  "negotiable_items": [
    {{
      "title": "Short descriptive title",
      "section_reference": "Article X, Section Y",
      "plain_english": "what this is",
      "negotiation_script": "word-for-word script to use with admissions coordinator"
    }}
  ],
  "missing_protections": [
    "Description of important protection that should be in the contract but isn't"
  ],
  "positive_findings": [
    "Any genuinely family-friendly provisions worth noting"
  ],
  "contract_summary": "3-4 sentence plain English summary of what this contract contains and its overall character"
}}

CONTRACT TEXT:
{contract_text[:15000]}"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# ── Claude facility synthesis ──────────────────────────────────────────
def synthesize_facility_data(cms_data, state_data, facility_name):
    """Use Claude to explain facility data in plain English."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are explaining a senior care facility's track record to a worried adult child in plain English.

Facility: {facility_name}
CMS Data: {json.dumps(cms_data)}
State Inspection Data: {json.dumps(state_data)}

Write a plain-English summary of this facility's track record. Be honest and direct. Do not sugarcoat poor ratings.

Respond ONLY with valid JSON:
{{
  "facility_summary": "2-3 sentence overall assessment",
  "overall_rating_explanation": "plain English explanation of the star rating",
  "staffing_explanation": "plain English explanation of staffing data",
  "inspection_explanation": "plain English explanation of inspection findings",
  "key_concerns": ["concern 1", "concern 2"],
  "questions_to_ask": ["specific question to ask the facility based on their record"]
}}"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )

    raw = message.content[0].text.strip()
    clean = raw.replace("```json", "").replace("```", "").strip()
    return json.loads(clean)

# ── Report PDF generation ──────────────────────────────────────────────
def generate_report_html(job, contract_analysis, facility_synthesis, cms_data, state_data):
    """Build the HTML that will be converted to PDF."""
    now = datetime.now().strftime("%B %d, %Y")
    risk = contract_analysis.get("overall_risk", "Unknown")
    risk_color = {"Low": "#2a7d4a", "Moderate": "#c47820", "High": "#b83232"}.get(risk, "#717169")

    red_flags = contract_analysis.get("red_flags", [])
    watch_items = contract_analysis.get("watch_items", [])
    negotiable = contract_analysis.get("negotiable_items", [])
    missing = contract_analysis.get("missing_protections", [])

    def flag_html(item, badge_color, badge_text, border_color, bg_color):
        return f"""
        <div style="border:1px solid {border_color};border-radius:8px;margin-bottom:12px;overflow:hidden;background:{bg_color};">
          <div style="padding:10px 16px;border-bottom:1px solid {border_color};display:flex;align-items:center;gap:8px;">
            <span style="background:{badge_color};color:white;font-size:10px;font-weight:600;padding:2px 8px;border-radius:4px;text-transform:uppercase;letter-spacing:0.05em;">{badge_text}</span>
            <span style="font-size:14px;font-weight:500;color:#0f2744;">{item.get('title','')}</span>
            <span style="font-size:11px;color:#717169;margin-left:auto;font-family:monospace;">{item.get('section_reference','')}</span>
          </div>
          <div style="padding:14px 16px;">
            <div style="font-size:13px;font-style:italic;color:#3d3d3a;background:rgba(255,255,255,0.6);border-left:3px solid {badge_color};padding:8px 12px;border-radius:0 6px 6px 0;margin-bottom:10px;line-height:1.6;">{item.get('clause_text','')}</div>
            <p style="font-size:14px;color:#3d3d3a;margin:0 0 8px;line-height:1.65;">{item.get('plain_english','')}</p>
            {"<p style='font-size:13px;color:#3d3d3a;margin:0 0 8px;line-height:1.6;'><strong>Why it matters:</strong> " + item.get('why_it_matters','') + "</p>" if item.get('why_it_matters') else ""}
            <div style="background:rgba(255,255,255,0.7);border-radius:6px;padding:8px 12px;font-size:13px;color:#3d3d3a;line-height:1.55;">
              <strong style="color:#0f2744;">{"Ask for" if badge_text != "Negotiate" else "Script"}:</strong> {item.get('action') or item.get('negotiation_script','')}
            </div>
          </div>
        </div>"""

    red_flags_html = "".join([flag_html(f, "#b83232", "Red Flag", "#f5c6c6", "#fdf0f0") for f in red_flags])
    watch_html = "".join([flag_html(w, "#c47820", "Watch", "#f5dba0", "#fef7ed") for w in watch_items])
    neg_html = "".join([flag_html(n, "#2a7d4a", "Negotiate", "#b8e0c8", "#edf7f0") for n in negotiable])

    cms_overall = cms_data.get("overall_rating", "N/A")
    cms_health = cms_data.get("health_inspection_rating", "N/A")
    cms_staffing = cms_data.get("staffing_rating", "N/A")
    cms_quality = cms_data.get("quality_measure_rating", "N/A")

    def stars(rating):
        try:
            r = int(rating)
            return "★" * r + "☆" * (5 - r)
        except:
            return "N/A"

    missing_html = "".join([f"<li style='font-size:14px;color:#3d3d3a;margin-bottom:6px;line-height:1.6;'>{m}</li>" for m in missing]) if missing else "<li style='font-size:14px;color:#717169;'>No significant missing protections identified.</li>"

    questions_html = ""
    if facility_synthesis.get("questions_to_ask"):
        questions_html = "".join([f"<li style='font-size:14px;color:#3d3d3a;margin-bottom:8px;line-height:1.6;font-style:italic;'>"{q}"</li>" for q in facility_synthesis.get("questions_to_ask", [])])

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: 'Helvetica Neue', Arial, sans-serif; color: #1c1c1a; margin: 0; padding: 0; font-size: 14px; line-height: 1.6; }}
  .cover {{ background: #0f2744; padding: 48px 48px 40px; color: white; }}
  .cover-brand {{ font-size: 22px; color: white; margin-bottom: 32px; letter-spacing: -0.02em; }}
  .cover-brand span {{ color: #1a9e8a; }}
  .cover-facility {{ font-size: 26px; color: white; margin-bottom: 6px; letter-spacing: -0.02em; }}
  .cover-loc {{ font-size: 14px; color: rgba(255,255,255,0.5); margin-bottom: 28px; }}
  .cover-scores {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
  .score-card {{ background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; padding: 14px; }}
  .score-label {{ font-size: 10px; font-weight: 600; letter-spacing: 0.1em; text-transform: uppercase; color: rgba(255,255,255,0.45); margin-bottom: 6px; }}
  .score-val {{ font-size: 22px; color: white; font-weight: 300; }}
  .cover-disclaimer {{ font-size: 11px; color: rgba(255,255,255,0.25); margin-top: 24px; padding-top: 16px; border-top: 1px solid rgba(255,255,255,0.1); line-height: 1.6; }}
  .body {{ padding: 40px 48px; }}
  .section {{ margin-bottom: 36px; }}
  .section-header {{ display: flex; align-items: center; gap: 10px; margin-bottom: 16px; padding-bottom: 10px; border-bottom: 2px solid #eeebe3; }}
  .section-num {{ width: 28px; height: 28px; background: #0f2744; color: white; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; flex-shrink: 0; }}
  .section-title {{ font-size: 20px; color: #0f2744; font-weight: 400; letter-spacing: -0.01em; }}
  .summary-box {{ background: #f9f7f3; border: 1px solid #eeebe3; border-radius: 8px; padding: 16px 20px; margin-bottom: 16px; }}
  .summary-box p {{ font-size: 15px; color: #3d3d3a; margin: 0 0 8px; line-height: 1.7; }}
  .summary-box p:last-child {{ margin: 0; }}
  .facility-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 16px; }}
  .fac-card {{ background: white; border: 1px solid #eeebe3; border-radius: 8px; padding: 12px; text-align: center; }}
  .fac-stars {{ font-size: 16px; color: #c47820; margin-bottom: 4px; }}
  .fac-label {{ font-size: 11px; color: #717169; line-height: 1.4; }}
  .risk-bar {{ height: 8px; background: linear-gradient(to right, #2a7d4a, #c47820, #b83232); border-radius: 100px; margin: 12px 0 6px; position: relative; }}
  .risk-marker {{ width: 14px; height: 14px; background: #0f2744; border: 2px solid white; border-radius: 50%; position: absolute; top: 50%; transform: translate(-50%, -50%); box-shadow: 0 1px 4px rgba(0,0,0,0.2); }}
  .risk-labels {{ display: flex; justify-content: space-between; font-size: 11px; color: #717169; }}
  .neg-item {{ background: white; border: 1px solid #eeebe3; border-radius: 8px; padding: 14px 16px; margin-bottom: 10px; display: flex; gap: 12px; }}
  .neg-num {{ width: 24px; height: 24px; background: #e8f6f4; color: #0f7c6b; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; flex-shrink: 0; margin-top: 2px; }}
  .neg-title {{ font-size: 14px; font-weight: 600; color: #0f2744; margin-bottom: 4px; }}
  .neg-script {{ font-size: 13px; font-style: italic; color: #3d3d3a; background: #f9f7f3; border-left: 3px solid #c2ebe5; padding: 6px 10px; border-radius: 0 6px 6px 0; margin-top: 6px; line-height: 1.6; }}
  .footer {{ background: #0f2744; padding: 24px 48px; color: rgba(255,255,255,0.3); font-size: 11px; line-height: 1.6; }}
</style>
</head>
<body>

<div class="cover">
  <div class="cover-brand">Facility<span>Truth</span> Report</div>
  <div class="cover-facility">{job['facility_name']}</div>
  <div class="cover-loc">{job.get('facility_state','')} · Report date: {now}</div>
  <div class="cover-scores">
    <div class="score-card">
      <div class="score-label">Contract risk</div>
      <div class="score-val" style="color:{'#f4a0a0' if risk=='High' else '#fcd577' if risk=='Moderate' else '#86d9b0'};">{risk}</div>
    </div>
    <div class="score-card">
      <div class="score-label">Medicare rating</div>
      <div class="score-val">{cms_overall} / 5</div>
    </div>
    <div class="score-card">
      <div class="score-label">Red flags found</div>
      <div class="score-val" style="color:#f4a0a0;">{len(red_flags)}</div>
    </div>
  </div>
  <div class="cover-disclaimer">This report is informational only and does not constitute legal or financial advice. FacilityTruth analyzes publicly available facility data and the contract text provided. We recommend consulting a qualified elder law attorney before signing any assisted living agreement.</div>
</div>

<div class="body">

  <div class="section">
    <div class="section-header">
      <div class="section-num">1</div>
      <div class="section-title">Contract analysis</div>
    </div>
    <div class="summary-box">
      <p>{contract_analysis.get('contract_summary','')}</p>
      <p>The contract contains <strong>{len(red_flags)} red flag{'s' if len(red_flags) != 1 else ''}</strong>, <strong>{len(watch_items)} watch item{'s' if len(watch_items) != 1 else ''}</strong>, and <strong>{len(negotiable)} negotiable item{'s' if len(negotiable) != 1 else ''}</strong>.</p>
    </div>
    {red_flags_html}
    {watch_html}
    {neg_html}
    {"<div class='summary-box'><p><strong>Missing protections:</strong></p><ul style='margin:8px 0 0;padding-left:20px;'>" + missing_html + "</ul></div>" if missing else ""}
  </div>

  <div class="section">
    <div class="section-header">
      <div class="section-num">2</div>
      <div class="section-title">Facility track record</div>
    </div>
    <div class="summary-box">
      <p>{facility_synthesis.get('facility_summary','')}</p>
    </div>
    <div class="facility-grid">
      <div class="fac-card">
        <div class="fac-stars">{stars(cms_overall)}</div>
        <div class="fac-label">Overall Medicare rating</div>
      </div>
      <div class="fac-card">
        <div class="fac-stars">{stars(cms_health)}</div>
        <div class="fac-label">Health inspections</div>
      </div>
      <div class="fac-card">
        <div class="fac-stars">{stars(cms_staffing)}</div>
        <div class="fac-label">Staffing ratio</div>
      </div>
      <div class="fac-card">
        <div class="fac-stars">{stars(cms_quality)}</div>
        <div class="fac-label">Quality measures</div>
      </div>
    </div>
    <div class="summary-box">
      <p><strong>Staffing:</strong> {facility_synthesis.get('staffing_explanation','')}</p>
      <p><strong>Inspections:</strong> {facility_synthesis.get('inspection_explanation','')}</p>
    </div>
    {"<div class='summary-box'><p><strong>Key concerns:</strong></p><ul style='margin:8px 0 0;padding-left:20px;'>" + "".join([f"<li style='font-size:14px;color:#3d3d3a;margin-bottom:6px;'>{c}</li>" for c in facility_synthesis.get('key_concerns',[])]) + "</ul></div>" if facility_synthesis.get('key_concerns') else ""}
  </div>

  <div class="section">
    <div class="section-header">
      <div class="section-num">3</div>
      <div class="section-title">Negotiation guide</div>
    </div>
    <div class="summary-box">
      <p>These are the specific requests to make — in priority order — before you sign. Approach the admissions coordinator calmly and confirm any agreed changes in writing as a signed addendum.</p>
    </div>
    {"".join([f"<div class='neg-item'><div class='neg-num'>{i+1}</div><div><div class='neg-title'>{n.get('title','')}</div><div class='neg-script'>{n.get('negotiation_script','')}</div></div></div>" for i, n in enumerate(negotiable)])}
    {"<div class='summary-box'><p><strong>Questions to ask about the facility's inspection record:</strong></p><ul style='margin:8px 0 0;padding-left:20px;'>" + questions_html + "</ul></div>" if questions_html else ""}
  </div>

  <div class="section">
    <div class="section-header">
      <div class="section-num">4</div>
      <div class="section-title">Overall risk and next steps</div>
    </div>
    <div class="summary-box">
      <p style="font-size:11px;font-weight:600;color:#717169;text-transform:uppercase;letter-spacing:0.08em;margin-bottom:6px;">Overall risk rating</p>
      <p style="font-size:32px;color:{risk_color};font-weight:300;margin-bottom:8px;">{risk}</p>
      <p>{contract_analysis.get('risk_explanation','')}</p>
      <div class="risk-bar"><div class="risk-marker" style="left:{'20%' if risk=='Low' else '55%' if risk=='Moderate' else '82%'};"></div></div>
      <div class="risk-labels"><span>Low</span><span>Moderate</span><span>High</span></div>
    </div>
    <div class="summary-box">
      <p><strong>A {risk.lower()} rating does not necessarily mean you should not choose this facility.</strong> It means the agreement as written needs attention before you sign. Use the negotiation guide above and get any agreed changes in writing as a signed addendum.</p>
      <p>For complex situations or if the facility refuses all negotiation, consult a qualified elder law attorney. Find one through the National Academy of Elder Law Attorneys at <strong>naela.org</strong>.</p>
    </div>
  </div>

</div>

<div class="footer">
  FacilityTruth provides informational reports only. Nothing in this report constitutes legal, financial, or medical advice. Report generated {now} · facilitytruth.com · support@facilitytruth.com
</div>

</body>
</html>"""
    return html

def generate_pdf(html_content):
    """Convert HTML to PDF using weasyprint."""
    try:
        from weasyprint import HTML
        pdf_bytes = HTML(string=html_content).write_pdf()
        return pdf_bytes
    except Exception as e:
        print(f"WeasyPrint error: {e}")
        return None

# ── SendGrid delivery ──────────────────────────────────────────────────
def send_report_email(job, pdf_bytes, contract_analysis):
    """Send the PDF report to the customer."""
    import base64 as b64
    from sendgrid.helpers.mail import Attachment, FileContent, FileName, FileType, Disposition

    sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
    risk = contract_analysis.get("overall_risk", "Complete")

    html_body = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:40px 20px;">
      <div style="background:#0f2744;padding:24px 32px;border-radius:12px 12px 0 0;">
        <div style="font-size:20px;color:white;">Facility<span style="color:#1a9e8a;">Truth</span> Report</div>
      </div>
      <div style="background:#f9f7f3;border:1px solid #eeebe3;border-top:none;border-radius:0 0 12px 12px;padding:32px;">
        <p style="font-size:16px;color:#1c1c1a;">Hi {job['customer_name']},</p>
        <p style="font-size:15px;color:#3d3d3a;line-height:1.7;">Your FacilityTruth report for <strong>{job['facility_name']}</strong> is attached. Your report found a <strong style="color:{'#b83232' if risk=='High' else '#c47820' if risk=='Moderate' else '#2a7d4a'};">{risk} overall risk rating</strong>.</p>
        <p style="font-size:15px;color:#3d3d3a;line-height:1.7;">Open the attached PDF to see the full analysis, facility track record, and your personalized negotiation guide.</p>
        <div style="background:white;border:1px solid #eeebe3;border-radius:8px;padding:16px 20px;margin:24px 0;">
          <p style="font-size:13px;color:#717169;margin:0 0 4px;">Report summary</p>
          <p style="font-size:15px;color:#1c1c1a;margin:0;">{contract_analysis.get('contract_summary','')[:200]}...</p>
        </div>
        <p style="font-size:13px;color:#717169;line-height:1.6;">This report is informational only and does not constitute legal advice. For complex situations we recommend consulting a qualified elder law attorney at naela.org.</p>
        <p style="font-size:13px;color:#717169;">Questions? Reply to this email or contact <a href="mailto:support@facilitytruth.com" style="color:#0f7c6b;">support@facilitytruth.com</a></p>
      </div>
      <p style="font-size:11px;color:#aaaa9f;text-align:center;margin-top:20px;">&copy; 2026 FacilityTruth · <a href="https://facilitytruth.com/terms.html" style="color:#aaaa9f;">Terms</a> · <a href="https://facilitytruth.com/privacy.html" style="color:#aaaa9f;">Privacy</a></p>
    </div>"""

    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=job['customer_email'],
        subject=f"Your FacilityTruth Report — {job['facility_name']}",
        html_content=html_body
    )

    # Attach PDF
    encoded_pdf = b64.b64encode(pdf_bytes).decode()
    attachment = Attachment(
        FileContent(encoded_pdf),
        FileName(f"FacilityTruth-Report-{job['facility_name'].replace(' ','-')}.pdf"),
        FileType("application/pdf"),
        Disposition("attachment")
    )
    message.attachment = attachment
    sg.send(message)

def send_confirmation_email(customer_email, customer_name, facility_name):
    """Send immediate confirmation that report is being prepared."""
    sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)

    html_body = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:40px 20px;">
      <div style="background:#0f2744;padding:24px 32px;border-radius:12px 12px 0 0;">
        <div style="font-size:20px;color:white;">Facility<span style="color:#1a9e8a;">Truth</span></div>
      </div>
      <div style="background:#f9f7f3;border:1px solid #eeebe3;border-top:none;border-radius:0 0 12px 12px;padding:32px;">
        <p style="font-size:16px;color:#1c1c1a;">Hi {customer_name},</p>
        <p style="font-size:15px;color:#3d3d3a;line-height:1.7;">We've received your contract for <strong>{facility_name}</strong> and your analysis is underway.</p>
        <p style="font-size:15px;color:#3d3d3a;line-height:1.7;">Your complete report will arrive at this email address within <strong>30 minutes</strong>.</p>
        <div style="background:#e8f6f4;border:1px solid #c2ebe5;border-radius:8px;padding:16px 20px;margin:24px 0;">
          <p style="font-size:14px;color:#0f7c6b;margin:0;">We are analyzing your contract clause by clause and pulling {facility_name}'s Medicare inspection record and facility data. Your report will include red flags, watch items, a negotiation guide, and an overall risk score.</p>
        </div>
        <p style="font-size:13px;color:#717169;">Questions? Contact <a href="mailto:support@facilitytruth.com" style="color:#0f7c6b;">support@facilitytruth.com</a></p>
      </div>
    </div>"""

    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=customer_email,
        subject=f"Your FacilityTruth report is being prepared — {facility_name}",
        html_content=html_body
    )
    sg.send(message)

def send_owner_notification(job, contract_analysis, cms_data, elapsed_seconds):
    """Send owner notification with operational data."""
    sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
    risk = contract_analysis.get("overall_risk", "Unknown")
    red_flags = contract_analysis.get("red_flags", [])
    elapsed_min = round(elapsed_seconds / 60, 1)

    top_flags = "".join([f"<li>{f.get('title','')}</li>" for f in red_flags[:3]])

    html_body = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;">
      <h2 style="color:#0f2744;">New FacilityTruth Report Delivered</h2>
      <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
        <tr><td style="padding:8px;border-bottom:1px solid #eee;color:#717169;">Customer</td><td style="padding:8px;border-bottom:1px solid #eee;">{job['customer_name']} ({job['customer_email']})</td></tr>
        <tr><td style="padding:8px;border-bottom:1px solid #eee;color:#717169;">Facility</td><td style="padding:8px;border-bottom:1px solid #eee;">{job['facility_name']}, {job['facility_state']}</td></tr>
        <tr><td style="padding:8px;border-bottom:1px solid #eee;color:#717169;">Risk rating</td><td style="padding:8px;border-bottom:1px solid #eee;color:{'#b83232' if risk=='High' else '#c47820' if risk=='Moderate' else '#2a7d4a'};font-weight:600;">{risk}</td></tr>
        <tr><td style="padding:8px;border-bottom:1px solid #eee;color:#717169;">Red flags</td><td style="padding:8px;border-bottom:1px solid #eee;">{len(red_flags)}</td></tr>
        <tr><td style="padding:8px;border-bottom:1px solid #eee;color:#717169;">CMS data found</td><td style="padding:8px;border-bottom:1px solid #eee;">{'Yes — ' + str(cms_data.get('overall_rating','N/A')) + ' stars' if cms_data.get('found') else 'No'}</td></tr>
        <tr><td style="padding:8px;border-bottom:1px solid #eee;color:#717169;">Pipeline time</td><td style="padding:8px;border-bottom:1px solid #eee;">{elapsed_min} minutes</td></tr>
      </table>
      <p style="color:#717169;font-size:13px;"><strong>Top flags found:</strong></p>
      <ul style="color:#3d3d3a;font-size:13px;">{top_flags}</ul>
    </div>"""

    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=NOTIFY_EMAIL,
        subject=f"FacilityTruth Report Delivered — {job['customer_name']} · {risk} Risk",
        html_content=html_body
    )
    sg.send(message)

# ── Main pipeline ──────────────────────────────────────────────────────
def run_pipeline(job_id, pdf_bytes):
    """Run the full analysis pipeline for a job."""
    start_time = datetime.now()
    job = get_job(job_id)
    if not job:
        print(f"Job {job_id} not found")
        return

    results = {}

    # Run all three jobs in parallel threads
    def job_contract():
        try:
            text = extract_text_from_pdf(pdf_bytes)
            if not text or len(text.strip()) < 100:
                update_job(job_id, status_contract="failed",
                          contract_text="Contract text could not be extracted. Document quality may be too low.")
                results["contract"] = {"overall_risk": "Unknown", "contract_summary": "Contract could not be analyzed — document quality insufficient.", "red_flags": [], "watch_items": [], "negotiable_items": [], "missing_protections": [], "risk_explanation": ""}
                return
            analysis = analyze_contract(text, job["facility_name"], job["facility_state"])
            update_job(job_id, status_contract="complete", contract_text=text)
            results["contract"] = analysis
        except Exception as e:
            print(f"Contract job error: {e}")
            update_job(job_id, status_contract="failed")
            results["contract"] = {"overall_risk": "Unknown", "contract_summary": "Contract analysis encountered an error.", "red_flags": [], "watch_items": [], "negotiable_items": [], "missing_protections": [], "risk_explanation": ""}

    def job_cms():
        try:
            cms = fetch_cms_data(job["facility_name"], job["facility_state"])
            update_job(job_id, status_cms="complete", cms_data=json.dumps(cms))
            results["cms"] = cms
        except Exception as e:
            print(f"CMS job error: {e}")
            update_job(job_id, status_cms="failed")
            results["cms"] = {"found": False, "provider_name": job["facility_name"]}

    def job_state():
        try:
            cms_id = results.get("cms", {}).get("cms_id", "")
            state = fetch_state_inspection_data(job["facility_name"], job["facility_state"], cms_id)
            update_job(job_id, status_state="complete", state_data=json.dumps(state))
            results["state"] = state
        except Exception as e:
            print(f"State job error: {e}")
            update_job(job_id, status_state="failed")
            results["state"] = {"available": False}

    # Start parallel threads
    t1 = threading.Thread(target=job_contract)
    t2 = threading.Thread(target=job_cms)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # State job can use CMS ID from completed CMS job
    t3 = threading.Thread(target=job_state)
    t3.start()
    t3.join()

    # Synthesize facility data
    try:
        facility_synthesis = synthesize_facility_data(
            results.get("cms", {}),
            results.get("state", {}),
            job["facility_name"]
        )
    except Exception as e:
        print(f"Synthesis error: {e}")
        facility_synthesis = {"facility_summary": "Facility data could not be fully analyzed.", "staffing_explanation": "", "inspection_explanation": "", "key_concerns": [], "questions_to_ask": []}

    # Generate PDF report
    try:
        html = generate_report_html(job, results["contract"], facility_synthesis, results.get("cms", {}), results.get("state", {}))
        pdf_bytes_report = generate_pdf(html)
        update_job(job_id, status_report="complete", delivered_at=datetime.now())
    except Exception as e:
        print(f"PDF generation error: {e}")
        pdf_bytes_report = None

    # Send report to customer
    if pdf_bytes_report:
        send_report_email(job, pdf_bytes_report, results["contract"])
    else:
        print(f"PDF generation failed for job {job_id} — report not delivered")

    # Send owner notification
    elapsed = (datetime.now() - start_time).total_seconds()
    send_owner_notification(job, results["contract"], results.get("cms", {}), elapsed)

# ── Flask routes ───────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return "FacilityTruth backend is running.", 200

@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    """Receive Stripe payment confirmation and kick off pipeline."""
    import stripe
    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
    webhook_secret = STRIPE_WEBHOOK_SECRET

    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except Exception as e:
        print(f"Stripe webhook error: {e}")
        return jsonify({"error": str(e)}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session.get("metadata", {})

        customer_name  = metadata.get("customer_name", "")
        customer_email = session.get("customer_email", metadata.get("customer_email", ""))
        facility_name  = metadata.get("facility_name", "")
        facility_state = metadata.get("facility_state", "")
        job_id         = metadata.get("job_id", str(uuid.uuid4()))

        # Send confirmation email immediately
        try:
            send_confirmation_email(customer_email, customer_name, facility_name)
        except Exception as e:
            print(f"Confirmation email error: {e}")

        # Create job record
        create_job(job_id, customer_name, customer_email, facility_name, facility_state)

        # Contract file will be retrieved separately via Typeform webhook
        # Store job_id for Typeform to reference

    return jsonify({"status": "ok"}), 200

@app.route("/webhook/typeform", methods=["POST"])
def typeform_webhook():
    """Receive Typeform submission with contract file."""
    try:
        data = request.json
        answers = data.get("form_response", {}).get("answers", [])
        definition = data.get("form_response", {}).get("definition", {})
        fields = definition.get("fields", [])

        field_map = {}
        for i, field in enumerate(fields):
            if i < len(answers):
                field_map[field.get("title", "").lower()] = answers[i]

        def get_val(answer):
            if not answer:
                return ""
            t = answer.get("type")
            if t == "text": return answer.get("text", "")
            if t == "email": return answer.get("email", "")
            if t == "choice": return answer.get("choice", {}).get("label", "")
            if t == "file_url": return answer.get("file_url", "")
            return ""

        customer_name  = ""
        customer_email = ""
        facility_name  = ""
        facility_state = ""
        file_url       = ""

        for title, answer in field_map.items():
            val = get_val(answer)
            if "name" in title: customer_name = val
            elif "email" in title: customer_email = val
            elif "facility name" in title or "facility" in title: facility_name = val
            elif "state" in title: facility_state = val
            elif "upload" in title or "contract" in title or "file" in title:
                file_url = answer.get("file_url", "")

        if not file_url:
            return jsonify({"error": "No file uploaded"}), 400

        # Download the contract file
        file_response = requests.get(file_url, timeout=60)
        pdf_bytes = file_response.content

        # Create job and start pipeline
        job_id = str(uuid.uuid4())
        create_job(job_id, customer_name, customer_email, facility_name, facility_state)

        # Send confirmation immediately
        send_confirmation_email(customer_email, customer_name, facility_name)

        # Run pipeline in background thread
        thread = threading.Thread(target=run_pipeline, args=(job_id, pdf_bytes))
        thread.daemon = True
        thread.start()

        return jsonify({"status": "ok", "job_id": job_id}), 200

    except Exception as e:
        print(f"Typeform webhook error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/webhook/payment-then-form", methods=["POST"])
def combined_webhook():
    """
    Alternative flow: Typeform collects everything including payment link.
    This endpoint handles the full intake in one shot for simpler setup.
    """
    return typeform_webhook()

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
