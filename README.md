# MF IRR Calculator

A personal finance tool that parses a CAMS/KFintech Consolidated Account Statement (CAS) PDF and computes XIRR (annualised return) for each mutual fund holding.

## What it does

- Parses password-protected CAS PDFs from CAMSONLINE (CAMS) or KFintech
- Computes XIRR per fund using actual transaction cashflows and current market value
- Classifies funds by asset class (Equity, Debt, Hybrid, etc.)
- Compares fund XIRR against the relevant benchmark index (Nifty 50, Nifty 500, Nifty Midcap 150, etc.)
- Exports results to Excel (transactions + XIRR summary)

## How to get your CAS PDF

1. Go to [https://www.camsonline.com/Investors/Statements/Consolidated-Account-Statement](https://www.camsonline.com/Investors/Statements/Consolidated-Account-Statement)
2. **Statement Type** — select **Detailed (includes transaction listing)**
3. **Period** — select **Specific Period**
   - Start date: `01-Apr-2010` (or earlier if you have investments before that date)
   - End date: today's date
4. **Folio Listing** — select **With Zero Balance Folios** (ensures exited funds are included for accurate XIRR)
5. **Email** — enter your registered email ID
6. **Password** — set it as your PAN in lowercase letters (e.g. if your PAN is `ABCDE1234F`, enter `abcde1234f`)
7. Submit — you will receive the PDF at your email within a few minutes
8. The PDF password to open it is the same: your PAN in lowercase

## Requirements

- Python 3.10 or higher
- pip

## Setup

```bash
# 1. Create and activate a virtual environment (recommended)
python -m venv venv

# On Windows:
venv\Scripts\activate

# On Mac/Linux:
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
python app.py
```

## Usage

1. Open your browser and go to `http://localhost:5000`
2. Upload your CAS PDF and enter the password (your PAN in lowercase, e.g. `abcde1234f`)
3. Click **Analyse** — parsing a large PDF (500+ pages) may take 2–5 minutes
4. View your portfolio dashboard with XIRR per fund
5. Click **Download Excel** to export results

## Notes

- The PDF is processed entirely in memory and never saved to disk
- Benchmark data (Nifty, S&P 500, etc.) is fetched from Yahoo Finance on first use and cached locally in `benchmark_data/`
- Funds with zero closing balance are correctly classified as Exited
- Franklin Templeton segregated portfolio units (frozen debt funds) are handled separately
