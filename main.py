from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import pdfplumber
import requests
import tempfile
import os
import re

app = FastAPI(title="V-Extractor", version="1.0.0")

def title_case_name(s):
    if not s:
        return ''
    words = s.strip().split()
    result = []
    for w in words:
        if w.upper() in ('JR.', 'SR.', 'III', 'II', 'IV', 'MA.', 'STO.', 'STA.'):
            result.append(w.upper())
        else:
            result.append(w.capitalize())
    return ' '.join(result)

def title_case(s):
    if not s:
        return ''
    return ' '.join(w.capitalize() for w in s.strip().split())

def parse_name(full_name):
    full_name = full_name.strip()
    if ',' in full_name:
        parts = full_name.split(',', 1)
        last_name = title_case_name(parts[0].strip())
        rest = parts[1].strip().split()
        first_name = title_case_name(rest[0]) if rest else ''
        middle_name = title_case_name(' '.join(rest[1:])) if len(rest) > 1 else ''
    else:
        last_name = title_case_name(full_name)
        first_name = middle_name = ''
    return last_name, first_name, middle_name

def extract_voters_from_pdf(pdf_path):
    PRECINCT_RE = re.compile(r'Prec\s*:\s*(\S+)', re.IGNORECASE)
    NAME_COL_MAX_X = 370
    MARKER_MAP = {'*': 'youth', 'A': 'illiterate', 'B': 'pwd', 'C': 'senior'}

    voters = []
    current_precinct = ''
    province = ''
    city = ''
    barangay = ''

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            text = page.extract_text() or ''

            # Extract header info from first page
            if page_num == 0:
                for line in text.split('\n'):
                    if 'PROVINCE :' in line:
                        province = title_case(line.split('PROVINCE :')[-1].strip())
                    elif 'CITY / MUNICIPALITY :' in line:
                        city = title_case(line.split('CITY / MUNICIPALITY :')[-1].strip())
                    elif 'BARANGAY :' in line:
                        barangay = title_case(line.split('BARANGAY :')[-1].strip())

            # Get precinct number
            prec = PRECINCT_RE.search(text)
            if prec:
                current_precinct = prec.group(1).strip()

            # Extract using word bounding boxes
            words = page.extract_words(keep_blank_chars=False)
            if not words:
                continue

            # Group words by vertical position
            lines_dict = {}
            for w in words:
                y_key = round(w['top'] / 3) * 3
                if y_key not in lines_dict:
                    lines_dict[y_key] = []
                lines_dict[y_key].append(w)

            for y_key in sorted(lines_dict.keys()):
                line_words = sorted(lines_dict[y_key], key=lambda w: w['x0'])

                # Must start with a number
                if not line_words[0]['text'].isdigit():
                    continue

                voter_no = int(line_words[0]['text'])
                name_words = []
                addr_words = []
                marker = ''

                for w in line_words[1:]:
                    txt = w['text']
                    x = w['x0']
                    if x < NAME_COL_MAX_X:
                        if txt in ('*', 'A', 'B', 'C') and not name_words:
                            marker = txt
                        else:
                            name_words.append(txt)
                    else:
                        addr_words.append(txt)

                if not name_words:
                    continue

                full_name = ' '.join(name_words)
                address = title_case(' '.join(addr_words))
                last_name, first_name, middle_name = parse_name(full_name)

                voters.append({
                    'voter_no': voter_no,
                    'precinct_no': current_precinct,
                    'last_name': last_name,
                    'first_name': first_name,
                    'middle_name': middle_name,
                    'address': address,
                    'barangay': barangay,
                    'city': city,
                    'province': province,
                    'voter_category': MARKER_MAP.get(marker, 'regular'),
                    'survey_status': 'pending'
                })

    return voters

class ExtractRequest(BaseModel):
    pdf_url: str
    pdf_upload_id: str
    supabase_url: str
    supabase_key: str

@app.get("/")
def health_check():
    return {"status": "ok", "service": "v-extractor"}

@app.post("/extract")
async def extract(req: ExtractRequest):
    # Download PDF from Supabase storage
    try:
        response = requests.get(req.pdf_url, timeout=60)
        response.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to download PDF: {str(e)}")

    # Save to temp file
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        tmp.write(response.content)
        tmp_path = tmp.name

    try:
        # Extract voters
        voters = extract_voters_from_pdf(tmp_path)

        if not voters:
            return {
                "success": False,
                "records_extracted": 0,
                "message": "No voters found in PDF"
            }

        # Insert to Supabase in batches of 100
        headers = {
            "apikey": req.supabase_key,
            "Authorization": f"Bearer {req.supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }

        total_inserted = 0
        batch_size = 100
        errors = []

        for i in range(0, len(voters), batch_size):
            batch = voters[i:i + batch_size]
            for v in batch:
                v['pdf_upload_id'] = req.pdf_upload_id

            res = requests.post(
                f"{req.supabase_url}/rest/v1/voters_list",
                json=batch,
                headers=headers,
                timeout=30
            )

            if res.status_code in (200, 201):
                total_inserted += len(batch)
            else:
                errors.append(f"Batch {i}: {res.text[:100]}")

        return {
            "success": True,
            "records_extracted": len(voters),
            "records_inserted": total_inserted,
            "errors": errors[:3] if errors else [],
            "message": f"Extracted {len(voters)} voters, inserted {total_inserted}"
        }

    finally:
        os.unlink(tmp_path)
