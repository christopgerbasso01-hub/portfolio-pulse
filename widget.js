/**
 * Portfolio Pulse — iOS home-screen widget (Scriptable)
 * =====================================================
 * Paste this into a new script in the Scriptable app (free, App Store), then
 * add a Scriptable widget to your home screen and set its script to this one.
 *
 * Reads GET /api/snapshot?widget=1 — the same figures the dashboard hero shows,
 * from the same server-side snapshot, so the widget can never disagree with the
 * app. The response is ~200 bytes.
 *
 * Sizes: small  → value + today
 *        medium → value + today + all-time + sparkline + account row
 *        large  → same as medium with every account listed
 *
 * Note: tapping opens the site in Safari. iOS has no way to deep-link into an
 * installed home-screen web app, so it cannot open the PWA itself.
 */

const API = "https://portfolio-pulse-dun.vercel.app/api/snapshot?widget=1";
const SITE = "https://portfolio-pulse-dun.vercel.app";
const CACHE_FILE = "pulse-widget-cache.json";

// Palette lifted from the app's own CSS variables
const C = {
  bgTop: new Color("#0c1524"),
  bgBot: new Color("#060b14"),
  cyan: new Color("#00d4ff"),
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
  w.refreshAfterDate = new Date(Date.now() + 15 * 60 * 1000);

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
  const hl = head.addText("PORTFOLIO");
  hl.font = Font.semiboldSystemFont(8);
  hl.textColor = C.cyan;
  head.addSpacer();
  if (stale) label(head, "offline", 8);

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

  if (!small) {
    row.addSpacer();
    const at = row.addText(
      signedMoney(data.pnl) + "  " + signedPct(data.roi_pct)
    );
    at.font = Font.mediumMonospacedSystemFont(11);
    at.textColor = toneColor(data.pnl);
    at.lineLimit = 1;
  }

  if (small) {
    w.addSpacer(4);
    w.addImage(sparkline(data.spark, 130, 30, C.cyan)).applyFittingContentMode();
    return w;
  }

  // Sparkline
  w.addSpacer(8);
  const img = w.addImage(sparkline(data.spark, 300, family === "large" ? 70 : 46, C.cyan));
  img.applyFillingContentMode();

  // Accounts
  const accts = Object.entries(data.accounts || {}).sort((a, b) => b[1] - a[1]);
  if (accts.length) {
    w.addSpacer(8);
    const shown = family === "large" ? accts : accts.slice(0, 4);
    const arow = w.addStack();
    arow.spacing = 10;
    const nameMap = { Investment: "NON-REG" };
    shown.forEach(([name, v]) => {
      const cell = arow.addStack();
      cell.layoutVertically();
      const n = cell.addText((nameMap[name] || name).toUpperCase());
      n.font = Font.semiboldSystemFont(7.5);
      n.textColor = C.muted;
      const a = cell.addText(compact(v));
      a.font = Font.boldMonospacedSystemFont(11);
      a.textColor = C.text;
      a.lineLimit = 1;
      a.minimumScaleFactor = 0.6;
      arow.addSpacer();
    });
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
