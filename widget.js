/**
 * Portfolio Pulse — iOS home-screen widget (Scriptable)
 * =====================================================
 * Paste this into a new script in the Scriptable app (free, App Store), then
 * add a Scriptable widget to your home screen and set its script to this one.
 *
 * Reads GET /api/snapshot?widget=1[&live=1] — the same figures the dashboard
 * hero shows, computed by the same server-side function, so the widget cannot
 * disagree with the app. The response is a few hundred bytes.
 *
 * With live=1 the portfolio is re-priced on request; without it, the last
 * stored daily snapshot is returned. See SETTINGS.live below.
 *
 * Sizes: small  → value + today
 *        medium → value + today + all-time + sparkline + account row
 *        large  → same as medium with every account listed
 *
 * Note: tapping opens the site in Safari. iOS has no way to deep-link into an
 * installed home-screen web app, so it cannot open the PWA itself.
 */

// ═══ CUSTOMISE HERE ══════════════════════════════════════════════════════════
// Everything you're likely to want to change lives in this block. Edit, then
// long-press the widget on the home screen and tap Reload to see it.
const SETTINGS = {
  live: true,           // true = priced now; false = last daily snapshot (faster)
  showSparkline: true,  // the 30-day mini chart
  showAccounts: true,   // the TFSA / FHSA / RRSP / Non-Reg row
  showAllTime: true,    // all-time $ and % beside today's change
  showAsOf: true,       // "9:41 AM" timestamp in the header
  accentColor: "#00d4ff",       // header + sparkline colour
  headerText: "PORTFOLIO",      // change to anything, or "" to hide
  // Which accounts to show, in order. Names must match the app's.
  // e.g. ["TFSA", "FHSA"] to show only those two.
  accounts: ["TFSA", "FHSA", "RRSP", "Investment"],
  accountLabels: { Investment: "NON-REG" },  // rename for display
  refreshMinutes: 15,   // hint only — iOS decides the real cadence
};
// ═════════════════════════════════════════════════════════════════════════════

const BASE = "https://portfolio-pulse-dun.vercel.app";
const API = BASE + "/api/snapshot?widget=1" + (SETTINGS.live ? "&live=1" : "");
const SITE = BASE;
const CACHE_FILE = "pulse-widget-cache.json";

// Palette lifted from the app's own CSS variables
const C = {
  bgTop: new Color("#0c1524"),
  bgBot: new Color("#060b14"),
  cyan: new Color(SETTINGS.accentColor),
  text: new Color("#e6edf5"),
  muted: new Color("#7d8da5"),
  green: new Color("#10b981"),
  red: new Color("#ef4444"),
};

// ── Data ─────────────────────────────────────────────────────────────────────
// Widgets get woken up in the background where the network may be unavailable,
// so a successful fetch is cached and reused rather than showing an error card.
function cachePath() {
  const fm = FileManager.local();
  return fm.joinPath(fm.cacheDirectory(), CACHE_FILE);
}

async function getData() {
  try {
    const req = new Request(API);
    req.timeoutInterval = 15;
    const d = await req.loadJSON();
    if (d && d.ok) {
      FileManager.local().writeString(cachePath(), JSON.stringify(d));
      return { data: d, stale: false };
    }
  } catch (e) {
    // fall through to cache
  }
  const fm = FileManager.local();
  if (fm.fileExists(cachePath())) {
    try {
      return { data: JSON.parse(fm.readString(cachePath())), stale: true };
    } catch (e) {}
  }
  return { data: null, stale: false };
}

// ── Formatting ───────────────────────────────────────────────────────────────
const money = (v) =>
  (v < 0 ? "-$" : "$") + Math.abs(Math.round(v)).toLocaleString("en-CA");

const signedMoney = (v) =>
  (v >= 0 ? "+" : "-") + "$" + Math.abs(Math.round(v)).toLocaleString("en-CA");

const signedPct = (v) => (v >= 0 ? "+" : "") + Number(v || 0).toFixed(2) + "%";

const compact = (v) => {
  const a = Math.abs(v);
  if (a >= 1000) return (v < 0 ? "-" : "") + "$" + Math.round(a / 1000) + "K";
  return money(v);
};

const toneColor = (v) => (v >= 0 ? C.green : v < 0 ? C.red : C.muted);

// ── Sparkline ────────────────────────────────────────────────────────────────
function sparkline(values, w, h, color) {
  const ctx = new DrawContext();
  ctx.size = new Size(w, h);
  ctx.opaque = false;
  ctx.respectScreenScale = true;

  const pts = (values || []).filter((v) => typeof v === "number");
  if (pts.length < 2) return ctx.getImage();

  const min = Math.min(...pts);
  const max = Math.max(...pts);
  const span = max - min || 1;
  const x = (i) => (i / (pts.length - 1)) * (w - 2) + 1;
  const y = (v) => h - 2 - ((v - min) / span) * (h - 4);

  // Fill under the line, mirroring the app's translucent blue chart fill
  const fill = new Path();
  fill.move(new Point(x(0), h));
  pts.forEach((v, i) => fill.addLine(new Point(x(i), y(v))));
  fill.addLine(new Point(x(pts.length - 1), h));
  fill.closeSubpath();
  ctx.setFillColor(new Color("#3b82f6", 0.18));
  ctx.addPath(fill);
  ctx.fillPath();

  const line = new Path();
  line.move(new Point(x(0), y(pts[0])));
  pts.forEach((v, i) => i && line.addLine(new Point(x(i), y(v))));
  ctx.setStrokeColor(color);
  ctx.setLineWidth(2);
  ctx.addPath(line);
  ctx.strokePath();

  return ctx.getImage();
}

// ── Widget ───────────────────────────────────────────────────────────────────
function label(stack, text, size = 9) {
  const t = stack.addText(text);
  t.font = Font.mediumSystemFont(size);
  t.textColor = C.muted;
  return t;
}

function build({ data, stale }) {
  const w = new ListWidget();
  const g = new LinearGradient();
  g.colors = [C.bgTop, C.bgBot];
  g.locations = [0, 1];
  w.backgroundGradient = g;
  w.url = SITE;
  w.setPadding(14, 14, 14, 14);
  // A hint only — iOS decides the actual refresh cadence.
  w.refreshAfterDate = new Date(Date.now() + SETTINGS.refreshMinutes * 60 * 1000);

  if (!data) {
    const t = w.addText("Portfolio Pulse");
    t.font = Font.semiboldSystemFont(13);
    t.textColor = C.text;
    const e = w.addText("No data yet — open the app once.");
    e.font = Font.systemFont(10);
    e.textColor = C.muted;
    return w;
  }

  const family = config.widgetFamily || "medium";
  const small = family === "small";

  // Header
  const head = w.addStack();
  head.centerAlignContent();
  if (SETTINGS.headerText) {
    const hl = head.addText(SETTINGS.headerText);
    hl.font = Font.semiboldSystemFont(8);
    hl.textColor = C.cyan;
  }
  head.addSpacer();
  // "offline" wins over the timestamp — if the data is cached, when it was
  // fetched matters more than what time it is now.
  if (stale) label(head, "offline", 8);
  else if (SETTINGS.showAsOf) {
    const df = new DateFormatter();
    df.dateFormat = "h:mm a";
    label(head, df.string(new Date()), 8);
  }

  w.addSpacer(small ? 4 : 6);

  // Total value
  const val = w.addText(money(data.value));
  val.font = Font.boldMonospacedSystemFont(small ? 20 : 26);
  val.textColor = C.text;
  val.minimumScaleFactor = 0.6;
  val.lineLimit = 1;

  w.addSpacer(3);

  // Today + all-time
  const row = w.addStack();
  row.centerAlignContent();
  const today = row.addText(
    signedMoney(data.day) + "  " + signedPct(data.day_pct)
  );
  today.font = Font.mediumMonospacedSystemFont(small ? 10 : 11);
  today.textColor = toneColor(data.day);
  today.lineLimit = 1;
  today.minimumScaleFactor = 0.7;

  // All-time is omitted rather than shown as +$0 if the server couldn't
  // supply it — a wrong number is worse than a missing one.
  if (!small && SETTINGS.showAllTime && data.pnl != null) {
    row.addSpacer();
    const at = row.addText(
      signedMoney(data.pnl) + "  " + signedPct(data.roi_pct)
    );
    at.font = Font.mediumMonospacedSystemFont(11);
    at.textColor = toneColor(data.pnl);
    at.lineLimit = 1;
  }

  if (small) {
    if (SETTINGS.showSparkline) {
      w.addSpacer(4);
      w.addImage(sparkline(data.spark, 130, 30, C.cyan)).applyFittingContentMode();
    }
    return w;
  }

  // Sparkline
  if (SETTINGS.showSparkline) {
    w.addSpacer(8);
    const img = w.addImage(
      sparkline(data.spark, 300, family === "large" ? 70 : 46, C.cyan)
    );
    img.applyFillingContentMode();
  }

  // Accounts — follows the order listed in SETTINGS.accounts, skipping any the
  // server didn't return, so a renamed or closed account degrades quietly.
  if (SETTINGS.showAccounts) {
    const have = data.accounts || {};
    const shown = SETTINGS.accounts.filter((n) => have[n] != null);
    if (shown.length) {
      w.addSpacer(8);
      const arow = w.addStack();
      arow.spacing = 10;
      shown.forEach((name) => {
        const cell = arow.addStack();
        cell.layoutVertically();
        const n = cell.addText(
          String(SETTINGS.accountLabels[name] || name).toUpperCase()
        );
        n.font = Font.semiboldSystemFont(7.5);
        n.textColor = C.muted;
        const a = cell.addText(compact(have[name]));
        a.font = Font.boldMonospacedSystemFont(11);
        a.textColor = C.text;
        a.lineLimit = 1;
        a.minimumScaleFactor = 0.6;
        arow.addSpacer();
      });
    }
  }

  return w;
}

// ── Run ──────────────────────────────────────────────────────────────────────
const result = await getData();
const widget = build(result);

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  const f = config.widgetFamily || "medium";
  if (f === "small") widget.presentSmall();
  else if (f === "large") widget.presentLarge();
  else widget.presentMedium();
}
Script.complete();
