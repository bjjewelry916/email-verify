import csv
import io
import re
import dns.resolver
import smtplib
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
        server = smtplib.SMTP(timeout=10)
        server.connect(mx_record)
        server.helo("example.com")
        server.mail("probe@example.com")
        code, _ = server.rcpt(f"doesnotexist123@{domain}")
        server.quit()

        if code == 250:
            return "risky", "domain_accepts_all"

    except Exception:
        pass

    try:
        server = smtplib.SMTP(timeout=10)
        server.connect(mx_record)
        server.helo("example.com")
        server.mail("verifier@example.com")
        code, _ = server.rcpt(email)
        server.quit()

    except Exception:
        return "risky", "smtp_timeout"

    if code == 250:
        return "valid", "smtp_ok"
    elif code == 550:
        return "invalid", "smtp_reject"
    else:
        return "risky", f"smtp_{code}"

@app.route('/api/verify', methods=['POST'])
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

    output = io.StringIO()

    fieldnames = list(reader[0].keys()) + ['status', 'reason']

    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    results = []

    for row in reader:
        email = (row.get(email_field) or '').strip()

        if not email:
            status, reason = 'invalid', 'empty_email'
        else:
            status, reason = check_email(email)

        row['status'] = status
        row['reason'] = reason

        writer.writerow(row)

        results.append({
            "email": email,
            "status": status,
            "reason": reason
        })

    output.seek(0)

    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            "Content-Disposition": f"attachment; filename=verified-{file.filename}"
        }
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

    