import base64, hmac, json, os, re, time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request, send_from_directory

KEY = os.environ.get("API_KEY", "")                  # your private access key
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")    # free key from Google AI Studio
SKIP = ("image", "tts", "audio", "live", "embed", "robotics", "computer", "native", "learnlm")
MODEL_CACHE = {"t": 0, "names": []}
NEWS_URLS = ["https://nfs.faireconomy.media/ff_calendar_thisweek.json",
             "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json"]

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
NEWS = {"t": 0, "fail": 0, "items": [], "source": "", "why": ""}


@app.before_request
def guard():
    if request.path == "/":
        return None
    if not KEY or not hmac.compare_digest(request.headers.get("X-Key", ""), KEY):
        return jsonify(error="Wrong or missing key"), 401


@app.get("/")
def home():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/api/ping")
def ping():
    return jsonify(ok=True)


# ---------------------------------------------------------------- Gemini helpers
def candidate_models():
    """Ask Google which Gemini 'flash' models this key can use, newest first."""
    out = [m for m in (os.environ.get("GEMINI_MODEL"),) if m]
    now = time.time()
    if now - MODEL_CACHE["t"] > 21600:
        try:
            r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                             headers={"x-goog-api-key": GEMINI_KEY}, timeout=15)
            names = []
            for m in r.json().get("models", []):
                n = m["name"].split("/")[-1]
                if "generateContent" in m.get("supportedGenerationMethods", []) and "flash" in n \
                        and not any(x in n for x in SKIP):
                    names.append(n)

            def rank(n):
                v = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
                return (-(float(v.group(1)) if v else 0), "lite" in n, "preview" in n or "exp" in n, n)
            MODEL_CACHE.update(t=now, names=sorted(names, key=rank))
        except Exception:
            pass
    out += [n for n in MODEL_CACHE["names"] if n not in out]
    return (out or ["gemini-2.5-flash"])[:5]


def call_gemini(body):
    """Try each candidate model in turn. Returns (response_json or None, list of failures)."""
    tried = []
    for model in candidate_models():
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=60)
        except requests.RequestException:
            tried.append(f"{model}: network error")
            continue
        if r.status_code == 200:
            return r.json(), tried
        try:
            msg = r.json()["error"]["message"][:90]
        except Exception:
            msg = r.text[:90]
        tried.append(f"{model}: {r.status_code} {msg}")
    return None, tried


def reply_text(resp):
    parts = resp["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))


# ---------------------------------------------------------------- news
def from_feed():
    why = []
    for u in NEWS_URLS:
        try:
            r = requests.get(u, timeout=10, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            if r.status_code != 200:
                why.append(f"HTTP {r.status_code}")
                continue
            items = [
                {"title": e.get("title", ""), "country": e.get("country", ""), "date": e.get("date", ""),
                 "impact": e.get("impact"), "forecast": e.get("forecast", ""), "previous": e.get("previous", "")}
                for e in r.json() if e.get("impact") in ("High", "Medium")]
            if items:
                return items, ""
            why.append("empty")
        except Exception as ex:
            why.append(type(ex).__name__)
    return None, ", ".join(why)


def from_ai():
    """Last resort: have Gemini compile the week's events using Google Search."""
    if not GEMINI_KEY:
        return None
    now = datetime.now(timezone.utc)
    prompt = (f"Now it is {now.strftime('%A %Y-%m-%d %H:%M')} UTC. Use Google Search to find the high-impact (red folder) "
              "forex economic events scheduled from now through the next 7 days, for example NFP, CPI, FOMC, central bank "
              "rate decisions, GDP, PMI and central bank chair speeches. Reply with only a JSON array. Each item: "
              '{"title":"","country":"3-letter currency code","date":"ISO 8601 in UTC","impact":"High","forecast":"","previous":""}. '
              "Only include events you found a source for.")
    resp, _ = call_gemini({"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}]})
    if not resp:
        return None
    try:
        arr = json.loads(re.search(r"\[.*\]", reply_text(resp), re.S).group(0))
    except Exception:
        return None
    items = []
    for e in arr:
        try:
            datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
            items.append({"title": str(e.get("title", ""))[:90], "country": str(e.get("country", ""))[:3].upper(),
                          "date": str(e["date"]), "impact": "High",
                          "forecast": str(e.get("forecast", "") or ""), "previous": str(e.get("previous", "") or "")})
        except Exception:
            continue
    return sorted(items, key=lambda x: x["date"]) or None


@app.get("/api/news")
def news():
    now = time.time()
    ttl = 3600 if NEWS["source"] == "feed" else 1800
    if (not NEWS["items"] or now - NEWS["t"] > ttl) and now - NEWS["fail"] > 300:
        items, why = from_feed()
        src = "feed"
        if not items:
            items, src = from_ai(), "ai"
        if items:
            NEWS.update(items=items, t=now, source=src, why=why)
        else:
            NEWS.update(fail=now, why=why)
    if not NEWS["items"]:
        return jsonify(error="No news source worked (" + (NEWS["why"] or "unknown") + ")"), 502
    return jsonify(items=NEWS["items"], source=NEWS["source"], why=NEWS["why"])


# ---------------------------------------------------------------- chart analysis
PROMPT = """You are an ICT / Smart Money Concepts analyst. Study this chart screenshot. {note}
Analyse it with these tools:
- Market structure (SMC): swing highs and lows, BOS, CHoCH / MSS, and the current bias.
- Liquidity: buy-side pools (equal highs, previous highs) and sell-side pools (equal lows, previous lows), and any sweep or stop run that already happened (inducement).
- Premium / discount: take the current dealing range, find its 50% equilibrium, and say whether price is in premium or discount. Longs belong in discount, shorts in premium.
- PD arrays: fair value gaps, order blocks, breaker blocks, mitigation and rejection blocks, liquidity voids, and the OTE zone (62 to 79 percent retracement).
- Order flow: use only what the screenshot shows: displacement candles, imbalances, absorption or rejection wicks, and volume, delta or footprint if visible. If none of those are visible, say order flow cannot be confirmed from this image.
- AMD / Power of 3: accumulation, manipulation (liquidity sweep or Judas swing), distribution. Say which phase price is in.
Then give ONE trade idea that enters from a PD array in the correct zone, puts the stop beyond the swept or protected swing, and targets opposing liquidity.
Reply with JSON only, with exactly these keys:
{"pair":"or null","timeframe":"or null","bias":"bullish|bearish|neutral","structure":"one sentence",
"amd":{"phase":"accumulation|manipulation|distribution|unclear","note":"one sentence"},
"premium_discount":{"zone":"premium|discount|equilibrium","range_high":number or null,"range_low":number or null},
"liquidity":{"buy_side":[numbers],"sell_side":[numbers],"swept":"one sentence or null"},
"pd_arrays":[{"type":"FVG|OB|Breaker|Mitigation|Rejection|Void|OTE","side":"bullish|bearish","low":number,"high":number}],
"orderflow":"one sentence","direction":"buy|sell|none","entry_type":"limit|market|stop",
"entry":number or null,"stop_loss":number or null,"take_profit":number or null,"take_profit_2":number or null,
"entry_basis":"which PD array and where inside it","invalidation":"one sentence","confidence":"low|medium|high","reason":"two short sentences"}
Rules: read prices only from the chart's price axis and last-price label. At most 4 pd_arrays. A buy needs stop_loss < entry < take_profit; a sell needs the reverse. Do not invent levels you cannot see. If structure is unclear or there is no setup in the correct zone, set direction to "none" and explain why in reason."""


@app.post("/api/analyze")
def analyze():
    if not GEMINI_KEY:
        return jsonify(error="GEMINI_API_KEY is not set on the server"), 500
    img = request.files.get("image")
    if not img:
        return jsonify(error="No image received"), 400
    note = request.form.get("note", "")[:100]
    body = {
        "contents": [{"parts": [
            {"text": PROMPT.replace("{note}", f"The user says: {note}." if note else "")},
            {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(img.read()).decode()}}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    resp, tried = call_gemini(body)
    if not resp:
        return jsonify(error="Scan failed. " + " | ".join(tried[:3])), 502
    try:
        out = json.loads(reply_text(resp))
        return jsonify(out[0] if isinstance(out, list) else out)
    except Exception:
        return jsonify(error="Could not read the AI reply. Try scanning again."), 502
