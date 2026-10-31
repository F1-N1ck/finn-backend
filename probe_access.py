# Probe script for FINN data access.
# Run on the machine or host that will run the ingest job.
# Install first:  pip install requests
# NOTE: written but NOT run by me. Treat results as the real test.

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

SYMBOLS = ["RELIANCE", "TCS", "KPITTECH"]


def test_yahoo(symbol):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + ".NS"
    params = {"range": "3mo", "interval": "1d"}
    response = requests.get(url, params=params, headers=HEADERS, timeout=20)
    print("YAHOO", symbol, "status", response.status_code)
    if response.status_code == 200:
        data = response.json()
        timestamps = data["chart"]["result"][0]["timestamp"]
        print("   rows:", len(timestamps))


def test_nse(symbol):
    session = requests.Session()
    session.headers.update(HEADERS)
    # Step 1: visit a normal NSE page so the session gets cookies.
    home = session.get(
        "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        timeout=20,
    )
    print("NSE page status", home.status_code)
    # Step 2: call the announcements API with a date window.
    url = "https://www.nseindia.com/api/corporate-announcements"
    params = {
        "index": "equities",
        "symbol": symbol,
        "from_date": "24-10-2026",
        "to_date": "31-10-2026",
    }
    response = session.get(url, params=params, timeout=20)
    print("NSE", symbol, "status", response.status_code)
    if response.status_code == 200:
        items = response.json()
        print("   announcements:", len(items))


def test_bse(scrip_code):
    session = requests.Session()
    session.headers.update(HEADERS)
    session.headers.update({"Referer": "https://www.bseindia.com/"})
    url = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
    params = {
        "pageno": 1,
        "strCat": -1,
        "strPrevDate": "20261024",
        "strScrip": scrip_code,
        "strSearch": "P",
        "strToDate": "20261031",
        "strType": "C",
        "subcategory": -1,
    }
    response = session.get(url, params=params, timeout=20)
    print("BSE", scrip_code, "status", response.status_code)
    if response.status_code == 200:
        rows = response.json().get("Table", [])
        print("   rows:", len(rows))


def test_screener(symbol):
    url = "https://www.screener.in/company/" + symbol + "/consolidated/"
    response = requests.get(url, headers=HEADERS, timeout=20)
    print("SCREENER", symbol, "status", response.status_code)
    if response.status_code == 200:
        print("   has documents section:", 'id="documents"' in response.text)


for symbol in SYMBOLS:
    test_yahoo(symbol)
    test_nse(symbol)
    test_screener(symbol)

# BSE scrip codes (taken from screener.in URLs): RELIANCE 500325, KPITTECH 542651
test_bse("500325")
test_bse("542651")
