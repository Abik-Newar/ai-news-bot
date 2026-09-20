// worker.js — instant Telegram ack + trigger GitHub (free Cloudflare Workers).
// Why: GitHub cron can only reply every 30 min. This replies "checking..." in <2s
// and triggers the GitHub responder for the full result in ~1-2 min.
// Costs $0 (Workers free 100k req/day). Secrets set in dashboard, never in code.
//
// Setup (5 min, no CLI):
// 1. Cloudflare dashboard -> Workers & Pages -> Create Worker -> Deploy -> Edit code,
//    paste this file, Deploy.
// 2. Worker -> Settings -> Variables -> Add secrets:
//      TELEGRAM_BOT_TOKEN, GITHUB_TOKEN (fine-grained PAT: this repo only, Actions Read+Write),
//      GITHUB_REPO = Abik-Newar/ai-news-bot, WEBHOOK_SECRET = any-long-random-string
// 3. In browser open (replace):
//    https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<worker>.workers.dev/webhook/<WEBHOOK_SECRET>
// 4. Send /news in Telegram -> instant "checking..." -> full digest ~1 min later.
// To remove: setWebhook?url= (empty) and re-enable polling.

export default {
  async fetch(req, env) {
    try {
      const url = new URL(req.url);
      if (url.pathname !== `/webhook/${env.WEBHOOK_SECRET || "changeme"}`) {
        return new Response("ai-news-bot worker ok");
      }
      const update = await req.json().catch(() => null);
      if (!update) return new Response("ok");

      const tg = `https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}`;
      const msg = update.message || update.channel_post;
      const cb = update.callback_query;

      // Instant ack so user sees "checking..." in ~1-2 seconds (top priority).
      const jobs = [];
      if (cb) {
        // answer button tap immediately (stops spinner), full pack comes via GitHub run
        jobs.push(
          fetch(`${tg}/answerCallbackQuery`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ callback_query_id: cb.id, text: "checking... 🔍" }),
          }).catch(() => {})
        );
      } else if (msg && msg.chat) {
        jobs.push(
          fetch(`${tg}/sendMessage`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              chat_id: msg.chat.id,
              text: "checking... 🔍\nGot it — scanning now, result in ~1 min.",
            }),
          }).catch(() => {})
        );
      }

      // Trigger GitHub for the real work (RSS scan / detail pack / learn).
      // Uses repository_dispatch so the update payload travels with the trigger —
      // no getUpdates polling needed (which breaks while a webhook is active).
      // NEVER fail silently: if GitHub rejects the trigger, tell the user why.
      let dStatus = 0;
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
        dStatus = dr.status;
      } catch (e) {
        dStatus = -1;
      }
      await Promise.all(jobs);
      if (dStatus !== 204 && (msg && msg.chat)) {
        const why =
          dStatus === 404
            ? "GITHUB_REPO name is wrong (must be exactly Abik-Newar/ai-news-bot)."
            : dStatus === 401
              ? "GITHUB_TOKEN is missing/invalid (fine-grained PAT, Actions Read+Write on this repo)."
              : dStatus === 403
                ? "GITHUB_TOKEN lacks permission (needs Actions Read+Write on this repo)."
                : `GitHub said ${dStatus}. Re-paste GITHUB_TOKEN in Worker settings + Redeploy.`;
        await fetch(`${tg}/sendMessage`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            chat_id: msg.chat.id,
            text: `⚠️ Instant link broken (${dStatus}). ${why}\nYour answer still arrives via the 5-min auto-check — nothing lost.`,
          }),
        }).catch(() => {});
      }
      return new Response("ok");
    } catch (e) {
      return new Response("ok");
    }
  },
};
