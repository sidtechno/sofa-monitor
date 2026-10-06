# Main script for testing the sofa monitor
# main.py

import os

os.environ["EMAIL_USER"] = "eric.dugal@gmail.com"
os.environ["EMAIL_PASSWORD"] = "alic iivk mlpd dsaz"
os.environ["EMAIL_TO"] = "eric.dugal@hotmail.com"

from notify import send_email

send_email(
    "Sofa test",
    "Your sofa monitor works!"
    )