import argparse
import asyncio
import base64
from collections import Counter, OrderedDict
from datetime import date, datetime
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
from time import monotonic
from urllib.error import HTTPError, URLError
from urllib.request import Request as OutgoingRequest, urlopen

from dotenv import dotenv_values
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
import uvicorn

from codex_client import ask_codex, CodexError, find_codex
from dashboard_server import mount_dashboard
from line_booking_menu import BookingMenu, handle_booking_postback, handle_booking_text, with_booking_menu
from reservations import booking_instructions, DEFAULT_DURATION_MINUTES, ReservationBook, SplitOffer, TAIPEI_TIME


ROOT = Path(__file__).resolve().parent
LOGGER = logging.getLogger("uvicorn.error")
MAX_WEBHOOK_BYTES = 256 * 1024
MAX_INPUT_CHARACTERS = 3000
REPLY_URL = "https://api.line.me/v2/bot/message/reply"
SPLIT_POSTBACK = re.compile(r"split:([0-9a-f]{32}):([1-4](?:,[1-4]){1,11})")


def _split_label(split: tuple[int, ...]) -> str:
    label = "＋".join(str(group) for group in split)
    if len(label) <= 20:
        return label
    return "＋".join(f"{group}×{count}" if count > 1 else str(group) for group, count in Counter(split).items())


def _line_message_date(timestamp: object) -> date | None:
    if type(timestamp) is not int:
        return None
    try:
        return datetime.fromtimestamp(timestamp / 1000, TAIPEI_TIME).date()
    except (OverflowError, OSError, ValueError):
        return None


def send_reply(access_token: str, reply_token: str, answer: str | SplitOffer | BookingMenu) -> None:
    # LINE counts text limits in UTF-16 code units.
    text = answer.text if isinstance(answer, (SplitOffer, BookingMenu)) else answer
    message = {"type": "text", "text": text.encode("utf-16-le")[:9000].decode("utf-16-le", errors="ignore")}
    if isinstance(answer, SplitOffer):
        items = []
        for split in answer.options:
            label = _split_label(split)
            groups = ",".join(map(str, split))
            items.append({"type": "action", "action": {
                "type": "postback", "label": label, "displayText": f"選擇拆桌 {label}",
                "data": f"split:{answer.offer_id}:{groups}",
            }})
        message["quickReply"] = {"items": items}
    elif isinstance(answer, BookingMenu):
        message["quickReply"] = {"items": [{"type": "action", "action": action} for action in answer.actions]}
    request = OutgoingRequest(
        REPLY_URL,
        data=json.dumps({"replyToken": reply_token, "messages": [message]}).encode("utf-8"),
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=8):
            LOGGER.info("已透過 LINE Messaging API 回覆訊息。")
    except HTTPError as exc:
        LOGGER.warning("LINE 回覆失敗，HTTP %d", exc.code)
    except (URLError, OSError):
        LOGGER.warning("無法連線到 LINE 回覆 API。")


def create_app(
    secret: str,
    access_token: str,
    *,
    reservation_path: Path | None = None,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    dashboard_access_key: str | None = None,
    dashboard_site_origin: str | None = None,
    dashboard_management_key: str | None = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    book = ReservationBook(reservation_path or (ROOT / ".runtime" / "reservations.sqlite3"), duration_minutes)
    mount_dashboard(app, book, dashboard_access_key, dashboard_site_origin, dashboard_management_key)
    seen_events: OrderedDict[str, float] = OrderedDict()

    async def process_message(
        customer_id: str, event_id: str, reply_token: str, kind: str, content: str, sent_on: date | None,
        selected_datetime: str | None,
    ) -> None:
        try:
            if kind == "postback":
                selection = SPLIT_POSTBACK.fullmatch(content)
                if selection:
                    offer_id, groups = selection.groups()
                    split = tuple(int(group) for group in groups.split(","))
                    answer = with_booking_menu(await asyncio.to_thread(book.choose_split, customer_id, offer_id, split))
                else:
                    answer = await asyncio.to_thread(
                        handle_booking_postback, book, customer_id, event_id, content, selected_datetime,
                    )
                    if answer is None:
                        return
            elif len(content) > MAX_INPUT_CHARACTERS:
                answer = f"訂位訊息太長。\n\n{booking_instructions()}"
            else:
                answer = await asyncio.to_thread(
                    handle_booking_text, book, customer_id, event_id, content, sent_on=sent_on,
                )
        except (sqlite3.Error, OSError) as exc:
            LOGGER.error("讀寫訂位資料庫失敗（%s）。", type(exc).__name__)
            answer = "訂位暫時無法處理，請稍後重傳完整訂位訊息。"
        await asyncio.to_thread(send_reply, access_token, reply_token, answer)

    async def process_batch(messages: list[tuple[str, str, str, str, str, date | None, str | None]]) -> None:
        await asyncio.gather(*(process_message(*message) for message in messages))

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/webhook")
    async def webhook(request: Request, background_tasks: BackgroundTasks):
        signature = request.headers.get("x-line-signature", "").encode("utf-8")
        if not signature:
            raise HTTPException(status_code=401, detail="Missing LINE signature")

        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="Webhook too large")
            body.extend(chunk)

        expected = base64.b64encode(hmac.digest(secret.encode("utf-8"), body, "sha256"))
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(status_code=401, detail="Invalid LINE signature")

        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid webhook JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise HTTPException(status_code=400, detail="Missing webhook events")

        now = monotonic()
        while seen_events and next(iter(seen_events.values())) <= now - 600:
            seen_events.popitem(last=False)

        messages = []
        for event in payload["events"]:
            if not isinstance(event, dict) or event.get("mode") != "active":
                continue
            kind = event.get("type")
            if kind not in ("message", "postback"):
                continue
            source, data = event.get("source"), event.get(kind)
            if not isinstance(source, dict) or source.get("type") != "user" or not isinstance(data, dict):
                continue
            token, event_id = event.get("replyToken"), event.get("webhookEventId")
            if not all(isinstance(value, str) and value.strip() for value in (token, event_id)):
                continue
            if event_id in seen_events:
                continue
            seen_events[event_id] = now
            customer_id = source.get("userId")
            selected_datetime = None
            if kind == "postback":
                content = data.get("data")
                params = data.get("params")
                if isinstance(params, dict) and isinstance(params.get("datetime"), str):
                    selected_datetime = params["datetime"]
            else:
                content = data.get("text") if data.get("type") == "text" else ""
            if not isinstance(customer_id, str):
                customer_id = ""
            if not isinstance(content, str):
                content = ""
            sent_on = _line_message_date(event.get("timestamp")) if kind == "message" else None
            messages.append((customer_id, event_id, token, kind, content, sent_on, selected_datetime))

        if messages:
            background_tasks.add_task(process_batch, messages)
        return Response(status_code=200)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="LINE 餐廳訂位助理")
    commands = parser.add_subparsers(dest="command", required=True)
    smoke = commands.add_parser("ask", help="在本機測試 Codex 回覆")
    smoke.add_argument("message")
    serve = commands.add_parser("serve", help="啟動 LINE webhook")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    settings = {**dotenv_values(ROOT / ".env", encoding="utf-8-sig", interpolate=False), **os.environ}
    if args.command == "ask":
        try:
            executable = find_codex(settings.get("CODEX_BIN"))
            model = settings.get("CODEX_MODEL") or None
            print(ask_codex(executable, model, args.message))
            return 0
        except CodexError as exc:
            print(exc, file=sys.stderr)
            return 1

    secret = settings.get("LINE_CHANNEL_SECRET") or ""
    access_token = settings.get("LINE_CHANNEL_ACCESS_TOKEN") or ""
    if not secret or not access_token:
        parser.error("請依 README 填寫 .env 的 LINE_CHANNEL_SECRET 和 LINE_CHANNEL_ACCESS_TOKEN。")
    try:
        duration = int(settings.get("RESERVATION_DURATION_MINUTES") or DEFAULT_DURATION_MINUTES)
        if not 1 <= duration <= 180:
            raise ValueError
    except ValueError:
        parser.error("RESERVATION_DURATION_MINUTES 請填 1 到 180 之間的整數。")
    uvicorn.run(
        create_app(secret, access_token, duration_minutes=duration,
                   dashboard_access_key=settings.get("DASHBOARD_ACCESS_TOKEN"),
                   dashboard_site_origin=settings.get("DASHBOARD_SITE_ORIGIN"),
                   dashboard_management_key=settings.get("DASHBOARD_MANAGEMENT_TOKEN")),
        host="127.0.0.1", port=args.port,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
