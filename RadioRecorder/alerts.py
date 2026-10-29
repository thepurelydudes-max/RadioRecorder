"""Optional email warning for prolonged audio silence."""
from __future__ import annotations
from datetime import datetime
from email.message import EmailMessage
import smtplib
import ssl


def send_silence_email(config: dict) -> None:
    recipient = str(config.get('email_to', '')).strip()
    server = str(config.get('smtp_server', '')).strip()
    if not recipient or not server:
        raise ValueError('Для email-уведомления заполни SMTP-сервер и получателя')
    account = str(config.get('smtp_user', '')).strip()
    password = str(config.get('smtp_password', ''))
    port = int(config.get('smtp_port', 587))
    note = EmailMessage()
    note['From'] = account or recipient
    note['To'] = recipient
    note['Subject'] = 'RadioRecorder: обнаружена тишина'
    note.set_content('В аудиовходе обнаружена тишина.\n'
                     f'Время: {datetime.now():%Y-%m-%d %H:%M:%S}\n'
                     f'Порог: {config.get("silence_db", -60)} dB\n'
                     f'Длительность: {config.get("silence_seconds", 10)} сек.\n')
    if port == 465:
        with smtplib.SMTP_SSL(server, port, timeout=12, context=ssl.create_default_context()) as conn:
            if account:
                conn.login(account, password)
            conn.send_message(note)
    else:
        with smtplib.SMTP(server, port, timeout=12) as conn:
            if config.get('smtp_tls', True):
                conn.starttls(context=ssl.create_default_context())
            if account:
                conn.login(account, password)
            conn.send_message(note)
