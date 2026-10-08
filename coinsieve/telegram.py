"""Telegram Bot API client. Message wording lives in digest.py, the card image in card.py.

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

    def _call(self, method, **kwargs):
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            r = requests.post(url, timeout=self.timeout, **kwargs)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise TelegramError(self._redact(e)) from None
        if not data.get("ok"):
            raise TelegramError(f"{data.get('error_code')} {self._redact(data.get('description'))}")
        return data["result"]["message_id"]

    def send(self, chat_id, text, parse_mode=None):
        """Send a text message; return its message_id."""
        payload = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._call("sendMessage", json=payload)

    def send_photo(self, chat_id, path, caption, parse_mode="HTML"):
        """Send a local image with a caption (<= 1024 chars); return its message_id."""
        with open(path, "rb") as f:
            return self._call("sendPhoto", data={"chat_id": chat_id, "caption": caption, "parse_mode": parse_mode},
                              files={"photo": f})
