from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import pdfplumber
import requests
import tempfile
import os
import re
import logging
import threading

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
        total_pages = len(pdf.pages)
        logger.info(f"Processing {total_pages} pages...")

        for page_num, page in enumerate(pdf.pages):
            text = page.extract_text() or ''

            if page_num == 0:
                for line in text.split('\n'):
                    if 'PROVINCE :' in line:
                        province = title_case(line.split('PROVINCE :')[-1].strip())
                    elif 'CITY / MUNICIPALITY :' in line:
                        city = title_case(line.split('CITY / MUNICIPALITY :')[-1].strip())
                    elif 'BARANGAY :' in line:
                        barangay = title_case(line.split('BARANGAY :')[-1].strip())

            prec = PRECINCT_RE.search(text)
            if prec:
                current_precinct = prec.group(1).strip()

            words = page.extract_words(keep_blank_chars=False)
            if not words:
                continue

            lines_dict = {}
            for w in words:
                y_key = round(w['top'] / 3) * 3
                if y_key not in lines_dict:
                    lines_dict[y_key] = []
                lines_dict[y_key].append(w)

            for y_key in sorted(lines_dict.keys()):
                line_words = sorted(lines_dict[y_key], key=lambda w: w['x0'])

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

            if (page_num + 1) % 100 == 0:
                logger.info(f"Page {page_num+1}/{total_pages} — {len(voters)} voters")

    return voters

def insert_to_supabase(voters, pdf_upload_id, supabase_url, supabase_key):
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal"
    }

    total_inserted = 0
    batch_size = 100

    for i in range(0, len(voters), batch_size):
        batch = voters[i:i + batch_size]
        for v in batch:
            v['pdf_upload_id'] = pdf_upload_id
        try:
            res = requests.post(
                f"{supabase_url}/rest/v1/voters_list",
                json=batch,
                headers=headers,
                timeout=30
            )
            if res.status_code in (200, 201):
                total_inserted += len(batch)
                logger.info(f"Inserted batch {i//batch_size + 1} — total: {total_inserted}")
            else:
                logger.error(f"Insert error: {res.status_code} - {res.text[:200]}")
        except Exception as e:
            logger.error(f"Insert exception: {str(e)}")

    # Update pdf_uploads status
    try:
        update_res = requests.patch(
            f"{supabase_url}/rest/v1/pdf_uploads?id=eq.{pdf_upload_id}",
            json={
                "status": "completed",
                "records_count": total_inserted,
                "progress": 1.0,
            },
            headers=headers,
            timeout=30
        )
        logger.info(f"Status update: {update_res.status_code}")
    except Exception as e:
        logger.error(f"Status update error: {str(e)}")

    logger.info(f"COMPLETE! Total inserted: {total_inserted}")

def process_in_background(pdf_url, pdf_upload_id, supabase_url, supabase_key):
    logger.info(f"Background job started for: {pdf_upload_id}")
    tmp_path = None

    # Tracking stats
    total_in_pdf = 0
    total_inserted = 0
    total_duplicates = 0
    total_errors = 0
    duplicate_details = []
    error_details = []

    try:
        response = requests.get(pdf_url, timeout=120)
        response.raise_for_status()
        logger.info(f"Downloaded {len(response.content)} bytes")

        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name

        PRECINCT_RE = re.compile(r'Prec\s*:\s*(\S+)', re.IGNORECASE)
        NAME_COL_MAX_X = 370
        MARKER_MAP = {'*': 'youth', 'A': 'illiterate', 'B': 'pwd', 'C': 'senior'}

        headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }

        # Get total pages first
        with pdfplumber.open(tmp_path) as pdf:
            total_pages = len(pdf.pages)
        logger.info(f"Total pages: {total_pages}")

        current_precinct = ''
        province = ''
        city = ''
        barangay = ''
        BATCH_SIZE = 30
        PAGE_INSERT_SIZE = 100

        for batch_start in range(0, total_pages, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, total_pages)
            batch_voters = []

            # Open and close PDF fresh for each batch
            with pdfplumber.open(tmp_path) as pdf:
                for page_num in range(batch_start, batch_end):
                    page = pdf.pages[page_num]
                    text = page.extract_text() or ''

                    if page_num == 0:
                        for line in text.split('\n'):
                            if 'PROVINCE :' in line:
                                province = title_case(line.split('PROVINCE :')[-1].strip())
                            elif 'CITY / MUNICIPALITY :' in line:
                                city = title_case(line.split('CITY / MUNICIPALITY :')[-1].strip())
                            elif 'BARANGAY :' in line:
                                barangay = title_case(line.split('BARANGAY :')[-1].strip())

                    prec = PRECINCT_RE.search(text)
                    if prec:
                        current_precinct = prec.group(1).strip()

                    words = page.extract_words(keep_blank_chars=False)
                    if not words:
                        continue

                    lines_dict = {}
                    for w in words:
                        y_key = round(w['top'] / 3) * 3
                        if y_key not in lines_dict:
                            lines_dict[y_key] = []
                        lines_dict[y_key].append(w)

                    for y_key in sorted(lines_dict.keys()):
                        line_words = sorted(
                            lines_dict[y_key], key=lambda w: w['x0'])
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
                        last_name, first_name, middle_name = parse_name(full_name)

                        batch_voters.append({
                            'voter_no': voter_no,
                            'precinct_no': current_precinct,
                            'last_name': last_name,
                            'first_name': first_name,
                            'middle_name': middle_name,
                            'address': title_case(' '.join(addr_words)),
                            'barangay': barangay,
                            'city': city,
                            'province': province,
                            'voter_category': MARKER_MAP.get(marker, 'regular'),
                            'survey_status': 'pending',
                            'pdf_upload_id': pdf_upload_id
                        })

            # Insert batch with duplicate tracking
            for i in range(0, len(batch_voters), PAGE_INSERT_SIZE):
                insert_batch = batch_voters[i:i + PAGE_INSERT_SIZE]
                total_in_pdf += len(insert_batch)

                try:
                    # Try bulk insert first
                    res = requests.post(
                        f"{supabase_url}/rest/v1/voters_list",
                        json=insert_batch,
                        headers=headers,
                        timeout=30
                    )

                    if res.status_code in (200, 201):
                        # All inserted successfully
                        total_inserted += len(insert_batch)

                    elif res.status_code == 409:
                        # Bulk had duplicates — insert one by one
                        for voter in insert_batch:
                            try:
                                single = requests.post(
                                    f"{supabase_url}/rest/v1/voters_list",
                                    json=voter,
                                    headers=headers,
                                    timeout=10
                                )
                                if single.status_code in (200, 201):
                                    total_inserted += 1
                                elif single.status_code == 409:
                                    total_duplicates += 1
                                    # Only store first 100 duplicates
                                    if len(duplicate_details) < 100:
                                        duplicate_details.append({
                                            "precinct": voter.get("precinct_no"),
                                            "voter_no": voter.get("voter_no"),
                                            "name": f"{voter.get('last_name')}, {voter.get('first_name')}"
                                        })
                                else:
                                    total_errors += 1
                                    if len(error_details) < 50:
                                        error_details.append({
                                            "precinct": voter.get("precinct_no"),
                                            "voter_no": voter.get("voter_no"),
                                            "error": single.text[:100]
                                        })
                            except Exception as e:
                                total_errors += 1
                    else:
                        total_errors += len(insert_batch)
                        if len(error_details) < 50:
                            error_details.append({
                                "batch": f"{batch_start}-{batch_end}",
                                "error": res.text[:100]
                            })

                except Exception as e:
                    total_errors += len(insert_batch)
                    if len(error_details) < 50:
                        error_details.append({
                            "batch": f"{batch_start}-{batch_end}",
                            "error": str(e)
                        })

            logger.info(f"Pages {batch_start}-{batch_end} done — inserted: {total_inserted} dupes: {total_duplicates}")

            # Free memory
            del batch_voters
            import gc
            gc.collect()

        # Log final report
        logger.info(f"""
EXTRACTION REPORT:
  Total in PDF:   {total_in_pdf}
  Inserted:       {total_inserted}
  Duplicates:     {total_duplicates}
  Errors:         {total_errors}
        """)

        # Generate CSV
        try:
            logger.info("Generating CSV...")
            csv_headers = "precinct_no,voter_no,last_name,first_name,middle_name,address,barangay,city,province,voter_category\n"
            csv_rows = []
            offset = 0

            while True:
                res = requests.get(
                    f"{supabase_url}/rest/v1/voters_list",
                    params={
                        "pdf_upload_id": f"eq.{pdf_upload_id}",
                        "order": "precinct_no.asc,voter_no.asc",
                        "limit": 1000,
                        "offset": offset,
                        "select": "precinct_no,voter_no,last_name,first_name,middle_name,address,barangay,city,province,voter_category"
                    },
                    headers=headers,
                    timeout=30
                )
                if res.status_code != 200:
                    break
                batch = res.json()
                if not batch:
                    break
                for v in batch:
                    csv_rows.append(",".join([
                        f'"{v.get("precinct_no","")}"',
                        f'"{v.get("voter_no","")}"',
                        f'"{v.get("last_name","")}"',
                        f'"{v.get("first_name","")}"',
                        f'"{v.get("middle_name","")}"',
                        f'"{v.get("address","")}"',
                        f'"{v.get("barangay","")}"',
                        f'"{v.get("city","")}"',
                        f'"{v.get("province","")}"',
                        f'"{v.get("voter_category","")}"',
                    ]))
                offset += 1000
                if len(batch) < 1000:
                    break

            csv_content = csv_headers + "\n".join(csv_rows)
            csv_filename = f"converted_{pdf_upload_id}.csv"

            upload_res = requests.post(
                f"{supabase_url}/storage/v1/object/csv-exports/{csv_filename}",
                data=csv_content.encode('utf-8'),
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": "text/csv",
                    "x-upsert": "true"
                },
                timeout=60
            )

            if upload_res.status_code in (200, 201):
                logger.info(f"CSV ready: {csv_filename} with {len(csv_rows)} rows")
            else:
                logger.error(f"CSV error: {upload_res.text[:200]}")

        except Exception as e:
            logger.error(f"CSV generation error: {str(e)}")

        # Update pdf_uploads with full report
        requests.patch(
            f"{supabase_url}/rest/v1/pdf_uploads?id=eq.{pdf_upload_id}",
            json={
                "status": "completed",
                "records_count": total_inserted,
                "progress": 1.0,
                "total_in_pdf": total_in_pdf,
                "total_inserted": total_inserted,
                "total_duplicates": total_duplicates,
                "total_errors": total_errors,
                "duplicate_details": duplicate_details,
                "error_details": error_details,
            },
            headers=headers,
            timeout=30
        )

        logger.info(f"COMPLETE! Inserted: {total_inserted} Dupes: {total_duplicates}")

    except Exception as e:
        logger.error(f"Background job error: {str(e)}")
        try:
            requests.patch(
                f"{supabase_url}/rest/v1/pdf_uploads?id=eq.{pdf_upload_id}",
                json={
                    "status": "error",
                    "error_message": str(e),
                    "total_in_pdf": total_in_pdf,
                    "total_inserted": total_inserted,
                    "total_duplicates": total_duplicates,
                    "total_errors": total_errors,
                },
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": "application/json"
                },
                timeout=10
            )
        except:
            pass
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except:
                pass
                
class ExtractRequest(BaseModel):
    pdf_url: str
    pdf_upload_id: str
    supabase_url: str
    supabase_key: str

class TestExtractRequest(BaseModel):
    pdf_url: str

@app.get("/")
def health_check():
    return {"status": "ok", "service": "v-extractor"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/test-extract")
async def test_extract(req: TestExtractRequest):
    """Test endpoint - no Supabase needed, returns first 10 voters"""
    logger.info(f"TEST extract: {req.pdf_url}")
    try:
        response = requests.get(req.pdf_url, timeout=120)
        response.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {str(e)}")

    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        tmp.write(response.content)
        tmp_path = tmp.name

    try:
        # Only process first 20 pages for quick test
        voters = []
        PRECINCT_RE = re.compile(r'Prec\s*:\s*(\S+)', re.IGNORECASE)
        NAME_COL_MAX_X = 370
        MARKER_MAP = {'*': 'youth', 'A': 'illiterate', 'B': 'pwd', 'C': 'senior'}
        current_precinct = ''

        with pdfplumber.open(tmp_path) as pdf:
            for page_num, page in enumerate(pdf.pages[:20]):
                text = page.extract_text() or ''
                prec = PRECINCT_RE.search(text)
                if prec:
                    current_precinct = prec.group(1).strip()
                words = page.extract_words(keep_blank_chars=False)
                if not words:
                    continue
                lines_dict = {}
                for w in words:
                    y_key = round(w['top'] / 3) * 3
                    if y_key not in lines_dict:
                        lines_dict[y_key] = []
                    lines_dict[y_key].append(w)
                for y_key in sorted(lines_dict.keys()):
                    line_words = sorted(lines_dict[y_key], key=lambda w: w['x0'])
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
                    last_name, first_name, middle_name = parse_name(full_name)
                    voters.append({
                        'voter_no': voter_no,
                        'precinct_no': current_precinct,
                        'last_name': last_name,
                        'first_name': first_name,
                        'middle_name': middle_name,
                        'address': title_case(' '.join(addr_words)),
                    })

        return {
            "success": True,
            "voters_from_first_20_pages": len(voters),
            "sample": voters[:10],
            "message": f"Test successful! Found {len(voters)} voters in first 20 pages."
        }
    finally:
        try:
            os.unlink(tmp_path)
        except:
            pass

@app.post("/extract")
async def extract(req: ExtractRequest, background_tasks: BackgroundTasks):
    """Main extract endpoint - runs in background, returns immediately"""
    logger.info(f"Extract request for: {req.pdf_upload_id}")

    # Start background processing
    background_tasks.add_task(
        process_in_background,
        req.pdf_url,
        req.pdf_upload_id,
        req.supabase_url,
        req.supabase_key
    )

    # Return immediately — processing continues in background
    return {
        "success": True,
        "message": "Extraction started in background",
        "pdf_upload_id": req.pdf_upload_id
    }
