# Free RSS + API sources. All zero-cost, no keys needed.
# type: rss | hn | arxiv
SOURCES = [
    {"name": "OpenAI News", "url": "https://openai.com/news/rss.xml", "type": "rss"},
    {"name": "Google AI Blog", "url": "https://blog.google/technology/ai/rss/", "type": "rss"},
    {"name": "DeepMind Blog", "url": "https://blog.google/technology/ai/rss/", "type": "rss"},
    {"name": "HuggingFace Blog", "url": "https://huggingface.co/blog/feed.xml", "type": "rss"},
    {"name": "TechCrunch AI", "url": "https://techcrunch.com/category/artificial-intelligence/feed/", "type": "rss"},
    {"name": "The Verge", "url": "https://www.theverge.com/rss/index.xml", "type": "rss", "filter": True},
    {"name": "MIT TechReview AI", "url": "https://www.technologyreview.com/topic/artificial-intelligence/feed/", "type": "rss"},
    {"name": "TechCrunch Startups", "url": "https://techcrunch.com/category/startups/feed/", "type": "rss"},
    # Entertainment comes from viral RSS (Reddit recency + BoredPanda/TwistedSifter crowd-picked stories).
    {"name": "HackerNews AI", "url": "https://hn.algolia.com/api/v1/search?query=artificial%20intelligence&tags=story&hitsPerPage=10", "type": "hn"},
    {"name": "Reddit Videos", "url": "https://www.reddit.com/r/videos/.rss", "type": "rss"},
    {"name": "BoredPanda Viral", "url": "https://www.boredpanda.com/feed/rss", "type": "rss"},
    {"name": "TwistedSifter", "url": "https://twistedsifter.com/feed/", "type": "rss"},
    {"name": "HackerNews Front", "url": "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=20", "type": "hn"},
    {"name": "HackerNews Launch", "url": "https://hn.algolia.com/api/v1/search_by_date?query=startup%20launch&tags=show_hn&hitsPerPage=15", "type": "hn"},
]

# Only send items newer than this on first run to avoid spam (hours)
MAX_AGE_HOURS = 12

# Max Telegram alerts per run (protects you from 50 msgs at once)
MAX_PER_RUN = 10

# Keywords for general feeds (HN). AI feeds accept everything.
KEYWORDS = ["ai", "llm", "gpt", "claude", "gemini", "agent", "openai", "anthropic", "diffusion", "transformer", "copilot", "midjourney", "stable diffusion", "llama", "mistral"]
