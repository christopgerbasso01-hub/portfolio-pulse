# Portfolio Pulse — iPhone App Setup Guide

Follow these steps once to deploy your app. After that, it updates itself — no action needed.

---

## Step 1 — Create a GitHub Repository

1. Go to **github.com** and sign in
2. Click the **+** button (top right) → **New repository**
3. Name it: `portfolio-pulse`
4. Set it to **Public** (required for free GitHub Pages hosting)
5. Leave everything else as-is → click **Create repository**

---

## Step 2 — Upload Your App Files

After creating the repo, click **uploading an existing file** (shown on the empty repo page).

Drag and drop all 5 files from your `pwa` folder:
- `index.html`
- `manifest.json`
- `sw.js`
- `icon-192.png`
- `icon-512.png`

Click **Commit changes**.

---

## Step 3 — Enable GitHub Pages

1. In your repo, click **Settings** (top tab)
2. Scroll to **Pages** in the left sidebar
3. Under **Source**, select **Deploy from a branch**
4. Set Branch to **main** and folder to **/ (root)**
5. Click **Save**

Wait 1–2 minutes. Your app will be live at:
```
https://YOUR-USERNAME.github.io/portfolio-pulse/
```

(Replace YOUR-USERNAME with your actual GitHub username)

---

## Step 4 — Add to iPhone Home Screen

1. On your iPhone, open **Safari** (must be Safari, not Chrome)
2. Go to your app URL above
3. Wait for the page to fully load and prices to appear
4. Tap the **Share** button (rectangle with arrow pointing up)
5. Scroll down and tap **Add to Home Screen**
6. Name it **Portfolio Pulse** → tap **Add**

The app icon will appear on your home screen. Tap it and it opens fullscreen like a native app, with live prices loading automatically.

---

## Updating Your Holdings

When you buy or sell a position, open `index.html` in a text editor and find the `HOLDINGS` array near the top of the JavaScript section. Each line looks like:

```javascript
{ t:'NVDA', n:'NVIDIA', a:'TFSA', s:40, c:645, fx:'USD' },
```

- `t` = ticker symbol
- `n` = display name
- `a` = account (TFSA, FHSA, RRSP, or Investment)
- `s` = number of shares
- `c` = your total cost basis in CAD
- `fx` = currency the stock trades in (USD or CAD)

After editing, re-upload `index.html` to GitHub (same way as Step 2). The app updates within minutes.

---

## Troubleshooting

**Prices show "—"**: Yahoo Finance API can occasionally be slow. Tap the ↻ refresh button in the top-right corner. If it persists, it may be a temporary API outage — try again in a few minutes.

**App didn't update after I uploaded new files**: Hard-refresh in Safari: tap the Share button → tap **Reload** (or Settings → Clear History and Website Data).

**"Add to Home Screen" option is missing**: You must use Safari. Chrome and Firefox on iPhone don't support PWA installation.
