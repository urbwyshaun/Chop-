import base64, hmac, json, os, time

import requests
from flask import Flask, jsonify, request, send_from_directory

KEY = os.environ.get("API_KEY", "")                  # your private access key
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")    # free key from Google AI Studio
MODELS = [m for m in (os.environ.get("GEMINI_MODEL"), "gemini-2.5-flash", "gemini-2.5-flash-lite") if m]
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
NEWS = {"t": 0, "items": []}


@app.before_request
def guard():
    if request.path == "/":
        return None
    if not KEY or not hmac.compare_digest(request.headers.get("X-Key", ""), KEY):
        return jsonify(error="Wrong or missing key"), 401


@app.get("/")
def home():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


@app.get("/api/news")
def news():
    if time.time() - NEWS["t"] > 1800:          # refresh at most every 30 minutes
        try:
            r = requests.get(NEWS_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            NEWS["items"] = [
                {"title": e.get("title", ""), "country": e.get("country", ""), "date": e.get("date", ""),
                 "impact": e.get("impact"), "forecast": e.get("forecast", ""), "previous": e.get("previous", "")}
                for e in r.json() if e.get("impact") in ("High", "Medium")]
            NEWS["t"] = time.time()
        except Exception as ex:
            if not NEWS["items"]:
                return jsonify(error="News feed unavailable: " + str(ex)[:80]), 502
    return jsonify(items=NEWS["items"])


PROMPT = """You are a cautious technical analyst for forex, gold and crypto charts. Read this chart screenshot. {note}
Identify the instrument and timeframe if shown, the trend, and the nearest support and resistance. Then give one trade idea.
Reply with JSON only, using exactly these keys:
{{"pair":"e.g. EURUSD or null","timeframe":"e.g. M15 or null","direction":"buy|sell|none","entry_type":"market|limit|stop",
"entry":number or null,"stop_loss":number or null,"take_profit":number or null,
"support":[up to 2 numbers],"resistance":[up to 2 numbers],"confidence":"low|medium|high","reason":"two short sentences"}}
Read prices only from the chart's price axis and last-price label. A buy needs stop_loss < entry < take_profit; a sell needs the reverse.
If the image is unclear or there is no good setup, use direction "none" and explain why in reason."""


@app.post("/api/analyze")
def analyze():
    if not GEMINI_KEY:
        return jsonify(error="GEMINI_API_KEY is not set on the server"), 500
    img = request.files.get("image")
    if not img:
        return jsonify(error="No image received"), 400
    data = base64.b64encode(img.read()).decode()
    body = {
        "contents": [{"parts": [
            {"text": PROMPT.format(note=request.form.get("note", "")[:100])},
            {"inline_data": {"mime_type": "image/jpeg", "data": data}}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    err = "no model tried"
    for model in MODELS:                         # fall back to the next model on errors
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=60)
        except requests.RequestException as ex:
            err = str(ex)[:120]
            continue
        if r.status_code == 200:
            try:
                out = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                if isinstance(out, list):
                    out = out[0]
                return jsonify(out)
            except Exception:
                err = "Could not read the AI reply"
                continue
        err = f"Gemini {model} error {r.status_code}: {r.text[:140]}"
    return jsonify(error=err), 502
