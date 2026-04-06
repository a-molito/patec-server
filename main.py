import os
import re
import uuid
import hmac
import json
import hashlib
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote
from fastapi import FastAPI, Request, Header, HTTPExcepion
from starlette.middleware.base import BaseHTTPMiddleware
import httpx
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("patec")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "agent_3901kn83d76tf72tvg450k0fb8ek")
PATEC_API_KEY = os.getenv("PATEC_API_KEY", "")
ELEVENLABS_WEBHOOK_SECRET = os.getenv("ELEVENLABS_WEBHOOK_SECRET", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_PHONE_NUMBER_ID = os.getenv("ELEVENLABS_PHONE_NUMBER_ID", "")

AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY", "")
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_CALLS = "📞 Anrufe"
AIRTABLE_TABLE_CUSTOMERS = "👥 Kunden"
AIRTABLE_TABLE_TICKETS = "🎫 Tickets"

telegram_context: dict = {}
outbound_conversations: set = set()  # Speichert Conversation-IDs ausgehender Anrufe

limiter = Limiter(key_func=get_remote_address, default_limits=["30/minute"])
app = FastAPI(title="PATEC Telefonagent API v3")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(SecurityHeadersMiddleware)


def check_api_key(x_api_key: Optional[str]) -> None:
    if not PATEC_API_KEY:
        raise HTTPException(status_code=500, detail="PATEC_API_KEY not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, PATEC_API_KEY):
        raise HTTPException(status_code=403, detail="Forbidden: invalid or missing API key")


def normalize_phone_e164(phone: str, default_country: str = "+49") -> str:
    digits = re.sub(r"[^\d+]", "", phone.strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif digits.startswith("+"):
        pass
    elif digits.startswith("0"):
        digits = default_country + digits[1:]
    else:
        digits = default_country.lstrip("+") + digits
    if not digits.startswith("+"):
        digits = "+" + digits
    return digits


def _detect_priority(text: str) -> str:
    t = text.lower()
    high_kw = ["dringend", "notfall", "notruf", "kaputt", "defekt", "ausgefallen",
                "kein wasser", "kein strom", "rohrbruch", "gefahr", "brand", "sofort", "gas", "spinnt"]
    low_kw = ["frage", "info", "information", "termin", "anfrage", "beratung", "angebot", "allgemein"]
    if any(kw in t for kw in high_kw):
        return "Hoch"
    if any(kw in t for kw in low_kw):
        return "Niedrig"
    return "Mittel"


async def _send_telegram_message(text: str) -> Optional[int]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram nicht konfiguriert: TOKEN=%s CHAT=%s", bool(TELEGRAM_BOT_TOKEN), bool(TELEGRAM_CHAT_ID))
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        )
        log.info(f"Telegram sendMessage: {resp.status_code} {resp.text[:200]}")
        if resp.status_code == 200:
            return resp.json().get("result", {}).get("message_id")
        return None


def _airtable_headers() -> dict:
    return {"Authorization": f"Bearer {AIRTABLE_API_KEY}", "Content-Type": "application/json"}


def _airtable_url(table_name: str) -> str:
    return f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{quote(table_name, safe='')}"


async def _airtable_search(table_name: str, formula: str, max_records: int = 10) -> list:
    params = f"?filterByFormula={quote(formula, safe='')}&maxRecords={max_records}"
    url = _airtable_url(table_name) + params
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=_airtable_headers())
            if resp.status_code == 200:
                return resp.json().get("records", [])
            log.error(f"Airtable search error {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        log.error(f"Airtable search exception: {e}")
    return []


async def _airtable_create(table_name: str, fields: dict) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _airtable_url(table_name),
                headers=_airtable_headers(),
                json={"fields": fields}
            )
            if resp.status_code in (200, 201):
                return resp.json().get("id")
            log.error(f"Airtable create error {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        log.error(f"Airtable create exception: {e}")
    return None


async def _airtable_update(table_name: str, record_id: str, fields: dict) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                f"{_airtable_url(table_name)}/{record_id}",
                headers=_airtable_headers(),
                json={"fields": fields}
            )
            return resp.status_code in (200, 201)
    except Exception as e:
        log.error(f"Airtable update exception: {e}")
        return False


async def upsert_customer(phone: str, name: str = None, update_call_count: bool = True) -> Optional[str]:
    if not phone:
        return None
    norm_phone = normalize_phone_e164(phone)
    formula = f"{{Telefon}}='{norm_phone}'"
    records = await _airtable_search(AIRTABLE_TABLE_CUSTOMERS, formula, max_records=1)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    if records:
        record = records[0]
        record_id = record["id"]
        current_count = record.get("fields", {}).get("Anruf-Anzahl", 0) or 0
        update_fields = {"Letzter Anruf": now_iso}
        if update_call_count:
            update_fields["Anruf-Anzahl"] = current_count + 1
        if name and not record.get("fields", {}).get("Name"):
            update_fields["Name"] = name
        await _airtable_update(AIRTABLE_TABLE_CUSTOMERS, record_id, update_fields)
        return record_id
    else:
        fields = {
            "Telefon": norm_phone,
            "Erstanruf": now_iso,
            "Letzter Anruf": now_iso,
            "Anruf-Anzahl": 1 if update_call_count else 0,
        }
        if name:
            fields["Name"] = name
        return await _airtable_create(AIRTABLE_TABLE_CUSTOMERS, fields)


async def log_to_airtable(call_data: dict):
    if not AIRTABLE_API_KEY or not AIRTABLE_BASE_ID:
        return None
    anruf_id = call_data.get("conversation_id") or str(uuid.uuid4())[:8].upper()
    combined_text = f"{call_data.get('anliegen','')} {call_data.get('summary','')}"
    prioritaet = call_data.get("prioritaet_override") or _detect_priority(combined_text)
    rueckruf = call_data.get("rueckruf_override")
    if rueckruf is None:
        rueckruf = any(kw in combined_text.lower() for kw in ["rueckruf", "zurueckrufen", "callback", "ruf mich"])
    datum_iso = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    fields = {
        "Anruf-ID": anruf_id,
        "Name": call_data.get("name", "Unbekannt"),
        "Telefon": call_data.get("phone", ""),
        "Anliegen": call_data.get("anliegen", ""),
        "Datum": datum_iso,
        "Dauer (Sek.)": int(call_data.get("duration_secs", 0)),
        "Zusammenfassung": call_data.get("summary", ""),
        "Status": "Neu",
        "Prioritaet": prioritaet,
        "Rueckruf erforderlich": rueckruf,
        "Anruftyp": call_data.get("anruftyp", "eingehend"),
        "Notizen": call_data.get("notizen", ""),
    }
    return await _airtable_create(AIRTABLE_TABLE_CALLS, fields)


async def trigger_outbound_call(phone: str, instruction: str) -> dict:
    if not ELEVENLABS_API_KEY:
        return {"success": False, "reason": "ELEVENLABS_API_KEY nicht konfiguriert"}
    if not ELEVENLABS_PHONE_NUMBER_ID:
        return {"success": False, "reason": "ELEVENLABS_PHONE_NUMBER_ID nicht konfiguriert"}
    phone = normalize_phone_e164(phone)
    payload = {
        "agent_id": ELEVENLABS_AGENT_ID,
        "agent_phone_number_id": ELEVENLABS_PHONE_NUMBER_ID,
        "to_number": phone,
        "conversation_initiation_client_data": {
            "dynamic_variables": {"owner_instruction": instruction},
            "conversation_config_override": {
                "agent": {
                    "first_message": "Guten Tag, hier ist der Telefonassistent von PATEC. Schön, dass ich Sie erreiche. Haben Sie kurz einen Moment?"
                }
            }
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.elevenlabs.io/v1/convai/twilio/outbound-call",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json=payload,
            )
            log.info(f"Outbound call {resp.status_code}: {resp.text[:200]}")
            if resp.status_code in (200, 201, 202):
                try:
                    body = resp.json()
                    if body.get("success") is False:
                        return {"success": False, "reason": body.get("message", "Unbekannter Fehler")}
                    conv_id = body.get("conversation_id", "") or body.get("callSid", "")
                    if conv_id:
                        outbound_conversations.add(conv_id)
                    return {"success": True, "data": body}
                except Exception:
                    return {"success": True, "data": {}}
            return {"success": False, "status_code": resp.status_code, "detail": resp.text}
    except Exception as e:
        return {"success": False, "reason": str(e)}


# ═══════════════════════════════════════════════════════════════
# ENDPOINTS
# ═══════════════════════════════════════════════════════════════

@app.get("/health")
@limiter.limit("30/minute")
async def health(request: Request):
    return {"status": "ok", "version": "3.0"}


@app.get("/")
@limiter.limit("30/minute")
async def root(request: Request):
    return {"message": "PATEC API laeuft", "version": "3.0"}


@app.post("/tools/lookup_customer")
@limiter.limit("30/minute")
async def lookup_customer(request: Request, x_api_key: Optional[str] = Header(None)):
    """Sucht Kunden anhand der Telefonnummer. Gibt Kundeninfo + letzte Anrufe zurueck."""
    check_api_key(x_api_key)
    data = await request.json()
    # Support both old "phone" field and new caller_id/called_number pair
    caller_id_raw = data.get("caller_id", data.get("phone", "")).strip()
    called_number_raw = data.get("called_number", "").strip()

    # Determine which number belongs to the CUSTOMER
    # For inbound: caller_id = customer, called_number = PATEC
    # For outbound: caller_id = PATEC, called_number = customer
    PATEC_NUMBERS = ("+498941432021", "+4989414")
    def is_patec_number(n):
        return any(n.startswith(p) for p in PATEC_NUMBERS)

    if caller_id_raw and not is_patec_number(normalize_phone_e164(caller_id_raw)):
        phone_raw = caller_id_raw
    elif called_number_raw and not is_patec_number(normalize_phone_e164(called_number_raw)):
        phone_raw = called_number_raw
    else:
        phone_raw = caller_id_raw  # fallback

    if not phone_raw:
        return {"found": False, "error": "Keine Telefonnummer angegeben"}

    phone = normalize_phone_e164(phone_raw)
    log.info(f"lookup_customer: {phone_raw!r} -> {phone!r}")

    records = await _airtable_search(AIRTABLE_TABLE_CUSTOMERS, f"{{Telefon}}='{phone}'", max_records=1)
    recent_calls_raw = await _airtable_search(
        AIRTABLE_TABLE_CALLS,
        f"{{Telefon}}='{phone}'",
        max_records=5
    )
    recent_calls = []
    for r in recent_calls_raw:
        f = r.get("fields", {})
        recent_calls.append({
            "datum": f.get("Datum", ""),
            "anliegen": f.get("Anliegen", ""),
            "zusammenfassung": (f.get("Zusammenfassung") or "")[:200],
        })

    if records:
        customer = records[0]
        fields = customer.get("fields", {})
        return {
            "found": True,
            "customer_id": customer["id"],
            "name": fields.get("Name", ""),
            "telefon": phone,
            "hat_pv_anlage": fields.get("Hat PV-Anlage", False),
            "anlage_info": fields.get("Anlage-Info", ""),
            "anruf_anzahl": fields.get("Anruf-Anzahl", 0),
            "notizen": fields.get("Notizen", ""),
            "letzte_anrufe": recent_calls,
        }
    return {
        "found": False,
        "telefon": phone,
        "letzte_anrufe": recent_calls,
    }


@app.post("/tools/create_ticket")
@limiter.limit("30/minute")
async def create_ticket(request: Request, x_api_key: Optional[str] = Header(None)):
    """Erstellt ein Ticket in Airtable und sendet Telegram-Benachrichtigung."""
    check_api_key(x_api_key)
    data = await request.json()

    titel = data.get("titel", "Neues Anliegen")
    beschreibung = data.get("beschreibung", "")
    kategorie = data.get("kategorie", "Sonstiges")
    prioritaet = data.get("prioritaet") or _detect_priority(f"{titel} {beschreibung}")
    kunden_name = data.get("kunden_name", "Unbekannt")
    kunden_telefon = data.get("kunden_telefon", "")
    # Fallback: use caller_id/called_number if kunden_telefon not provided
    if not kunden_telefon:
        _ci = data.get("caller_id", "")
        _cn = data.get("called_number", "")
        _patec = "+498941432021"
        if _ci and normalize_phone_e164(_ci) != _patec:
            kunden_telefon = _ci
        elif _cn and normalize_phone_e164(_cn) != _patec:
            kunden_telefon = _cn
    gewuenschter_termin = data.get("gewuenschter_termin", "")
    anruf_id = data.get("anruf_id", "")
    zustaendig = data.get("zustaendig", "Aki Paleopanis")

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    norm_phone = normalize_phone_e164(kunden_telefon) if kunden_telefon else ""

    ticket_fields = {
        "Titel": titel,
        "Beschreibung": beschreibung,
        "Status": "Neu",
        "Priorität": prioritaet,
        "Kategorie": kategorie,
        "Kunden-Name": kunden_name,
        "Kunden-Telefon": norm_phone,
        "Erstellt am": now_iso,
        "Zuletzt aktualisiert": now_iso,
        "Zuständig": zustaendig,
    }
    if gewuenschter_termin:
        ticket_fields["Gewünschter Termin"] = gewuenschter_termin
    if anruf_id:
        ticket_fields["Anruf-ID"] = anruf_id

    ticket_id = await _airtable_create(AIRTABLE_TABLE_TICKETS, ticket_fields)
    if norm_phone:
        asyncio.create_task(upsert_customer(norm_phone, kunden_name, update_call_count=False))

    prio_emoji = {"Hoch": "🔴", "Mittel": "🟡", "Niedrig": "🟢"}.get(prioritaet, "⚪")
    kat_emoji = {
        "PV-Anlage": "☀️", "Elektrotechnik": "⚡", "SHK": "🔧",
        "Termin": "📅", "Beratung / Angebot": "💬", "Chef-Rückruf": "👤", "Sonstiges": "📋"
    }.get(kategorie, "📋")
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    termin_line = f"\n📅 *Gewünschter Termin:* {gewuenschter_termin}" if gewuenschter_termin else ""

    tg_text = (
        f"{kat_emoji} *Neues Ticket: {titel}*\n"
        f"⏰ {now_str}\n\n"
        f"👤 *Name:* {kunden_name}\n"
        f"📱 *Telefon:* {norm_phone or 'nicht angegeben'}\n"
        f"{prio_emoji} *Priorität:* {prioritaet}\n"
        f"📂 *Kategorie:* {kategorie}"
        f"{termin_line}\n\n"
        f"📝 *Beschreibung:*\n{beschreibung}"
    )

    message_id = await _send_telegram_message(tg_text)
    if message_id and norm_phone:
        telegram_context[str(message_id)] = {"name": kunden_name, "phone": norm_phone, "issue": titel}

    return {"success": True, "ticket_id": ticket_id, "telegram_sent": message_id is not None}


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
            for h in [8, 10, 14, 16]:
                slots.append({
                    "date": f"{day_names[d.weekday()]}, {d.strftime('%d.%m.%Y')}",
                    "time": f"{h:02d}:00-{h+1:02d}:00",
                })
        d += timedelta(days=1)
    return {"free_slots": slots[:6]}


@app.post("/tools/send_telegram")
@limiter.limit("30/minute")
async def send_telegram(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    data = await request.json()
    name = data.get("name") or "Nicht angegeben"
    phone = data.get("phone") or "Nicht angegeben"
    issue = data.get("issue") or "Nicht angegeben"
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    text = (
        f"📞 *Neuer Anruf bei PATEC*\n⏰ {now}\n\n"
        f"👤 *Name:* {name}\n📱 *Rückrufnummer:* {phone}\n🔧 *Anliegen:* {issue}"
    )
    message_id = await _send_telegram_message(text)
    if message_id:
        telegram_context[str(message_id)] = {"name": name, "phone": phone, "issue": issue}
    return {"success": message_id is not None}


@app.post("/tools/save_ticket")
@limiter.limit("30/minute")
async def save_ticket(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    return {"success": True, "message": "Bitte create_ticket verwenden"}


@app.post("/webhook/post-call")
@limiter.limit("30/minute")
async def post_call_webhook(request: Request):
    body = await request.body()
    if ELEVENLABS_WEBHOOK_SECRET:
        sig_header = request.headers.get("ElevenLabs-Signature", "")
        if not sig_header:
            raise HTTPException(status_code=403, detail="Missing ElevenLabs-Signature header")
        try:
            parts = dict(p.split("=", 1) for p in sig_header.split(","))
            timestamp = parts["t"]
            v0_sig = parts["v0"]
        except (KeyError, ValueError):
            raise HTTPException(status_code=403, detail="Invalid ElevenLabs-Signature format")
        message = f"{timestamp}.{body.decode()}"
        expected = hmac.new(
            ELEVENLABS_WEBHOOK_SECRET.encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, v0_sig):
            raise HTTPException(status_code=403, detail="Webhook signature mismatch")

    data = json.loads(body)
    payload = data.get("data", data)
    conversation_id = payload.get("conversation_id", "unknown")
    log.info(f"Post-Call webhook: {conversation_id}")

    metadata = payload.get("metadata", {})
    duration = metadata.get("call_duration_secs") or payload.get("call_duration_secs", 0)
    analysis = payload.get("analysis", {})
    transcript_summary = analysis.get("transcript_summary") or analysis.get("summary", "")
    transcript = payload.get("transcript", [])
    if not transcript_summary and transcript:
        lines = [f"{m.get('role','?').capitalize()}: {m.get('message','')}" for m in transcript[:2]]
        if len(transcript) > 2:
            lines.append(f"{transcript[-1].get('role','?').capitalize()}: {transcript[-1].get('message','')}")
        transcript_summary = "\n".join(lines)

    data_coll = analysis.get("data_collection_results") or {}
    caller_name = (data_coll.get("customer_name") or {}).get("value", "")
    caller_phone = (
        (data_coll.get("customer_phone") or {}).get("value", "") or
        payload.get("caller_id", "") or
        metadata.get("caller_id", "")
    )
    caller_issue = (data_coll.get("issue_type") or {}).get("value", "")
    urgency_str = (data_coll.get("urgency") or {}).get("value", "")
    callback_str = (data_coll.get("callback_requested") or {}).get("value", "")

    prioritaet_override = {"high": "Hoch", "medium": "Mittel", "low": "Niedrig"}.get(
        (urgency_str or "").lower(), None
    )
    rueckruf_override = (callback_str in (True, "yes", "true", "ja", "1")) if callback_str != "" else None
    norm_phone = normalize_phone_e164(caller_phone) if caller_phone else ""

    anruftyp = "ausgehend" if conversation_id in outbound_conversations else "eingehend"
    outbound_conversations.discard(conversation_id)
    asyncio.create_task(log_to_airtable({
        "conversation_id": conversation_id,
        "name": caller_name,
        "phone": norm_phone,
        "anliegen": caller_issue,
        "summary": transcript_summary,
        "duration_secs": duration,
        "prioritaet_override": prioritaet_override,
        "rueckruf_override": rueckruf_override,
        "anruftyp": anruftyp,
    }))
    if norm_phone:
        asyncio.create_task(upsert_customer(norm_phone, caller_name, update_call_count=True))

    return {"status": "received"}


@app.post("/webhook/telegram")
@limiter.limit("30/minute")
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message", {})
    if not message:
        return {"ok": True}
    chat_id = str(message.get("chat", {}).get("id", ""))
    if TELEGRAM_CHAT_ID and chat_id != str(TELEGRAM_CHAT_ID):
        return {"ok": True}
    reply_to = message.get("reply_to_message")
    if not reply_to:
        return {"ok": True}
    instruction = message.get("text", "").strip()
    if not instruction:
        return {"ok": True}

    original_message_id = str(reply_to.get("message_id", ""))
    original_text = reply_to.get("text", "")
    customer = telegram_context.get(original_message_id)
    phone = customer["phone"] if customer else None

    if not phone or phone == "Nicht angegeben":
        match = re.search(r"(?:Telefon|Rückrufnummer|Tel\.?|Nummer)[^:\n]*:\s*\*?\s*(\+?[\d\s\-\/]+)", original_text)
        if match:
            phone = re.sub(r"[\s\-\/]", "", match.group(1).strip().strip("*").strip())

    if not phone or phone in ("Nicht angegeben", "", "Kein"):
        await _send_telegram_message("❌ Konnte Rückrufnummer nicht ermitteln.")
        return {"ok": True}

    customer_name = (customer or {}).get("name", "Kunde")
    await _send_telegram_message(
        f"📞 Starte Anruf zu *{customer_name}* ({phone})…\n📋 Anweisung: _{instruction}_"
    )
    result = await trigger_outbound_call(phone, instruction)
    if result["success"]:
        await _send_telegram_message(f"✅ Anruf zu *{customer_name}* ({phone}) erfolgreich gestartet.")
    else:
        reason = result.get("reason") or result.get("detail", "Unbekannter Fehler")
        await _send_telegram_message(f"❌ Anruf zu {phone} fehlgeschlagen.\nGrund: {reason}")
    return {"ok": True}


@app.get("/calls")
@limiter.limit("30/minute")
async def get_calls(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    url = f"{_airtable_url(AIRTABLE_TABLE_CALLS)}?sort[0][field]=Datum&sort[0][direction]=desc&maxRecords=50"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_airtable_headers())
        data = resp.json()
        return {"total": len(data.get("records", [])), "calls": [{"id": r["id"], **r["fields"]} for r in data.get("records", [])]}


@app.get("/tickets")
@limiter.limit("30/minute")
async def get_tickets_endpoint(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    url = f"{_airtable_url(AIRTABLE_TABLE_TICKETS)}?sort[0][field]=Erstellt am&sort[0][direction]=desc&maxRecords=50"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_airtable_headers())
        data = resp.json()
        return {"total": len(data.get("records", [])), "tickets": [{"id": r["id"], **r["fields"]} for r in data.get("records", [])]}


@app.get("/customers")
@limiter.limit("30/minute")
async def get_customers(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    url = f"{_airtable_url(AIRTABLE_TABLE_CUSTOMERS)}?sort[0][field]=Letzter Anruf&sort[0][direction]=desc&maxRecords=100"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_airtable_headers())
        data = resp.json()
        return {"total": len(data.get("records", [])), "customers": [{"id": r["id"], **r["fields"]} for r in data.get("records", [])]}


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
    return {"webhook_url": webhook_url, "telegram_response": resp.json()}


@app.get("/setup/telegram-test")
@limiter.limit("5/minute")
async def test_telegram(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    async with httpx.AsyncClient(timeout=10) as client:
        bot_resp = await client.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe")
        webhook_resp = await client.get(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo")
    msg_id = await _send_telegram_message("🔔 PATEC Test-Nachricht — Telegram funktioniert! ✅")
    return {
        "token_configured": bool(TELEGRAM_BOT_TOKEN),
        "chat_id": TELEGRAM_CHAT_ID,
        "bot_info": bot_resp.json(),
        "webhook_info": webhook_resp.json(),
        "test_message_sent": msg_id is not None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
