import csv
import io
import re
import time
import uuid
import threading
import dns.resolver
import smtplib
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# --- CONFIGURATION & CONSTANTS ---
# Strict regex to ensure standard formatting (blocks illegal chars/spaces)
EMAIL_REGEX = re.compile(r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$")
DISPOSABLE_DOMAINS = {"mailinator.com", "10minutemail.com", "guerrillamail.com"}
ROLE_BASED_PREFIXES = {"info", "support", "admin", "sales", "contact"}

# In-memory dictionary to track background jobs
jobs_data = {}

# --- STEP-BY-STEP EMAIL VERIFICATION LOGIC ---
def check_email(email):
    # STEP 1: Syntax & Basic Formatting Check
    if not EMAIL_REGEX.match(email):
        return "invalid", "bad_syntax"

    local_part, domain = email.split('@')

    if domain.lower() in DISPOSABLE_DOMAINS:
        return "invalid", "disposable_domain"
    if local_part.lower() in ROLE_BASED_PREFIXES:
        return "invalid", "role_based"

    # STEP 2: DNS & MX Verification
    try:
        records = dns.resolver.resolve(domain, 'MX')
        # Grab the highest priority mail server
        mx_record = str(records[0].exchange)
    except dns.resolver.NXDOMAIN:
        return "invalid", "domain_does_not_exist"
    except Exception:
        return "invalid", "no_mx_records"

    # STEP 3: SMTP Handshake (The Mailbox Check)
    def smtp_handshake():
        try:
            # Connect to the specific mail server (timeout prevents infinite hangs)
            server = smtplib.SMTP(timeout=5)
            server.connect(mx_record)
            
            # Start conversation
            server.helo("example.com")
            server.mail("verifier@example.com")
            
            # Ask the server if this specific mailbox exists
            code, _ = server.rcpt(email)
            
            # Disconnect BEFORE sending an email
            server.quit()
            return code
        except Exception:
            return None

    code = smtp_handshake()

    # Handle "Grey-listing" (If server says "try again later", wait 5s and retry)
    if code in [421, 450, 451, 452, 503]:
        time.sleep(5)
        code = smtp_handshake()

    # Evaluate the final SMTP response code
    if code == 250:
        return "valid", "smtp_ok_mailbox_exists"
    elif code == 550:
        return "invalid", "smtp_reject_mailbox_not_found"
    elif code is None:
        return "risky", "smtp_connection_timeout"
    else:
        return "risky", f"smtp_unclear_response_{code}"

# --- ROUTES ---
@app.route('/verify', methods=['POST'])
def verify():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['file']
    job_id = str(uuid.uuid4())
    
    # Read the CSV into memory
    content = file.read().decode('utf-8', errors='ignore')
    reader = list(csv.DictReader(io.StringIO(content)))
    
    if not reader:
        return jsonify({"error": "Empty CSV"}), 400
        
    total_rows = len(reader)
    email_field = next((f for f in reader[0].keys() if f.lower().strip() == 'email'), None)

    if not email_field:
        return jsonify({"error": "No 'email' column found"}), 400

    # Prepare output CSV structure
    output = io.StringIO()
    fieldnames = list(reader[0].keys()) + ['status', 'reason']
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    # Initialize job state
    jobs_data[job_id] = {
        "progress": 0,
        "row": 0,
        "total": total_rows,
        "log": "Starting...",
        "cancel": False,
        "output": output,
        "writer": writer,
        "records": reader,
        "email_field": email_field,
        "filename": file.filename
    }

    # Background processing function
    def process_job():
        for i, row in enumerate(reader, start=1):
            if jobs_data[job_id]['cancel']:
                jobs_data[job_id]['log'] = f"❌ Canceled job {job_id}"
                break
            
            email = (row.get(email_field) or '').strip()
            if not email:
                status, reason = 'invalid', 'empty_email'
            else:
                status, reason = check_email(email)
            
            row['status'] = status
            row['reason'] = reason
            writer.writerow(row)
            
            percent = int((i / total_rows) * 100)
            jobs_data[job_id].update({
                "progress": percent, 
                "row": i,
                "log": f"✅ {email} → {status} ({reason})"
            })

    # Start processing in a separate thread so the server doesn't freeze
    threading.Thread(target=process_job).start()

    return jsonify({"job_id": job_id})

@app.route('/progress')
def progress():
    job_id = request.args.get("job_id")
    d = jobs_data.get(job_id, {})
    return jsonify({"percent": d.get("progress", 0), "row": d.get("row", 0), "total": d.get("total", 0)})

@app.route('/log')
def log():
    job_id = request.args.get("job_id")
    return Response(jobs_data.get(job_id, {}).get("log", ""), mimetype='text/plain')

@app.route('/cancel', methods=['POST'])
def cancel():
    job_id = request.args.get("job_id")
    if job_id in jobs_data:
        jobs_data[job_id]['cancel'] = True
    return '', 204

@app.route('/download')
def download():
    job_id = request.args.get("job_id")
    filter_type = request.args.get("type", "all")
    job = jobs_data.get(job_id)
    
    if not job:
        return "Invalid job ID", 404

    # Read the completed CSV from memory
    job['output'].seek(0)
    reader = list(csv.DictReader(job['output']))

    # Apply filters based on the user's click
    if filter_type == "valid":
        filtered = [row for row in reader if row['status'] == 'valid']
    elif filter_type == "risky":
        filtered = [row for row in reader if row['status'] == 'risky']
    elif filter_type == "risky_invalid":
        filtered = [row for row in reader if row['status'] in ('risky', 'invalid')]
    else:
        filtered = reader

    # Write filtered results to a new CSV string
    output = io.StringIO()
    if len(filtered) > 0:
        writer = csv.DictWriter(output, fieldnames=filtered[0].keys())
        writer.writeheader()
        writer.writerows(filtered)

    output.seek(0)
    download_name = f"{filter_type}-{job['filename']}"
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={"Content-Disposition": f"attachment; filename={download_name}"}
    )

if __name__ == '__main__':
    import os
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5050))
    )