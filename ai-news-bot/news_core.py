# Shared core for instant pipeline: fetch -> categorize -> score -> viral pack.
# $0, no LLM, template-based. Used by bot.py (digest) and tbot.py (interactive).
import re
import html
import json
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus

import feedparser
import requests
import socket
socket.setdefaulttimeout(15)

try:
    from sources import SOURCES, MAX_PER_RUN, KEYWORDS
except ImportError:
    SOURCES, MAX_PER_RUN, KEYWORDS = [], 12, []

# Handles locked direction (user gave base, we use daily variants for uniformity).
# Change here once finalized: must be same on IG/TikTok/Shorts.
HANDLES = {
    "business": "@founderfilesdaily",       # base: @founderfiles (taken)
    "entertainment": "@viralvaultdaily",    # base: @viralvault (taken/crowded)
    "ai": "@botbriefdaily",                 # base: @botbrief (IG free, YT/X taken)
}
PAGE_NAMES = {
    "business": "FounderFiles | Business",
    "entertainment": "ViralVault | Entertainment",
    "ai": "BotBrief | AI",
}
PAGE_EMOJI = {"business": "\U0001f4bc", "entertainment": "\U0001f3ac", "ai": "\U0001f916"}

BIZ_KW = ["launch", "startup", "founder", "funding", "raises", "raised", "seed",
          "series a", "ipo", "acqui", "startup", "business", "revenue", "profit",
          "unicorn", "yc ", "y combinator", "product hunt", "just launched"]
ENT_KW = ["viral", "streamer", "live", "twitch", "kick", "ishowspeed", "kaicenat",
          "celeb", "kardashian", "taylor swift", "mrbeast", "funny", "fail",
          "cctv", "caught on camera", "fight", "drama", "meme", "tiktok"]
AI_KW = ["ai", "llm", "gpt", "claude", "gemini", "agent", "openai", "anthropic",
         "diffusion", "transformer", "copilot", "midjourney", "llama", "mistral",
         "sora", "sun o", "robot"]


def clean(text, n=300):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()[:n]


def categorize(title, summary, hint=None):
    low = f"{title} {summary}".lower()
    scores = {
        "business": sum(1 for k in BIZ_KW if k in low),
        "entertainment": sum(1 for k in ENT_KW if k in low),
        "ai": sum(1 for k in AI_KW if k in low),
    }
    # hint from source breaks ties
    if hint in scores:
        scores[hint] += 1.5
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        # default routing: tech-y -> ai, else business
        return "ai"
    return best


def score_item(title, source_name, published_parsed):
    s = 0
    # source weight: official labs + launches first
    high = ["openai", "google", "deepmind", "anthropic", "techcrunch startups",
            "product hunt", "hackernews"]
    if any(h in source_name.lower() for h in high):
        s += 3
    low = title.lower()
    if any(k in low for k in ["just launched", "breaking", "launches", "raises $", "viral"]):
        s += 2
    # de-boost raw politics so pages stay advertiser-safe; fun/launches rank first
    if any(k in low for k in ["trump", "biden", "election", "white house", "senate", "congress"]):
        s -= 2
    if published_parsed:
        try:
            dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            if age_h < 2:
                s += 3
            elif age_h < 6:
                s += 2
            elif age_h < 12:
                s += 1
        except Exception:
            pass
    return s


def is_recent(published_parsed, max_hours):
    if not published_parsed:
        return True
    try:
        dt = datetime(*published_parsed[:6], tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt < timedelta(hours=max_hours)
    except Exception:
        return True


def fetch_all(seen=None, max_age_hours=6, max_per_run=12):
    seen = seen or set()
    out = []
    for src in SOURCES:
        try:
            if src.get("type") == "hn":
                out.extend(_fetch_hn(src, seen, max_age_hours))
            elif src.get("type") == "arxiv":
                out.extend(_fetch_arxiv(src, seen))
            else:
                out.extend(_fetch_rss(src, seen, max_age_hours))
        except Exception as ex:
            print("fetch fail", src.get("name"), ex)
        time.sleep(0.3)
    # categorize + score + sort
    for it in out:
        it["page"] = categorize(it["title"], it.get("summary", ""), src_hint(it))
        it["score"] = score_item(it["title"], it["source"], it.get("pp"))
    out.sort(key=lambda x: (-x.get("score", 0), x.get("title", "")))
    return out[:max_per_run]


def src_hint(item):
    # recover hint from source name if known
    s = item.get("source", "").lower()
    if "startup" in s or "techcrunch" in s:
        return "business"
    if "reddit" in s or "verge" in s:
        return "entertainment"
    return "ai"


def _fetch_rss(src, seen, max_age_hours):
    # requests-first (respects timeout) then feedparser on bytes.
    # feedparser.parse(url) hangs on some 301s (blog.google) — never call it directly.
    items = []
    feed = None
    try:
        r = requests.get(src["url"], timeout=12,
                         headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) ai-news-bot/1.0"})
        if r.ok and r.content:
            feed = feedparser.parse(r.content)
    except Exception as ex:
        print("rss http fail", src["name"], str(ex)[:100])
    if feed is None or not feed.entries:
        return []  # fail fast, next source. No hanging fallback.
    for e in feed.entries[:10]:
        link = getattr(e, "link", "")
        title = getattr(e, "title", "").strip()
        if not link or not title or link in seen:
            continue
        if src.get("filter"):
            low = (title + " " + getattr(e, "summary", "")).lower()
            if not any(k in low for k in KEYWORDS):
                continue
        pp = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        if not is_recent(pp, max_age_hours):
            continue
        summary = getattr(e, "summary", "") or getattr(e, "description", "")
        items.append({"title": title, "link": link, "source": src["name"],
                      "summary": clean(summary, 400), "pp": pp})
    return items


def _fetch_hn(src, seen, max_age_hours):
    from email.utils import parsedate_to_datetime
    items = []
    try:
        r = requests.get(src["url"], timeout=20).json()
        for h in r.get("hits", [])[:12]:
            title = h.get("title", "") or ""
            link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            if link in seen or not title:
                continue
            low = title.lower()
            # launch-type HN passes even without AI kw
            is_launch = any(k in low for k in ["launch", "show hn", "startup", "funding", "ai"])
            if not is_launch and not any(k in low for k in KEYWORDS):
                continue
            try:
                ca = h.get("created_at", "")
                dt = parsedate_to_datetime(ca) if "GMT" in ca else datetime.fromisoformat(ca.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) - dt > timedelta(hours=max_age_hours):
                    continue
            except Exception:
                pass
            if h.get("points", 0) < 10:
                continue
            items.append({"title": title, "link": link, "source": "HackerNews",
                          "summary": f"{h.get('points')} points", "pp": None})
    except Exception as ex:
        print("hn fail", ex)
    return items


def _fetch_arxiv(src, seen):
    items = []
    try:
        r = requests.get(src["url"], timeout=20, headers={"User-Agent": "ai-news-bot/1.0"})
        entries = re.findall(r"<entry>.*?</entry>", r.text, re.DOTALL)[:8]
        for it in entries:
            t = re.search(r"<title>(.*?)</title>", it, re.DOTALL)
            l = re.search(r"<id>(.*?)</id>", it, re.DOTALL)
            s = re.search(r"<summary>(.*?)</summary>", it, re.DOTALL)
            if not t or not l:
                continue
            title = re.sub(r"\s+", " ", t.group(1)).strip()
            link = l.group(1).strip()
            if link in seen:
                continue
            summary = re.sub(r"\s+", " ", s.group(1)).strip() if s else ""
            items.append({"title": "[Paper] " + title, "link": link,
                          "source": "arXiv cs.AI", "summary": clean(summary, 400), "pp": None})
            if len(items) >= 2:
                break
    except Exception as ex:
        print("arxiv fail", ex)
    return items


# ---------- viral pack ----------

def short_topic(title):
    t = re.sub(r"^\[Paper\]\s*", "", title).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:90]


def build_pack(item):
    """Everything Rohan (edit) + Reshab (post) need for virality. Template-based, $0."""
    topic = short_topic(item["title"])
    page = item.get("page", "ai")
    handle = HANDLES.get(page, "")
    q = quote_plus(topic[:60])
    titles = [
        f"{topic[:55]}",
        f"POV: {topic[:50]}",
        f"{topic[:40]} in 25 seconds",
    ]
    if page == "business":
        hook = f"STOP. {topic[:45]} just happened."
        hashtags = "#startup #business #founder #launch #money"
        script = "0-1s hook above -> 1-8s what launched + proof screenshot -> 8-20s why it prints money -> 20-25s CTA follow for Day 2"
        cta = "Follow for startup launches daily"
    elif page == "entertainment":
        hook = f"WAIT FOR IT. {topic[:45]} 😳"
        hashtags = "#viral #funny #caught #live #drama"
        script = "0-1s WAIT freeze-frame -> 1-6s buildup -> 6-20s payoff x2 replay zoom -> 20-25s comment bait"
        cta = "Follow for daily viral drops"
    else:
        hook = f"AI JUST DROPPED: {topic[:50]}"
        hashtags = "#ai #ainews #tech #aitools #future"
        script = "0-1s hook above -> 1-8s screen-record demo -> 8-18s before/after proof -> 18-25s where to try + CTA"
        cta = "Follow for AI drops daily"

    return {
        "titles": titles,
        "hook": hook,
        "script": script,
        "caption": f"{titles[0]}\n\n{clean(item.get('summary',''),150)}\n\n{cta} {handle}",
        "hashtags": hashtags,
        "videos": {
            "YouTube search": f"https://www.youtube.com/results?search_query={q}",
            "TikTok search": f"https://www.tiktok.com/search?q={q}",
            "Pexels stock": f"https://www.pexels.com/search/{q}/",
            "Google News": f"https://news.google.com/search?q={q}",
        },
        "expiry": "4-6 hrs (entertainment) / 12-24 hrs (biz/AI). If expired, skip.",
        "rohan_edit": f"CapCut {PAGE_NAMES[page]} template, <28s 1080x1920, captions ON, progress bar, hook text 0-1s: {hook[:60]}. File: DATE_PAGE_FORMAT_01.",
        "reshab_post": f"Post via phone apps, cover = hook text, first comment = question bait. Reply first 15 comments in 30 min.",
        "credit": f"Source: {item['source']} {item['link']} — add 'via {item['source']}' + transformative edit (<30s) to avoid bans.",
    }


def format_digest(items):
    lines = ["<b>\u26a1 INSTANT DROPS (2h scan)</b>", "Tap a button for full Viral Pack.\n"]
    for i, it in enumerate(items, 1):
        e = PAGE_EMOJI.get(it.get("page", "ai"), "")
        lines.append(f"<b>{i}. {e} {html.escape(short_topic(it['title']))}</b>")
        lines.append(f"   {html.escape(it['source'])} | {html.escape(it.get('page',''))} | <a href=\"{html.escape(it['link'])}\">link</a>")
    lines.append("\nPhone: GitHub app → Run workflow → detail=N for full pack. Laptop on: tap button or /detail N.")
    return "\n".join(lines)[:3800]


def format_pack(idx, item, pack):
    L = []
    L.append(f"<b>\U0001f525 VIRAL PACK #{idx} — {PAGE_NAMES.get(item.get('page','ai'))} {HANDLES.get(item.get('page','ai'),'')}</b>")
    L.append(f"\U0001f4cc <b>{html.escape(short_topic(item['title']))}</b>")
    L.append(f"📰 {html.escape(item['source'])} — <a href=\"{html.escape(item['link'])}\">source</a>")
    L.append("")
    L.append("<b>Titles (pick 1):</b>")
    for t in pack["titles"]:
        L.append(f"• {html.escape(t)}")
    L.append(f"\n<b>Hook 0-1s:</b> {html.escape(pack['hook'])}")
    L.append(f"<b>Script 25s:</b> {html.escape(pack['script'])}")
    L.append("")
    L.append("<b>Caption:</b>")
    L.append(f"<pre>{html.escape(pack['caption'][:300])}</pre>")
    L.append(f"<b>Hashtags:</b> {html.escape(pack['hashtags'])}")
    L.append("")
    L.append("<b>Videos (Rohan pulls from here):</b>")
    for k, v in pack["videos"].items():
        L.append(f"• <a href=\"{html.escape(v)}\">{k}</a>")
    L.append("")
    L.append(f"<b>Rohan (edit):</b> {html.escape(pack['rohan_edit'])}")
    L.append(f"<b>Reshab (post):</b> {html.escape(pack['reshab_post'])}")
    L.append(f"<b>Credit/safety:</b> {html.escape(pack['credit'])}")
    L.append(f"<i>⏳ {html.escape(pack['expiry'])}</i>")
    return "\n".join(L)[:3800]


def keyboard_for_items(items):
    # Telegram inline keyboard: 2 buttons per row
    kb = []
    row = []
    for i in range(1, len(items) + 1):
        row.append({"text": f"📦 Pack #{i}", "callback_data": f"pack:{i}"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    return {"inline_keyboard": kb}
