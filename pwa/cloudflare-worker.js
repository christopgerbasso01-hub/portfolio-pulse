/**
 * Portfolio Pulse — Cloudflare Worker
 * Proxies Yahoo Finance API with proper headers, adds CORS so the PWA can call it freely.
 *
 * Deploy at: https://dash.cloudflare.com → Workers & Pages → Create Worker → paste this code → Deploy
 * Your URL will be: https://portfolio-prices.<your-username>.workers.dev
 *
 * Usage: https://portfolio-prices.<you>.workers.dev/?s=AAPL,TSLA,NVDA,USDCAD=X
 */

export default {
  async fetch(request) {
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': '*',
    };

    // Handle preflight
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders });
    }

    const url = new URL(request.url);
    const symbols = url.searchParams.get('s') || '';

    if (!symbols) {
      return new Response(JSON.stringify({ error: 'No symbols provided' }), {
        status: 400,
        headers: { 'Content-Type': 'application/json', ...corsHeaders },
      });
    }

    const upstream = `https://query1.finance.yahoo.com/v7/finance/quote?symbols=${encodeURIComponent(symbols)}&fields=regularMarketPrice,regularMarketPreviousClose,currency`;

    try {
      const res = await fetch(upstream, {
        headers: {
          'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
          'Accept': 'application/json, text/plain, */*',
          'Accept-Language': 'en-US,en;q=0.9',
          'Referer': 'https://finance.yahoo.com/',
          'Origin': 'https://finance.yahoo.com',
        },
      });

      const text = await res.text();

      return new Response(text, {
        status: res.status,
        headers: {
          'Content-Type': 'application/json',
          'Cache-Control': 'max-age=30',   // Cache for 30s at Cloudflare edge
          ...corsHeaders,
        },
      });
    } catch (e) {
      return new Response(JSON.stringify({ error: e.message }), {
        status: 500,
        headers: { 'Content-Type': 'application/json', ...corsHeaders },
      });
    }
  },
};
