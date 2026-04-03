import os
import hmac
import json
import hashlib
import logging
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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "agent_3901kn83d76tf72tvg450k0fb8ek")
PATEC_API_KEY = os.getenv("PATEC_API_KEY", "")
ELEVENLABS_WEBHOOK_SECRET = os.getenv("ELEVENLABS_WEBHOOK_SECRET", "")

tickets = []

# Rate limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["30/minute"])

app = FastAPI(title="PATEC Telefonagent API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# Security headers middleware
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


# API-key helper - always returns 403 (never 422) for missing/wrong key
def check_api_key(x_api_key: Optional[str]) -> None:
    if not PATEC_API_KEY:
        raise HTTPException(status_code=500, detail="PATEC_API_KEY not configured on server")
    if not x_api_key or not hmac.compare_digest(x_api_key, PATEC_API_KEY):
        raise HTTPException(status_code=403, detail="Forbidden: invalid or missing API key")


# Public endpoints (no auth)
@app.get("/health")
@limiter.limit("30/minute")
async def health(request: Request):
    return {"status": "ok", "tickets": len(tickets)}


@app.get("/")
@limiter.limit("30/minute")
async def root(request: Request):
    return {"message": "PATEC API laeuft", "version": "1.0"}


# Protected tool endpoints
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
    # Security: always send to the server-configured TELEGRAM_CHAT_ID.
    # Any chat_id supplied by the agent in the request body is intentionally ignored.
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"success": False, "reason": "nicht konfiguriert"}
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    text = f"Neuer Anruf PATEC - {now}"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        )
    return {"success": resp.status_code == 200}


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


# Webhook (HMAC-signed by ElevenLabs, no API-key)
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
            ELEVENLABS_WEBHOOK_SECRET.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, v0_sig):
            raise HTTPException(status_code=403, detail="Webhook signature mismatch")

    data = json.loads(body)
    log.info(f"Post-Call: {data.get('conversation_id', 'unknown')}")
    return {"status": "received"}


@app.get("/tickets")
@limiter.limit("30/minute")
async def get_tickets(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    return tickets


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
