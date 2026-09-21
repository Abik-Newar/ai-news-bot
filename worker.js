// worker.js v2 — EVERYTHING <5s, $0 Cloudflare Workers.
// Packs + news digest + detail served INSTANTLY from news_cache.json in GitHub.
// GitHub Actions only runs in background to refresh the cache (no waiting).
//
// Why v1 "pack not working": fetchCache only tried authed API on main branch.
// If GITHUB_TOKEN was missing/expired, or repo/branch mismatched, cache fetch
// returned null with no visible reason. v2 tries 4 URLs (authed + public,
// main + master), reports the exact HTTP status to you, and adds a /debug page.
//
// Deploy: Cloudflare -> Workers -> Edit code -> paste -> Deploy.
// Secrets needed: TELEGRAM_BOT_TOKEN, GITHUB_REPO (= Abik-Newar/ai-news-bot),
//   GITHUB_TOKEN (PAT with Contents Read; Actions Read+Write for background refresh),
//   WEBHOOK_SECRET.
// Debug: open https://<worker>.workers.dev/ -> shows version + env presence.
//        open https://<worker>.workers.dev/debug -> tries cache fetch, shows status.

const VERSION = "v2-instant-all";

export default {
  async fetch(req, env) {
    const url = new URL(req.url);

    // ---- debug pages (no secret needed, no secrets leaked) ----
    if (url.pathname === "/" || url.pathname === "/debug") {
      const info = {
        version: VERSION,
        ok: true,
        hasTelegramToken: !!(env.TELEGRAM_BOT_TOKEN || "").length,
        hasGithubToken: !!(env.GITHUB_TOKEN || "").length,
        githubRepo: env.GITHUB_REPO || "(not set, using default)",
        webhookPathSet: !!(env.WEBHOOK_SECRET || "").length,
      };
      if (url.pathname === "/debug") {
        const res = await fetchCacheDebug(env);
        info.cache = res;
      }
      return new Response(JSON.stringify(info, null, 2), {
        headers: { "content-type": "application/json" },
      });
    }

    try {
      if (url.pathname !== `/webhook/${env.WEBHOOK_SECRET || "changeme"}`) {
        return new Response("ai-news-bot worker ok " + VERSION);
      }
      const update = await req.json().catch(() => null);
      if (!update) return new Response("ok");

      const tg = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}`;
      const msg = update.message || update.channel_post;
      const cb = update.callback_query;

      // ============ PACK BUTTON ============
      if (cb) {
        const chatId = cb?.message?.chat?.id ?? cb?.from?.id;
        const data = cb?.data || "";
        if (!chatId) return new Response("ok");
        await tgApi(tg, "answerCallbackQuery", { callback_query_id: cb.id });

        if (data.startsWith("pack:")) {
          const idx = parseInt(data.split(":")[1], 10);
          const { cache, err } = await loadCache(env);
          if (cache && idx >= 1 && idx <= cache.length) {
            await tgApi(tg, "sendMessage", {
              chat_id: chatId,
              text: formatPack(idx, cache[idx - 1], buildPack(cache[idx - 1])),
              parse_mode: "HTML",
              disable_web_page_preview: false,
            });
            return new Response("ok");
          }
          // visible reason instead of silence
          await tgApi(tg, "sendMessage", {
            chat_id: chatId,
            text: cache
              ? `Pack #${idx} expired (only ${cache.length} in cache). Type <b>news</b> for fresh.`
              : `⚠️ Cache unreachable (${err}). Fix: check Worker /debug, then re-paste GITHUB_TOKEN + Redeploy. Type <b>news</b> to retry via backup.`,
            parse_mode: "HTML",
          });
          // background refresh attempt (fire-and-forget)
          if (cache === null) triggerGithub(env, update).catch(() => {});
          return new Response("ok");
        }
        return new Response("ok");
      }

      // ============ TEXT MESSAGES ============
      if (msg && msg.chat) {
        const chatId = msg.chat.id;
        const text = (msg.text || "").trim();
        const low = text.toLowerCase();

        // detail N / bare number / pack N -> INSTANT from cache
        const detailMatch =
          low.match(/^\/?details?\s+(\d+)/) || low.match(/^#?(\d+)$/) || low.match(/^pack\s+(\d+)/);
        if (detailMatch) {
          const idx = parseInt(detailMatch[1], 10);
          const { cache, err } = await loadCache(env);
          if (cache && idx >= 1 && idx <= cache.length) {
            await tgApi(tg, "sendMessage", {
              chat_id: chatId,
              text: formatPack(idx, cache[idx - 1], buildPack(cache[idx - 1])),
              parse_mode: "HTML",
              disable_web_page_preview: false,
            });
          } else {
            await tgApi(tg, "sendMessage", {
              chat_id: chatId,
              text: cache
                ? `Pack #${idx} expired. Type <b>news</b> for a fresh scan.`
                : `⚠️ Cache unreachable (${err}). Open Worker /debug, fix token, Redeploy.`,
              parse_mode: "HTML",
            });
          }
          return new Response("ok");
        }

        // news / start / help / empty -> INSTANT digest from cache + background refresh
        if (
          !low ||
          low.includes("news") ||
          low.startsWith("/start") ||
          low.startsWith("/help") ||
          low === "hi" ||
          low === "hello" ||
          low === "hey" ||
          low === "help" ||
          low === "start"
        ) {
          const { cache, err } = await loadCache(env);
          if (cache && cache.length) {
            await tgApi(tg, "sendMessage", {
              chat_id: chatId,
              text: formatDigest(cache),
              parse_mode: "HTML",
              disable_web_page_preview: false,
              reply_markup: keyboardFor(cache.length),
            });
            // refresh in background so NEXT news is fresh (don't await)
            triggerGithub(env, update).catch(() => {});
          } else {
            await tgApi(tg, "sendMessage", {
              chat_id: chatId,
              text: `checking... 🔍\nCache unreachable (${err}) — running fresh scan (~1 min).`,
            });
            const st = await triggerGithub(env, update);
            if (st !== 204) await sendDispatchError(tg, chatId, st);
          }
          return new Response("ok");
        }

        // everything else (more / learn: / good N / taste / reset) NEEDS GitHub
        // (fresh RSS scan or taste.json write). Ack instantly, result ~1 min.
        await tgApi(tg, "sendMessage", {
          chat_id: chatId,
          text: "checking... 🔍\nGot it — scanning now, result in ~1 min.",
        });
        const st = await triggerGithub(env, update);
        if (st !== 204) await sendDispatchError(tg, chatId, st);
        return new Response("ok");
      }

      return new Response("ok");
    } catch (e) {
      return new Response("ok");
    }
  },
};

// ---------- telegram ----------
async function tgApi(tg, method, payload) {
  try {
    await fetch(`${tg}/${method}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e) {}
}

// ---------- GitHub ----------
async function triggerGithub(env, update) {
  try {
    const dr = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "content-type": "application/json",
      },
      body: JSON.stringify({ event_type: "telegram", client_payload: { update } }),
    });
    return dr.status;
  } catch (e) {
    return -1;
  }
}

async function sendDispatchError(tg, chatId, dStatus) {
  const why =
    dStatus === 404
      ? "GITHUB_REPO wrong (must be exactly Abik-Newar/ai-news-bot)."
      : dStatus === 401
        ? "GITHUB_TOKEN missing/invalid."
        : dStatus === 403
          ? "GITHUB_TOKEN lacks permission (Contents Read + Actions Read+Write)."
          : `GitHub said ${dStatus}. Re-paste GITHUB_TOKEN + Redeploy.`;
  await tgApi(tg, "sendMessage", {
    chat_id: chatId,
    text: `⚠️ Instant link broken (${dStatus}). ${why}`,
  });
}

// Try authed + public, main + master. Returns {cache, err}.
async function loadCache(env) {
  const res = await fetchCacheDebug(env);
  if (res.ok) return { cache: res.items, err: "" };
  return { cache: null, err: res.error || "unknown" };
}

// Shared fetch logic, also used by /debug. Never throws.
async function fetchCacheDebug(env) {
  const repo = (env.GITHUB_REPO || "Abik-Newar/ai-news-bot").trim();
  const token = (env.GITHUB_TOKEN || "").trim();
  const attempts = [];
  const urls = [
    `https://api.github.com/repos/${repo}/contents/news_cache.json?ref=main`,
    `https://api.github.com/repos/${repo}/contents/news_cache.json?ref=master`,
    `https://raw.githubusercontent.com/${repo}/main/news_cache.json`,
    `https://raw.githubusercontent.com/${repo}/master/news_cache.json`,
  ];
  for (const u of urls) {
    try {
      const isApi = u.includes("api.github.com");
      const headers = {};
      if (token) {
        headers.Authorization = `Bearer ${token}`;
        if (isApi) headers.Accept = "application/vnd.github+json";
      }
      const r = await fetch(u, { headers });
      attempts.push(`${u.split("github")[1]} -> ${r.status}`);
      if (!r.ok) continue;
      let data;
      if (isApi) {
        const j = await r.json();
        if (!j || !j.content) continue;
        data = JSON.parse(b64ToText(j.content));
      } else {
        data = await r.json();
      }
      if (Array.isArray(data)) {
        return { ok: true, count: data.length, items: data, tried: attempts };
      }
    } catch (e) {
      attempts.push(`${u.split("github")[1]} -> err`);
    }
  }
  return {
    ok: false,
    error: `tried ${attempts.join("; ") || "no fetch"} | repo=${repo} token=${token ? "set" : "MISSING"}`,
    tried: attempts,
  };
}

function b64ToText(b64) {
  const clean = String(b64 || "").replace(/\n/g, "");
  const bin = atob(clean);
  const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
  return new TextDecoder().decode(bytes);
}

// ---------- formatting (JS port of news_core.py) ----------
const HANDLES = {
  business: "@founderfilesdaily",
  entertainment: "@viralvaultdaily",
  ai: "@botbriefdaily",
};
const PAGE_NAMES = {
  business: "FounderFiles | Business",
  entertainment: "ViralVault | Entertainment",
  ai: "BotBrief | AI",
};

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function stripTags(s) {
  return String(s ?? "").replace(/<[^>]+>/g, "");
}
function unescapeHtml(s) {
  return String(s ?? "")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x27;/g, "'")
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)));
}
function clean(text, n = 300) {
  return unescapeHtml(stripTags(text)).trim().slice(0, n);
}
function shortTopic(title) {
  const t = String(title || "").replace(/^\[Paper\]\s*/, "").trim();
  return t.replace(/\s+/g, " ").slice(0, 90);
}
function fmtAge(item) {
  const a = item?.age_hours ?? 99;
  let age;
  if (a >= 90) age = "date unknown";
  else if (a < 1) age = `${Math.max(Math.floor(a * 60), 1)}m ago`;
  else if (a < 48) age = a < 24 ? `${Math.floor(a)}h ago` : `${Math.floor(a / 24)}d ago`;
  else age = `${Math.floor(a / 24)}d ago`;
  const pub = String(item?.published || "").slice(0, 16).replace("T", " ");
  return `🕒 ${age}` + (pub ? ` · ${pub}` : "");
}

// Numbers shown = cache index (so Pack #N always matches button #N).
function formatDigest(cache) {
  const L = ["<b>⚡ BEST OF BEST — fresh scan</b>"];
  const groups = [
    ["business", "💼 FounderFiles | Business — startups just launched"],
    ["entertainment", "🎬 ViralVault | Entertainment — famous people"],
    ["ai", "🤖 BotBrief | AI — new drops people feel"],
  ];
  for (const [key, head] of groups) {
    L.push(`\n<b>${esc(head)}</b>`);
    let any = false;
    cache.forEach((it, i) => {
      if ((it.page || "ai") !== key) return;
      any = true;
      const n = i + 1;
      L.push(`<b>${n}. ${esc(shortTopic(it.title))}</b>`);
      L.push(
        `   ${esc(fmtAge(it))} | ⭐${it.score ?? 0} | ${esc(it.source)} | <a href="${esc(it.link)}">link</a>`
      );
    });
    if (!any) L.push("<i>Nothing here right now.</i>");
  }
  L.push("\nTap 📦 below or type <b>detail N</b>. <b>more / learn:</b> runs fresh (~1 min).");
  return L.join("\n").slice(0, 3800);
}

function keyboardFor(n) {
  const kb = [];
  let row = [];
  for (let i = 1; i <= n; i++) {
    row.push({ text: `📦 Pack #${i}`, callback_data: `pack:${i}` });
    if (row.length === 2) {
      kb.push(row);
      row = [];
    }
  }
  if (row.length) kb.push(row);
  return { inline_keyboard: kb };
}

function buildPack(item) {
  const topic = shortTopic(item.title);
  const page = item.page || "ai";
  const handle = HANDLES[page] || "";
  const q = encodeURIComponent(topic.slice(0, 60));
  const titles = [`${topic.slice(0, 55)}`, `POV: ${topic.slice(0, 50)}`, `${topic.slice(0, 40)} in 25 seconds`];
  let overlay = topic.replace(/[^A-Za-z0-9 ]/g, "").trim().toUpperCase().split(/\s+/);
  overlay = overlay.slice(0, 7).join(" ") || topic.slice(0, 30).toUpperCase();
  let hook, hashtags, script, cta, caption;
  if (page === "business") {
    hook = `STOP. ${topic.slice(0, 45)} just happened.`;
    hashtags = "#startup #business #founder #launch #money";
    script = "0-1s hook above -> 1-8s what launched + proof screenshot -> 8-20s why it prints money -> 20-25s CTA follow for Day 2";
    cta = "Follow for startup launches daily";
    caption = `${titles[0]}\n\n${clean(item.summary, 150)}\n\n${cta} ${handle}`;
  } else if (page === "entertainment") {
    hook = `WAIT FOR IT. ${topic.slice(0, 45)}`;
    hashtags = "#viral #funny #caught #live #drama";
    script = "0-1s WAIT freeze-frame -> 1-6s buildup -> 6-20s payoff x2 replay zoom -> 20-25s comment bait";
    cta = "Follow for daily viral drops";
    caption = `${titles[0]}\n\n${clean(item.summary, 140)}\n\n${cta} ${handle}`;
  } else {
    hook = topic.slice(0, 60);
    hashtags = "#ai #ainews #aiupdates";
    script = "0-1s overlay text only (no voice hype) -> 1-8s screen-record demo -> 8-18s before/after proof -> 18-25s question bait on screen + CTA";
    const summary = clean(item.summary, 180);
    let qbait = "What do you think — hype or real shift? 💬";
    const lowT = topic.toLowerCase();
    if (lowT.includes("robot")) qbait = "Would you trust a robot to do this better than a human? 🤔💬";
    else if (lowT.includes("price") || lowT.includes("market") || lowT.includes("billion") || lowT.includes("million"))
      qbait = "Growing AI future or bubble waiting to burst? 🤔💬";
    else if (lowT.includes("space") || lowT.includes("science"))
      qbait = "Are we just scratching the surface of AI's potential? 🚀💬";
    cta = `Follow for more ${handle} 🔌 Source: ${item.source || ""}`;
    caption = `${titles[0]}\n\n${summary}\n\n${qbait}\n${cta}\n${hashtags}`;
  }
  let rohanNote, reshabNote;
  if (page === "ai") {
    rohanNote =
      `CapCut ${PAGE_NAMES[page]} template 1080x1920, captions ON. ` +
      `Slide1/carousel cover text (bold white, 5-7 words): ${overlay}. ` +
      `Reel: screen-record demo, subtitles, no hype voice. File: DATE_PAGE_FORMAT_01.`;
    reshabNote =
      "Cover = overlay text above. Caption = title + explainer + question + Follow + Source. " +
      "First comment = same question bait. Reply first 15 in 30 min. Tags: #ai #ainews #aiupdates.";
  } else if (page === "entertainment") {
    rohanNote =
      `CapCut ${PAGE_NAMES[page]} template, <28s 1080x1920. BBC-style if world news: clean photo, ` +
      `minimal text, subtitles only. If viral: freeze-frame + zoom x2. Cover: ${overlay}.`;
    reshabNote = "BBC-style: 1-sentence fact + #location #bbcnews style tags. Viral-style: WAIT + comment bait.";
  } else {
    rohanNote =
      `CapCut ${PAGE_NAMES[page]} template, <28s 1080x1920, captions ON, ` +
      `progress bar, hook 0-1s: ${hook.slice(0, 60)}. File: DATE_PAGE_FORMAT_01.`;
    reshabNote =
      "Post via phone apps, cover = hook text, first comment = question bait. " +
      "Reply first 15 comments in 30 min.";
  }
  return {
    titles, hook, script, caption,
    image_text: overlay,
    hashtags,
    videos: {
      "YouTube search": `https://www.youtube.com/results?search_query=${q}`,
      "TikTok search": `https://www.tiktok.com/search?q=${q}`,
      "Pexels stock": `https://www.pexels.com/search/${q}/`,
      "Google News": `https://news.google.com/search?q=${q}`,
    },
    expiry: "4-6 hrs (entertainment) / 24h (biz/AI). If expired, skip.",
    rohan_edit: rohanNote,
    reshab_post: reshabNote,
    credit: `Source: ${item.source} ${item.link} — add 'via ${item.source}' + transformative edit (<30s) to avoid bans.`,
  };
}

function formatPack(idx, item, pack) {
  const L = [
    `<b>🔥 VIRAL PACK #${idx} — ${PAGE_NAMES[item.page || "ai"] || ""} ${HANDLES[item.page || "ai"] || ""}</b>`,
    `📌 <b>${esc(shortTopic(item.title))}</b>`,
    `${esc(fmtAge(item))} — when it went public`,
    `📰 ${esc(item.source)} — <a href="${esc(item.link)}">source</a>`,
    "",
    "<b>Titles (pick 1):</b>",
  ];
  for (const t of pack.titles) L.push(`• ${esc(t)}`);
  L.push(`\n<b>Hook 0-1s:</b> ${esc(pack.hook)}`);
  L.push(`<b>Image text (cover/slide1):</b> ${esc(pack.image_text || "")}`);
  L.push(`<b>Script 25s:</b> ${esc(pack.script)}`);
  L.push("\n<b>Caption:</b>");
  L.push(`<pre>${esc(String(pack.caption || "").slice(0, 300))}</pre>`);
  L.push(`<b>Hashtags:</b> ${esc(pack.hashtags)}`);
  L.push("\n<b>Videos (Rohan pulls from here):</b>");
  for (const [k, v] of Object.entries(pack.videos)) L.push(`• <a href="${esc(v)}">${esc(k)}</a>`);
  L.push("");
  L.push(`<b>Rohan (edit):</b> ${esc(pack.rohan_edit)}`);
  L.push(`<b>Reshab (post):</b> ${esc(pack.reshab_post)}`);
  L.push(`<b>Credit/safety:</b> ${esc(pack.credit)}`);
  L.push(`<i>⏳ ${esc(pack.expiry)}</i>`);
  return L.join("\n").slice(0, 3800);
}
