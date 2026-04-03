import os
import re
import uuid
import hmac
import json
import hashlib
import logging
import asyncio
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request, Header, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
import httpx
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("patec")

TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID", "")
ELEVENLABS_AGENT_ID    = os.getenv("ELEVENLABS_AGENT_ID", "agent_3901kn83d76tf72tvg450k0fb8ek")
PATEC_API_KEY          = os.getenv("PATEC_API_KEY", "")
ELEVENLABS_WEBHOOK_SECRET = os.getenv("ELEVENLABS_WEBHOOK_SECRET", "")
ELEVENLABS_API_KEY     = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_PHONE_NUMBER_ID = os.getenv("ELEVENLABS_PHONE_NUMBER_ID", "")

# Airtable
AIRTABLE_API_KEY    = os.getenv("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID    = os.getenv("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_NAME = os.getenv("AIRTABLE_TABLE_NAME", "Anrufe")

tickets: list = []
telegram_context: dict = {}

limiter = Limiter(key_func=get_remote_address, default_limits=["30/minute"])
app = FastAPI(title="PATEC Telefonagent API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]  = "nosniff"
        response.headers["X-Frame-Options"]         = "DENY"
        response.headers["X-XSS-Protection"]        = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"]         = "strict-origin-when-cross-origin"
        return response

app.add_middleware(SecurityHeadersMiddleware)


def check_api_key(x_api_key: Optional[str]) -> None:
    if not PATEC_API_KEY:
        raise HTTPException(status_code=500, detail="PATEC_API_KEY not configured on server")
    if not x_api_key or not hmac.compare_digest(x_api_key, PATEC_API_KEY):
        raise HTTPException(status_code=403, detail="Forbidden: invalid or missing API key")


async def _send_telegram_message(text: str) -> Optional[int]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        )
        if resp.status_code == 200:
            return resp.json().get("result", {}).get("message_id")
    return None


def _detect_priority(text: str) -> str:
    t = text.lower()
    high_kw = ["dringend", "notfall", "notruf", "kaputt", "defekt", "ausgefallen",
                "kein wasser", "kein strom", "rohrbruch", "gefahr", "brand", "sofort", "leck", "gas"]
    low_kw  = ["frage", "info", "information", "termin", "anfrage", "beratung", "angebot", "allgemein"]
    if any(kw in t for kw in high_kw):
        return "Hoch"
    if any(kw in t for kw in low_kw):
        return "Niedrig"
    return "Mittel"


def _detect_callback(text: str) -> bool:
    t = text.lower()
    cb_kw = ["rueckruf", "zurueckrufen", "callback", "ruf mich", "bitte anrufen"]
    return any(kw in t for kw in cb_kw)


async def log_to_airtable(call_data: dict):
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        log.warning("Airtable nicht konfiguriert")
        return None

    anruf_id  = call_data.get("conversation_id") or str(uuid.uuid4())[:8].upper()
    name      = call_data.get("name", "Unbekannt")
    telefon   = call_data.get("phone", "")
    anliegen  = call_data.get("anliegen") or call_data.get("issue", "")
    summary   = call_data.get("summary", "")
    duration  = call_data.get("duration_secs", 0)
    notizen   = call_data.get("notizen", "")
    datum_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

    combined_text = f"{anliegen} {summary}"
    prioritaet    = _detect_priority(combined_text)
    rueckruf      = _detect_callback(combined_text)

    fields = {
        "Anruf-ID":              anruf_id,
        "Name":                  name,
        "Telefon":               telefon,
        "Anliegen":              anliegen,
        "Datum":                 datum_iso,
        "Dauer (Sek.)":          int(duration) if duration else 0,
        "Zusammenfassung":       summary,
        "Status":                "Neu",
        "Prioritaet":            prioritaet,
        "Rueckruf erforderlich": rueckruf,
        "Notizen":               notizen,
    }

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, headers=headers, json={"fields": fields})
            if resp.status_code in (200, 201):
                record_id = resp.json().get("id")
                log.info(f"Airtable: Anruf {anruf_id} gespeichert -> {record_id}")
                return record_id
            log.error(f"Airtable Fehler {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        log.error(f"Airtable Exception: {e}")
        return None


async def trigger_outbound_call(phone: str, instruction: str) -> dict:
    if not ELEVENLABS_API_KEY:
        return {"success": False, "reason": "ELEVENLABS_API_KEY nicht konfiguriert"}
    if not ELEVENLABS_PHONE_NUMBER_ID:
        return {"success": False, "reason": "ELEVENLABS_PHONE_NUMBER_ID nicht konfiguriert"}

    payload = {
        "agent_id":               ELEVENLABS_AGENT_ID,
        "agent_phone_number_id":  ELEVENLABS_PHONE_NUMBER_ID,
        "to_number":              phone,
        "conversation_initiation_client_data": {
            "dynamic_variables": {"owner_instruction": instruction}
        },
    }
    log.info(f"Outbound call to {phone}: {instruction!r}")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
            headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code == 200:
            return {"success": True, "data": resp.json()}
        log.error(f"Outbound call failed {resp.status_code}: {resp.text}")
        return {"success": False, "status_code": resp.status_code, "detail": resp.text}


@app.get("/health")
@limiter.limit("30/minute")
async def health(request: Request):
    return {"status": "ok", "tickets": len(tickets)}


@app.get("/")
@limiter.limit("30/minute")
async def root(request: Request):
    return {"message": "PATEC API laeuft", "version": "2.0"}


@app.post("/tools/save_ticket")
@limiter.limit("30/minute")
async def save_ticket(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    data = await request.json()
    ticket = {"id": len(tickets) + 1, "timestamp": datetime.now().isoformat(), **data}
    tickets.append(ticket)
    return {"success": True, "ticket_id": ticket["id"]}


@app.post("/tools/send_telegram")
@limiter.limit("30/minute")
async def send_telegram(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"success": False, "reason": "nicht konfiguriert"}
    data = await request.json()
    na    = "Nicht angegeben"
    name  = data.get("name") or na
    phone = data.get("phone") or na
    issue = data.get("issue") or na
    now   = datetime.now().strftime("%d.%m.%Y %H:%M")
    text = (
        f"\U0001f4de *Neuer Anruf bei PATEC*\n"
        f"\u23f0 {now}\n\n"
        f"\U0001f464 *Name:* {name}\n"
        f"\U0001f4f1 *Rueckrufnummer:* {phone}\n"
        f"\U0001f527 *Anliegen:* {issue}"
    )
    message_id = await _send_telegram_message(text)
    if message_id:
        telegram_context[str(message_id)] = {"name": name, "phone": phone, "issue": issue}
        log.info(f"Stored context for message_id={message_id}: {name}, {phone}")
    return {"success": message_id is not None}


@app.post("/tools/check_calendar")
@limiter.limit("30/minute")
async def check_calendar(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    from datetime import date, timedelta
    slots = []
    d = date.today() + timedelta(days=1)
    day_names = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag"]
    while len(slots) < 8:
        if d.weekday() < 5:
            for h in [9, 11, 14, 16]:
                slots.append({
                    "date": f"{day_names[d.weekday()]}, {d.strftime('%d.%m.%Y')}",
                    "time": f"{h:02d}:00-{h+1:02d}:00",
                })
        d += timedelta(days=1)
    return {"free_slots": slots[:6]}


@app.post("/webhook/post-call")
@limiter.limit("30/minute")
async def post_call_webhook(request: Request):
    body = await request.body()

    if ELEVENLABS_WEBHOOK_SECRET:
        sig_header = request.headers.get("ElevenLabs-Signature", "")
        if not sig_header:
            raise HTTPException(status_code=403, detail="Missing ElevenLabs-Signature header")
        try:
            parts     = dict(p.split("=", 1) for p in sig_header.split(","))
            timestamp = parts["t"]
            v0_sig    = parts["v0"]
        except (KeyError, ValueError):
            raise HTTPException(status_code=403, detail="Invalid ElevenLabs-Signature format")
        message  = f"{timestamp}.{body.decode()}"
        expected = hmac.new(
            ELEVENLABS_WEBHOOK_SECRET.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, v0_sig):
            raise HTTPException(status_code=403, detail="Webhook signature mismatch")

    data            = json.loads(body)
    conversation_id = data.get("conversation_id", "unknown")
    log.info(f"Post-Call: {conversation_id}")

    now      = datetime.now().strftime("%d.%m.%Y %H:%M")
    metadata = data.get("metadata", {})
    duration = metadata.get("call_duration_secs") or data.get("call_duration_secs", 0)
    analysis = data.get("analysis", {})
    transcript_summary = analysis.get("transcript_summary") or analysis.get("summary", "")

    transcript = data.get("transcript", [])
    if not transcript_summary and transcript:
        lines = [f"{m.get('role','?').capitalize()}: {m.get('message','')}" for m in transcript[:2]]
        if len(transcript) > 2:
            lines.append("...")
            lines.append(f"{transcript[-1].get('role','?').capitalize()}: {transcript[-1].get('message','')}")
        transcript_summary = "\n".join(lines)

    data_coll    = analysis.get("data_collection_results") or {}
    caller_name  = (data_coll.get("name") or {}).get("value", "")
    caller_phone = (data_coll.get("phone") or {}).get("value", "")
    caller_issue = (data_coll.get("issue") or data_coll.get("anliegen") or {}).get("value", "")

    text = (
        f"\U0001f4cb *Gespraechsprotokoll PATEC*\n"
        f"\u23f0 {now}\n"
        f"\U0001f194 Gespraech: `{conversation_id}`\n"
        f"\u23f1 Dauer: {duration}s\n\n"
        f"\U0001f4dd *Zusammenfassung:*\n{transcript_summary or 'Keine Zusammenfassung verfuegbar'}"
    )
    await _send_telegram_message(text)

    asyncio.create_task(log_to_airtable({
        "conversation_id": conversation_id,
        "name":            caller_name,
        "phone":           caller_phone,
        "anliegen":        caller_issue,
        "summary":         transcript_summary,
        "duration_secs":   duration,
    }))

    return {"status": "received"}


@app.post("/webhook/telegram")
@limiter.limit("30/minute")
async def telegram_webhook(request: Request):
    update = await request.json()
    log.info(f"Telegram update: {json.dumps(update)[:500]}")

    message = update.get("message", {})
    if not message:
        return {"ok": True}

    chat_id = str(message.get("chat", {}).get("id", ""))
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        log.warning(f"Ignoring message from unexpected chat_id: {chat_id}")
        return {"ok": True}

    reply_to = message.get("reply_to_message")
    if not reply_to:
        return {"ok": True}

    instruction = message.get("text", "").strip()
    if not instruction:
        return {"ok": True}

    original_message_id = str(reply_to.get("message_id", ""))
    original_text       = reply_to.get("text", "")
    customer            = telegram_context.get(original_message_id)
    phone               = customer["phone"] if customer else None

    if not phone or phone == "Nicht angegeben":
        match = re.search(r"Rueckrufnummer.*?:\s*(.+)", original_text)
        if match:
            phone = match.group(1).strip()

    if not phone or phone == "Nicht angegeben":
        await _send_telegram_message("Konnte Rueckrufnummer nicht ermitteln.")
        return {"ok": True}

    customer_name = (customer or {}).get("name", "Kunde")
    result = await trigger_outbound_call(phone, instruction)
    if result["success"]:
        await _send_telegram_message(
            f"\u2705 Anruf zu *{customer_name}* ({phone}) wird gestartet.\n"
            f"\U0001f4cb Nachricht: _{instruction}_"
        )
    else:
        reason = result.get("reason") or result.get("detail", "Unbekannter Fehler")
        await _send_telegram_message(f"\u274c Anruf zu {phone} fehlgeschlagen: {reason}")

    return {"ok": True}


@app.get("/calls")
@limiter.limit("30/minute")
async def get_calls(request: Request, x_api_key: Optional[str] = Header(None)):
    """Gibt die letzten Anrufe aus Airtable zurueck."""
    check_api_key(x_api_key)
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        raise HTTPException(status_code=503, detail="Airtable nicht konfiguriert")

    url = (
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}"
        f"?sort[0][field]=Datum&sort[0][direction]=desc&maxRecords=50"
    )
    headers = {"Authorization": f"Bearer {AIRTABLE_API_KEY}"}

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        data = resp.json()

    records = [{"id": r["id"], **r["fields"]} for r in data.get("records", [])]
    return {"total": len(records), "calls": records}


@app.get("/setup/telegram-webhook")
@limiter.limit("5/minute")
async def setup_telegram_webhook(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    webhook_url = "https://web-production-3812a.up.railway.app/webhook/telegram"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": webhook_url},
        )
        data = resp.json()
    log.info(f"setWebhook: {data}")
    return {"webhook_url": webhook_url, "telegram_response": data}


@app.get("/tickets")
@limiter.limit("30/minute")
async def get_tickets(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    return tickets


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
