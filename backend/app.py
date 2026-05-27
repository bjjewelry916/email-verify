import csv
import io
import re
import smtplib
import socket
import dns.resolver
import os
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

EMAIL_REGEX = re.compile(r"[^@]+@[^@]+\.[^@]+")

DISPOSABLE_DOMAINS = {
    "mailinator.com",
    "10minutemail.com",
    "guerrillamail.com"
}

ROLE_BASED_PREFIXES = {
    "info",
    "support",
    "admin",
    "sales",
    "contact"
}

def check_email(email):
    if not EMAIL_REGEX.match(email):
        return "invalid", "bad_syntax"

    domain = email.split('@')[1]
    local = email.split('@')[0]

    if domain.lower() in DISPOSABLE_DOMAINS:
        return "invalid", "disposable_domain"

    if local.lower() in ROLE_BASED_PREFIXES:
        return "invalid", "role_based"

    try:
        records = dns.resolver.resolve(domain, 'MX')
        mx_record = str(records[0].exchange)
    except Exception:
        return "invalid", "no_mx"

    try:
        # Reduced timeout slightly to fail faster on dead servers
        server = smtplib.SMTP(timeout=3)
        server.connect(mx_record)
        server.helo("example.com")
        server.mail("verify@example.com")
        code, _ = server.rcpt(email)
        server.quit()

        if code == 250:
            return "valid", "smtp_ok"
        elif code == 550:
            return "invalid", "smtp_reject"
        else:
            return "risky", f"smtp_{code}"

    except socket.timeout:
        return "risky", "timeout"
    except Exception:
        return "risky", "smtp_error"

@app.route('/verify', methods=['POST'])
def verify():
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['file']

    if not file.filename.endswith('.csv'):
        return jsonify({"error": "Only CSV files allowed"}), 400

    try:
        content = file.read().decode('utf-8', errors='ignore')
    except Exception:
        return jsonify({"error": "Could not read CSV"}), 400

    reader = list(csv.DictReader(io.StringIO(content)))

    if not reader:
        return jsonify({"error": "CSV is empty"}), 400

    email_field = next(
        (f for f in reader[0].keys() if f.lower().strip() == 'email'),
        None
    )

    if not email_field:
        return jsonify({"error": "No email column found"}), 400

    # Helper function to process a single row
    def process_row(row):
        email = (row.get(email_field) or '').strip()
        if not email:
            status, reason = "invalid", "empty_email"
        else:
            status, reason = check_email(email)
        
        row['status'] = status
        row['reason'] = reason
        return row

    # Use ThreadPoolExecutor to process up to 20 emails concurrently
    # This turns a 5-minute job into a 15-second job.
    with ThreadPoolExecutor(max_workers=20) as executor:
        processed_rows = list(executor.map(process_row, reader))

    output = io.StringIO()
    fieldnames = list(reader[0].keys()) + ['status', 'reason']
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    
    writer.writeheader()
    writer.writerows(processed_rows)

    output.seek(0)

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            "Content-Disposition": f"attachment; filename=verified-{file.filename}"
        }
    )

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
    # Removed the crashing `gunicorn app:app` line