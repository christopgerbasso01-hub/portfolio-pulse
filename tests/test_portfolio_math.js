// Portfolio math — tax projection, XIRR, risk stats, dividend forecast.
//
// Pulls the pure functions straight out of index.html (same approach as
// test_ledger_engine.js), so these tests exercise what the dashboard runs.
//
// Pins the November 2026 review findings:
//   - the projection taxed UNREALIZED gains, netted capital losses against
//     salary, and multiplied all income by the top marginal rate (~$29.7K on
//     $100K instead of ~$20K);
//   - the Home Buyers' Plan was hard-coded at $35K (it has been $60K since
//     April 2024, and is capped at what the RRSP holds);
//   - tax-loss advice ignored the superficial loss rule across TFSA/RRSP/FHSA.
//
// Run: node tests/test_portfolio_math.js
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

function extract(name) {
  const start = html.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`${name} not found in index.html`);
  let i = html.indexOf('{', start), depth = 0;
  for (; i < html.length; i++) {
    if (html[i] === '{') depth++;
    else if (html[i] === '}' && --depth === 0) break;
  }
  return html.slice(start, i + 1);
}
function extractConst(name) {
  const start = html.indexOf(`const ${name} = {`);
  if (start < 0) throw new Error(`const ${name} not found`);
  let i = html.indexOf('{', start), depth = 0;
  for (; i < html.length; i++) {
    if (html[i] === '{') depth++;
    else if (html[i] === '}' && --depth === 0) break;
  }
  return html.slice(start, i + 2);
}

const ctx = { console };
vm.createContext(ctx);
const fns = ['_bracketTax', '_ontarioHealthPremium', 'ontarioIncomeTax', 'ontarioMarginalRate',
             'superficialLossCheck', 'nonRegTaxYear', 'hbpUsable', 'xirr', 'monthlyReturns',
             'riskStats', 'calendarYearReturns', 'forwardDividends', '_divNetFactor', 'ppLotModel'];
vm.runInContext(`
  const USD_BOOK_RATE = 1.3925;
  ${extractConst('TAX_PARAMS')}
  ${fns.map(extract).join('\n')}
  ${fns.map(f => `this.${f} = ${f};`).join('\n')}
  this.TAX_PARAMS = TAX_PARAMS;
`, ctx);

const fails = [];
function ck(name, got, want, tol) {
  const ok = tol != null ? Math.abs(got - want) <= tol : JSON.stringify(got) === JSON.stringify(want);
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `\n        got=${JSON.stringify(got)} want=${JSON.stringify(want)}`}`);
  if (!ok) fails.push(name);
}

console.log('\nOntario income tax (2026)');
ck('bracket tax: $100K federal brackets', ctx._bracketTax(100000, ctx.TAX_PARAMS.fed.brackets),
   58523 * 0.14 + (100000 - 58523) * 0.205, 0.01);
ck('no tax below the basic personal amounts', ctx.ontarioIncomeTax({ salary: 12000 }).total, 0, 0.5);
const t100 = ctx.ontarioIncomeTax({ salary: 100000 });
ck('$100K salary: about $20.0K (old panel showed $31.5K × all income)', t100.total, 20025, 60);
ck('$100K salary: Ontario health premium $750', t100.ohp, 750, 0.01);
const mr = ctx.ontarioMarginalRate({ salary: 100000 });
ck('$100K marginal rate = 20.5% fed + 9.15% ON (+ surtax sliver)', mr, 0.2965 + 0.0183, 0.0025);
ck('capital gains add tax at half the marginal rate',
   ctx.ontarioIncomeTax({ salary: 100000, taxableGains: 5000 }).total - t100.total, 5000 * mr, 25);
const div = ctx.ontarioIncomeTax({ salary: 100000, eligDiv: 1000 }).total - t100.total;
ck('$1,000 eligible dividend taxed well below salary rate', div > 0 && div < 1000 * mr * 0.7, true);
const usd = ctx.ontarioIncomeTax({ salary: 100000, otherIncome: 1000, foreignTaxPaid: 150 }).total - t100.total;
ck('$1,000 US dividend: withholding credited back', usd, 1000 * mr - 150, 5);

console.log('\nRealized gains only — losses never reduce salary');
const sales = [
  { date: '2026-11-09', account: 'Investment', ticker: 'AVGO', realizedCAD: -3200 },
  { date: '2026-11-09', account: 'Investment', ticker: 'MSTR', realizedCAD: -900 },
  { date: '2026-08-21', account: 'RRSP',       ticker: 'MU',   realizedCAD: -500 },   // registered → ignored
  { date: '2025-03-01', account: 'Investment', ticker: 'OLD',  realizedCAD: 9999 },   // other year → ignored
];
const yr = ctx.nonRegTaxYear(2026, sales, [], { gains: 0, losses: 0 });
ck('2026 non-reg net = −$4,100', yr.net, -4100, 0.01);
ck('…registered and prior-year sales excluded', yr.sales.length, 2);
const yr2 = ctx.nonRegTaxYear(2026, [{ date: '2026-02-01', account: 'Investment', ticker: 'X', realizedCAD: 5000 }, ...sales], [], {});
ck('gains net against losses', yr2.net, 900, 0.01);
const divYr = ctx.nonRegTaxYear(2026, [], [
  { type: 'DIVIDEND', date: '2026-07-01', account: 'Investment', ticker: 'RY.TO', amount: 34, ccy: 'CAD' },
  { type: 'DIVIDEND', date: '2026-07-01', account: 'Investment', ticker: 'V', amount: 8.5, ccy: 'USD', rate: 1.4 },
  { type: 'DIVIDEND', date: '2026-07-01', account: 'TFSA',       ticker: 'NVDA', amount: 2, ccy: 'USD', rate: 1.4 },
], {});
ck('Canadian dividend is eligible', divYr.eligDiv, 34, 0.01);
ck('US dividend grossed up for 15% withholding', divYr.foreignGross, 8.5 * 1.4 / 0.85, 0.01);
ck('…and the withholding becomes a credit', divYr.foreignTax, 8.5 * 1.4 / 0.85 * 0.15, 0.01);

console.log("\nHome Buyers' Plan");
ck('capped at $60K', ctx.hbpUsable(90000), 60000);
ck('limited to what the RRSP holds', ctx.hbpUsable(17570), 17570);

console.log('\nSuperficial loss rule — every account counts');
const txs = [
  { type: 'BUY', date: '2026-10-20', account: 'TFSA', ticker: 'SOXL' },
  { type: 'BUY', date: '2026-08-01', account: 'Investment', ticker: 'SOXL' },
  { type: 'BUY', date: '2026-11-25', account: 'RRSP', ticker: 'FNGU' },
];
const sl1 = ctx.superficialLossCheck('SOXL', '2026-11-09', txs);
ck('TFSA buy 20 days before a non-reg sale is caught', sl1.buys.map(b => b.account), ['TFSA']);
ck('safe to rebuy from day 31', sl1.clearDate, '2026-12-10');
ck('RRSP buy 16 days after is caught', ctx.superficialLossCheck('FNGU', '2026-11-09', txs).buys.length, 1);
ck('other tickers are not', ctx.superficialLossCheck('AVGO', '2026-11-09', txs).buys.length, 0);

console.log('\nXIRR');
ck('one deposit doubling in exactly a year = 100%',
   ctx.xirr([{ date: '2025-01-01', amount: -100 }, { date: '2026-01-01', amount: 200 }]), 1.0, 0.003);
ck('10% a year for two years',
   ctx.xirr([{ date: '2024-01-01', amount: -1000 }, { date: '2026-01-01', amount: 1210 }]), 0.10, 0.001);
const two = ctx.xirr([{ date: '2024-01-01', amount: -1000 }, { date: '2025-01-01', amount: -1000 },
                      { date: '2026-01-01', amount: 2310 }]);
ck('two deposits at 10%: 1000×1.21 + 1000×1.1 = 2310', two, 0.10, 0.001);
ck('no inflow → null', ctx.xirr([{ date: '2025-01-01', amount: 100 }]), null);

console.log('\nMonthly returns (modified Dietz) and risk');
const mv = [{ x: '2026-01', y: 1000 }, { x: '2026-02', y: 1100 }, { x: '2026-03', y: 2200 }];
const c  = [{ x: '2026-01', y: 1000 }, { x: '2026-02', y: 1000 }, { x: '2026-03', y: 2000 }];
const mr2 = ctx.monthlyReturns(mv, c);
ck('Feb +10% with no flow', mr2[0].r, 0.10, 1e-9);
ck('Mar: a $1,000 deposit is not counted as return', mr2[1].r, (2200 - 1100 - 1000) / (1100 + 500), 1e-9);
const series = [0.05, -0.10, 0.02, -0.20, 0.15, 0.04, 0.03].map((r, i) => ({ x: `2026-${String(i + 2).padStart(2, '0')}`, r }));
const bench = { '2026-01': 100 };
let b = 100; series.forEach(p => { b *= 1 + p.r / 2; bench[p.x] = b; });
const rk = ctx.riskStats(series, bench);
ck('max drawdown chains −10%, +2%, −20%', rk.maxDd, 0.9 * 1.02 * 0.8 - 1, 1e-9);
ck('beta = 2 when portfolio moves twice the benchmark', rk.beta, 2, 1e-9);
ck('correlation 1', rk.corr, 1, 1e-9);
ck('S&P time-weighted uses the same months', rk.benchTwrAnn,
   Math.pow(series.reduce((x, p) => x * (1 + p.r / 2), 1), 12 / series.length) - 1, 1e-12);
const yrs = ctx.calendarYearReturns([{ x: '2025-11', r: 0.1 }, { x: '2025-12', r: -0.1 }, { x: '2026-01', r: 0.05 }]);
ck('calendar years chain their own months', yrs.map(y => [y.year, +y.r.toFixed(4), y.partial]),
   [['2025', -0.01, true], ['2026', 0.05, true]]);

console.log('\nDividend forecast');
const monthly = ['2025-12-23', '2026-03-25', '2026-04-24', '2026-05-22', '2026-06-24', '2026-07-27',
                 '2026-08-25', '2026-09-23', '2026-10-26'].map(d => ({ date: d, amount: 0.266 }));
const fwd = ctx.forwardDividends(monthly, '2026-11-22');
ck('monthly payer with feed gaps still projects 12 payments', fwd.length, 12);
const q = ['2025-12-23', '2026-03-24', '2026-06-23', '2026-09-22'].map(d => ({ date: d, amount: 0.31 }));
ck('quarterly payer → 4 payments', ctx.forwardDividends(q, '2026-11-22').length, 4);
ck('no history → nothing', ctx.forwardDividends([], '2026-11-22').length, 0);
ck('US-listed in TFSA keeps 85%', ctx._divNetFactor({ ticker: 'SHEL', ccy: 'USD', account: 'TFSA' }), 0.85);
ck('US-listed in RRSP keeps 100% (treaty)', ctx._divNetFactor({ ticker: 'SPXL', ccy: 'USD', account: 'RRSP' }), 1);
ck('Canadian-listed keeps 100%', ctx._divNetFactor({ ticker: 'RY.TO', ccy: 'CAD', account: 'TFSA' }), 1);

console.log('\nP&L by purchase (average cost)');
const lb = { positions: { 'XYZ|TFSA': { lots: [
  { date: '2025-01-02', bought: 10, shares: 10, price: 100, ccy: 'USD', fx: 1.4, split: 1, cost: 1400, orig_cost: 1400, div: 0 }],
  events: [{ date: '2025-01-02', type: 'BUY', shares: 10, price: 100, ccy: 'USD', fx: 1.4 }] } } };
const lm = ctx.ppLotModel('XYZ', 'TFSA', lb, [
  { type: 'BUY', date: '2026-07-01', account: 'TFSA', ticker: 'XYZ', shares: 10, price: 200, ccy: 'USD', rate: 1.4, commission: 0, id: 'b' },
  { type: 'DIVIDEND', date: '2026-08-01', account: 'TFSA', ticker: 'XYZ', amount: 20, ccy: 'USD', rate: 1.5, id: 'd' },
  { type: 'SELL', date: '2026-09-01', account: 'TFSA', ticker: 'XYZ', shares: 5, price: 300, ccy: 'USD', rate: 1.4, commission: 0, id: 's' },
  { type: 'SPLIT', date: '2026-10-01', account: 'TFSA', ticker: 'XYZ', ratio: 2, id: 'x' },
  { type: 'BUY', date: '2026-10-01', account: 'RRSP', ticker: 'XYZ', shares: 99, price: 1, ccy: 'USD', rate: 1.4, id: 'other' },
]);
ck('two purchases, other account ignored', lm.lots.length, 2);
ck('a sale of 5 of 20 trims each purchase by a quarter, then ×2 split', lm.lots.map(l => +l.shares.toFixed(6)), [15, 15]);
ck('remaining cost = 3/4 of each purchase', lm.lots.map(l => +l.cost.toFixed(2)), [1050, 2100]);
const sale = lm.events.find(e => e.type === 'SELL');
ck('realized = proceeds − average cost (5 × 300 × 1.4 − 5 × 4200/20)', +sale.realized.toFixed(2), 2100 - 1050);
ck('dividend shared by shares held that day', lm.lots.map(l => +l.div.toFixed(2)), [15, 15]);
ck('pre-log history is flagged', lm.hasHistory, true);

console.log(fails.length ? `\n${fails.length} FAILED` : '\nALL PASS');
process.exit(fails.length ? 1 : 0);
