# responder.py — laptop-closed listener via GitHub Actions (every ~8 min).
# Single pass: read updates -> reply -> save offset. $0.
# Handles: "news" or /news -> 3-category best-of-best digest.
#          /detail N or button tap -> full Viral Pack. /start -> help.
import os
import json
import requests

from news_core import (fetch_all, build_pack, format_digest,
                       format_pack, keyboard_for_items)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
HERE = os.path.dirname(__file__)
STATE_FILE = os.path.join(HERE, "telegram_state.json")
CACHE_FILE = os.path.join(HERE, "news_cache.json")
SEEN_FILE = os.path.join(HERE, "seen.json")

HELP = ("⚡ <b>Instant Pipeline</b> — type <b>news</b> for best-of-best.\n"
        "💼 Business (FounderFiles) | 🎬 Entertainment (ViralVault) | 🤖 AI (BotBrief)\n"
        "Then <b>/detail N</b> or tap 📦 for the full Viral Pack "
        "(titles, hook, script, caption, hashtags, video links, Rohan + Reshab notes).")


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception as ex:
        print("save fail", path, str(ex)[:80])


def send(chat_id, text, reply_markup=None):
    if not TOKEN:
        print("SKIP send (no token):", text[:120])
        return False
    payload = {"chat_id": chat_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": False}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(f"{BASE}/sendMessage", json=payload, timeout=20)
        print("send:", r.status_code, "to", chat_id)
        return r.ok
    except Exception as ex:
        print("send err", str(ex)[:100])
        return False


def do_news(chat_id):
    seen = set(load_json(SEEN_FILE, []))
    items = fetch_all(seen=seen, max_per_run=9)
    cache = [{k: v for k, v in it.items() if k != "pp" and not k.startswith("_")}
             for it in items]
    # keep visible numbers stable for /detail
    for i, c in enumerate(cache, 1):
        c["_n"] = i
    save_json(CACHE_FILE, cache)
    if not items:
        send(chat_id, "Slow window — no best-of-best right now. "
                      "Rohan cuts 1 filler, Reshab schedules it.")
        return
    for it in items:
        seen.add(it["link"])
    save_json(SEEN_FILE, sorted(seen)[-1000:])
    flat = [{**c, "_n": i + 1} for i, c in enumerate(cache)]
    send(chat_id, format_digest(flat), reply_markup=keyboard_for_items(flat))
    print(f"news -> {chat_id}: {len(flat)} items")


def do_pack(chat_id, idx):
    cache = load_json(CACHE_FILE, [])
    if not cache or idx < 1 or idx > len(cache):
        send(chat_id, f"Pack #{idx} expired. Type news for a fresh scan.")
        return
    item = cache[idx - 1]
    send(chat_id, format_pack(idx, item, build_pack(item)))
    print(f"pack #{idx} -> {chat_id}")


def handle(upd):
    msg = upd.get("message") or upd.get("channel_post")
    if msg:
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        low = text.lower()
        if low in ("news", "/news", "/news@lynqstudiobot"):
            do_news(chat_id)
        elif low.startswith("/detail"):
            try:
                do_pack(chat_id, int(low.split()[1]))
            except Exception:
                send(chat_id, "Use /detail N — e.g. /detail 2")
        elif low.startswith("/start") or low.startswith("/help"):
            send(chat_id, HELP)
        return
    cb = upd.get("callback_query")
    if cb:
        try:
            requests.post(f"{BASE}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"]}, timeout=10)
        except Exception:
            pass
        if cb.get("data", "").startswith("pack:"):
            try:
                do_pack(cb["message"]["chat"]["id"], int(cb["data"].split(":")[1]))
            except Exception:
                pass


def main():
    if not TOKEN:
        print("No TELEGRAM_BOT_TOKEN.")
        return
    state = load_json(STATE_FILE, {})
    offset = state.get("last_update_id", 0) + 1
    try:
        r = requests.get(f"{BASE}/getUpdates",
                         params={"timeout": 10, "offset": offset}, timeout=25).json()
    except Exception as ex:
        print("poll err", str(ex)[:100])
        return
    results = r.get("result", [])
    print(f"updates: {len(results)}")
    for upd in results:
        try:
            handle(upd)
        except Exception as ex:
            print("handle err", str(ex)[:100])
        state["last_update_id"] = max(state.get("last_update_id", 0),
                                      upd.get("update_id", 0))
    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
