import base64, hmac, json, os, re, time

import requests
from flask import Flask, jsonify, request, send_from_directory

KEY = os.environ.get("API_KEY", "")                  # your private access key
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")    # free key from Google AI Studio
SKIP = ("image", "tts", "audio", "live", "embed", "robotics", "computer", "native", "learnlm")
MODEL_CACHE = {"t": 0, "names": []}
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


@app.get("/api/ping")
def ping():
    return jsonify(ok=True)       # only checks that your access key is right


@app.get("/api/news")
def news():
    # The free feed allows only a couple of downloads per 5 minutes per IP, so cache for an hour
    # and wait 5 minutes between retries after a failure.
    now = time.time()
    if now - NEWS["t"] > 3600 and now - NEWS.get("fail", 0) > 300:
        try:
            r = requests.get(NEWS_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            raw = r.json()          # raises if the feed sent a "Request Denied" web page instead
            NEWS["items"] = [
                {"title": e.get("title", ""), "country": e.get("country", ""), "date": e.get("date", ""),
                 "impact": e.get("impact"), "forecast": e.get("forecast", ""), "previous": e.get("previous", "")}
                for e in raw if e.get("impact") in ("High", "Medium")]
            NEWS["t"] = now
        except Exception:
            NEWS["fail"] = now
    if not NEWS["items"]:
        return jsonify(error="The free news feed is limiting requests right now"), 502
    return jsonify(items=NEWS["items"])


PROMPT = """You are a cautious technical analyst for forex, gold and crypto charts. Read this chart screenshot. {note}
Identify the instrument and timeframe if shown, the trend, and the nearest support and resistance. Then give one trade idea.
Reply with JSON only, using exactly these keys:
{{"pair":"e.g. EURUSD or null","timeframe":"e.g. M15 or null","direction":"buy|sell|none","entry_type":"market|limit|stop",
"entry":number or null,"stop_loss":number or null,"take_profit":number or null,
"support":[up to 2 numbers],"resistance":[up to 2 numbers],"confidence":"low|medium|high","reason":"two short sentences"}}
Read prices only from the chart's price axis and last-price label. A buy needs stop_loss < entry < take_profit; a sell needs the reverse.
If the image is unclear or there is no good setup, use direction "none" and explain why in reason."""


def candidate_models():
    """Ask Google which Gemini 'flash' models this key can use, newest first, so we never hard-code a retired name."""
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
    tried = []
    for model in candidate_models():             # fall back to the next model on errors
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=60)
        except requests.RequestException:
            tried.append(f"{model}: network error")
            continue
        if r.status_code == 200:
            try:
                out = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
                if isinstance(out, list):
                    out = out[0]
                return jsonify(out)
            except Exception:
                tried.append(f"{model}: unreadable reply")
                continue
        try:
            msg = r.json()["error"]["message"][:90]
        except Exception:
            msg = r.text[:90]
        tried.append(f"{model}: {r.status_code} {msg}")
    return jsonify(error="Scan failed. " + " | ".join(tried[:3])), 502
