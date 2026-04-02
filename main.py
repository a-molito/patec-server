import os
import httpx
import logging
from datetime import datetime
from fastapi import FastAPI, Request
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("patec")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "agent_3901kn83d76tf72tvg450k0fb8ek")

tickets = []

app = FastAPI(title="PATEC Telefonagent API")

@app.get("/health")
async def health():
    return {"status": "ok", "tickets": len(tickets)}

@app.get("/")
async def root():
    return {"message": "PATEC API laeuft", "version": "1.0"}

@app.post("/tools/save_ticket")
async def save_ticket(request: Request):
    data = await request.json()
    ticket = {"id": len(tickets) + 1, "timestamp": datetime.now().isoformat(), **data}
    tickets.append(ticket)
    return {"success": True, "ticket_id": ticket["id"]}

@app.post("/tools/send_telegram")
async def send_telegram(request: Request):
    data = await request.json()
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {"success": False, "reason": "nicht konfiguriert"}
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    text = "Neuer Anruf PATEC - " + now
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
        )
    return {"success": resp.status_code == 200}

@app.post("/tools/check_calendar")
async def check_calendar(request: Request):
    from datetime import date, timedelta
    slots = []
    d = date.today() + timedelta(days=1)
    day_names = ["Montag","Dienstag","Mittwoch","Donnerstag","Freitag"]
    while len(slots) < 8:
        if d.weekday() < 5:
            for h in [9,11,14,16]:
                slots.append({"date": f"{day_names[d.weekday()]}, {d.strftime('%d.%m.%Y')}", "time": f"{h:02d}:00-{h+1:02d}:00"})
        d += timedelta(days=1)
    return {"free_slots": slots[:6]}

@app.post("/webhook/post-call")
async def post_call_webhook(request: Request):
    data = await request.json()
    log.info(f"Post-Call: {data.get('conversation_id', 'unknown')}")
    return {"status": "received"}

@app.get("/tickets")
async def get_tickets():
    return tickets

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
