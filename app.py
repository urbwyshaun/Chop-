import base64, hmac, json, os, time

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

KEY = os.environ.get("API_KEY", "")                      # your private access key
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")        # free key from Google AI Studio
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
NEWS_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024
BOTS, REPORTS, NEWS = [], {}, {"t": 0, "items": []}     # in memory: resets when the server restarts


@app.before_request
def guard():
    if request.path == "/":
        return None
    sent = request.headers.get("X-Key", "")
    if not KEY or not hmac.compare_digest(sent, KEY):
        return jsonify(error="unauthorized"), 401


@app.get("/")
def home():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "index.html")


# ---- dashboard <-> server ------------------------------------------------
@app.get("/api/bots")
def get_bots():
    now = time.time()
    rep = {k: {**v, "age": int(now - v["seen"])} for k, v in REPORTS.items()}
    return jsonify(bots=BOTS, reports=rep)


@app.post("/api/bots")
def set_bots():
    global BOTS
    data = request.get_json(silent=True) or {}
    BOTS = [
        {"name": str(b.get("name", ""))[:24],
         "pairs": [str(p)[:12] for p in b.get("pairs", [])][:10],
         "run": bool(b.get("run"))}
        for b in data.get("bots", [])
    ][:20]
    return jsonify(ok=True)


# ---- MT5 EA <-> server (plain text so MQL5 can parse it easily) ----------
@app.get("/ea/config")
def ea_config():
    b = next((x for x in BOTS if x["name"] == request.args.get("bot", "")), None)
    out = f"run={1 if b and b['run'] and b['pairs'] else 0}\n"
    if b and b["pairs"]:
        out += "pairs=" + ",".join(b["pairs"]) + "\n"
    return Response(out, mimetype="text/plain")


@app.post("/ea/report")
def ea_report():
    f = request.form
    REPORTS[f.get("bot", "")[:24]] = {
        "balance": f.get("balance"), "equity": f.get("equity"),
        "open": f.get("open"), "seen": time.time()}
    return jsonify(ok=True)


# ---- news ------------------------------------------------------------------
@app.get("/api/news")
def news():
    if time.time() - NEWS["t"] > 1800:
        try:
            r = requests.get(NEWS_URL, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            NEWS["items"] = [
                {"title": e["title"], "country": e["country"], "date": e["date"]}
                for e in r.json() if e.get("impact") == "High"]
            NEWS["t"] = time.time()
        except Exception as ex:
            if not NEWS["items"]:
                return jsonify(error="News feed unavailable: " + str(ex)[:80]), 502
    return jsonify(items=NEWS["items"])


# ---- chart image -> trade idea --------------------------------------------
PROMPT = """You are a cautious forex/CFD technical analyst. Read this chart screenshot. {note}
Reply with JSON only: {{"direction":"buy|sell|none","entry":number or null,"stop_loss":number or null,"take_profit":number or null,"confidence":"low|medium|high","reason":"two short sentences"}}
Use only prices you can read from the chart's price axis. If the chart is unclear or there is no good setup, use direction "none"."""


@app.post("/api/analyze")
def analyze():
    if not GEMINI_KEY:
        return jsonify(error="GEMINI_API_KEY is not set on the server"), 500
    img = request.files.get("image")
    if not img:
        return jsonify(error="No image received"), 400
    body = {
        "contents": [{"parts": [
            {"text": PROMPT.format(note=request.form.get("note", "")[:100])},
            {"inline_data": {"mime_type": "image/jpeg",
                             "data": base64.b64encode(img.read()).decode()}}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
        headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=60)
    if r.status_code != 200:
        return jsonify(error=f"Gemini error {r.status_code}: {r.text[:160]}"), 502
    try:
        return jsonify(json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"]))
    except Exception:
        return jsonify(error="Could not read the AI reply. Try again."), 502
