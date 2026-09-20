# news_core.py — Best-of-best engine for instant pipeline.
# $0, no LLM. Engagement signals + emotion scoring + per-category quotas.
# Laptop closed = GitHub Actions runs this. Used by bot.py, tbot.py, responder.py.
import re
import html
import math
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
    SOURCES, MAX_PER_RUN, KEYWORDS = [], 9, []

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) ai-news-bot/1.0"}

HANDLES = {
    "business": "@founderfilesdaily",
    "entertainment": "@viralvaultdaily",
    "ai": "@botbriefdaily",
}
PAGE_NAMES = {
    "business": "FounderFiles | Business",
    "entertainment": "ViralVault | Entertainment",
    "ai": "BotBrief | AI",
}
PAGE_EMOJI = {"business": "💼", "entertainment": "🎬", "ai": "🤖"}

# ---- hard filters: NEVER reach the pages (process-politics, promo, mod posts) ----
# NOTE: famous names are NOT blocked — people care about famous people.
BLOCK = ["senate", "congress", "election", "democrat", "republican",
         "parliament", "minister ", "white house", "arxiv", "[paper]",
         "ticket", "prices go up", "disrupt ", "webinar", "sponsored",
         "register now", "early bird", "use code ", "we're hiring",
         "join our team", "blueprint", "whitepaper", "framework paper",
         "to our ", "rules", "daily discussion", "open thread", "weekly thread",
         "megathread", "sidebar", "lottery"]

# Famous people = people care + feel something. Entertainment boost.
FAMOUS = ["swift", "kelce", "bieber", "kardashian", "jenner", "hadid",
          "grande", "eilish", "beyonce", "rihanna", "drake", "weeknd",
          "ronaldo", "messi", "neymar", "kohli", "mrbeast", "pewdiepie",
          "ishowspeed", "kaicenat", "musk", "gates", "bezos", "zuckerberg",
          "cook", "altman", "hamilton", "verstappen", "dicaprio", "johansson",
          "rock", "cena", "bts", "blackpink", "ambani", "shah rukh", "salman khan",
          "alia", "virat", "modi", "taylor", "kylie", "kendall",
          "ariana", "billie", "selena", "gomez", "zendaya", "holland",
          "gosling", "chalamet", "adele"]

BIZ_KW = ["launch", "launches", "startup", "startups", "founder", "founders",
          "funding", "raises", "raised", "seed", "series a", "series b", "ipo",
          "acqui", "revenue", "profit", "unicorn", "y combinator",
          "product hunt", "just launched", "business", "billion",
          "million users", "shut down", "layoff"]
ENT_KW = ["viral", "streamer", "streamers", "twitch", "kick", "ishowspeed",
          "kaicenat", "celeb", "celebrity", "celebrities", "mrbeast", "funny",
          "fail", "fails", "cctv", "caught on camera", "fight", "drama",
          "meme", "memes", "tiktok", "satisfying", "insane", "reaction",
          "prank", "freakout", "audition", "proposal", "wedding", "rescue"]
AI_KW = ["ai", "llm", "gpt", "claude", "gemini", "agent", "agents", "openai",
         "anthropic", "diffusion", "transformer", "copilot", "midjourney",
         "llama", "mistral", "sora", "robot", "robots", "open-source model",
         "beats gpt", "beats claude"]

# curiosity/emotion triggers: people click + feel something
EMOTION = ["just", "first", "broke", "breaks", "insane", "viral", "exposed",
           "secret", "leaked", "banned", "shut", "vs ", "wins", "fails",
           "reaction", "caught", "live", "million", "billion", "free",
           "mind-blow", "genius", "scam", "plot twist", "turns into",
           "quits", "fired", "record", "youngest", "oldest", "never seen",
           "satisfying", "transformation", "proposal", "rescue", "chase"]


def clean(text, n=300):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()[:n]


def blocked(title, summary=""):
    low = f"{title} {summary}".lower()
    return any(b in low for b in BLOCK)


def _kw(low, phrases):
    """Word-ish match: 'ai' must not fire inside 'spain', 'celeb' not in 'celebrate'."""
    n = 0
    for p in phrases:
        p = p.strip().lower()
        if re.search(r"(?<![a-z])" + re.escape(p) + r"(?![a-z])", low):
            n += 1
    return n


def categorize(title, summary, hint=None):
    low = f"{title} {summary}".lower()
    scores = {
        "business": _kw(low, BIZ_KW),
        "entertainment": _kw(low, ENT_KW) + _kw(low, FAMOUS) * 2,
        "ai": _kw(low, AI_KW),
    }
    if hint in scores:
        scores[hint] += 1.5
    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return None  # no signal = not best-of-best = drop it
    return best


def emotion_bonus(title):
    low = title.lower()
    bonus = sum(1 for e in EMOTION if e in low)
    if re.search(r"\$\d|\d+m\b|\d+k\b|\d+x\b|\d+-\w+|\?", low):
        bonus += 1  # numbers / questions = curiosity gap
    if len(title) > 140:
        bonus -= 1  # rambling headlines don't hook
    return bonus


def engagement_score(eng):
    # log scale: 10k upvotes >> 100, but diminishing
    try:
        return math.log10(max(int(eng), 1) + 1)
    except Exception:
        return 0


# ---------------- taste learning: feed examples -> pattern ----------------
# $0, no LLM. Stores keyword/source/page weights in taste.json.
# You send 5-15 examples of "best news", bot boosts similar in future.
import os as _os
import json as _json

TASTE_FILE = _os.path.join(_os.path.dirname(__file__), "taste.json")
_STOP = set("the a an of to in on for with and or is are was were be by at from as it its this that these those will just new day than then than vs via over under out up into over after before".split())


def _tokens(text):
    toks = re.findall(r"[a-z0-9$]+", (text or "").lower())
    return [t for t in toks if len(t) > 2 and t not in _STOP][:30]


def load_taste():
    try:
        with open(TASTE_FILE) as f:
            d = _json.load(f)
            if isinstance(d, dict):
                d.setdefault("kw", {})
                d.setdefault("src", {})
                d.setdefault("page", {})
                d.setdefault("n", 0)
                return d
    except Exception:
        pass
    return {"kw": {}, "src": {}, "page": {}, "n": 0}


def save_taste(d):
    try:
        with open(TASTE_FILE, "w") as f:
            _json.dump(d, f)
    except Exception as ex:
        print("taste save fail", str(ex)[:80])


def learn_example(title, source="", page="", link=""):
    """Learn one example. Returns tokens learned."""
    d = load_taste()
    toks = _tokens(title)
    for t in toks:
        d["kw"][t] = d["kw"].get(t, 0) + 1
    if source:
        s = source.strip().lower()[:60]
        d["src"][s] = d["src"].get(s, 0) + 1
    if page in ("business", "entertainment", "ai"):
        d["page"][page] = d["page"].get(page, 0) + 1
    d["n"] = d.get("n", 0) + 1
    # keep file small: top 200 keywords only
    if len(d["kw"]) > 200:
        top = sorted(d["kw"].items(), key=lambda x: -x[1])[:200]
        d["kw"] = dict(top)
    save_taste(d)
    return toks


def taste_boost(item):
    """Extra score from learned taste. 0 if no training yet."""
    d = load_taste()
    if not d.get("n"):
        return 0.0
    toks = set(_tokens(f"{item.get('title','')} {item.get('summary','')}"))
    s = 0.0
    for t in toks:
        c = d["kw"].get(t, 0)
        if c >= 2:
            s += min(c * 0.4, 2.0)
        elif c == 1:
            s += 0.3
    src = (item.get("source") or "").lower()[:60]
    if src and d["src"].get(src):
        s += min(d["src"][src] * 0.5, 1.5)
    pg = item.get("page")
    if pg and d["page"].get(pg):
        s += min(d["page"][pg] * 0.2, 1.0)
    return round(min(s, 6.0), 2)


def taste_summary():
    d = load_taste()
    if not d.get("n"):
        return "No training yet. Send 5-15 examples like:\nlearn: OpenAI launches Sora 2 with audit logs"
    top_kw = sorted(d["kw"].items(), key=lambda x: -x[1])[:15]
    top_src = sorted(d["src"].items(), key=lambda x: -x[1])[:8]
    return (f"Trained on {d.get('n',0)} examples.\n"
            f"Top keywords: {', '.join(f'{k}({v})' for k,v in top_kw)}\n"
            f"Top sources: {', '.join(f'{k}({v})' for k,v in top_src)}\n"
            f"Pages: {d.get('page',{})}")


def rank(item):
    """Single number: higher = people care + feel something + fresh."""
    s = 0.0
    s += engagement_score(item.get("engagement", 0)) * 2.0
    s += emotion_bonus(item.get("title", "")) * 1.5
    low = f"{item.get('title', '')} {item.get('summary', '')}".lower()
    if _kw(low, FAMOUS):
        s += 2.5  # famous face = people care
    s += taste_boost(item)  # learned from your examples
    age_h = item.get("age_hours", 99)
    if age_h < 3:
        s += 3
    elif age_h < 8:
        s += 2
    elif age_h < 24:
        s += 1
    elif age_h > 36:
        s -= 2
    hi = ["openai", "google", "deepmind", "anthropic", "techcrunch",
          "hackernews", "product hunt"]
    if any(h in item.get("source", "").lower() for h in hi):
        s += 1
    return round(s, 2)


def now_utc():
    return datetime.now(timezone.utc)


# ---------------- fetchers (all fail-fast, never hang) ----------------

def _rss_items(src, max_age_hours):
    items = []
    try:
        r = requests.get(src["url"], timeout=12, headers=UA)
        if not r.ok or not r.content:
            return []
        feed = feedparser.parse(r.content)
    except Exception as ex:
        print("rss fail", src["name"], str(ex)[:100])
        return []
    for e in feed.entries[:15]:
        link = getattr(e, "link", "")
        title = getattr(e, "title", "").strip()
        if not link or not title:
            continue
        summary = getattr(e, "summary", "") or getattr(e, "description", "")
        if blocked(title, summary):
            continue
        if src.get("filter"):
            low = (title + " " + summary).lower()
            if not any(k in low for k in KEYWORDS):
                continue
        pp = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        age_h, pub = 99, ""
        if pp:
            try:
                dt = datetime(*pp[:6], tzinfo=timezone.utc)
                age_h = (now_utc() - dt).total_seconds() / 3600
                pub = dt.isoformat(timespec="minutes")
            except Exception:
                pass
        if age_h > max_age_hours:
            continue
        items.append({"title": clean(title, 160), "link": link,
                      "source": src["name"], "summary": clean(summary, 400),
                      "engagement": 0, "age_hours": round(age_h, 1),
                      "published": pub})
    return items


def _hn_items(src, max_age_hours, min_points=30):
    from email.utils import parsedate_to_datetime
    items = []
    try:
        r = requests.get(src["url"], timeout=12, headers=UA).json()
    except Exception as ex:
        print("hn fail", str(ex)[:100])
        return []
    for h in r.get("hits", [])[:15]:
        title = h.get("title", "") or ""
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        if not title or blocked(title):
            continue
        pts = h.get("points", 0) or 0
        if pts < min_points:
            continue
        try:
            ca = h.get("created_at", "")
            dt = (parsedate_to_datetime(ca) if "GMT" in ca
                  else datetime.fromisoformat(ca.replace("Z", "+00:00")))
            age_h = (now_utc() - dt).total_seconds() / 3600
            pub = dt.isoformat(timespec="minutes")
        except Exception:
            age_h, pub = 99, ""
        if age_h > max_age_hours:
            continue
        items.append({"title": clean(title, 160), "link": link,
                      "source": "HackerNews",
                      "summary": f"{pts} points, {h.get('num_comments', 0)} comments",
                      "engagement": pts + (h.get("num_comments", 0) or 0),
                      "age_hours": round(age_h, 1), "published": pub})
    return items


REDDIT_SUBS = [
    ("videos", "entertainment"),
    ("interestingasfuck", "entertainment"),
    ("Damnthatsinteresting", "entertainment"),
    ("memes", "entertainment"),
]


def _reddit_items(max_age_hours=12, min_score=1500):
    """Reddit hot.json = real crowd votes = people-care signal. $0, no key."""
    items = []
    for sub, page in REDDIT_SUBS:
        try:
            r = requests.get(f"https://www.reddit.com/r/{sub}/hot.json?limit=25",
                             timeout=12, headers=UA).json()
        except Exception as ex:
            print("reddit fail", sub, str(ex)[:80])
            continue
        for c in r.get("data", {}).get("children", []):
            d = c.get("data", {})
            title = d.get("title", "") or ""
            if (not title or d.get("stickied") or d.get("over_18")
                    or blocked(title)):
                continue
            score = d.get("score", 0) or 0
            comments = d.get("num_comments", 0) or 0
            if score < min_score and comments < 150:
                continue  # crowd didn't care -> skip
            try:
                age_h = (now_utc().timestamp() - float(d.get("created_utc", 0))) / 3600
            except Exception:
                age_h = 99
            if age_h > max_age_hours:
                continue
            items.append({"title": clean(title, 160),
                          "link": "https://www.reddit.com" + d.get("permalink", ""),
                          "source": f"Reddit r/{sub}",
                          "summary": f"{score} upvotes, {comments} comments",
                          "engagement": score + comments * 3,
                          "age_hours": round(age_h, 1), "_page": page})
        time.sleep(0.4)
    return items


def fetch_categorized(seen=None, per_page=3, window_boost=1.0):
    """Returns {business:[...], entertainment:[...], ai:[...]} — best of best only.
    window_boost>1 looks further back (for 'more'/'older' requests)."""
    seen = seen or set()
    wb = max(1.0, min(window_boost, 5.0))
    pool = []
    for src in SOURCES:
        t = src.get("type")
        try:
            if t == "hn":
                # Show HN / launches: fresher window, lower bar (new things)
                if "launch" in src.get("name", "").lower() or "show" in src.get("url", "").lower():
                    pool.extend(_hn_items(src, 36 * wb, 15))
                else:
                    pool.extend(_hn_items(src, 36 * wb, 40))
            elif t == "arxiv":
                continue  # papers = filler bank, never instant
            else:
                name = src.get("name", "").lower()
                if "startup" in name or "techcrunch" in name:
                    pool.extend(_rss_items(src, 36 * wb))   # biz moves stay relevant
                elif ("reddit" in name or "verge" in name or "e! news" in name
                        or "variety" in name or "deadline" in name
                        or "justjared" in name or "fauxmoi" in name
                        or "bbc" in name or "cnn" in name):
                    pool.extend(_rss_items(src, 24 * wb))   # world/ent expires
                else:
                    pool.extend(_rss_items(src, 30 * wb))   # AI news
        except Exception as ex:
            print("fetch fail", src.get("name"), str(ex)[:80])
        time.sleep(0.2)

    # dedup + seen + rank
    best = {}
    for it in pool:
        if it["link"] in seen or it["link"] in best:
            continue
        page = it.pop("_page", None) or categorize(
            it["title"], it.get("summary", ""), _hint(it))
        if not page:  # no category signal -> not best-of-best -> drop
            continue
        it["page"] = page
        it["score"] = rank(it)
        best[it["link"]] = it
    out = {"business": [], "entertainment": [], "ai": []}
    for it in best.values():
        out.get(it["page"], out["ai"]).append(it)
    for k in out:
        out[k].sort(key=lambda x: -x["score"])
        out[k] = out[k][:per_page]
    return out


def _hint(item):
    s = item.get("source", "").lower()
    if "launch" in s or "show_hn" in s or "startup" in s or "product" in s:
        return "business"
    if ("reddit" in s or "verge" in s or "e!" in s or "variety" in s
            or "deadline" in s or "justjared" in s or "fauxmoi" in s):
        return "entertainment"
    return None  # world + HN front + blogs: let keywords vote


def fetch_all(seen=None, max_age_hours=12, max_per_run=9, window_boost=1.0,
              only_cat=None):
    """Flat list, interleaved by category. only_cat = top-up one page."""
    cats = fetch_categorized(seen=seen, per_page=max(2, max_per_run // 3),
                             window_boost=window_boost)
    if only_cat in cats:
        return cats[only_cat][:max_per_run]
    flat = []
    for i in range(max([len(v) for v in cats.values()] + [0])):
        for k in ("business", "entertainment", "ai"):
            if i < len(cats[k]):
                flat.append(cats[k][i])
    return flat[:max_per_run]


def fmt_age(item):
    """'🕒 5h ago · Sep 20 14:30' — when it went public on the internet."""
    a = item.get("age_hours", 99)
    if a >= 90:
        age = "date unknown"
    elif a < 1:
        age = f"{max(int(a * 60), 1)}m ago"
    elif a < 48:
        age = f"{int(a)}h ago" if a < 24 else f"{int(a // 24)}d ago"
    else:
        age = f"{int(a // 24)}d ago"
    pub = (item.get("published") or "")[:16].replace("T", " ")
    return f"🕒 {age}" + (f" · {pub}" if pub else "")


def parse_command(text):
    """Plain chat words -> action. Returns tuples:
    ('news', boost, only_cat) | ('detail', n) | ('help',) | (None,)"""
    t = (text or "").strip().lower().split("@")[0].strip()
    if not t:
        return (None,)
    if t in ("hi", "hello", "hey", "/start", "start", "/help", "help"):
        return ("help",)
    m = re.match(r"^/?details?\s+(\d+)", t) or re.match(r"^#?(\d+)$", t)
    if m:
        return ("detail", int(m.group(1)))
    if "detail" in t and not re.search(r"\d", t):
        return ("help",)
    cat = None
    if any(w in t for w in ("business", "founder", "startup", "money")):
        cat = "business"
    elif any(w in t for w in ("entertainment", "viral", "celebr", "famous", "movie", "music")):
        cat = "entertainment"
    elif re.search(r"(?<![a-z])ai(?![a-z])", t):
        cat = "ai"
    m2 = re.search(r"(\d+)\s*h", t)
    if m2:  # "24h", "news 48h" -> look that far back
        return ("news", max(1.0, min(int(m2.group(1)) / 12.0, 5.0)), cat)
    if any(w in t for w in ("more", "older", "other", "another", "different", "new ones", "next")):
        return ("news", 2.5, cat)
    if "news" in t or cat:
        return ("news", 1.0, cat)
    return (None,)


# ---------------- packs + digest ----------------

def short_topic(title):
    t = re.sub(r"^\[Paper\]\s*", "", title).strip()
    return re.sub(r"\s+", " ", t)[:90]


def build_pack(item):
    topic = short_topic(item["title"])
    page = item.get("page", "ai")
    handle = HANDLES.get(page, "")
    q = quote_plus(topic[:60])
    # Style learned from @getintoai (818K), @artificialintelligenceee (911K), @bbcnews
    # AI pages: plain-sentence title, explainer + question bait + Follow + Source + 3-5 tags
    # BBC: factual title, 1-sentence fact + location tags + #bbcnews, clean photo, no big text
    titles = [f"{topic[:55]}", f"POV: {topic[:50]}", f"{topic[:40]} in 25 seconds"]
    # image overlay text: 5-7 words uppercase for slide1 / video cover
    overlay = re.sub(r"[^A-Za-z0-9 ]", "", topic).strip().upper().split()
    overlay = " ".join(overlay[:7]) if overlay else topic[:30].upper()
    if page == "business":
        hook = f"STOP. {topic[:45]} just happened."
        hashtags = "#startup #business #founder #launch #money"
        script = ("0-1s hook above -> 1-8s what launched + proof screenshot -> "
                  "8-20s why it prints money -> 20-25s CTA follow for Day 2")
        cta = "Follow for startup launches daily"
        caption = (f"{titles[0]}\n\n{clean(item.get('summary', ''), 150)}\n\n{cta} {handle}")
    elif page == "entertainment":
        hook = f"WAIT FOR IT. {topic[:45]}"
        hashtags = "#viral #funny #caught #live #drama"
        script = ("0-1s WAIT freeze-frame -> 1-6s buildup -> 6-20s payoff x2 "
                  "replay zoom -> 20-25s comment bait")
        cta = "Follow for daily viral drops"
        # BBC-style: factual, no hype caps in caption
        caption = (f"{titles[0]}\n\n{clean(item.get('summary', ''), 140)}\n\n{cta} {handle}")
    else:
        # getintoai formula: plain title + 2-sentence explainer + question + Follow + Source
        hook = topic[:60]  # no caps hype, plain like getintoai
        hashtags = "#ai #ainews #aiupdates"
        script = ("0-1s overlay text only (no voice hype) -> 1-8s screen-record demo -> 8-18s "
                  "before/after proof -> 18-25s question bait on screen + CTA")
        summary = clean(item.get('summary', ''), 180)
        # question bait like getintoai: "What do you think...?"
        qbait = "What do you think — hype or real shift? 💬"
        low_t = topic.lower()
        if "robot" in low_t:
            qbait = "Would you trust a robot to do this better than a human? 🤔💬"
        elif "price" in low_t or "market" in low_t or "billion" in low_t or "million" in low_t:
            qbait = "Growing AI future or bubble waiting to burst? 🤔💬"
        elif "space" in low_t or "science" in low_t:
            qbait = "Are we just scratching the surface of AI's potential? 🚀💬"
        cta = f"Follow for more {handle} 🔌 Source: {item.get('source','')}"
        caption = f"{titles[0]}\n\n{summary}\n\n{qbait}\n{cta}\n{hashtags}"
    if page == "ai":
        rohan_note = (f"CapCut {PAGE_NAMES[page]} template 1080x1920, captions ON. "
                      f"Slide1/carousel cover text (bold white, 5-7 words): {overlay}. "
                      f"Reel: screen-record demo, subtitles, no hype voice. File: DATE_PAGE_FORMAT_01.")
        reshab_note = ("Cover = overlay text above. Caption = title + explainer + question + Follow + Source. "
                       "First comment = same question bait. Reply first 15 in 30 min. Tags: #ai #ainews #aiupdates.")
    elif page == "entertainment":
        rohan_note = (f"CapCut {PAGE_NAMES[page]} template, <28s 1080x1920. BBC-style if world news: clean photo, "
                      f"minimal text, subtitles only. If viral: freeze-frame + zoom x2. Cover: {overlay}.")
        reshab_note = "BBC-style: 1-sentence fact + #location #bbcnews style tags. Viral-style: WAIT + comment bait."
    else:
        rohan_note = (f"CapCut {PAGE_NAMES[page]} template, <28s 1080x1920, captions ON, "
                      f"progress bar, hook 0-1s: {hook[:60]}. File: DATE_PAGE_FORMAT_01.")
        reshab_note = ("Post via phone apps, cover = hook text, first comment = question bait. "
                       "Reply first 15 comments in 30 min.")
    return {
        "titles": titles, "hook": hook, "script": script,
        "caption": caption,
        "image_text": overlay,
        "hashtags": hashtags,
        "videos": {
            "YouTube search": f"https://www.youtube.com/results?search_query={q}",
            "TikTok search": f"https://www.tiktok.com/search?q={q}",
            "Pexels stock": f"https://www.pexels.com/search/{q}/",
            "Google News": f"https://news.google.com/search?q={q}",
        },
        "expiry": "4-6 hrs (entertainment) / 24h (biz/AI). If expired, skip.",
        "rohan_edit": rohan_note,
        "reshab_post": reshab_note,
        "credit": (f"Source: {item['source']} {item['link']} — add 'via {item['source']}' "
                   "+ transformative edit (<30s) to avoid bans."),
    }


def format_digest(items):
    """Compat flat digest -> delegates to 3-category layout."""
    cats = {"business": [], "entertainment": [], "ai": []}
    for it in items:
        cats.get(it.get("page", "ai"), cats["ai"]).append(it)
    return format_digest_3cat(cats)


def format_digest_3cat(cats, older=False):
    L = ["<b>⚡ BEST OF BEST — fresh scan</b>" if not older
         else "<b>📦 MORE NEWS — further back, skipping what you saw</b>"]
    n = 0
    order = (("business", "💼 FounderFiles | Business — startups just launched"),
             ("entertainment", "🎬 ViralVault | Entertainment — famous people"),
             ("ai", "🤖 BotBrief | AI — new drops people feel"))
    for key, head in order:
        L.append(f"\n<b>{head}</b>")
        items = cats.get(key, [])
        if not items:
            L.append("<i>Nothing more here — try 'more' + hours back, e.g. 'more 48h'.</i>")
            continue
        for it in items:
            n += 1
            it["_n"] = n
            L.append(f"<b>{n}. {html.escape(short_topic(it['title']))}</b>")
            L.append(f"   {fmt_age(it)} | ⭐{it.get('score', 0)} | {html.escape(it['source'])} | "
                     f"<a href=\"{html.escape(it['link'])}\">link</a>")
    L.append("\nWant others? Type: <b>more</b> (older batch) · <b>more business / more ai / "
             "more entertainment</b> (top-up a page) · <b>more 48h</b> (2 days back).\n"
             "Details: <b>detail N</b> or tap 📦.")
    return "\n".join(L)[:3800]


def format_pack(idx, item, pack):
    L = [f"<b>🔥 VIRAL PACK #{idx} — {PAGE_NAMES.get(item.get('page', 'ai'))} "
         f"{HANDLES.get(item.get('page', 'ai'), '')}</b>",
         f"📌 <b>{html.escape(short_topic(item['title']))}</b>",
         f"{fmt_age(item)} — when it went public",
         f"📰 {html.escape(item['source'])} — "
         f"<a href=\"{html.escape(item['link'])}\">source</a>", "",
         "<b>Titles (pick 1):</b>"]
    for t in pack["titles"]:
        L.append(f"• {html.escape(t)}")
    L.append(f"\n<b>Hook 0-1s:</b> {html.escape(pack['hook'])}")
    L.append(f"<b>Image text (cover/slide1):</b> {html.escape(pack.get('image_text',''))}")
    L.append(f"<b>Script 25s:</b> {html.escape(pack['script'])}")
    L.append("\n<b>Caption:</b>")
    L.append(f"<pre>{html.escape(pack['caption'][:300])}</pre>")
    L.append(f"<b>Hashtags:</b> {html.escape(pack['hashtags'])}")
    L.append("\n<b>Videos (Rohan pulls from here):</b>")
    for k, v in pack["videos"].items():
        L.append(f"• <a href=\"{html.escape(v)}\">{k}</a>")
    L.append("")
    L.append(f"<b>Rohan (edit):</b> {html.escape(pack['rohan_edit'])}")
    L.append(f"<b>Reshab (post):</b> {html.escape(pack['reshab_post'])}")
    L.append(f"<b>Credit/safety:</b> {html.escape(pack['credit'])}")
    L.append(f"<i>⏳ {html.escape(pack['expiry'])}</i>")
    return "\n".join(L)[:3800]


def keyboard_for_items(items):
    kb, row = [], []
    for i in range(1, len(items) + 1):
        row.append({"text": f"📦 Pack #{i}", "callback_data": f"pack:{i}"})
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    return {"inline_keyboard": kb}
