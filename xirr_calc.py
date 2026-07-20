"""
Gross XIRR calculator for Mutual Fund transactions.

Computes per-fund XIRR using FIFO lot matching.
Produces two output sheets:
  - Fund XIRR Summary  : active / exited / overall XIRR per fund+folio
  - Exited by Quarter  : exited XIRR grouped by investment quarter (Indian FY)
"""

import pandas as pd
import numpy as np
from datetime import date
from pathlib import Path

_DIR = Path(__file__).parent

def _find_transactions_file() -> Path:
    """Pick the most recently modified *_Transactions.xlsx in the project folder."""
    candidates = sorted(_DIR.glob('*_Transactions.xlsx'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError("No *_Transactions.xlsx found. Run parse_cas.py first.")
    return candidates[0]


# ── XIRR (Newton-Raphson) ────────────────────────────────────────────────────

def _xirr(cashflows: list[tuple]) -> float | None:
    """
    cashflows: [(date, amount), ...]
    Negative amount = outflow (investment), positive = inflow (redemption/value).
    Returns annualised rate as decimal (e.g. 0.15 = 15 %), or None if unsolvable.
    """
    if len(cashflows) < 2:
        return None
    amounts = [a for _, a in cashflows]
    if all(a <= 0 for a in amounts) or all(a >= 0 for a in amounts):
        return None  # need both in and out

    t0 = min(d for d, _ in cashflows)

    def t(d):
        return (d - t0).days / 365.25

    def npv(r):
        return sum(a / (1 + r) ** t(d) for d, a in cashflows)

    def dnpv(r):
        return sum(-t(d) * a / (1 + r) ** (t(d) + 1) for d, a in cashflows)

    r = 0.15  # initial guess
    for _ in range(300):
        fn  = npv(r)
        dfn = dnpv(r)
        if abs(dfn) < 1e-14:
            break
        step = fn / dfn
        r -= step
        if r <= -0.9999:
            r = -0.5
        if abs(step) < 1e-10:
            break

    return round(r, 6) if abs(npv(r)) < 1.0 else None


# ── Indian financial-year quarter helper ─────────────────────────────────────

def to_fy_quarter(d: date) -> str:
    """Convert a date to 'Q1 FY2025' (Apr–Jun = Q1 of that FY)."""
    m, y = d.month, d.year
    if m < 4:      # Jan–Mar  → Q4 of previous FY
        return f"Q4 FY{y}"
    elif m < 7:    # Apr–Jun  → Q1
        return f"Q1 FY{y + 1}"
    elif m < 10:   # Jul–Sep  → Q2
        return f"Q2 FY{y + 1}"
    else:          # Oct–Dec  → Q3
        return f"Q3 FY{y + 1}"


# ── Transaction classifier ───────────────────────────────────────────────────

def _action(row) -> str:
    """Return 'buy', 'sell', or 'skip'."""
    if row['status'] in ('INFO', 'FAILED'):
        return 'skip'
    u = row['units']
    if pd.isna(u) or abs(u) < 1e-6:
        return 'skip'   # TDS, notifications, etc.
    return 'buy' if u > 0 else 'sell'


# ── FIFO matching ────────────────────────────────────────────────────────────

def fifo_match(txns: pd.DataFrame, closing: dict | None):
    """
    txns    : all transactions for one (isin, folio), sorted by date ascending
    closing : dict from Closing Values sheet (closing_units, closing_nav,
              nav_date, market_value) or None

    Returns
    -------
    active_cfs  : [(date, amount)] — active purchase lots + terminal market value
    exited_cfs  : [(date, amount)] — exited purchase lots + their redemption proceeds
    exit_records: list of dicts for quarterly breakdown
    portfolio_status : 'Active', 'Exited', 'Partial'
    """
    all_lots = []   # {purchase_date, remaining, apu, exits:[{exit_date,units,exit_nav}]}

    for _, row in txns.iterrows():
        act = _action(row)
        if act == 'skip':
            continue

        if act == 'buy':
            u = row['units']
            apu = abs(row['amount']) / u if u > 1e-6 else abs(row['nav'])
            all_lots.append({
                'purchase_date': row['date'],
                'original_units': u,
                'remaining': u,
                'apu': apu,
                'exits': [],
            })

        else:  # sell
            units_left = abs(row['units'])
            edate = row['date']
            enav  = abs(row['amount']) / units_left if units_left > 1e-6 else abs(row['nav'])

            for lot in all_lots:
                if lot['remaining'] < 1e-6 or units_left < 1e-6:
                    continue
                take = min(lot['remaining'], units_left)
                lot['exits'].append({'exit_date': edate, 'units': take, 'exit_nav': enav})
                lot['remaining'] -= take
                units_left      -= take

    # ── Build cash-flow lists ────────────────────────────────────────────────
    active_cfs   = []
    exited_cfs   = []
    exit_records = []

    total_orig  = sum(lot['original_units'] for lot in all_lots)
    total_rem   = sum(lot['remaining']      for lot in all_lots)

    if   total_orig < 1e-6:
        portfolio_status = 'Exited'
    elif total_rem   < 1e-6:
        portfolio_status = 'Exited'
    elif total_rem >= total_orig - 1e-6:
        portfolio_status = 'Active'
    else:
        portfolio_status = 'Partial'

    for lot in all_lots:
        apu = lot['apu']
        pd_  = lot['purchase_date']

        # ── Exited portion of this lot ───────────────────────────────────────
        exited_u = lot['original_units'] - lot['remaining']
        if exited_u > 1e-6:
            # One purchase CF per exit event (proportional to units in that exit)
            for ex in lot['exits']:
                cost     = ex['units'] * apu
                proceeds = ex['units'] * ex['exit_nav']
                exited_cfs.append((pd_,            -cost))
                exited_cfs.append((ex['exit_date'],  proceeds))
                exit_records.append({
                    'purchase_date'    : pd_,
                    'investment_quarter': to_fy_quarter(pd_),
                    'exit_date'        : ex['exit_date'],
                    'units'            : ex['units'],
                    'purchase_nav'     : apu,
                    'exit_nav'         : ex['exit_nav'],
                    'cost'             : cost,
                    'proceeds'         : proceeds,
                })

        # ── Active (remaining) portion of this lot ───────────────────────────
        if lot['remaining'] > 1e-6:
            active_cfs.append((pd_, -lot['remaining'] * apu))

    # Terminal value for active portion
    if active_cfs and closing is not None:
        mv  = closing.get('market_value') or 0
        nav = closing.get('closing_nav')  or 0
        cu  = closing.get('closing_units') or 0
        if mv < 1e-2:
            mv = cu * nav
        if mv > 1e-2:
            active_cfs.append((closing['nav_date'], mv))

    return active_cfs, exited_cfs, exit_records, portfolio_status


# ── Main ─────────────────────────────────────────────────────────────────────

def _net_cfs(active_cfs, exited_cfs, charges):
    """
    Return (net_active_cfs, net_exited_cfs, net_all_cfs) after adding charges.

    Stamp Duty is on purchases → split between active/exited proportional to invested amounts.
    STT Paid is on redemptions → goes entirely to exited portion.
    """
    active_invested  = -sum(a for _, a in active_cfs  if a < 0)
    exited_invested  = -sum(a for _, a in exited_cfs  if a < 0)
    total_invested   = active_invested + exited_invested
    active_ratio     = (active_invested / total_invested) if total_invested > 0 else 0.5

    stamp  = [(r['date'], -r['amount']) for r in charges if r['charge_type'] == 'Stamp Duty']
    stt    = [(r['date'], -r['amount']) for r in charges if r['charge_type'] == 'STT Paid']

    net_active  = active_cfs  + [(d, a * active_ratio)       for d, a in stamp]
    net_exited  = exited_cfs  + [(d, a * (1 - active_ratio)) for d, a in stamp] + stt
    net_all     = active_cfs  + exited_cfs + stamp + stt

    return net_active, net_exited, net_all


def main():
    excel_in  = _find_transactions_file()
    excel_out = excel_in.with_name(excel_in.stem.replace('_Transactions', '_XIRR') + '.xlsx')
    print(f"Input : {excel_in.name}")
    print(f"Output: {excel_out.name}")

    df       = pd.read_excel(excel_in, sheet_name='Transactions')
    df_close = pd.read_excel(excel_in, sheet_name='Closing Values')
    df_chg   = pd.read_excel(excel_in, sheet_name='Stamp Duty & STT')

    # Ensure date columns are Python date objects
    df['date']            = pd.to_datetime(df['date']).dt.date
    df_close['nav_date']  = pd.to_datetime(df_close['nav_date']).dt.date
    df_chg['date']        = pd.to_datetime(df_chg['date']).dt.date

    # Build closing lookup: (isin, folio) → dict
    # When multiple closing rows exist for the same folio (duplicate entries in PDF),
    # keep the one with the highest market value (most recent / active plan).
    closing_lookup = {}
    for _, row in df_close.iterrows():
        key = (row['isin'], row['folio'])
        existing = closing_lookup.get(key)
        if existing is None or row['market_value'] > existing.get('market_value', 0):
            closing_lookup[key] = row.to_dict()

    # Build charges lookup: (isin, folio) → list of charge dicts
    charges_lookup = {}
    for _, row in df_chg.iterrows():
        key = (row['isin'], row['folio'])
        charges_lookup.setdefault(key, []).append(row.to_dict())

    # ── Process each (isin, folio) group ─────────────────────────────────────
    summary_rows = []
    net_rows     = []
    quarter_rows = []

    groups = df.groupby(['isin', 'folio'], sort=False)
    total  = len(groups)

    for i, ((isin, folio), grp) in enumerate(groups, 1):
        grp = grp.sort_values('date')
        meta = grp.iloc[0]

        closing = closing_lookup.get((isin, folio))

        active_cfs, exited_cfs, exit_records, status = fifo_match(grp, closing)

        closing_nav  = closing['closing_nav']   if closing else None
        nav_date     = closing['nav_date']      if closing else None
        active_units = closing['closing_units'] if closing else None
        market_value = closing['market_value']  if closing else None

        active_invested = -sum(a for _, a in active_cfs  if a < 0)
        exited_invested = -sum(a for _, a in exited_cfs  if a < 0)
        total_redeemed  =  sum(a for _, a in exited_cfs  if a > 0)

        def _row(portion, invested, value, xirr_val, units=None, nav=None, nav_dt=None):
            return {
                'amc'            : meta['amc'],
                'fund_name'      : meta['fund_name'],
                'isin'           : isin,
                'folio'          : folio,
                'portion'        : portion,
                'amount_invested': round(invested, 2),
                'current_value'  : round(value, 2) if value is not None else None,
                'active_units'   : units,
                'closing_nav'    : nav,
                'nav_date'       : nav_dt,
                'xirr'           : xirr_val,
            }

        # Active portion row (only when units remain)
        if active_invested > 0:
            summary_rows.append(_row(
                'Active', active_invested, market_value,
                _xirr(active_cfs), active_units, closing_nav, nav_date,
            ))

        # Exited portion row (only when redemptions exist)
        if exited_invested > 0:
            summary_rows.append(_row(
                'Exited', exited_invested, total_redeemed,
                _xirr(exited_cfs),
            ))

        # Overall row (always — shows the full picture across both portions)
        all_cfs = exited_cfs + active_cfs
        overall_value = (market_value or 0) + total_redeemed
        summary_rows.append(_row(
            'Overall', active_invested + exited_invested, overall_value,
            _xirr(all_cfs), active_units, closing_nav, nav_date,
        ))

        # ── Net XIRR rows (same structure, charges deducted as extra outflows) ─
        charges = charges_lookup.get((isin, folio), [])
        na_cfs, ne_cfs, nall_cfs = _net_cfs(active_cfs, exited_cfs, charges)

        if active_invested > 0:
            net_rows.append(_row(
                'Active', active_invested, market_value,
                _xirr(na_cfs), active_units, closing_nav, nav_date,
            ))
        if exited_invested > 0:
            net_rows.append(_row(
                'Exited', exited_invested, total_redeemed,
                _xirr(ne_cfs),
            ))
        net_rows.append(_row(
            'Overall', active_invested + exited_invested, overall_value,
            _xirr(nall_cfs), active_units, closing_nav, nav_date,
        ))

        # ── Quarterly breakdown for exited lots ───────────────────────────────
        if exit_records:
            q_groups = {}
            for rec in exit_records:
                q = rec['investment_quarter']
                q_groups.setdefault(q, []).append(rec)

            for q, recs in sorted(q_groups.items()):
                q_cfs = []
                for r in recs:
                    q_cfs.append((r['purchase_date'], -r['cost']))
                    q_cfs.append((r['exit_date'],      r['proceeds']))

                quarter_rows.append({
                    'amc'                : meta['amc'],
                    'fund_name'          : meta['fund_name'],
                    'isin'               : isin,
                    'folio'              : folio,
                    'investment_quarter' : q,
                    'units_exited'       : round(sum(r['units']    for r in recs), 4),
                    'amount_invested'    : round(sum(r['cost']     for r in recs), 2),
                    'redemption_proceeds': round(sum(r['proceeds'] for r in recs), 2),
                    'first_purchase_date': min(r['purchase_date'] for r in recs),
                    'last_exit_date'     : max(r['exit_date']     for r in recs),
                    'holding_days'       : (max(r['exit_date']    for r in recs)
                                            - min(r['purchase_date'] for r in recs)).days,
                    'xirr'               : _xirr(q_cfs),
                })

        if i % 10 == 0 or i == total:
            print(f"  {i}/{total} fund+folio groups processed …")

    # ── Write output ──────────────────────────────────────────────────────────
    df_summary = pd.DataFrame(summary_rows)
    df_quarter = pd.DataFrame(quarter_rows)

    df_net = pd.DataFrame(net_rows)

    # Format XIRR as % (e.g. 0.1527 → 15.27)
    for frame in (df_summary, df_net, df_quarter):
        if 'xirr' in frame.columns:
            frame['xirr'] = frame['xirr'].apply(
                lambda x: round(x * 100, 2) if pd.notna(x) else None
            )

    with pd.ExcelWriter(excel_out, engine='openpyxl', date_format='DD-MMM-YYYY') as writer:
        df_summary.to_excel(writer, sheet_name='Gross XIRR',      index=False)
        df_net.to_excel(    writer, sheet_name='Net XIRR',         index=False)
        df_quarter.to_excel(writer, sheet_name='Exited by Quarter', index=False)

    print(f"\nSaved to {excel_out}")
    print(f"  Sheet 'Gross XIRR'      : {len(df_summary)} rows")
    print(f"  Sheet 'Net XIRR'        : {len(df_net)} rows")
    print(f"  Sheet 'Exited by Quarter': {len(df_quarter)} rows")
    print()
    print("Note: xirr in % p.a.  Net XIRR deducts Stamp Duty + STT as additional outflows.")


if __name__ == '__main__':
    main()
