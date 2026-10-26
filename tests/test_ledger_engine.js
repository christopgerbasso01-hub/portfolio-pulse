// Ledger engine — the transaction code in index.html, run for real.
//
// Pulls applyOneTx() and checkLedgerIntegrity() straight out of index.html, so
// these tests exercise exactly what the dashboard runs, not a copy.
//
// Pins the October 2026 bugs:
//   - a logged DIVIDEND updated `dividends` but not `realized`, so Total P&L
//     (unrealized + realized) left out $1,897 of dividends, $1,497 of it TXF.TO;
//   - a SELL that fully closed a USD position stored CAD in total_pnl, which every
//     reader treats as USD and converts again (MU: -514 shown as -716);
//   - re-closing a previously closed position overwrote instead of adding.
//
// Run: node tests/test_ledger_engine.js
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

const ctx = { console };
vm.createContext(ctx);
vm.runInContext(`
  const USD_BOOK_RATE = 1.3925;
  var _LEDGER_SELL_CAD = 0;
  ${extract('applyOneTx')}
  ${extract('checkLedgerIntegrity')}
  this.applyOneTx = applyOneTx;
  this.checkLedgerIntegrity = checkLedgerIntegrity;
  this.sellCAD = () => _LEDGER_SELL_CAD;
  this.resetSell = () => { _LEDGER_SELL_CAD = 0; };
`, ctx);

const fails = [];
function ck(name, got, want, tol) {
  const ok = tol != null ? Math.abs(got - want) <= tol : JSON.stringify(got) === JSON.stringify(want);
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : `\n        got=${JSON.stringify(got)} want=${JSON.stringify(want)}`}`);
  if (!ok) fails.push(name);
}
const clone = o => JSON.parse(JSON.stringify(o));
function run(baseH, baseC, txs) {
  const h = clone(baseH), c = clone(baseC), cash = [], contrib = {};
  ctx.resetSell();
  txs.forEach(t => ctx.applyOneTx(h, c, contrib, cash, t));
  return { h, c, cash, check: ctx.checkLedgerIntegrity(baseH, baseC, h, c, txs, ctx.sellCAD()) };
}

// TXF.TO FHSA as the opening balance has it: realized already includes dividends.
const TXF = { ticker: 'TXF.TO', account: 'FHSA', ccy: 'CAD', shares: 434, cost_total: 11411.13,
              realized: 480.26, dividends: 480.26 };

console.log('── a logged dividend reaches Total P&L ──');
let r = run([TXF], [], [{ type: 'DIVIDEND', ticker: 'TXF.TO', account: 'FHSA', date: '2026-09-30', amount: 124.04, ccy: 'CAD' }]);
ck('dividends column grows', r.h[0].dividends, 604.30, 0.001);
ck('realized grows by the same amount', r.h[0].realized, 604.30, 0.001);
ck('realized still equals dividends for a never-sold holding', r.h[0].realized, r.h[0].dividends, 0.001);
ck('the cash lands in the account', r.cash[0].amount, 124.04, 0.001);
ck('the ledger check is clean', r.check, { problems: [], orphans: [] });

const UDOW = { ticker: 'UDOW', account: 'RRSP', ccy: 'USD', shares: 36, cost_total: 1807.06,
               book_rate: 1.42404, realized: 69.02, dividends: 57.74 };
r = run([UDOW], [], [{ type: 'DIVIDEND', ticker: 'UDOW', account: 'RRSP', date: '2026-09-29', amount: 5.27, ccy: 'USD', rate: 1.4243 }]);
ck('a USD dividend is added to realized in CAD at its own rate', r.h[0].realized, 69.02 + 5.27 * 1.4243, 0.001);

console.log('\n── closing a USD position keeps total_pnl in USD ──');
const MU = { ticker: 'MU', account: 'RRSP', ccy: 'USD', shares: 7, cost_total: 700, book_rate: 1.40,
             realized: 0, dividends: 0 };
r = run([MU], [], [{ type: 'SELL', ticker: 'MU', account: 'RRSP', date: '2026-08-21', shares: 7, price: 50, rate: 1.40, ccy: 'USD' }]);
// proceeds 350 USD * 1.40 = 490 CAD, ACB 980 CAD -> -490 CAD
const mu = r.c[0];
ck('realized is the CAD loss', mu.realized, -490, 0.001);
ck('total_pnl is the USD loss', mu.total_pnl, -350, 0.001);
ck('read the way the dashboard reads it, it is the CAD loss once', mu.total_pnl * mu.book_rate, -490, 0.001);
ck('the ledger check is clean', r.check.problems, []);

console.log('\n── a dividend paid after a full sale still counts ──');
r = run([MU], [], [
  { type: 'SELL', ticker: 'MU', account: 'RRSP', date: '2026-08-21', shares: 7, price: 50, rate: 1.40, ccy: 'USD' },
  { type: 'DIVIDEND', ticker: 'MU', account: 'RRSP', date: '2026-08-28', amount: 1.05, ccy: 'USD', rate: 1.40 },
]);
ck('it is added to the closed position', r.c[0].realized, -490 + 1.47, 0.001);
ck('...in both currencies', r.c[0].total_pnl * r.c[0].book_rate, -490 + 1.47, 0.001);
ck('the ledger check is clean', r.check.problems, []);

console.log('\n── re-closing a position closed before ADDS this round ──');
const OLD = { ticker: 'FNGA', account: 'TFSA', ccy: 'USD', cost_total: 0, book_rate: 1.40, total_pnl: 100, dividends: 0 };
r = run([], [OLD], [
  { type: 'BUY',  ticker: 'FNGA', account: 'TFSA', date: '2026-09-01', shares: 10, price: 10, rate: 1.40, ccy: 'USD' },
  { type: 'SELL', ticker: 'FNGA', account: 'TFSA', date: '2026-09-10', shares: 10, price: 12, rate: 1.40, ccy: 'USD' },
]);
// earlier 100 USD + this round (120-100)=20 USD
ck('total_pnl = earlier result + this round', r.c[0].total_pnl, 120, 0.001);
ck('realized (CAD) matches', r.c[0].realized, 168, 0.001);
ck('the ledger check is clean', r.check.problems, []);

console.log('\n── the self-check catches the old bugs ──');
// Re-create the old DIVIDEND behaviour: dividends bumped, realized not.
const broken = clone([TXF]);
broken[0].dividends += 124.04;
const tx = [{ type: 'DIVIDEND', ticker: 'TXF.TO', account: 'FHSA', date: '2026-09-30', amount: 124.04, ccy: 'CAD' }];
let res = ctx.checkLedgerIntegrity([TXF], [], broken, [], tx, 0);
ck('a dividend missing from realized is flagged', res.problems.length, 1);
ck('...with the exact amount', /124\.04 CAD is unaccounted for/.test(res.problems[0] || ''), true);
// Re-create the old SELL behaviour: CAD stored in total_pnl.
res = ctx.checkLedgerIntegrity([], [], [], [{ ticker: 'MU', account: 'RRSP', ccy: 'USD', book_rate: 1.4,
                                              realized: -490, total_pnl: -490 }], [], -490);
ck('CAD stored in a USD total_pnl is flagged', res.problems.some(p => /MU \(RRSP\) closed P&L/.test(p)), true);
res = ctx.checkLedgerIntegrity([], [], [UDOW], [], [{ type: 'DIVIDEND', ticker: 'BYDDF', account: 'TFSA',
                                                       date: '2026-08-04', amount: 0.13, ccy: 'USD' }], 0);
ck('a dividend booked to an account without the holding is reported', res.orphans.length, 1);

console.log(`\n${fails.length ? fails.length + ' FAILED' : 'ALL PASS'}`);
process.exit(fails.length ? 1 : 0);
