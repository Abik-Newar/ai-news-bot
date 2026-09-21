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
            const pack = await buildPack(cache[idx - 1]);
            await tgApi(tg, "sendMessage", {
              chat_id: chatId,
              text: formatPack(idx, cache[idx - 1], pack),
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
            const pack = await buildPack(cache[idx - 1]);
            await tgApi(tg, "sendMessage", {
              chat_id: chatId,
              text: formatPack(idx, cache[idx - 1], pack),
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

async function fetchYoutubeVideos(topic, limit = 7) {
  // Best-effort REAL video URLs for exact topic. 4s cap so packs stay <5s.
  try {
    const q = encodeURIComponent(String(topic || "").slice(0, 60));
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 4000);
    const r = await fetch(`https://www.youtube.com/results?search_query=${q}`, {
      signal: ctrl.signal,
    }).catch(() => null);
    clearTimeout(t);
    if (!r || !r.ok) return [];
    const html = await r.text();
    const ids = [...html.matchAll(/"videoId":"([A-Za-z0-9_-]{11})"/g)].map((m) => m[1]);
    const seen = new Set();
    const out = [];
    for (const vid of ids) {
      if (seen.has(vid)) continue;
      seen.add(vid);
      out.push({
        title: `Real video ${out.length + 1} for: ${String(topic || "").slice(0, 45)}`,
        url: `https://www.youtube.com/watch?v=${vid}`,
      });
      if (out.length >= limit) break;
    }
    return out;
  } catch (e) {
    return [];
  }
}

function hookOptions(topic, page) {
  const short = String(topic || "").slice(0, 45);
  if (page === "business")
    return [`STOP. ${short} JUST HAPPENED`, `Nobody saw ${short} coming`, `POV: ${short}`];
  if (page === "entertainment")
    return [`WAIT FOR IT. ${short}`, `You missed ${short} 😱`, `POV: ${short} LIVE`];
  return [String(topic || "").slice(0, 60), `${String(topic || "").slice(0, 50)} — real or hype?`, `POV: ${String(topic || "").slice(0, 50)}`];
}

function platformPacks(item, topic, hook, handle, summary) {
  const src = item.source || "";
  const link = item.link || "";
  const credit = `Via ${src}`;
  const page = item.page || "ai";
  const base =
    page === "business" ? "#startup #business #founder"
    : page === "entertainment" ? "#viral #entertainment #trending"
    : "#ai #ainews #tech";
  const title60 = String(topic || "").slice(0, 60);
  return {
    instagram: { title: title60, caption: `${hook}\n${summary.slice(0, 100)}\nFollow ${handle} daily\n${credit}`, tags: `${base} #reels #reelsindia #explore #fyp #daily`, credit: `${credit} ${link}` },
    facebook: { title: title60, caption: `${hook}\n${summary.slice(0, 120)}\nFollow for daily drops. ${credit}`, tags: `${base} #reels #facebookreels`, credit: `${credit} ${link}` },
    tiktok: { title: title60.slice(0, 50), caption: `${hook} ${summary.slice(0, 80)}`, tags: `${base} #fyp #foryou #viral`, credit: `${credit} ${link}` },
    youtube: { title: title60, caption: `${hook}\n${summary.slice(0, 120)}\nSource: ${link}\nFollow for daily shorts.`, tags: `${base.replace(/#/g, "").replace(/ /g, ", ")}, shorts, viral`, credit: `${credit} ${link}` },
    x: { title: title60, caption: `${hook}\n${title60}\n${link}`, tags: `${base} #news`, credit: `${credit} ${link}` },
  };
}

async function buildPack(item) {
  const topic = shortTopic(item.title);
  const page = item.page || "ai";
  const handle = HANDLES[page] || "";
  const q = encodeURIComponent(topic.slice(0, 60));
  const gq = encodeURIComponent(topic.slice(0, 60));
  const titles = [`${topic.slice(0, 55)}`, `POV: ${topic.slice(0, 50)}`, `${topic.slice(0, 40)} in 25 seconds`];
  const hooks = hookOptions(topic, page);
  const hook = hooks[0];
  let overlay = hook.replace(/[^A-Za-z0-9 ]/g, "").trim().toUpperCase().split(/\s+/);
  overlay = overlay.slice(0, 7).join(" ") || topic.slice(0, 30).toUpperCase();
  const summary = clean(item.summary, 180);
  let hashtags, script;
  if (page === "business") {
    hashtags = "#startup #business #founder #launch #money";
    script = "0-1s hook above -> 1-8s what launched + proof screenshot -> 8-20s why it prints money -> 20-25s CTA follow for Day 2";
  } else if (page === "entertainment") {
    hashtags = "#viral #funny #caught #live #drama";
    script = "0-1s WAIT freeze-frame -> 1-6s buildup -> 6-20s payoff x2 replay zoom -> 20-25s comment bait";
  } else {
    hashtags = "#ai #ainews #aiupdates";
    script = "0-1s overlay text only (no voice hype) -> 1-8s screen-record demo -> 8-18s before/after proof -> 18-25s question bait on screen + CTA";
  }
  const realVideos = await fetchYoutubeVideos(topic);
  return {
    titles, hooks, hook, script,
    caption: `${titles[0]}\n\n${summary}`,
    image_text: overlay,
    hashtags,
    videos: {
      "YouTube search": `https://www.youtube.com/results?search_query=${q}`,
      "TikTok search": `https://www.tiktok.com/search?q=${q}`,
      "Google News": `https://news.google.com/search?q=${gq}`,
    },
    real_videos: realVideos,
    image_links: {
      "Google Images": `https://www.google.com/search?tbm=isch&q=${gq}`,
      "Unsplash": `https://unsplash.com/s/photos/${q}`,
      "Pexels": `https://www.pexels.com/search/${q}/`,
    },
    platforms: platformPacks(item, topic, hook, handle, summary),
    expiry: "4-6 hrs (entertainment) / 24h (biz/AI). If expired, skip.",
    rohan_edit:
      page === "ai"
        ? `CapCut ${PAGE_NAMES[page]} template 1080x1920, captions ON. On-screen hook (bold white, 5-7 words): ${overlay}. Reel: screen-record demo, subtitles, no hype voice. File: DATE_PAGE_FORMAT_01.`
        : page === "entertainment"
          ? `CapCut ${PAGE_NAMES[page]} template, <28s 1080x1920. BBC-style if world news: clean photo, minimal text, subtitles only. If viral: freeze-frame + zoom x2. Cover: ${overlay}.`
          : `CapCut ${PAGE_NAMES[page]} template, <28s 1080x1920, captions ON, progress bar, hook 0-1s: ${hook.slice(0, 60)}. File: DATE_PAGE_FORMAT_01.`,
    reshab_post: "See per-platform section below.",
    credit: `Source: ${item.source} ${item.link} — add 'via ${item.source}' + transformative edit (<30s) to avoid bans.`,
    director_brief: summary,
  };
}

function formatPack(idx, item, pack) {
  const L = [
    `<b>🔥 VIRAL PACK #${idx} — ${PAGE_NAMES[item.page || "ai"] || ""} ${HANDLES[item.page || "ai"] || ""}</b>`,
    `📌 <b>${esc(shortTopic(item.title))}</b>`,
    `${esc(fmtAge(item))} — when it went public`,
    `📰 ${esc(item.source)} — <a href="${esc(item.link)}">source</a>`,
    "",
    "<b>👁 FOR YOU (context):</b>",
    esc((pack.director_brief || "").slice(0, 220)),
    `⭐${item.score ?? 0} | ⏳ ${esc(pack.expiry || "")}`,
    "",
    "<b>🎬 ROHAN (edit):</b>",
    `On-screen hook: <b>${esc(pack.hook || "")}</b>`,
    `Alt: ${esc(((pack.hooks || []).slice(1, 3)).join(" / "))}`,
    esc((pack.rohan_edit || "").slice(0, 220)),
  ];
  const rv = pack.real_videos || [];
  if (rv.length) {
    L.push(`<b>Real videos (top ${Math.min(rv.length, 7)} for exact topic — pick most on-topic):</b>`);
    rv.slice(0, 7).forEach((v, i) => L.push(`${i + 1}. <a href="${esc(v.url)}">${esc((v.title || "video").slice(0, 45))}</a>`));
  } else {
    L.push("<b>No direct video match — images / search:</b>");
    for (const [k, v] of Object.entries(pack.image_links || {})) L.push(`• <a href="${esc(v)}">${esc(k)}</a>`);
  }
  for (const [k, v] of Object.entries(pack.videos || {})) L.push(`• <a href="${esc(v)}">${esc(k)}</a>`);
  L.push(`Credit: ${esc((pack.credit || "").slice(0, 160))}`);
  L.push("");
  L.push("<b>📲 RESHAB (post per platform):</b>");
  for (const pname of ["instagram", "facebook", "tiktok", "youtube", "x"]) {
    const p = (pack.platforms || {})[pname];
    if (!p) continue;
    L.push(`<b>${pname.toUpperCase()}:</b> ${esc((p.title || "").slice(0, 60))}`);
    L.push(`${esc((p.caption || "").slice(0, 140))}`);
    L.push(`${esc((p.tags || "").slice(0, 120))}`);
    L.push(`<i>${esc((p.credit || "").slice(0, 120))}</i>`);
  }
  const text = L.join("\n");
  if (text.length > 3800) return text.slice(0, 3800).rsplit("\n", 1)[0];
  return text;
}
