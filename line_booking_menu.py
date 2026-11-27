from dataclasses import dataclass
from datetime import date, datetime
import re
import unicodedata

from reservations import booking_instructions, ReservationBook, SplitOffer, TAIPEI_TIME


MAX_LISTED_BOOKINGS = 12
SLOT_FORMAT = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}"
BOOKING_ACTION = re.compile(rf"booking:(show|ask-cancel|cancel|move):(B[0-9]{{1,12}})(?::({SLOT_FORMAT}))?")
MISSING_CUSTOMER = "目前無法辨識您的 LINE 帳號。請聯絡餐廳協助查詢訂位。"


@dataclass(frozen=True)
class BookingMenu:
    text: str
    actions: tuple[dict[str, str], ...]


def _message_button(label: str, text: str | None = None) -> dict[str, str]:
    return {"type": "message", "label": label, "text": text or label}


def _postback_button(label: str, data: str) -> dict[str, str]:
    return {"type": "postback", "label": label, "data": data}


def _move_button(label: str, reference: str, starts_at: datetime) -> dict[str, str]:
    return {"type": "datetimepicker", "label": label, "mode": "datetime",
            "data": f"booking:move:{reference}:{starts_at:%Y-%m-%dT%H:%M}"}


HOME = _message_button("功能選單")
MY_BOOKINGS = _message_button("我的訂位")
MAIN_BUTTONS = (_message_button("我要訂位", "訂位"), MY_BOOKINGS,
                _message_button("改期訂位", "改期"), _message_button("取消訂位", "取消"))


def _booking_line(reference: str, starts_at: datetime, people: int) -> str:
    return f"{reference}｜{starts_at:%Y-%m-%d %H:%M}｜{people} 位"


def _booking_list(book: ReservationBook, customer_id: str, kind: str) -> BookingMenu:
    if not customer_id:
        return BookingMenu(MISSING_CUSTOMER, (HOME,))
    records = book.upcoming_for_customer(customer_id, now=datetime.now(TAIPEI_TIME))
    if not records:
        return BookingMenu("目前沒有即將到來的訂位。", (_message_button("我要訂位", "訂位"), HOME))
    help_text = {
        "list": "以下是您的訂位，點選編號查看可用操作。",
        "cancel": "點選要取消的訂位編號，下一步會請您確認。",
        "move": "點選要改期的訂位編號，再選新日期和時間（台灣時間）。",
    }[kind]
    lines = [help_text]
    actions = []
    for reference, starts_at, people in records[:MAX_LISTED_BOOKINGS]:
        lines.append(_booking_line(reference, starts_at, people))
        if kind == "move":
            actions.append(_move_button(reference, reference, starts_at))
        else:
            action = "show" if kind == "list" else "ask-cancel"
            actions.append(_postback_button(reference, f"booking:{action}:{reference}"))
    if len(records) > MAX_LISTED_BOOKINGS:
        lines.append("先顯示最接近的 12 筆。其他訂位可用訂位編號輸入改期或取消指令。")
    return BookingMenu("\n".join(lines), (*actions, HOME))


def with_booking_menu(answer: str | SplitOffer) -> BookingMenu | SplitOffer:
    return answer if isinstance(answer, SplitOffer) else BookingMenu(answer, MAIN_BUTTONS)


def handle_booking_text(
    book: ReservationBook, customer_id: str, event_id: str, message: str, *, sent_on: date | None = None,
) -> BookingMenu | SplitOffer:
    text = unicodedata.normalize("NFKC", message).strip()
    if text in ("功能", "選單", "功能選單"):
        return BookingMenu("您可以在這裡訂位、查看自己的訂位、改期或取消。請選擇下方按鈕。", MAIN_BUTTONS)
    if text in ("訂位", "我要訂位"):
        return with_booking_menu(booking_instructions())
    if text in ("我的訂位", "查詢訂位"):
        return _booking_list(book, customer_id, "list")
    if text in ("取消", "取消訂位"):
        return _booking_list(book, customer_id, "cancel")
    if text in ("改期", "改期訂位", "更改訂位"):
        return _booking_list(book, customer_id, "move")
    return with_booking_menu(book.reply(customer_id, event_id, message, sent_on=sent_on))


def handle_booking_postback(
    book: ReservationBook, customer_id: str, event_id: str, data: str, selected_datetime: str | None,
) -> BookingMenu | None:
    action = BOOKING_ACTION.fullmatch(data)
    if action is None:
        return None
    if not customer_id:
        return BookingMenu(MISSING_CUSTOMER, (HOME,))
    kind, reference, expected_slot = action.groups()
    if kind in ("move", "cancel"):
        if not expected_slot:
            return BookingMenu("請從「我的訂位」重新選擇。", (MY_BOOKINGS, HOME))
        if kind == "cancel":
            message = book.cancel(reference, customer_id=customer_id, expected_slot=expected_slot)
        else:
            if not selected_datetime or not re.fullmatch(SLOT_FORMAT, selected_datetime):
                return BookingMenu("請重新點選改期並選擇完整日期與時間。", (MY_BOOKINGS, HOME))
            day, clock = selected_datetime.split("T")
            message = book.reschedule(reference, day, clock, event_id,
                                      customer_id=customer_id, expected_slot=expected_slot)
        return BookingMenu(message, (MY_BOOKINGS, HOME))

    records = book.upcoming_for_customer(customer_id, now=datetime.now(TAIPEI_TIME), reference=reference)
    if not records:
        return BookingMenu("找不到可操作的訂位，請重新查看。", (MY_BOOKINGS, HOME))
    booking = _booking_line(*records[0])
    starts_at = records[0][1]
    if kind == "ask-cancel":
        confirm = _postback_button("確認取消", f"booking:cancel:{reference}:{starts_at:%Y-%m-%dT%H:%M}")
        return BookingMenu(f"{booking}\n要取消這筆訂位並釋放桌位嗎？", (confirm, MY_BOOKINGS, HOME))
    return BookingMenu(f"{booking}\n改期時請選擇台灣時間。",
                       (_move_button("改期", reference, starts_at),
                        _postback_button("取消訂位", f"booking:ask-cancel:{reference}"), MY_BOOKINGS, HOME))
