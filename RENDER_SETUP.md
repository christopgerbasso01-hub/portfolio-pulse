# Running Portfolio Pulse on Render (free)

The same site, hosted somewhere other than Vercel. Your data does **not** move: it lives in Upstash
(Redis), so both hosts read and write the same trades, dividends and settings.

## Steps (about 15 minutes; no payment card should be asked for)

1. **Account** — go to render.com and sign up with GitHub.
2. **Create the service** — Dashboard → **New +** → **Blueprint** → pick this repository → **Apply**.
   Render reads `render.yaml` and creates a *free* web service called `portfolio-pulse`.
   If any screen asks for a card or shows a price above $0, stop and don't continue.
3. **Secrets** — the service asks for the values marked in `render.yaml`. Copy them from Vercel
   (Project → Settings → Environment Variables → eye icon): `KV_REST_API_URL`, `KV_REST_API_TOKEN`,
   `CRON_SECRET`, `VAPID_PRIVATE_KEY`, `FMP_API_KEY`, `FINNHUB_API_KEY`, `GROQ_API_KEY`, `TAVILY_API_KEY`.
   Leave blank any you don't have. Never paste these into chat or commit them.
4. **SITE_URL** — when Render shows the service address (like `https://portfolio-pulse.onrender.com`),
   set the `SITE_URL` variable to exactly that.
5. **Open it** — the first load after it has been idle takes about a minute.
6. **Point the background jobs at the new address** (use only ONE host for jobs, or you get duplicates):
   - GitHub → repo → Settings → Secrets and variables → Actions → **Variables** → new variable
     `SITE_URL` = the Render address.
   - cron-job.org: change each job's address from `portfolio-pulse-dun.vercel.app` to the Render address.
   - Add a cron-job.org job that opens `<address>/healthz` every 10 minutes. Without it Render puts the
     service to sleep after 15 minutes idle (the free plan has 750 hours a month; always-on uses ~744).
7. **Phone** — open the new address, Add to Home Screen, turn notifications on again (they are tied to the address).
   If you use the Scriptable widget, change `BASE` at the top of `widget.js`.

## Free-plan limits to know

- Sleeps after 15 minutes without a visit; waking takes about a minute (the keep-alive job avoids it).
- Render may suspend a free service that makes an unusually large number of outside requests. The
  dashboard now refreshes prices every 20 s (market open) / 2 min (closed) and not at all in a hidden
  tab, and the server shares one price fetch between all open tabs, to stay well under that.
- The disk is wiped on every restart; nothing important is stored there (data is in Upstash).

## Going back to Vercel

Vercel resumes on its own on the date in its email (or when upgraded). The page uses whatever address
it was opened from, so both work. Code changes reach Vercel with `vercel --prod`; Render redeploys
by itself on every push to GitHub.
