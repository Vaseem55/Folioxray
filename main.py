import os
import uuid
import hmac
import hashlib
import tempfile
import json
import urllib.parse
import httpx
import razorpay
from bs4 import BeautifulSoup
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from dotenv import load_dotenv
import casparser

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
rzp = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

REPORT_PRICE_PAISE = 9900   # ₹99 in paise

@app.on_event("startup")
async def startup_check():
    key = RAZORPAY_KEY_ID or "NOT SET"
    status = "✅ LIVE" if key.startswith("rzp_live") else ("✅ TEST" if key.startswith("rzp_test") else "❌ MISSING")
    print(f"\n🔑 Razorpay key: {key[:18]}... {status}")
    print(f"🤖 OpenAI key:   {'✅ SET' if OPENAI_API_KEY else '❌ MISSING'}\n")

# =====================================================================
# IN-MEMORY SESSION STORE
# Holds parsed portfolio between upload and payment verification.
# Key: session_id (uuid), Value: dict with portfolio data
# =====================================================================
pending_sessions: dict[str, dict] = {}


# =====================================================================
# FUND HOLDINGS CACHE
# =====================================================================
CACHE_FILE = "fund_cache.json"

def load_cache():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_cache(cache_data):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache_data, f, indent=4)

FUND_HOLDINGS_CACHE = load_cache()


# =====================================================================
# HOLDINGS FETCHERS
# =====================================================================

async def fetch_holdings_from_amfi(fund_name: str, http_client: httpx.AsyncClient) -> list:
    """Fetch real holdings from AMFI monthly portfolio disclosure."""
    import re
    try:
        # Step 1: find scheme code from mfapi
        search_url = f"https://api.mfapi.in/mf/search?q={urllib.parse.quote(fund_name)}"
        r = await http_client.get(search_url, timeout=10.0)
        if r.status_code != 200 or not r.json():
            return []
        schemes = r.json()
        if not schemes:
            return []
        scheme_code = schemes[0]["schemeCode"]
        scheme_name = schemes[0]["schemeName"]
        print(f"  AMFI match: {scheme_name} (code {scheme_code})")

        # Step 2: fetch portfolio from mfapi portfolio endpoint
        port_url = f"https://api.mfapi.in/mf/{scheme_code}/portfolio"
        pr = await http_client.get(port_url, timeout=15.0)
        if pr.status_code != 200:
            return []
        data = pr.json()
        holdings_raw = data.get("portfolioDetails", data.get("portfolio", []))
        if not holdings_raw:
            return []

        cleaned = []
        for h in holdings_raw:
            name = (h.get("nameOfInstrument") or h.get("name") or h.get("companyName") or "").strip()
            weight = h.get("percentageToNav") or h.get("weight") or h.get("percentage") or 0
            name = re.sub(r'[^\w\s\.\-\(\)&,/]', '', name, flags=re.ASCII).strip()
            try:
                weight = float(weight)
            except:
                weight = 0.0
            # Only equity holdings (skip debt, cash, etc.)
            asset_type = h.get("instrumentType", h.get("assetType", "")).lower()
            if "debt" in asset_type or "bond" in asset_type or "tbill" in asset_type:
                continue
            if name and weight > 0:
                cleaned.append({"stock_name": name, "weight_percent": weight})

        cleaned.sort(key=lambda x: x["weight_percent"], reverse=True)
        print(f"  ✅ AMFI: got {len(cleaned)} equity holdings for '{fund_name}'")
        return cleaned[:15]
    except Exception as e:
        print(f"  AMFI fetch error: {e}")
        return []


async def fetch_from_vro(fund_name: str, http_client: httpx.AsyncClient) -> list:
    try:
        search_url = f"https://www.valueresearchonline.com/api/7/fund-selector/?q={urllib.parse.quote(fund_name)}&plan=direct"
        r = await http_client.get(search_url, timeout=12.0)
        if r.status_code != 200:
            return []
        funds = r.json()
        if not funds:
            return []
        fund_id = funds[0].get("id") or funds[0].get("fund_id")
        if not fund_id:
            return []
        port_url = f"https://www.valueresearchonline.com/funds/{fund_id}/portfolio/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        pr = await http_client.get(port_url, headers=headers, timeout=12.0)
        if pr.status_code != 200:
            return []
        soup = BeautifulSoup(pr.text, "lxml")
        holdings = []
        for row in soup.select("table.portfolioHoldingsTable tbody tr, table#equity-holdings tbody tr"):
            cols = row.find_all("td")
            if len(cols) >= 2:
                name = cols[0].get_text(strip=True)
                raw_weight = cols[-1].get_text(strip=True).replace("%", "").strip()
                try:
                    weight = float(raw_weight)
                    if name and weight > 0:
                        holdings.append({"stock_name": name, "weight_percent": weight})
                except ValueError:
                    continue
        if holdings:
            print(f"  ✅ VRO: got {len(holdings)} holdings for '{fund_name}'")
        return holdings
    except Exception as e:
        print(f"  VRO fetch error: {e}")
        return []


async def fetch_live_market_holdings(fund_name: str) -> list:
    if fund_name in FUND_HOLDINGS_CACHE:
        print(f"⚡ CACHE HIT: {fund_name}")
        return FUND_HOLDINGS_CACHE[fund_name]

    print(f"🔍 Fetching holdings for: {fund_name}")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    holdings = []
    async with httpx.AsyncClient(headers=headers, timeout=20.0, follow_redirects=True) as http_client:
        # Try AMFI real data first
        holdings = await fetch_holdings_from_amfi(fund_name, http_client)
        # Fallback to VRO scrape
        if not holdings:
            holdings = await fetch_from_vro(fund_name, http_client)
    if not holdings:
        print(f"  ⚠️ No real data found for '{fund_name}' — skipping (not using AI estimates)")

    FUND_HOLDINGS_CACHE[fund_name] = holdings
    save_cache(FUND_HOLDINGS_CACHE)
    print(f"💾 Saved {len(holdings)} holdings for '{fund_name}' to cache.")
    return holdings


# =====================================================================
# AI PORTFOLIO ANALYZER
# =====================================================================

async def analyze_portfolio_categories(portfolio_data: list) -> dict:
    prompt = f"""
    You are a SEBI-registered portfolio manager.
    Review these mutual funds: {json.dumps(portfolio_data)}.

    CRITICAL RULES:
    1. DO NOT GUESS STOCKS OR WEIGHTS.
    2. Identify 2 or 3 specific DOMESTIC EQUITY mutual funds that genuinely clash because they share the same category/sector mandate.
    3. COMPLETELY IGNORE any fund that invests internationally or in overseas/foreign equities (e.g. funds with words like "International", "Overseas", "Global", "US", "World", "Nasdaq", "S&P 500", "NYSE", "FOF" investing abroad).
    4. Return ONLY their exact names in the 'overlapping_funds' array.

    Respond with ONLY this JSON schema:
    {{
      "overlap_detected": true,
      "analysis_summary": "A sharp, 2-sentence breakdown explaining why these categories overlap.",
      "overlapping_funds": ["Exact scheme_name 1", "Exact scheme_name 2"],
      "suggested_replacements": ["Suggested Category to diversify into"]
    }}
    """
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a precise financial JSON API."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0,
            seed=42
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"OpenAI Execution Error: {e}")
        return {"overlap_detected": False, "overlapping_funds": []}


# =====================================================================
# SHARED OVERLAP BUILDER (used after payment verified)
# =====================================================================

async def build_full_report(domestic_portfolio: list) -> dict:
    ai_advice = await analyze_portfolio_categories(domestic_portfolio)

    overlapping_stocks_master = {}
    mapped_overlapping_funds = []
    fund_holdings_map = {}

    already_matched = set()
    for target_fund in ai_advice.get("overlapping_funds", []):
        matched_value = 0.0
        exact_name = target_fund
        for real_fund in domestic_portfolio:
            rname = real_fund["scheme_name"]
            if rname in already_matched:
                continue
            if target_fund.lower() in rname.lower() or rname.lower() in target_fund.lower():
                matched_value = real_fund["current_value_inr"]
                exact_name = rname
                already_matched.add(rname)
                break

        mapped_overlapping_funds.append({"scheme_name": exact_name, "current_value_inr": matched_value})
        live_holdings = await fetch_live_market_holdings(exact_name)
        fund_holdings_map[exact_name] = live_holdings

        for holding in live_holdings:
            stock = (holding.get("stock_name") or holding.get("name") or
                     holding.get("stock") or holding.get("company") or "Unknown")
            weight = float(holding.get("weight_percent") or holding.get("weight") or
                           holding.get("percentage") or holding.get("allocation") or 0)
            if not stock or stock == "Unknown" or weight <= 0:
                continue
            if stock not in overlapping_stocks_master:
                overlapping_stocks_master[stock] = {"stock_name": stock, "allocations": []}
            overlapping_stocks_master[stock]["allocations"].append({
                "fund_name": exact_name,
                "weight": weight,
                "weight_percent": weight,
                "stock_exposure_inr": round(matched_value * (weight / 100.0), 2)
            })

    final_overlapping_stocks = [
        data for data in overlapping_stocks_master.values()
        if len(data["allocations"]) > 1
    ]

    fund_names_list = list(fund_holdings_map.keys())
    pair_overlaps = []
    for i in range(len(fund_names_list)):
        for j in range(i + 1, len(fund_names_list)):
            name_a = fund_names_list[i]
            name_b = fund_names_list[j]
            def normalize(holdings):
                result = {}
                for h in holdings:
                    k = h.get("stock_name") or h.get("name") or h.get("stock") or h.get("company")
                    v = float(h.get("weight_percent") or h.get("weight") or h.get("percentage") or 0)
                    if k and v > 0:
                        result[k] = v
                return result
            holdings_a = normalize(fund_holdings_map[name_a])
            holdings_b = normalize(fund_holdings_map[name_b])
            common_stocks = []
            overlap_pct = 0.0
            for stock, wa in holdings_a.items():
                if stock in holdings_b:
                    wb = holdings_b[stock]
                    contribution = round(min(wa, wb), 2)
                    overlap_pct += contribution
                    common_stocks.append({"stock_name": stock, "weight_a": wa, "weight_b": wb, "overlap_contribution": contribution})
            common_stocks.sort(key=lambda x: x["overlap_contribution"], reverse=True)
            pair_overlaps.append({
                "fund_a": name_a, "fund_b": name_b,
                "overlap_percent": round(overlap_pct, 1),
                "common_stock_count": len(common_stocks),
                "common_stocks": common_stocks
            })
    pair_overlaps.sort(key=lambda x: x["overlap_percent"], reverse=True)

    ai_advice["overlapping_funds_rich"] = mapped_overlapping_funds
    ai_advice["overlapping_stocks_detail"] = final_overlapping_stocks
    ai_advice["pair_overlaps"] = pair_overlaps
    return ai_advice


# =====================================================================
# TEST SCENARIOS (bypass PDF upload for dev/validation)
# =====================================================================

TEST_SCENARIOS = {
    "high_overlap": [
        {"scheme_name": "Mirae Asset Large Cap Fund - Direct Plan - Growth", "current_value_inr": 150000},
        {"scheme_name": "Axis Bluechip Fund - Direct Plan - Growth", "current_value_inr": 120000},
        {"scheme_name": "HDFC Top 100 Fund - Direct Plan - Growth", "current_value_inr": 80000},
    ],
    "low_overlap": [
        {"scheme_name": "Parag Parikh Flexi Cap Fund - Direct Plan - Growth", "current_value_inr": 200000},
        {"scheme_name": "Kotak Small Cap Fund - Direct Plan - Growth", "current_value_inr": 100000},
        {"scheme_name": "ICICI Prudential Technology Fund - Direct Plan - Growth", "current_value_inr": 75000},
    ],
    "with_international": [
        {"scheme_name": "Mirae Asset Large Cap Fund - Direct Plan - Growth", "current_value_inr": 150000},
        {"scheme_name": "Motilal Oswal Nasdaq 100 FOF - Direct Plan - Growth", "current_value_inr": 50000},
        {"scheme_name": "SBI Magnum Midcap Fund - Direct Plan - Growth", "current_value_inr": 100000},
        {"scheme_name": "Franklin India Feeder - Franklin U.S. Opportunities Fund", "current_value_inr": 40000},
    ],
    "small_cap_overlap": [
        {"scheme_name": "Bandhan Small Cap Fund - Direct Plan - Growth", "current_value_inr": 90000},
        {"scheme_name": "quant Small Cap Fund - Direct Plan - Growth", "current_value_inr": 85000},
        {"scheme_name": "Nippon India Small Cap Fund - Direct Plan - Growth", "current_value_inr": 70000},
    ],
}


@app.get("/test-scenario/{scenario}")
async def test_scenario(scenario: str):
    """Dev-only: simulate a portfolio without uploading a PDF."""
    if scenario not in TEST_SCENARIOS:
        return JSONResponse(status_code=404, content={
            "available_scenarios": list(TEST_SCENARIOS.keys()),
            "usage": "/test-scenario/high_overlap"
        })
    portfolio = TEST_SCENARIOS[scenario]
    domestic = [f for f in portfolio if not is_international_fund(f["scheme_name"])]
    total = sum(f["current_value_inr"] for f in portfolio)
    session_id = str(uuid.uuid4())
    pending_sessions[session_id] = {
        "portfolio_summary": portfolio,
        "domestic_portfolio": domestic,
        "total_portfolio_value": total,
    }
    return JSONResponse(content={
        "status": "success",
        "scenario": scenario,
        "session_id": session_id,
        "total_portfolio_value": total,
        "fund_count": len(portfolio),
        "funds": portfolio,
        "note": "Test mode — no PDF needed. Use this session_id to call /create-order or /test-full-report"
    })


@app.get("/test-full-report/{scenario}")
async def test_full_report(scenario: str):
    """Dev-only: run full overlap analysis on a scenario without payment."""
    if scenario not in TEST_SCENARIOS:
        return JSONResponse(status_code=404, content={"available": list(TEST_SCENARIOS.keys())})
    portfolio = TEST_SCENARIOS[scenario]
    domestic = [f for f in portfolio if not is_international_fund(f["scheme_name"])]
    total = sum(f["current_value_inr"] for f in portfolio)
    ai_advice = await build_full_report(domestic)
    return JSONResponse(content={
        "status": "success",
        "scenario": scenario,
        "total_portfolio_value": total,
        "funds": portfolio,
        "domestic_fund_count": len(domestic),
        "international_excluded": len(portfolio) - len(domestic),
        "ai_advice": ai_advice,
    })


# =====================================================================
# HELPERS
# =====================================================================

INTERNATIONAL_KEYWORDS = [
    "international", "overseas", "global", "world", "us equity",
    "nasdaq", "s&p 500", "s&p500", "nyse", "emerging market",
    "china", "europe", "japan", "taiwan", "korea",
    "fof", "fund of fund", "foreign"
]

def is_international_fund(name: str) -> bool:
    lower = name.lower()
    return any(kw in lower for kw in INTERNATIONAL_KEYWORDS)


# =====================================================================
# ENDPOINTS
# =====================================================================

@app.get("/")
async def serve_frontend():
    return FileResponse("index.html", media_type="text/html; charset=utf-8")


@app.post("/audit-cas")
async def audit_cas_pdf(
    password: str = Form(...),
    cas_file: UploadFile = File(...)
):
    """
    Step 1 (Free): Parse the CAS PDF and return fund list + total value.
    Full overlap analysis is locked behind payment.
    """
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(await cas_file.read())
            tmp_path = tmp.name

        json_str = casparser.read_cas_pdf(tmp_path, password, output="json")
        parsed_data = json.loads(json_str)
        os.unlink(tmp_path)

        portfolio_summary = []
        fund_names_seen = []
        total_portfolio_value = 0.0

        for folio in parsed_data.get("folios", []):
            for scheme in folio.get("schemes", []):
                scheme_name = scheme.get("scheme", "Unknown Scheme")
                valuation_raw = scheme.get("valuation", {}).get("value", 0.0)
                try:
                    valuation = float(str(valuation_raw).replace(',', '').strip()) if valuation_raw else 0.0
                except:
                    valuation = 0.0
                total_portfolio_value += valuation
                if scheme_name not in fund_names_seen and valuation > 0:
                    fund_names_seen.append(scheme_name)
                    portfolio_summary.append({
                        "scheme_name": scheme_name,
                        "current_value_inr": round(valuation, 2)
                    })

        domestic_portfolio = [f for f in portfolio_summary if not is_international_fund(f["scheme_name"])]

        # Store parsed data for use after payment
        session_id = str(uuid.uuid4())
        pending_sessions[session_id] = {
            "portfolio_summary": portfolio_summary,
            "domestic_portfolio": domestic_portfolio,
            "total_portfolio_value": round(total_portfolio_value, 2),
        }

        return JSONResponse(content={
            "status": "success",
            "session_id": session_id,
            "total_portfolio_value": round(total_portfolio_value, 2),
            "fund_count": len(portfolio_summary),
            "funds": portfolio_summary,
        })

    except Exception as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})


@app.post("/create-order")
async def create_order(session_id: str = Form(...)):
    """
    Step 2: Create a Razorpay order for ₹99.
    """
    if session_id not in pending_sessions:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Session expired. Please re-upload your CAS."})

    try:
        order = rzp.order.create({
            "amount": REPORT_PRICE_PAISE,
            "currency": "INR",
            "receipt": f"folioxray_{session_id[:8]}",
            "notes": {"session_id": session_id}
        })
        return JSONResponse(content={
            "status": "success",
            "order_id": order["id"],
            "amount": REPORT_PRICE_PAISE,
            "currency": "INR",
            "key_id": RAZORPAY_KEY_ID,
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})


@app.post("/verify-payment")
async def verify_payment(
    razorpay_order_id: str = Form(...),
    razorpay_payment_id: str = Form(...),
    razorpay_signature: str = Form(...),
    session_id: str = Form(...),
):
    """
    Step 3: Verify Razorpay signature, then run and return the full overlap report.
    """
    # Verify signature
    body = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected_sig = hmac.new(
        RAZORPAY_KEY_SECRET.encode(),
        body.encode(),
        hashlib.sha256
    ).hexdigest()

    if expected_sig != razorpay_signature:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Payment verification failed."})

    if session_id not in pending_sessions:
        return JSONResponse(status_code=404, content={"status": "error", "message": "Session expired. Please re-upload your CAS."})

    session = pending_sessions.pop(session_id)

    try:
        ai_advice = await build_full_report(session["domestic_portfolio"])
        return JSONResponse(content={
            "status": "success",
            "total_portfolio_value": session["total_portfolio_value"],
            "funds": session["portfolio_summary"],
            "ai_advice": ai_advice,
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
