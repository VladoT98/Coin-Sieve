"""Telegram Bot API client. Message wording lives in digest.py.

Verified live: error {"ok": false, "error_code": 401, "description": "Unauthorized"};
success {"ok": true, "result": {"message_id": ...}}.
"""
import requests


class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token, timeout_s):
        self._token = token
        self.timeout = timeout_s

    def _redact(self, text):
        return str(text).replace(self._token, "<token>")  # request errors embed the URL

    def send(self, chat_id, text):
        """Send a plain-text message; return its message_id."""
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise TelegramError(self._redact(e)) from None
        if not data.get("ok"):
            raise TelegramError(f"{data.get('error_code')} {self._redact(data.get('description'))}")
        return data["result"]["message_id"]
