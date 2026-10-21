"""Telegram bot messages (https://core.telegram.org/bots/api#sendmessage). Standard library only."""
import json
import os
import urllib.parse
import urllib.request


def _call(method, params):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", data=data, timeout=20) as r:
        return json.load(r)


def send(text):
    """Send an HTML message; prints instead when no bot is configured (dry runs, replays)."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not os.environ.get("TELEGRAM_CHAT_ID"):
        print("[telegram not configured]\n" + text + "\n")
        return
    try:
        _call("sendMessage", dict(chat_id=os.environ["TELEGRAM_CHAT_ID"], text=text, parse_mode="HTML",
                                  disable_web_page_preview="true"))
    except Exception as e:  # an alert that fails to send must not stop the morning
        print(f"Telegram send failed: {e}\n{text}")


def chat_ids():
    """Chats that have messaged the bot: send it any message first, then run `python main.py chat-id`."""
    d = _call("getUpdates", {})
    out = {}
    for u in d.get("result", []):
        c = (u.get("message") or u.get("channel_post") or {}).get("chat") or {}
        if c.get("id"):
            out[c["id"]] = c.get("username") or c.get("title") or c.get("first_name") or ""
    return out
