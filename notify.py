# Notification module for sending emails
# notify.py

import os
import smtplib
from email.message import EmailMessage


def send_email(subject: str, body: str) -> None:
    email_user = os.getenv("EMAIL_USER")
    email_password = os.getenv("EMAIL_PASSWORD")
    email_to = os.getenv("EMAIL_TO")
    missing = [
        name
        for name, value in (
            ("EMAIL_USER", email_user),
            ("EMAIL_PASSWORD", email_password),
            ("EMAIL_TO", email_to),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required email environment variable(s): "
            + ", ".join(missing)
        )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_user
    msg["To"] = email_to
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(email_user, email_password)
        smtp.send_message(msg)