# tbot.py — interactive Telegram bot for Instant Pipeline (Rohan edits, Reshab posts).
# $0, requests-only polling (no extra deps).
# Commands: /start /news /detail N
# Flow: /news -> digest + buttons -> tap Pack #N -> full Viral Pack.
# Digest mode (GitHub Actions every 2h) uses bot.py. This file is for live interaction.
import os
import sys
import json
import time
import requests

from news_core import fetch_all, build_pack, format_digest, format_pack, keyboard_for_items, parse_command

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
BASE = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""
CACHE_FILE = os.path.join(os.path.dirname(__file__), "news_cache.json")
SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen.json")

HELP = (
    "⚡ <b>Instant Pipeline Bot</b>\n"
    "Rohan (edit) + Reshab (post) + You (pick).\n\n"
    "<b>Commands:</b>\n"
    "/news — fresh scan (last ~6h), grouped for 3 pages\n"
    "/detail N — viral pack for item N\n"
    "/help — this\n\n"
    "Flow: run /news every 2h → tap 📦 Pack button on the one you confirm → "
    "bot replies with titles, hook, script, caption, hashtags, video links, "
    "Rohan edit notes + Reshab post notes."
)


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_cache(items):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump(items, f, default=str)
    except Exception as ex:
        print("cache save fail", ex)


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def api(method, payload):
    r = requests.post(f"{BASE}/{method}", json=payload, timeout=20)
    return r.ok


def send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": False}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api("sendMessage", payload)


def do_news(chat_id, boost=1.0, only_cat=None, limit=10):
    seen = load_seen()
    items = fetch_all(seen=seen, max_per_run=limit, window_boost=boost,
                      only_cat=only_cat)
    # strip non-serializable pp tuples for cache
    cache_items = []
    for it in items:
        c = {k: v for k, v in it.items() if k != "pp"}
        cache_items.append(c)
    save_cache(cache_items)
    if not items:
        send(chat_id, "No fresh news in last 6h. Filler bank day — have Rohan cut 1 filler, Reshab schedule it.")
        return
    send(chat_id, format_digest(cache_items), reply_markup=keyboard_for_items(cache_items))
    print(f"news sent: {len(cache_items)}")


def do_pack(chat_id, idx):
    items = load_cache()
    if not items or idx < 1 or idx > len(items):
        send(chat_id, f"Pack #{idx} not found. Run /news first.")
        return
    item = items[idx - 1]
    pack = build_pack(item)
    send(chat_id, format_pack(idx, item, pack))
    print(f"pack sent: #{idx} {item['title'][:60]}")


def handle_update(upd):
    msg = upd.get("message") or upd.get("channel_post")
    if msg:
        chat_id = msg["chat"]["id"]
        text = (msg.get("text") or "").strip()
        action = parse_command(text)
        if action[0] == "help":
            send(chat_id, HELP)
        elif action[0] == "news":
            _, boost, cat = action
            do_news(chat_id, boost=boost, only_cat=cat)
        elif action[0] == "detail":
            do_pack(chat_id, action[1])
        return
    cb = upd.get("callback_query")
    if cb:
        chat_id = cb["message"]["chat"]["id"]
        data = cb.get("data", "")
        # ack to remove spinner
        try:
            requests.post(f"{BASE}/answerCallbackQuery",
                          json={"callback_query_id": cb["id"]}, timeout=10)
        except Exception:
            pass
        if data.startswith("pack:"):
            try:
                do_pack(chat_id, int(data.split(":")[1]))
            except Exception:
                send(chat_id, "Bad button. Run /news again.")


def poll(auto_hours=2):
    if not TOKEN:
        print("No TELEGRAM_BOT_TOKEN. Run with --test for dry-run.")
        return
    auto_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    print(f"tbot polling... auto-push every {auto_hours}h to {auto_chat or '(no group set)'}."
          " Commands: /news /detail N")
    offset = 0
    last_push = 0
    while True:
        try:
            # auto-push every 2h so digest + buttons share the same cache (no mismatch)
            now = time.time()
            if auto_chat and (now - last_push) > auto_hours * 3600:
                try:
                    do_news(auto_chat, max_age=6, limit=10)
                    last_push = now
                except Exception as ex:
                    print("auto-push err", ex)
            r = requests.get(f"{BASE}/getUpdates",
                             params={"timeout": 30, "offset": offset}, timeout=40)
            data = r.json()
            for upd in data.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                handle_update(upd)
        except KeyboardInterrupt:
            break
        except Exception as ex:
            print("poll err", ex)
            time.sleep(3)


def test_mode():
    print("=== DRY-RUN (no Telegram send) ===")
    seen = load_seen()
    items = fetch_all(seen=seen, max_age_hours=12, max_per_run=10)
    print(f"fetched: {len(items)}")
    cache_items = [{k: v for k, v in it.items() if k != "pp"} for it in items]
    print("\n--- DIGEST ---\n")
    print(format_digest(cache_items)[:2000])
    if cache_items:
        print("\n--- SAMPLE PACK #1 ---\n")
        pack = build_pack(cache_items[0])
        print(format_pack(1, cache_items[0], pack)[:3000])
    print("\nOK. Set TELEGRAM_BOT_TOKEN to go live: TELEGRAM_BOT_TOKEN=xxx python3 tbot.py")


if __name__ == "__main__":
    if "--test" in sys.argv or not TOKEN:
        # if token present and --poll forced, poll; else test
        if "--poll" in sys.argv and TOKEN:
            poll()
        else:
            test_mode()
    else:
        poll()
