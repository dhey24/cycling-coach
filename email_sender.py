import os
import time
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import date


SMTP_HOST = "smtp.mail.me.com"
SMTP_PORT = 587
SMTP_RETRIES = 3
SMTP_RETRY_DELAY = 10


def _build_message(from_addr, to_addr, subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg.attach(MIMEText(html, "html"))
    return msg


def _smtp_send(from_addr, password, to_addr, msg):
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(from_addr, password)
        smtp.sendmail(from_addr, to_addr, msg.as_string())


def send_email(html, today=None):
    if today is None:
        today = date.today()
    from_addr = os.environ["ICLOUD_ADDRESS"]
    password  = os.environ["ICLOUD_APP_PASSWORD"]
    to_addr   = os.environ["RECIPIENT_EMAIL"]
    subject   = f"Cycling Coach — Week of {today.strftime('%B %-d, %Y')}"
    msg = _build_message(from_addr, to_addr, subject, html)

    last_err = None
    for attempt in range(1, SMTP_RETRIES + 1):
        try:
            _smtp_send(from_addr, password, to_addr, msg)
            print(f"Email sent to {to_addr}")
            return
        except Exception as e:
            last_err = e
            if attempt < SMTP_RETRIES:
                print(f"SMTP attempt {attempt} failed ({e}), retrying in {SMTP_RETRY_DELAY}s...")
                time.sleep(SMTP_RETRY_DELAY)

    raise last_err


def send_error_email(error, today=None):
    if today is None:
        today = date.today()
    from_addr = os.environ.get("ICLOUD_ADDRESS", "")
    password  = os.environ.get("ICLOUD_APP_PASSWORD", "")
    to_addr   = os.environ.get("RECIPIENT_EMAIL", "")
    if not all([from_addr, password, to_addr]):
        print(f"Cannot send error email. Error was: {error}")
        return

    subject = f"Cycling Coach FAILED — {today.strftime('%B %-d, %Y')}"
    html = f"""<html><body>
      <p style="font-family:sans-serif;color:#dc2626;">
        <strong>Cycling Coach failed to generate this week.</strong>
      </p>
      <pre style="background:#f3f4f6;padding:12px;border-radius:4px;font-size:13px;">{error}</pre>
      <p style="font-family:sans-serif;font-size:13px;color:#6b7280;">
        Check <code>~/Library/Logs/cycling-coach/stderr.log</code> for details.
      </p>
    </body></html>"""

    try:
        msg = _build_message(from_addr, to_addr, subject, html)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(from_addr, password)
            smtp.sendmail(from_addr, to_addr, msg.as_string())
        print("Error notification sent.")
    except Exception as e2:
        print(f"Also failed to send error email: {e2}")
