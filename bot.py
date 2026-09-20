# bot.py — 30-min auto digest sender (GitHub Actions, cloud only, no laptop).
# Keeps $0: RSS -> Telegram group with numbered list + tap-to-expand buttons.
# Only BEST news auto-pushes (MIN_SCORE filter). Manual detail via DETAIL_N env.
import os
import json
import requests

from news_core import fetch_all, build_pack, format_digest, format_pack, keyboard_for_items

SEEN_FILE = os.path.join(os.path.dirname(__file__), "seen.json")
CACHE_FILE = os.path.join(os.path.dirname(__file__), "news_cache.json")


def load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen)[-1000:], f)


def send_digest(text, keyboard):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        print("SKIP telegram (no env). DRAFT:\n", text[:2000])
        return False
    payload = {"chat_id": chat, "text": text,
               "parse_mode": "HTML", "disable_web_page_preview": False}
    if keyboard:
        payload["reply_markup"] = keyboard
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json=payload, timeout=20)
    print("telegram:", r.status_code)
    return r.ok


def main():
    # Phone-run mode: DETAIL_N=N sends only pack #N from last cache (no laptop needed).
    try:
        detail_n = int(os.environ.get("DETAIL_N", "0") or "0")
    except Exception:
        detail_n = 0
    try:
        hours = int(os.environ.get("HOURS", "12") or "12")
        hours = max(1, min(hours, 24))
    except Exception:
        hours = 12
    if detail_n > 0:
        try:
            with open(CACHE_FILE) as f:
                cache_items = json.load(f)
        except Exception:
            cache_items = []
        if not cache_items or detail_n > len(cache_items):
            send_digest(f"No pack #{detail_n}. Run digest first (detail=0), then retry.", None)
            return
        item = cache_items[detail_n - 1]
        send_digest(format_pack(detail_n, item, build_pack(item)), None)
        print(f"done: detail pack #{detail_n}")
        return
    seen = load_seen()
    # 2h window = 30-min cadence + overlap so nothing is missed if one run fails
    items = fetch_all(seen=seen, max_age_hours=hours, max_per_run=10)
    # BEST-ONLY filter for auto-push: skip low-score filler silently
    try:
        min_score = float(os.environ.get("MIN_SCORE", "2.0") or "2.0")
    except Exception:
        min_score = 2.0
    best = [it for it in items if it.get("score", 0) >= min_score]
    # save cache for /detail lookup (strip non-serializable pp)
    cache_items = [{k: v for k, v in it.items() if k != "pp"} for it in best]
    with open(CACHE_FILE, "w") as f:
        json.dump(cache_items, f)
    if not best:
        # mark even filler as seen so next 30-min run doesn't reprocess same pool
        for it in items:
            seen.add(it["link"])
        save_seen(seen)
        print(f"done: found={len(items)} but none >= MIN_SCORE {min_score} (quiet, no telegram spam)")
        return
    ok = send_digest(format_digest(cache_items), keyboard_for_items(cache_items))
    for it in best:
        seen.add(it["link"])
        if not ok and not os.environ.get("TELEGRAM_BOT_TOKEN"):
            print("DRAFT:", it.get("page"), "|", it["title"][:80], "|", it["link"])
    # also remember skipped filler so pool stays fresh
    for it in items:
        seen.add(it["link"])
    save_seen(seen)
    print(f"done: found={len(items)} sent={len(best)}")


if __name__ == "__main__":
    main()
