"""WhatsApp webhook endpoints."""

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.config import get_settings
from app.services import whatsapp
from app.services.inbound import process_inbound, twilio_form_to_meta_payload

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    challenge = whatsapp.verify_subscription(hub_mode, hub_verify_token, hub_challenge)
    if challenge is None:
        raise HTTPException(status_code=403)
    return Response(content=challenge or "", media_type="text/plain")


@router.post("/webhook")
async def receive_webhook(request: Request) -> dict[str, str]:
    body = await request.body()
    if not whatsapp.verify_signature(body, request.headers.get("x-hub-signature-256")):
        logger.warning("webhook signature mismatch")
        raise HTTPException(status_code=401)

    pool = request.app.state.pool
    coalescer = getattr(request.app.state, "coalescer", None)
    task = asyncio.create_task(process_inbound(pool, json.loads(body), coalescer))
    background_tasks = request.app.state.background_tasks
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return {"status": "ok"}


@router.post("/twilio/webhook")
async def receive_twilio_webhook(request: Request) -> Response:
    form_data = await request.form()
    form = {key: str(value) for key, value in form_data.items()}
    settings = get_settings()
    url = settings.twilio_webhook_url or str(request.url)
    if not whatsapp.verify_twilio_signature(url, form, request.headers.get("x-twilio-signature")):
        logger.warning("twilio webhook signature mismatch")
        raise HTTPException(status_code=401)

    pool = request.app.state.pool
    coalescer = getattr(request.app.state, "coalescer", None)
    task = asyncio.create_task(process_inbound(pool, twilio_form_to_meta_payload(form), coalescer))
    background_tasks = request.app.state.background_tasks
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return Response(content="<Response></Response>", media_type="application/xml")
