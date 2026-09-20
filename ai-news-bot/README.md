# AI News X Bot — $0/mo, semi-auto, 10-min delay

No X API. No server cost. Human-in-loop so X won't ban you.

How it works:
1. GitHub Actions (free) runs `bot.py` every 10 min
2. Checks official AI labs + tech media + HN + arXiv (all free RSS)
3. Sends you a Telegram message with link + ready-to-copy X post
4. You tap copy -> paste in X app -> post. 10 seconds per news.

This gives you 5-15 min delay, not 2-3 min. 2-3 min for ALL internet is impossible without $10k/mo enterprise firehose. 10 min is enough to be "fastest curated" account.

## Setup (15 min)

1. Telegram approval inbox (free):
   - Chat with @BotFather -> /newbot -> copy token
   - Chat with @userinfobot -> copy your ID
   - Chat with your new bot once (press Start)

2. Put code on GitHub:
   - Create private repo, upload this folder
   - Repo Settings -> Secrets -> Actions -> add:
     - TELEGRAM_BOT_TOKEN
     - TELEGRAM_CHAT_ID
   - Actions tab -> enable workflows -> Run workflow once to test

3. Post format on X:
   - Always add source link + "via [source]"
   - Add 1-line why it matters, not just headline. That's what beats other news accounts.
   - Enable Settings -> "Automated account" label later to stay safe.

## Local test
```
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=xxx TELEGRAM_CHAT_ID=yyy python bot.py
# without telegram env, it just prints DRAFTs
python bot.py
```

## Costs
- GitHub Actions: $0 (2000 min free, this uses ~150 min/mo)
- Telegram: $0
- X posting: $0 (you post manually, no $0.20/link API fee)
- If you later want fully auto: budget ~$0.20 per link-post + need X Developer account.

## Make it THE go-to account
Don't post everything. Post:
- Official labs first (OpenAI/Anthropic/Google/xAI)
- Funding/launches from TechCrunch second
- 1-2 best papers/day max, with plain-English line
Skip rumors. Add "Confirmed:" + source. That's trust = followers.
