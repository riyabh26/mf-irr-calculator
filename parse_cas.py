"""
CAMS + KFintech Consolidated Account Statement (CAS) PDF Parser
Extracts all transactions into a structured DataFrame for IRR calculation.
"""

import re
import pdfplumber
import pandas as pd
from datetime import datetime
from pathlib import Path


# ── Regex patterns ─────────────────────────────────────────────────────────────

RE_FOLIO = re.compile(r'Folio No\s*:\s*(\S+).*?PAN\s*:\s*([A-Z0-9]+)', re.IGNORECASE)
RE_OPENING = re.compile(r'Opening Unit Balance', re.IGNORECASE)
RE_CLOSING = re.compile(r'Closing Unit Balance\s*:\s*([\d,\.]+)', re.IGNORECASE)
RE_CLOSING_FULL = re.compile(
    r'Closing Unit Balance\s*:\s*([\d,\.]+)'
    r'.*?NAV on\s+(\S+)\s*:\s*INR\s*([\d,\.]+)'
    r'.*?Total Cost Value\s*:\s*([\d,\.]+)'
    r'.*?Market Value on\s+\S+\s*:\s*INR\s*([\d,\.]+)',
    re.IGNORECASE
)

# Fund name line starts with alphanumeric prefix like "P8042-" or "128MLDGG-"
# Fund prefix like "P8042-", "128MLDGG-", "O243D-", "D835-"
# Prefix MUST contain at least one letter (to exclude date lines like "08-May-2020")
# After the hyphen the line must contain a fund keyword
RE_FUND_START = re.compile(
    r'^[A-Z0-9]*[A-Z][A-Z0-9]*-'   # 1+ alphanumeric with ≥1 letter, then hyphen
    r'(?=.*(Fund|Scheme|Equity|Debt|Liquid|Balanced|Index|ETF|Arbitrage|Savings|Bluechip|Midcap|Smallcap|Small Cap|Large Cap|Multi Cap|Flexi Cap|Hybrid|Cap Fund|Income))',
    re.IGNORECASE
)
RE_ISIN = re.compile(r'ISIN\s*:\s*(INF[A-Z0-9]*)', re.IGNORECASE)
RE_ISIN_CONT = re.compile(r'^([A-Z0-9]+)\s*\(?\s*Advisor\s*:', re.IGNORECASE)  # "917K01GE4(Advisor:"
RE_REGISTRAR = re.compile(r'Registrar\s*:\s*(CAMS|KFINTECH|KFIN\w*)', re.IGNORECASE)

# Transaction: starts with DD-Mon-YYYY
RE_TXN_DATE = re.compile(r'^(\d{2}-[A-Za-z]{3}-\d{4})\s+(.*)')
RE_STAMP = re.compile(r'\*\*\* Stamp Duty \*\*\*|\*\*\* STT Paid \*\*\*')
RE_STAMP_AMOUNT = re.compile(r'\*\*\* (Stamp Duty|STT Paid) \*\*\*\s+(\([\d,]+\.?\d*\)|[\d,]+\.?\d*)$', re.IGNORECASE)

# Trailing 4 values: amount, units, nav, unit_balance
# Values may be positive (1,000.00) or negative in parens ((1,000.00))
# Must start with a digit (inside or outside parens) to avoid matching bare commas
_NUM = r'(\(\d[\d,]*\.?\d*\)|\d[\d,]*\.?\d*)'
RE_TXN_NUMS = re.compile(rf'{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s*$')

# Failed/rejected transactions — have a *** marker + trailing amount, but no units/nav
# e.g. "22-Sep-2025 ***Refund KYC not Validated*** 10,00,000.00"
RE_FAILED_TXN = re.compile(
    r'\*\*\*.+?\*\*\*\s+([\d,]+\.?\d*)$'
)

# Informational markers — *** text *** with NO trailing number at all
# e.g. "27-Nov-2025 ***Address Updated from KRA Data***"
#      "22-Feb-2023 ******Address Updated from CVL Data******"
RE_INFO_MARKER = re.compile(r'^\*{2,}.+?\*{2,}\s*$')

# AMC standalone line — appears before Folio No, is just the AMC name
# Known pattern: ends with "Mutual Fund", "MF", "Asset Management", etc.
RE_AMC_LINE = re.compile(
    r'^([\w\s&]+(?:Mutual Fund|Asset Mutual Fund|MF|Asset Management Company))$',
    re.IGNORECASE
)

# Page boilerplate to skip
SKIP_RE = [
    re.compile(r'^6101-eviL'),
    re.compile(r'^4\.3V'),
    re.compile(r'^70541626062'),
    re.compile(r'^Consolidated Account Statement'),
    re.compile(r'^\d{2}-[A-Za-z]{3}-\d{4} To \d{2}-[A-Za-z]{3}-\d{4}'),  # CAS date-range header
    re.compile(r'^Date\s+Transaction'),
    re.compile(r'^\(INR\)'),
    re.compile(r'^Page \d+ of \d+'),
    re.compile(r'^Email Id:'),
    re.compile(r'^Mobile:'),
    re.compile(r'^This Consolidated'),
    re.compile(r'^holdinginvestments'),
    re.compile(r'^emailidentered'),
    re.compile(r'^consolidate'),
    re.compile(r'^emailid'),
    re.compile(r'^If you'),
    re.compile(r'^registered'),
    re.compile(r'^This statement'),
    re.compile(r'^holdings\. Please'),
    re.compile(r'^PORTFOLIO SUMMARY'),
    re.compile(r'^Cost Value\s+Market Value'),
    re.compile(r'^Mutual Fund including'),
    re.compile(r'^Total\s+[\d,]'),
    re.compile(r'^Nominee'),
    re.compile(r'^Entry Load'),
    re.compile(r'^Exit Load'),
    re.compile(r'^For \d+%'),
    re.compile(r'^For remaining'),
    re.compile(r'^Effective from'),
    re.compile(r'^friendly initiative'),
    re.compile(r'^balances and valuation'),
    re.compile(r'w\.e\.f'),             # exit load details
    # Portfolio summary AMC rows (have two large numbers at end)
    re.compile(r'(?:Mutual Fund|MF)\s+[\d,]+\.\d{2}\s+[\d,]+\.\d{2}$'),
]


def _should_skip(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    for pat in SKIP_RE:
        if pat.search(s):
            return True
    return False


def _clean_num(s: str) -> float:
    """Convert a number string to float; parentheses mean negative."""
    s = s.strip()
    if s.startswith('(') and s.endswith(')'):
        return -float(s[1:-1].replace(',', ''))
    return float(s.replace(',', ''))


def _parse_date(s: str) -> datetime:
    return datetime.strptime(s.strip(), '%d-%b-%Y')


def _extract_fund_info(line1: str, line2: str = '') -> dict:
    """Extract fund name, ISIN, registrar from one or two lines."""
    combined = (line1 + ' ' + line2).strip()

    m_isin = RE_ISIN.search(combined)
    isin = m_isin.group(1) if m_isin else None

    m_reg = RE_REGISTRAR.search(combined)
    registrar = m_reg.group(1).upper() if m_reg else None

    # Fund name: everything after the "CODE-" prefix, before " - ISIN:" or "Registrar :"
    name = combined
    # Remove prefix code (e.g. "P8042-")
    name = re.sub(r'^[A-Z0-9]{3,}-', '', name, flags=re.IGNORECASE).strip()
    # Remove from "- ISIN:" onwards
    name = re.sub(r'\s*-\s*ISIN\s*:.*', '', name, flags=re.IGNORECASE).strip()
    # Remove from "Registrar :" onwards
    name = re.sub(r'\s+Registrar\s*:.*', '', name, flags=re.IGNORECASE).strip()
    # Remove "(Non-Demat)", "(Non Demat)"
    name = re.sub(r'\s*\(Non[- ]?Demat\)', '', name, flags=re.IGNORECASE).strip()
    # Remove trailing " - "
    name = name.rstrip(' -').strip()

    return {'fund_name': name, 'isin': isin, 'registrar': registrar}


RE_INVESTOR_NAME = re.compile(r'^([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)$')


def parse_cas(pdf_path: str, password: str | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, str | None]:
    pdf_path = Path(pdf_path)
    records = []
    charges = []      # stamp duty + STT
    closing_data = [] # closing units / NAV / market value per fund+folio
    investor_name = None  # extracted from PDF header

    current_amc = None
    current_folio = None
    current_pan = None
    current_fund = None
    current_isin = None
    current_registrar = None

    # State for multi-line parsing
    pending_txn = None       # dict with date + partial raw text
    pending_fund_line = None  # first line of a fund name block
    in_portfolio_summary = False
    last_txn_description = None  # description of most recent transaction (for stamp duty linking)

    open_kwargs = {'password': password} if password else {}
    with pdfplumber.open(pdf_path, **open_kwargs) as pdf:
        total = len(pdf.pages)
        print(f"Parsing {total} pages...")

        for page_num, page in enumerate(pdf.pages):
            text = page.extract_text()
            if not text:
                continue

            # Extract investor name from first 3 pages (Title Case standalone line)
            if investor_name is None and page_num < 3:
                for ln in text.splitlines():
                    ln = ln.strip()
                    m_nm = RE_INVESTOR_NAME.match(ln)
                    if m_nm and len(ln.split()) >= 2:
                        investor_name = ln
                        break

            lines = text.split('\n')

            for raw_line in lines:
                line = raw_line.strip()
                if not line:
                    continue

                # ── Portfolio summary section (skip until first Folio No) ──
                if 'PORTFOLIO SUMMARY' in line:
                    in_portfolio_summary = True
                    continue
                if in_portfolio_summary:
                    if RE_FOLIO.search(line) or RE_AMC_LINE.match(line):
                        in_portfolio_summary = False
                        # fall through to process this folio/AMC line
                    else:
                        continue

                if _should_skip(line):
                    continue

                # ── Flush pending wrapped transaction ────────────────────────
                if pending_txn is not None:
                    # This line should complete the previous transaction
                    combined = pending_txn['raw'] + ' ' + line
                    m_nums = RE_TXN_NUMS.search(combined)
                    if m_nums and not RE_STAMP.search(line):
                        desc = combined[:m_nums.start()].strip()
                        desc = re.sub(r'^\d{2}-[A-Za-z]{3}-\d{4}\s+', '', desc)
                        last_txn_description = desc.strip()
                        records.append({
                            'amc': current_amc,
                            'folio': current_folio,
                            'pan': current_pan,
                            'fund_name': current_fund,
                            'isin': current_isin,
                            'registrar': current_registrar,
                            'date': pending_txn['date'],
                            'transaction': desc.strip(),
                            'amount': _clean_num(m_nums.group(1)),
                            'units': _clean_num(m_nums.group(2)),
                            'nav': _clean_num(m_nums.group(3)),
                            'unit_balance': _clean_num(m_nums.group(4)),
                            'status': 'OK',
                        })
                    pending_txn = None
                    # Don't continue — fall through to process this line normally

                # ── Pending fund name (second line continuation) ─────────────
                if pending_fund_line is not None:
                    is_continuation = (
                        RE_ISIN.search(line)
                        or '(Non-Demat)' in line
                        or '(Non Demat)' in line
                        or RE_ISIN_CONT.match(line)
                        or (current_isin is not None and len(current_isin) < 10)
                    )
                    if is_continuation:
                        combined = pending_fund_line + ' ' + line
                        # Handle split ISIN: "ISIN: INF" + "917K01GE4(Advisor:..."
                        isin_partial = re.search(r'ISIN\s*:\s*(INF)\s', combined, re.IGNORECASE)
                        if isin_partial:
                            m_cont = RE_ISIN_CONT.match(line)
                            if m_cont:
                                combined = combined.replace(
                                    'ISIN: ' + isin_partial.group(1),
                                    'ISIN: ' + isin_partial.group(1) + m_cont.group(1)
                                )
                        info = _extract_fund_info(combined)
                        current_fund = info['fund_name']
                        current_isin = info['isin']
                        if info['registrar']:
                            current_registrar = info['registrar']
                        pending_fund_line = None
                        continue
                    else:
                        # Not a continuation — parse what we had
                        info = _extract_fund_info(pending_fund_line)
                        current_fund = info['fund_name']
                        current_isin = info['isin']
                        if info['registrar']:
                            current_registrar = info['registrar']
                        pending_fund_line = None
                        # Fall through to process current line

                # ── Closing balance ──────────────────────────────────────────
                if RE_CLOSING.search(line):
                    m_cl = RE_CLOSING_FULL.search(line)
                    if m_cl:
                        closing_data.append({
                            'amc': current_amc,
                            'folio': current_folio,
                            'pan': current_pan,
                            'fund_name': current_fund,
                            'isin': current_isin,
                            'registrar': current_registrar,
                            'closing_units': _clean_num(m_cl.group(1)),
                            'closing_nav': _clean_num(m_cl.group(3)),
                            'nav_date': _parse_date(m_cl.group(2)),
                            'cost_value': _clean_num(m_cl.group(4)),
                            'market_value': _clean_num(m_cl.group(5)),
                        })
                    continue

                # ── Folio header ─────────────────────────────────────────────
                m = RE_FOLIO.search(line)
                if m:
                    current_folio = m.group(1)
                    current_pan = m.group(2)
                    continue

                # ── Opening balance ───────────────────────────────────────────
                if RE_OPENING.search(line):
                    continue

                # ── AMC header ────────────────────────────────────────────────
                if RE_AMC_LINE.match(line) and not RE_TXN_DATE.match(line):
                    current_amc = line
                    continue

                # ── Fund name start ───────────────────────────────────────────
                if RE_FUND_START.match(line):
                    m_isin = RE_ISIN.search(line)
                    if m_isin and len(m_isin.group(1)) >= 10:
                        # Full ISIN on this line
                        info = _extract_fund_info(line)
                        current_fund = info['fund_name']
                        current_isin = info['isin']
                        if info['registrar']:
                            current_registrar = info['registrar']
                    else:
                        # Partial/missing ISIN — may be wrapping to next line
                        pending_fund_line = line
                        m_reg = RE_REGISTRAR.search(line)
                        if m_reg:
                            current_registrar = m_reg.group(1).upper()
                        # Partially parse fund name and partial ISIN now
                        info = _extract_fund_info(line)
                        current_fund = info['fund_name']
                        current_isin = info['isin']  # may be short like "INF"
                    continue

                # ── Transaction line ──────────────────────────────────────────
                m_date = RE_TXN_DATE.match(line)
                if m_date:
                    date_str = m_date.group(1)
                    rest = m_date.group(2)

                    if RE_STAMP.search(rest):
                        m_ch = RE_STAMP_AMOUNT.search(rest)
                        if m_ch:
                            charges.append({
                                'amc': current_amc,
                                'folio': current_folio,
                                'pan': current_pan,
                                'fund_name': current_fund,
                                'isin': current_isin,
                                'registrar': current_registrar,
                                'date': _parse_date(date_str),
                                'charge_type': m_ch.group(1),
                                'amount': _clean_num(m_ch.group(2)),
                                'transaction': last_txn_description,
                            })
                        continue

                    # Informational marker — no amount, no units (e.g. address update)
                    if RE_INFO_MARKER.match(rest.strip()):
                        desc_match = re.search(r'\*{2,}(.+?)\*{2,}', rest)
                        desc = desc_match.group(1).strip() if desc_match else rest.strip()
                        records.append({
                            'amc': current_amc,
                            'folio': current_folio,
                            'pan': current_pan,
                            'fund_name': current_fund,
                            'isin': current_isin,
                            'registrar': current_registrar,
                            'date': _parse_date(date_str),
                            'transaction': desc,
                            'amount': 0.0,
                            'units': 0.0,
                            'nav': 0.0,
                            'unit_balance': None,
                            'status': 'INFO',
                        })
                        continue

                    # Failed/rejected transaction (KYC failure, payment bounce, etc.)
                    # These have only an amount — no units, nav, or balance change
                    m_fail = RE_FAILED_TXN.search(rest)
                    if m_fail:
                        desc = rest[:m_fail.start() + rest[m_fail.start():].index('*')].strip()
                        # Extract just the description (between *** markers)
                        desc_match = re.search(r'\*\*\*(.+?)\*\*\*', rest)
                        desc = desc_match.group(1).strip() if desc_match else rest.strip()
                        records.append({
                            'amc': current_amc,
                            'folio': current_folio,
                            'pan': current_pan,
                            'fund_name': current_fund,
                            'isin': current_isin,
                            'registrar': current_registrar,
                            'date': _parse_date(date_str),
                            'transaction': desc,
                            'amount': _clean_num(m_fail.group(1)),
                            'units': 0.0,
                            'nav': 0.0,
                            'unit_balance': None,   # balance unchanged — will be filled by caller
                            'status': 'FAILED',
                        })
                        continue

                    m_nums = RE_TXN_NUMS.search(rest)
                    if m_nums:
                        desc = rest[:m_nums.start()].strip()
                        last_txn_description = desc
                        records.append({
                            'amc': current_amc,
                            'folio': current_folio,
                            'pan': current_pan,
                            'fund_name': current_fund,
                            'isin': current_isin,
                            'registrar': current_registrar,
                            'date': _parse_date(date_str),
                            'transaction': desc,
                            'amount': _clean_num(m_nums.group(1)),
                            'units': _clean_num(m_nums.group(2)),
                            'nav': _clean_num(m_nums.group(3)),
                            'unit_balance': _clean_num(m_nums.group(4)),
                            'status': 'OK',
                        })
                    else:
                        # Check for 3-number lines: units, nav, balance (amount=0 placeholder)
                        m3 = re.search(
                            rf'{_NUM}\s+{_NUM}\s+{_NUM}\s*$', rest
                        )
                        if m3:
                            desc = rest[:m3.start()].strip()
                            last_txn_description = desc
                            records.append({
                                'amc': current_amc,
                                'folio': current_folio,
                                'pan': current_pan,
                                'fund_name': current_fund,
                                'isin': current_isin,
                                'registrar': current_registrar,
                                'date': _parse_date(date_str),
                                'transaction': desc,
                                'amount': 0.0,
                                'units': _clean_num(m3.group(1)),
                                'nav': _clean_num(m3.group(2)),
                                'unit_balance': _clean_num(m3.group(3)),
                                'status': 'OK',
                            })
                        else:
                            # Numbers on next line
                            pending_txn = {'date': _parse_date(date_str), 'raw': line}
                    continue

            if (page_num + 1) % 50 == 0:
                print(f"  {page_num + 1}/{total} pages, {len(records)} transactions...")

    df = pd.DataFrame(records)
    df_charges = pd.DataFrame(charges)
    df_closing = pd.DataFrame(closing_data)

    if df.empty:
        print("WARNING: No transactions found!")
        return df, df_charges, df_closing

    df['date'] = pd.to_datetime(df['date']).dt.date
    df = df.sort_values(['isin', 'folio', 'date']).reset_index(drop=True)

    if not df_charges.empty:
        df_charges['date'] = pd.to_datetime(df_charges['date']).dt.date
        df_charges = df_charges.sort_values(['isin', 'folio', 'date']).reset_index(drop=True)

    if not df_closing.empty:
        df_closing['nav_date'] = pd.to_datetime(df_closing['nav_date']).dt.date

    print(f"\nDone! {len(df)} transactions, {df['isin'].nunique()} unique ISINs, "
          f"{df['folio'].nunique()} folios.")
    print(f"      {len(df_charges)} charge rows (stamp duty + STT).")
    print(f"      {len(df_closing)} closing balance rows.")
    print(f"      Investor name: {investor_name}")
    return df, df_charges, df_closing, investor_name


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python parse_cas.py <path_to_cas_pdf> [password]")
        sys.exit(1)
    pdf_file = Path(sys.argv[1])
    password = sys.argv[2] if len(sys.argv) > 2 else None
    df, df_charges, df_closing, investor_name = parse_cas(str(pdf_file), password=password)

    bad_isin = df[df['isin'].isna() | (df['isin'].str.len() < 10)]
    if not bad_isin.empty:
        print(f"\nWARNING: {len(bad_isin)} rows still have missing/short ISIN:")
        print(bad_isin[['amc','fund_name','isin','date']].drop_duplicates('fund_name').to_string())

    out = pdf_file.with_name(pdf_file.stem + '_Transactions.xlsx')
    with pd.ExcelWriter(out, engine='openpyxl', date_format='DD-MMM-YYYY') as writer:
        df.to_excel(writer, sheet_name='Transactions', index=False)
        df_charges.to_excel(writer, sheet_name='Stamp Duty & STT', index=False)
        df_closing.to_excel(writer, sheet_name='Closing Values', index=False)

    print(f"\nSaved to {out}")
    print(f"  Sheet 'Transactions'    : {len(df)} rows")
    print(f"  Sheet 'Stamp Duty & STT': {len(df_charges)} rows")
    print(f"  Sheet 'Closing Values'  : {len(df_closing)} rows")
