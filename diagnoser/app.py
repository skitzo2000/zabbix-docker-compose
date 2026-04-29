import json
import logging
import os
import threading
import time

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)
log = logging.getLogger("diagnoser")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

GROQ_API_KEY = os.environ["DIAG_GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("DIAG_GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

DISCORD_BOT_TOKEN = os.environ["DIAG_DISCORD_BOT_TOKEN"]
DISCORD_GUILD_ID = os.environ["DIAG_DISCORD_GUILD_ID"]
DISCORD_CHANNEL_NAME = os.environ.get("DIAG_DISCORD_CHANNEL_NAME", "jones-alerts")
DISCORD_API = "https://discord.com/api/v10"
DISCORD_HEADERS = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": "zabbix-diagnoser (zabbix.thenortonfamily.net)",
}

# Brain integration is gated behind a token; if unset we fail open.
BRAIN_TOKEN = os.environ.get("DIAG_BRAIN_TOKEN") or ""
BRAIN_PROXY_URL = os.environ.get("DIAG_BRAIN_PROXY_URL", "https://mcp.thenortonfamily.net/v1")
BRAIN_NAMESPACE = os.environ.get("DIAG_BRAIN_NAMESPACE", "goldfish-paul-gmail-com:zabbix-docker-compose")

SYSTEM_PROMPT = """You are an on-call diagnostic assistant for a small MSP (Norton Consulting). \
You receive Zabbix alerts about client infrastructure (pfSense firewalls, FreePBX, network switches, hosts) \
and produce a brief Discord post that helps the responder triage fast.

Rules:
- Stay under 1500 characters total. Discord truncates at 2000.
- Lead with one sentence stating what is wrong in plain language.
- Then bullet 2-4 likely causes ranked by probability, each with a one-line check or fix.
- Reference specific hostnames, items, and values from the alert. Don't invent fields.
- If the alert is genuinely ambiguous, say so — do not guess.
- No fluff, no "I am an AI", no apologies, no markdown headers. Plain prose + bullets.
"""

_channel_id_cache = {"id": None, "ts": 0.0}
_channel_lock = threading.Lock()


def _ensure_channel() -> str:
    with _channel_lock:
        if _channel_id_cache["id"] and time.time() - _channel_id_cache["ts"] < 3600:
            return _channel_id_cache["id"]

        r = requests.get(
            f"{DISCORD_API}/guilds/{DISCORD_GUILD_ID}/channels",
            headers=DISCORD_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        for ch in r.json():
            if ch.get("type") == 0 and ch.get("name", "").lower() == DISCORD_CHANNEL_NAME.lower():
                _channel_id_cache.update(id=ch["id"], ts=time.time())
                log.info("found existing channel #%s id=%s", DISCORD_CHANNEL_NAME, ch["id"])
                return ch["id"]

        r = requests.post(
            f"{DISCORD_API}/guilds/{DISCORD_GUILD_ID}/channels",
            headers=DISCORD_HEADERS,
            json={"name": DISCORD_CHANNEL_NAME, "type": 0,
                  "topic": "Zabbix alert diagnoses for the JonesShakes host group."},
            timeout=10,
        )
        r.raise_for_status()
        cid = r.json()["id"]
        _channel_id_cache.update(id=cid, ts=time.time())
        log.info("created channel #%s id=%s", DISCORD_CHANNEL_NAME, cid)
        return cid


def _fetch_brain_context(host: str, trigger_name: str) -> str:
    if not BRAIN_TOKEN:
        return ""
    try:
        r = requests.post(
            f"{BRAIN_PROXY_URL}/brAIn__r",
            headers={"Authorization": f"Bearer {BRAIN_TOKEN}", "Content-Type": "application/json"},
            json={
                "namespace": BRAIN_NAMESPACE,
                "query": f"{host} {trigger_name}",
                "max_tokens": 800,
                "format": "text",
                "mode": "auto",
            },
            timeout=8,
        )
        if r.status_code != 200:
            log.warning("brain recall %s: %s", r.status_code, r.text[:200])
            return ""
        body = r.json()
        return body.get("result") or body.get("text") or json.dumps(body)[:1500]
    except Exception as e:
        log.warning("brain recall failed: %s", e)
        return ""


def _store_diagnosis(alert: dict, summary: str) -> None:
    if not BRAIN_TOKEN:
        return
    try:
        episode_id = f"diagnosis:{alert.get('event_id', 'unknown')}:{int(time.time())}"
        text = (
            f"Alert: {alert.get('trigger_name')} on {alert.get('host')} "
            f"(severity={alert.get('severity')}, value={alert.get('item_value')})\n\n"
            f"Diagnosis:\n{summary}"
        )
        requests.post(
            f"{BRAIN_PROXY_URL}/brAIn__s",
            headers={"Authorization": f"Bearer {BRAIN_TOKEN}", "Content-Type": "application/json"},
            json={
                "namespace": BRAIN_NAMESPACE,
                "target": "episodic",
                "data": {
                    "episode_id": episode_id,
                    "text": text,
                    "metadata": {
                        "tags": ["diagnosis", "zabbix", alert.get("host", ""),
                                 alert.get("severity", "").lower()],
                        "host": alert.get("host"),
                        "trigger_id": alert.get("trigger_id"),
                        "event_id": alert.get("event_id"),
                    },
                },
            },
            timeout=8,
        )
    except Exception as e:
        log.warning("brain store failed: %s", e)


def _call_groq(alert: dict, brain_context: str) -> str:
    user_blob = "ALERT:\n" + json.dumps(alert, indent=2)
    if brain_context:
        user_blob += f"\n\nPRIOR CONTEXT FROM MEMORY:\n{brain_context}"
    r = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_blob},
            ],
            "temperature": 0.2,
            "max_tokens": 600,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _format_discord(alert: dict, summary: str) -> str:
    sev = alert.get("severity", "?")
    host = alert.get("host", "?")
    trig = alert.get("trigger_name", "?")
    when = alert.get("started_at", "")
    url = alert.get("trigger_url", "")
    head = f"**[{sev}] {host}** — {trig}"
    if when:
        head += f"\n_started {when}_"
    body = f"\n\n{summary}"
    tail = f"\n\n<{url}>" if url else ""
    out = head + body + tail
    return out[:1990]


def _post_discord(channel_id: str, content: str) -> None:
    r = requests.post(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        headers=DISCORD_HEADERS,
        json={"content": content},
        timeout=10,
    )
    r.raise_for_status()


@app.get("/healthz")
def healthz():
    return jsonify(ok=True, model=GROQ_MODEL, channel=DISCORD_CHANNEL_NAME, brain=bool(BRAIN_TOKEN))


@app.post("/diagnose")
def diagnose():
    alert = request.get_json(silent=True) or {}
    if not alert.get("host") or not alert.get("trigger_name"):
        return jsonify(ok=False, error="missing host or trigger_name"), 400

    log.info("diagnose: %s — %s (sev=%s)",
             alert.get("host"), alert.get("trigger_name"), alert.get("severity"))

    brain_ctx = _fetch_brain_context(alert.get("host", ""), alert.get("trigger_name", ""))
    try:
        summary = _call_groq(alert, brain_ctx)
    except Exception as e:
        log.exception("groq call failed")
        return jsonify(ok=False, error=f"groq: {e}"), 502

    try:
        cid = _ensure_channel()
        _post_discord(cid, _format_discord(alert, summary))
    except Exception as e:
        log.exception("discord post failed")
        return jsonify(ok=False, error=f"discord: {e}", summary=summary), 502

    threading.Thread(target=_store_diagnosis, args=(alert, summary), daemon=True).start()
    return jsonify(ok=True, summary_preview=summary[:120])
