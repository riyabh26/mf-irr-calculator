"""
Flask web app for MF CAS PDF parsing + XIRR dashboard.
"""
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path
from datetime import date

import pandas as pd
import io
from datetime import timedelta
from flask import Flask, request, jsonify, render_template, send_file, session

# Allow importing from the same directory
sys.path.insert(0, str(Path(__file__).parent))
from parse_cas import parse_cas
from xirr_calc import fifo_match, _xirr

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024   # 50 MB (CAS PDFs are rarely >5 MB)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-only-insecure-key')

# ── Benchmark index tickers (via yfinance) ────────────────────────────────────
# Data vintage:
#   BSE-SMLCAP.BO = BSE SmallCap (from Jan 2004)
#   ^NSMIDCP      = Nifty Midcap composite (from Jan 2010)
#   ^CNX100       = Nifty 100 (from Jan 2003)
#   ^BSESN        = BSE Sensex (from 1997)
#   ^GSPC / ^NDX  = S&P 500 / Nasdaq-100

BENCHMARK_TICKERS = {
    'BSE SmallCap':   'BSE-SMLCAP.BO',
    'Nifty Midcap':   '^NSMIDCP',
    'Nifty 100 TRI':  '^CNX100',
    'BSE Sensex TRI': '^BSESN',
    'S&P 500':        '^GSPC',
    'Nasdaq-100':     '^NDX',
}

BENCHMARK_DATA_DIR = Path(__file__).parent / 'benchmark_data'


def _ticker_file(ticker: str) -> Path:
    safe = ticker.replace('^', '').replace('.', '_').replace('/', '_')
    return BENCHMARK_DATA_DIR / f"{safe}.csv"


def _load_offline(ticker: str) -> dict:
    """Load saved price history from CSV. Returns {date: price}."""
    f = _ticker_file(ticker)
    if not f.exists():
        return {}
    try:
        df = pd.read_csv(f, parse_dates=['date'])
        return {row['date'].date(): float(row['close']) for _, row in df.iterrows()}
    except Exception:
        return {}


def _save_offline(ticker: str, prices: dict):
    """Persist price history to CSV, merging with any existing data."""
    BENCHMARK_DATA_DIR.mkdir(exist_ok=True)
    existing = _load_offline(ticker)
    merged = {**existing, **prices}
    rows = sorted(merged.items())
    pd.DataFrame(rows, columns=['date', 'close']).to_csv(_ticker_file(ticker), index=False)


def _fetch_ticker(ticker: str, start_date, end_date) -> dict:
    """Download one ticker from yfinance. Returns {date: price}."""
    try:
        import yfinance as yf
        raw = yf.download(ticker, start=str(start_date),
                          end=str(end_date + timedelta(days=1)),
                          progress=False, auto_adjust=True,
                          multi_level_index=False)
        close = raw['Close'] if 'Close' in raw.columns else raw.iloc[:, 0]
        return {idx.date(): float(v) for idx, v in close.items() if pd.notna(v)}
    except Exception:
        return {}


def _fetch_all_benchmarks(start_date, end_date) -> dict:
    """
    Returns {ticker: {date: price}}.
    Uses offline CSV cache; only fetches from Yahoo Finance for missing date ranges.
    """
    result = {}
    for ticker in set(BENCHMARK_TICKERS.values()):
        offline = _load_offline(ticker)
        latest_cached = max(offline.keys()) if offline else None

        # Need to fetch if we have no data, or data is stale (not current to within 5 days)
        stale_threshold = date.today() - timedelta(days=5)
        if latest_cached is None or latest_cached < stale_threshold:
            fetch_from = (latest_cached + timedelta(days=1)) if latest_cached else start_date
            new_prices = _fetch_ticker(ticker, fetch_from, date.today())
            if new_prices:
                _save_offline(ticker, new_prices)
                offline = {**offline, **new_prices}

        result[ticker] = offline
    return result


def _price_on_date(prices: dict, d) -> float | None:
    """Return closing price on or before d (handles weekends/holidays)."""
    if d in prices:
        return prices[d]
    prior = [k for k in prices if k <= d]
    return prices[max(prior)] if prior else None


def _benchmark_xirr_active(active_cfs: list, benchmark_name: str, index_prices: dict) -> float | None:
    """Benchmark XIRR for active position: replay buys in index, sell at latest price."""
    ticker = BENCHMARK_TICKERS.get(benchmark_name)
    if not ticker:
        return None
    prices = index_prices.get(ticker, {})
    if not prices:
        return None

    buys = sorted([(d, a) for d, a in active_cfs if a < 0], key=lambda x: x[0])
    if not buys:
        return None

    total_units = 0.0
    bench_cfs = []
    for d, amt in buys:
        p = _price_on_date(prices, d)
        if p and p > 0:
            total_units += (-amt) / p
            bench_cfs.append((d, amt))

    if total_units <= 0 or not bench_cfs:
        return None

    latest_date  = max(prices.keys())
    latest_price = prices[latest_date]
    bench_cfs.append((latest_date, round(total_units * latest_price, 2)))

    val = _xirr(bench_cfs)
    return round(val * 100, 2) if val is not None else None


def _benchmark_xirr_exited(exited_cfs: list, benchmark_name: str, index_prices: dict) -> float | None:
    """Benchmark XIRR for exited position: replay buys in index, sell at actual exit date(s).

    For each sell in the fund, we sell the same proportion of accumulated index units
    at the index price on that exit date.
    """
    ticker = BENCHMARK_TICKERS.get(benchmark_name)
    if not ticker:
        return None
    prices = index_prices.get(ticker, {})
    if not prices:
        return None

    buys  = sorted([(d, a) for d, a in exited_cfs if a < 0], key=lambda x: x[0])
    sells = sorted([(d, a) for d, a in exited_cfs if a > 0], key=lambda x: x[0])
    if not buys or not sells:
        return None

    # Accumulate index units from purchases
    total_units = 0.0
    bench_cfs = []
    for d, amt in buys:
        p = _price_on_date(prices, d)
        if p and p > 0:
            total_units += (-amt) / p
            bench_cfs.append((d, amt))  # same outflow

    if total_units <= 0 or not bench_cfs:
        return None

    # Total actual redemption proceeds — used to compute proportional exits
    total_proceeds = sum(a for _, a in sells)

    # Sell proportional index units on each actual exit date
    remaining_units = total_units
    for i, (d, proceeds) in enumerate(sells):
        p = _price_on_date(prices, d)
        if not p or p <= 0:
            continue
        if i == len(sells) - 1:
            # Last exit: sell all remaining units
            units_to_sell = remaining_units
        else:
            fraction = proceeds / total_proceeds
            units_to_sell = total_units * fraction
        bench_cfs.append((d, round(units_to_sell * p, 2)))
        remaining_units -= units_to_sell

    val = _xirr(bench_cfs)
    return round(val * 100, 2) if val is not None else None


# Per-user cashflow cache: { session_id -> { (isin, folio) -> {...} } }
# Keyed by a UUID stored in the browser session cookie — isolates concurrent users.
import uuid
from datetime import datetime
_user_cfs: dict = {}          # session_id -> cfs dict
_user_ts:  dict = {}          # session_id -> last-access datetime (for TTL cleanup)
_SESSION_TTL_HOURS = 4

def _get_session_id() -> str:
    if 'sid' not in session:
        session['sid'] = uuid.uuid4().hex
    return session['sid']

def _get_cached_cfs() -> dict:
    sid = _get_session_id()
    _user_ts[sid] = datetime.utcnow()
    return _user_cfs.setdefault(sid, {})

def _clear_cached_cfs():
    from datetime import datetime, timedelta
    sid = _get_session_id()
    _user_cfs[sid] = {}
    _user_ts[sid] = datetime.utcnow()
    # Evict sessions idle longer than TTL
    cutoff = datetime.utcnow() - timedelta(hours=_SESSION_TTL_HOURS)
    stale = [k for k, t in _user_ts.items() if t < cutoff]
    for k in stale:
        _user_cfs.pop(k, None)
        _user_ts.pop(k, None)


ISIN_OVERRIDES = {
    'Kotak Money Market Scheme - Direct Plan - Daily - IDCW': 'INF174KA1FH5',
}

# ── Fund classification ───────────────────────────────────────────────────────

def classify_fund(fund_name: str) -> str:
    n = fund_name.lower()
    if any(k in n for k in ('liquid fund', 'liquid plan', 'money market', 'cash management', 'overnight')):
        return 'Liquid'
    if any(k in n for k in ('equity savings', 'arbitrage')):
        return 'Equity Oriented'
    if any(k in n for k in ('multi asset', 'balanced advantage', 'balanced fund')):
        return 'Hybrid'
    if any(k in n for k in ('ultra short bond', 'treasury advantage', 'savings fund',
                             'flexible income', 'short term', 'gilt', 'bond fund',
                             'income fund', 'debt fund', 'credit risk')):
        return 'Debt'
    # Gold ETFs / FoFs
    if any(k in n for k in ('gold', 'silver')):
        return 'Gold'
    # US / international / global funds → always Equity
    if any(k in n for k in ('u.s.', ' us ', 'nasdaq', 'nyse', 'fang', 'global', 'international',
                             'overseas', 'world', 'opportunities fund of fund')):
        return 'Equity'
    return 'Equity'


# ── Benchmark mapping (Equity only) ──────────────────────────────────────────

def benchmark_for_fund(fund_name: str, asset_class: str) -> str | None:
    """Return the comparable index benchmark for equity funds only."""
    if asset_class != 'Equity':
        return None
    n = fund_name.lower()
    # US / international funds
    if any(k in n for k in ('u.s.', ' us ', 'nasdaq', 'nyse', 'fang', 'global',
                             'international', 'overseas', 'world', 'opportunities fund of fund')):
        return 'Nasdaq-100' if 'nasdaq' in n else 'S&P 500'
    # Small cap
    if any(k in n for k in ('small cap', 'smallcap', 'small-cap')):
        return 'BSE SmallCap'
    # Large & mid cap → Sensex (broader than pure large cap)
    if any(k in n for k in ('large & mid', 'large and mid', 'large midcap', 'large & midcap')):
        return 'BSE Sensex TRI'
    # Pure mid cap
    if any(k in n for k in ('mid cap', 'midcap', 'mid-cap')):
        return 'Nifty Midcap'
    # Large cap
    if any(k in n for k in ('large cap', 'largecap', 'large-cap')):
        return 'Nifty 100 TRI'
    # Default: flexi cap, contra, value, multi cap, sectoral, index funds
    return 'BSE Sensex TRI'


# ── Core processing ───────────────────────────────────────────────────────────

def process_pdf(pdf_path: str, pan: str) -> dict:
    # Clear stale cashflow cache from any previous upload
    _clear_cached_cfs()

    # 1. Parse PDF
    df, df_chg, df_close, investor_name = parse_cas(pdf_path, password=pan)

    # Apply ISIN overrides
    for name_frag, isin in ISIN_OVERRIDES.items():
        for frame in (df, df_chg, df_close):
            mask = (frame['fund_name'].str.contains(name_frag, case=False, na=False)
                    & frame['isin'].isna())
            frame.loc[mask, 'isin'] = isin

    # 2. Ensure date types
    df['date']           = pd.to_datetime(df['date']).dt.date
    df_close['nav_date'] = pd.to_datetime(df_close['nav_date']).dt.date
    df_chg['date']       = pd.to_datetime(df_chg['date']).dt.date

    # 3. Closing lookup
    closing_lookup = {}
    for _, row in df_close.iterrows():
        key = (row['isin'], row['folio'])
        existing = closing_lookup.get(key)
        if existing is None or row['market_value'] > existing.get('market_value', 0):
            closing_lookup[key] = row.to_dict()

    # 4. Charges lookup
    charges_lookup = {}
    for _, row in df_chg.iterrows():
        key = (row['isin'], row['folio'])
        charges_lookup.setdefault(key, []).append(row.to_dict())

    # 5. Per-(isin, folio) XIRR
    fund_results = []

    # Aggregated CFs for portfolio-level XIRR
    agg = {
        'active': [], 'exited': [],
        'eq_active': [], 'eq_exited': [],
    }

    for (isin, folio), grp in df.groupby(['isin', 'folio'], sort=False):
        grp    = grp.sort_values('date')
        meta   = grp.iloc[0]
        closing = closing_lookup.get((isin, folio))

        active_cfs, exited_cfs, _, _ = fifo_match(grp, closing)

        # If closing balance shows 0 units but FIFO left residual lots,
        # force those residuals to exited with £0 proceeds.
        # This handles Franklin segregated transfers and missed switch-outs.
        if closing is not None and closing.get('closing_units', 1) < 0.001:
            exited_cfs.extend(active_cfs)  # move residual buy CFs to exited
            active_cfs = []                # no active position remains

        closing_nav  = closing['closing_nav']   if closing else None
        nav_date     = closing['nav_date']      if closing else None
        active_units = closing['closing_units'] if closing else None
        market_value = closing['market_value']  if closing else None

        active_invested = -sum(a for _, a in active_cfs if a < 0)
        exited_invested = -sum(a for _, a in exited_cfs if a < 0)
        total_redeemed  =  sum(a for _, a in exited_cfs if a > 0)

        all_cfs       = exited_cfs + active_cfs
        overall_value = (market_value or 0) + total_redeemed

        def pct(x):
            return round(x * 100, 2) if x is not None else None

        portions = []

        if active_invested > 0:
            portions.append({
                'portion': 'Active',
                'invested': round(active_invested, 2),
                'current_value': round(market_value, 2) if market_value else None,
                'xirr': pct(_xirr(active_cfs)),
                'units': round(active_units, 4) if active_units else None,
                'nav':   round(closing_nav, 4)  if closing_nav else None,
                'nav_date': nav_date.isoformat() if isinstance(nav_date, date) else str(nav_date) if nav_date else None,
            })

        if exited_invested > 0:
            portions.append({
                'portion': 'Exited',
                'invested': round(exited_invested, 2),
                'current_value': round(total_redeemed, 2),
                'xirr': pct(_xirr(exited_cfs)),
                'units': None, 'nav': None, 'nav_date': None,
            })

        portions.append({
            'portion': 'Overall',
            'invested': round(active_invested + exited_invested, 2),
            'current_value': round(overall_value, 2),
            'xirr': pct(_xirr(all_cfs)),
            'units': round(active_units, 4) if active_units else None,
            'nav':   round(closing_nav, 4)  if closing_nav else None,
            'nav_date': nav_date.isoformat() if isinstance(nav_date, date) else str(nav_date) if nav_date else None,
        })

        ac      = classify_fund(meta['fund_name'])
        bm_name = benchmark_for_fund(meta['fund_name'], ac)

        # Cache cashflows for on-demand benchmark computation
        _get_cached_cfs()[(str(isin), str(folio))] = {
            'active_cfs':  active_cfs,
            'exited_cfs':  exited_cfs,
            'benchmark':   bm_name,
        }

        agg['active'].extend(active_cfs)
        agg['exited'].extend(exited_cfs)
        if ac == 'Equity':
            agg['eq_active'].extend(active_cfs)
            agg['eq_exited'].extend(exited_cfs)

        fund_results.append({
            'amc':        meta['amc'] if pd.notna(meta['amc']) else '',
            'fund_name':  meta['fund_name'],
            'isin':       isin if pd.notna(isin) else '',
            'folio':      folio,
            'asset_class': ac,
            'benchmark':  bm_name,
            'portions':   portions,
        })

    # 6. Summary aggregation — Active and Exited separately (no double-counting)
    asset_class_order = ['Equity', 'Equity Oriented', 'Hybrid', 'Debt', 'Liquid', 'Gold']
    ac_totals = {ac: {
        'active_invested': 0.0, 'active_value': 0.0,
        'exited_invested': 0.0, 'exited_value': 0.0,
    } for ac in asset_class_order}

    total_active_invested = 0.0
    total_active_value    = 0.0
    total_exited_invested = 0.0
    total_exited_value    = 0.0

    for fund in fund_results:
        ac = fund['asset_class']
        for p in fund['portions']:
            if p['portion'] == 'Active':
                v = p['current_value'] or 0
                ac_totals[ac]['active_invested'] += p['invested']
                ac_totals[ac]['active_value']    += v
                total_active_invested            += p['invested']
                total_active_value               += v
            elif p['portion'] == 'Exited':
                v = p['current_value'] or 0
                ac_totals[ac]['exited_invested'] += p['invested']
                ac_totals[ac]['exited_value']    += v
                total_exited_invested            += p['invested']
                total_exited_value               += v

    def pct(x): return round(x * 100, 2) if x is not None else None

    asset_classes = []
    for ac in asset_class_order:
        t = ac_totals[ac]
        if t['active_invested'] > 0 or t['exited_invested'] > 0:
            asset_classes.append({
                'name': ac,
                'active_invested':  round(t['active_invested'], 2),
                'active_value':     round(t['active_value'], 2),
                'exited_invested':  round(t['exited_invested'], 2),
                'exited_value':     round(t['exited_value'], 2),
            })

    summary = {
        'active_invested':  round(total_active_invested, 2),
        'active_value':     round(total_active_value, 2),
        'active_gain':      round(total_active_value - total_active_invested, 2),
        'active_gain_pct':  round((total_active_value / total_active_invested - 1) * 100, 2) if total_active_invested > 0 else None,
        'active_xirr':      pct(_xirr(agg['active'])),
        'exited_invested':  round(total_exited_invested, 2),
        'exited_value':     round(total_exited_value, 2),
        'exited_gain':      round(total_exited_value - total_exited_invested, 2),
        'exited_gain_pct':  round((total_exited_value / total_exited_invested - 1) * 100, 2) if total_exited_invested > 0 else None,
        'exited_xirr':      pct(_xirr(agg['exited'])),
        # Equity spotlight
        'eq_active_invested': round(-sum(a for _, a in agg['eq_active'] if a < 0), 2),
        'eq_active_value':    round(sum(a for _, a in agg['eq_active'] if a > 0), 2),
        'eq_active_xirr':     pct(_xirr(agg['eq_active'])),
        'eq_exited_invested': round(-sum(a for _, a in agg['eq_exited'] if a < 0), 2),
        'eq_exited_value':    round(sum(a for _, a in agg['eq_exited'] if a > 0), 2),
        'eq_exited_xirr':     pct(_xirr(agg['eq_exited'])),
    }

    return {
        'investor_name': investor_name,
        'summary': summary,
        'asset_classes': asset_classes,
        'funds': fund_results,
    }


# ── Excel fast-path ───────────────────────────────────────────────────────────

def _load_from_portfolio_excel(xl: pd.ExcelFile) -> dict:
    """Read a Portfolio XIRR Excel (downloaded from the dashboard) and rebuild the JSON structure."""
    df = xl.parse('Portfolio XIRR')
    df.columns = df.columns.str.strip()

    pct = lambda x: round(float(x), 1) if pd.notna(x) else None

    asset_class_order = ['Equity', 'Equity Oriented', 'Hybrid', 'Debt', 'Liquid', 'Gold']
    ac_totals = {ac: {'active_invested': 0.0, 'active_value': 0.0,
                      'exited_invested': 0.0, 'exited_value': 0.0} for ac in asset_class_order}
    agg_active_inv = agg_active_val = 0.0
    agg_exited_inv = agg_exited_val = 0.0
    eq_active_inv  = eq_active_val  = 0.0
    eq_exited_inv  = eq_exited_val  = 0.0

    fund_results = []
    for (isin, folio), grp in df.groupby(['ISIN', 'Folio'], sort=False):
        meta = grp.iloc[0]
        amc       = str(meta.get('AMC', '') or '')
        fund_name = str(meta.get('Fund Name', '') or '')
        ac        = classify_fund(fund_name)

        portions = []
        for _, row in grp.iterrows():
            portion  = str(row.get('Status', ''))
            invested = float(row.get('Invested (₹)', 0) or 0)
            cur_val  = float(row.get('Value (₹)', 0) or 0)
            xirr_val = pct(row.get('XIRR %'))
            units    = row.get('Units')
            nav      = row.get('NAV (₹)')
            nav_date = row.get('NAV Date')

            units    = int(round(float(units))) if pd.notna(units) else None
            nav      = round(float(nav), 2)     if pd.notna(nav)   else None
            nav_date = nav_date.date().isoformat() if hasattr(nav_date, 'date') else (str(nav_date) if pd.notna(nav_date) else None)

            portions.append({
                'portion': portion, 'invested': round(invested, 2),
                'current_value': round(cur_val, 2), 'xirr': xirr_val,
                'units': units, 'nav': nav, 'nav_date': nav_date,
            })

            if portion == 'Active':
                ac_totals[ac]['active_invested'] += invested
                ac_totals[ac]['active_value']    += cur_val
                agg_active_inv += invested; agg_active_val += cur_val
                if ac == 'Equity':
                    eq_active_inv += invested; eq_active_val += cur_val
            elif portion == 'Exited':
                ac_totals[ac]['exited_invested'] += invested
                ac_totals[ac]['exited_value']    += cur_val
                agg_exited_inv += invested; agg_exited_val += cur_val
                if ac == 'Equity':
                    eq_exited_inv += invested; eq_exited_val += cur_val

        fund_results.append({
            'amc': amc, 'fund_name': fund_name,
            'isin': str(isin) if pd.notna(isin) else '',
            'folio': str(folio), 'asset_class': ac,
            'benchmark': benchmark_for_fund(fund_name, ac),
            'portions': portions,
        })

    def _gain_pct(inv, val):
        return round((val / inv - 1) * 100, 1) if inv > 0 else None

    # Weighted-average XIRR from per-fund Active portions
    def _wavg(portions_xirr_inv):
        vals = [(x, i) for x, i in portions_xirr_inv if x is not None and i > 0]
        if not vals: return None
        return round(sum(x * i for x, i in vals) / sum(i for _, i in vals), 1)

    active_xirr_data = [
        (p['xirr'], p['invested'])
        for f in fund_results for p in f['portions'] if p['portion'] == 'Active'
    ]
    eq_active_xirr_data = [
        (p['xirr'], p['invested'])
        for f in fund_results if f['asset_class'] == 'Equity'
        for p in f['portions'] if p['portion'] == 'Active'
    ]

    asset_classes = []
    for ac in asset_class_order:
        t = ac_totals[ac]
        if t['active_invested'] > 0 or t['exited_invested'] > 0:
            asset_classes.append({
                'name':            ac,
                'active_invested': round(t['active_invested'], 2),
                'active_value':    round(t['active_value'], 2),
                'exited_invested': round(t['exited_invested'], 2),
                'exited_value':    round(t['exited_value'], 2),
            })

    summary = {
        'active_invested':    round(agg_active_inv, 2),
        'active_value':       round(agg_active_val, 2),
        'active_gain':        round(agg_active_val - agg_active_inv, 2),
        'active_gain_pct':    _gain_pct(agg_active_inv, agg_active_val),
        'active_xirr':        _wavg(active_xirr_data),
        'exited_invested':    round(agg_exited_inv, 2),
        'exited_value':       round(agg_exited_val, 2),
        'exited_gain':        round(agg_exited_val - agg_exited_inv, 2),
        'exited_gain_pct':    _gain_pct(agg_exited_inv, agg_exited_val),
        'exited_xirr':        None,
        'eq_active_invested': round(eq_active_inv, 2),
        'eq_active_value':    round(eq_active_val, 2),
        'eq_active_xirr':     _wavg(eq_active_xirr_data),
        'eq_exited_invested': round(eq_exited_inv, 2),
        'eq_exited_value':    round(eq_exited_val, 2),
        'eq_exited_xirr':     None,
    }

    return {
        'investor_name': 'Portfolio',
        'summary': summary,
        'asset_classes': asset_classes,
        'funds': fund_results,
    }


def load_from_xirr_excel(path: str) -> dict:
    """
    Read a pre-computed *_XIRR.xlsx (output of xirr_calc.py) and build the
    same JSON structure that process_pdf() returns — but in under a second.
    """
    xl = pd.ExcelFile(path)
    sheets = xl.sheet_names

    if 'Portfolio XIRR' in sheets:
        return _load_from_portfolio_excel(xl)

    if 'Gross XIRR' not in sheets:
        raise ValueError(
            f"Unrecognised Excel file. Expected either a downloaded Portfolio XIRR file "
            f"or a *_XIRR.xlsx file from xirr_calc.py, but found sheets: {sheets}."
        )
    df_gross = xl.parse('Gross XIRR')

    # Normalise column names (strip whitespace)
    df_gross.columns = df_gross.columns.str.strip()

    pct = lambda x: round(float(x), 1) if pd.notna(x) else None

    asset_class_order = ['Equity', 'Equity Oriented', 'Hybrid', 'Debt', 'Liquid', 'Gold']
    ac_totals = {ac: {
        'active_invested': 0.0, 'active_value': 0.0,
        'exited_invested': 0.0, 'exited_value': 0.0,
    } for ac in asset_class_order}

    agg_active_inv = agg_active_val = 0.0
    agg_exited_inv = agg_exited_val = 0.0
    eq_active_inv  = eq_active_val  = 0.0
    eq_exited_inv  = eq_exited_val  = 0.0

    # XIRR values weighted-sum approximation from per-fund rows
    # (true portfolio-level XIRR needs cash flows; use weighted avg as proxy)
    fund_results = []

    groups = df_gross.groupby(['isin', 'folio'], sort=False)
    for (isin, folio), grp in groups:
        meta = grp.iloc[0]
        amc       = str(meta.get('amc', '') or '')
        fund_name = str(meta.get('fund_name', '') or '')
        ac        = classify_fund(fund_name)

        portions = []
        for _, row in grp.iterrows():
            portion   = str(row.get('portion', ''))
            invested  = float(row.get('amount_invested', 0) or 0)
            cur_val   = float(row.get('current_value',  0) or 0)
            xirr_val  = pct(row.get('xirr'))
            units     = row.get('active_units')
            nav       = row.get('closing_nav')
            nav_date  = row.get('nav_date')

            units    = int(round(float(units))) if pd.notna(units) else None
            nav      = round(float(nav), 2)     if pd.notna(nav)   else None
            nav_date = nav_date.date().isoformat() if hasattr(nav_date, 'date') else (str(nav_date) if pd.notna(nav_date) else None)

            portions.append({
                'portion':       portion,
                'invested':      round(invested, 2),
                'current_value': round(cur_val, 2),
                'xirr':          xirr_val,
                'units':         units,
                'nav':           nav,
                'nav_date':      nav_date,
            })

            if portion == 'Active':
                ac_totals[ac]['active_invested'] += invested
                ac_totals[ac]['active_value']    += cur_val
                agg_active_inv += invested
                agg_active_val += cur_val
                if ac == 'Equity':
                    eq_active_inv += invested
                    eq_active_val += cur_val
            elif portion == 'Exited':
                ac_totals[ac]['exited_invested'] += invested
                ac_totals[ac]['exited_value']    += cur_val
                agg_exited_inv += invested
                agg_exited_val += cur_val
                if ac == 'Equity':
                    eq_exited_inv += invested
                    eq_exited_val += cur_val

        fund_results.append({
            'amc':        amc,
            'fund_name':  fund_name,
            'isin':       str(isin) if pd.notna(isin) else '',
            'folio':      str(folio),
            'asset_class': ac,
            'benchmark':  benchmark_for_fund(fund_name, ac),
            'portions':   portions,
        })

    asset_classes = []
    for ac in asset_class_order:
        t = ac_totals[ac]
        if t['active_invested'] > 0 or t['exited_invested'] > 0:
            asset_classes.append({
                'name':             ac,
                'active_invested':  round(t['active_invested'], 2),
                'active_value':     round(t['active_value'], 2),
                'exited_invested':  round(t['exited_invested'], 2),
                'exited_value':     round(t['exited_value'], 2),
            })

    def _gain_pct(inv, val):
        return round((val / inv - 1) * 100, 1) if inv > 0 else None

    # Overall XIRR from the Excel Overall rows (weighted by invested amount)
    overall_rows = df_gross[df_gross['portion'] == 'Overall']
    def _wavg_xirr(mask):
        sub = overall_rows[mask & overall_rows['xirr'].notna()]
        if sub.empty: return None
        w = sub['amount_invested'].fillna(0)
        if w.sum() < 1: return None
        return round((sub['xirr'] * w).sum() / w.sum(), 1)

    eq_mask = overall_rows['fund_name'].apply(lambda n: classify_fund(str(n)) == 'Equity')

    summary = {
        'active_invested':    round(agg_active_inv, 2),
        'active_value':       round(agg_active_val, 2),
        'active_gain':        round(agg_active_val - agg_active_inv, 2),
        'active_gain_pct':    _gain_pct(agg_active_inv, agg_active_val),
        'active_xirr':        _wavg_xirr(overall_rows['fund_name'].apply(lambda _: True)),
        'exited_invested':    round(agg_exited_inv, 2),
        'exited_value':       round(agg_exited_val, 2),
        'exited_gain':        round(agg_exited_val - agg_exited_inv, 2),
        'exited_gain_pct':    _gain_pct(agg_exited_inv, agg_exited_val),
        'exited_xirr':        _wavg_xirr(overall_rows['fund_name'].apply(lambda _: True)),
        'eq_active_invested': round(eq_active_inv, 2),
        'eq_active_value':    round(eq_active_val, 2),
        'eq_active_xirr':     _wavg_xirr(eq_mask),
        'eq_exited_invested': round(eq_exited_inv, 2),
        'eq_exited_value':    round(eq_exited_val, 2),
        'eq_exited_xirr':     _wavg_xirr(eq_mask),
    }

    return {
        'investor_name': None,  # not in Excel; could add a sheet later
        'summary': summary,
        'asset_classes': asset_classes,
        'funds': fund_results,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/process', methods=['POST'])
def process():
    uploaded = request.files.get('file')
    if not uploaded:
        return jsonify({'error': 'No file uploaded'}), 400

    fname  = uploaded.filename or ''
    suffix = Path(fname).suffix.lower()

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        uploaded.save(tmp.name)
        tmp_path = tmp.name

    try:
        if suffix == '.xlsx':
            result = load_from_xirr_excel(tmp_path)
        elif suffix == '.pdf':
            pan = (request.form.get('pan') or '').strip()
            if not pan:
                return jsonify({'error': 'PAN password is required for PDF upload'}), 400
            result = process_pdf(tmp_path, pan)
        else:
            return jsonify({'error': 'Please upload a PDF (CAS statement) or XLSX (pre-computed XIRR file)'}), 400

        return jsonify(result)
    except Exception as e:
        tb = traceback.format_exc()
        return jsonify({'error': str(e), 'detail': tb}), 500
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@app.route('/download', methods=['POST'])
def download():
    try:
        data = request.get_json()
        funds = data.get('funds', [])
        investor = data.get('investor_name') or 'Portfolio'

        rows = []
        for fund in funds:
            for p in fund.get('portions', []):
                rows.append({
                    'AMC':             fund['amc'],
                    'Fund Name':       fund['fund_name'],
                    'ISIN':            fund['isin'],
                    'Folio':           fund.get('folio', ''),
                    'Asset Class':     fund['asset_class'],
                    'Status':          p['portion'],
                    'Invested (₹)':    p['invested'],
                    'Value (₹)':       p['current_value'],
                    'Gain (₹)':        round(p['current_value'] - p['invested'], 2) if p['current_value'] is not None else None,
                    'Gain %':          round((p['current_value'] / p['invested'] - 1) * 100, 1) if p['invested'] and p['current_value'] else None,
                    'XIRR %':          p['xirr'],
                    'Units':           round(p['units']) if p['units'] is not None else None,
                    'NAV (₹)':         p['nav'],
                    'NAV Date':        p['nav_date'],
                })

        df = pd.DataFrame(rows)

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl', date_format='DD-MMM-YYYY') as writer:
            df.to_excel(writer, sheet_name='Portfolio XIRR', index=False)

            # Auto-width columns
            ws = writer.sheets['Portfolio XIRR']
            for col in ws.columns:
                max_len = max((len(str(c.value)) for c in col if c.value), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)

        buf.seek(0)
        filename = f"{investor.replace(' ', '_')}_Portfolio.xlsx"
        return send_file(buf, as_attachment=True, download_name=filename,
                         mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/benchmark', methods=['POST'])
def compute_benchmark():
    """Fetch index prices and compute benchmark XIRR for all cached equity funds."""
    cached_cfs = _get_cached_cfs()
    if not cached_cfs:
        return jsonify({'error': 'No processed data in memory. Please upload a file first.'}), 400

    all_dates = [
        d for entry in cached_cfs.values()
        for cfs in (entry['active_cfs'], entry.get('exited_cfs', []))
        for d, _ in cfs if d
    ]
    if not all_dates:
        return jsonify({'funds': []})

    min_date = min(all_dates)
    index_prices = _fetch_all_benchmarks(min_date, date.today())

    results = []
    for (isin, folio), entry in cached_cfs.items():
        bm_name     = entry.get('benchmark')
        if not bm_name:
            continue
        active_cfs  = entry.get('active_cfs', [])
        exited_cfs  = entry.get('exited_cfs', [])
        all_cfs     = exited_cfs + active_cfs

        active_bm  = _benchmark_xirr_active(active_cfs, bm_name, index_prices)
        exited_bm  = _benchmark_xirr_exited(exited_cfs, bm_name, index_prices)
        overall_bm = _benchmark_xirr_active(all_cfs,    bm_name, index_prices) if active_cfs else exited_bm

        results.append({
            'isin': isin, 'folio': str(folio),
            'active_benchmark_xirr':  active_bm,
            'exited_benchmark_xirr':  exited_bm,
            'overall_benchmark_xirr': overall_bm,
        })

    return jsonify({'funds': results})


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
