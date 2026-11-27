from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from itertools import combinations_with_replacement, permutations
import json
from pathlib import Path
import re
import sqlite3
import unicodedata
from uuid import uuid4


TAIPEI_TIME = timezone(timedelta(hours=8))
OPEN_PERIODS = ((time(11, 30), time(14, 30)), (time(17), time(20)))
TABLES = {2: range(1, 7), 4: range(7, 13)}
MAX_PARTY_SIZE = sum(size * len(tables) for size, tables in TABLES.items())
SPLIT_OFFER_MINUTES = 10
MAX_SPLIT_OPTIONS = 13
DEFAULT_DURATION_MINUTES = 90
FULL_REPLY = "這個時段適合您人數的桌位已滿。請換個時間，並重新傳送完整訂位訊息。"

FIELD_NAMES = ("日期", "時間", "人數", "聯絡方式")
RELATIVE_DAYS = {"今天": 0, "明天": 1, "後天": 2, "大後天": 3}
ISO_DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
SLASH_DATE = r"[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}"
CHINESE_DIGITS = dict(zip("零〇一二兩三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
CHINESE_NONZERO = "[一二兩三四五六七八九]"
NUMBER = rf"(?:[0-9]{{1,3}}|{CHINESE_NONZERO}?十{CHINESE_NONZERO}?|[零〇]?{CHINESE_NONZERO}|[零〇])"
WEEKDAY = re.compile(r"(?P<week>這|本|下)?(?:週|周|星期|禮拜)\s*(?P<weekday>[一二三四五六日天])")
MONTH_DAY = re.compile(rf"(?P<month>{NUMBER})\s*月\s*(?P<month_day>{NUMBER})\s*[日號]")
PEOPLE_UNIT = r"(?:個人|個|人|位)"
PEOPLE_PREFIX = r"(?:(?:一共|總共|共)\s*)?"
CLOCK_24 = r"(?:[01]?[0-9]|2[0-3]):[0-5][0-9]"
SPOKEN_CLOCK = re.compile(
    rf"(?P<period>凌晨|早上|上午|中午|下午|傍晚|晚上)\s*(?P<hour>{NUMBER})\s*"
    rf"(?:[點時](?:\s*(?:(?P<minute>{NUMBER})\s*分|(?P<half>半)|(?P<bare_minute>[0-9]{{2}})(?=[\s,;、]|$)))?"
    r"|:\s*(?P<colon_minute>[0-9]{2}))"
)
AMBIGUOUS_CLOCK = rf"{NUMBER}\s*[點時](?:\s*(?:半|{NUMBER}\s*分))?"
SEPARATOR = r"[\s,;、]*"
CHINESE_CONTACT_LABEL = r"(?:聯絡方式|電話|手機|電子郵件|信箱)"
CONTACT_LABEL = rf"(?:{CHINESE_CONTACT_LABEL}|e-?mail)"
CONTACT_PREFIX = rf"(?:{CHINESE_CONTACT_LABEL}\s*(?:是|:)?\s*|e-?mail(?:\s*(?:是|:)\s*|\s+))?"
BOOKING_PREFIX = re.compile(r"^(?:(?:我(?:們)?(?:想|要)|請(?:幫我)?|幫我)\s*)?(?:訂(?:位|桌)?|預約)[\s,;、:]*")
LABEL = re.compile(rf"(日期|時間|人數|{CONTACT_LABEL})\s*:\s*", re.IGNORECASE)
POSITIONAL = re.compile(
    rf"(\S+)\s+(\S+)\s+({PEOPLE_PREFIX}{NUMBER}(?:\s*{PEOPLE_UNIT})?)\s+{CONTACT_PREFIX}([^\r\n]+)", re.IGNORECASE,
)
COMPACT_FIELDS = (
    rf"(?P<day>{ISO_DATE}|{SLASH_DATE}|{'|'.join(RELATIVE_DAYS)}|{WEEKDAY.pattern}|{MONTH_DAY.pattern})",
    rf"(?P<clock>{CLOCK_24}|{SPOKEN_CLOCK.pattern}|{AMBIGUOUS_CLOCK})",
    rf"(?P<people>{PEOPLE_PREFIX}{NUMBER}\s*{PEOPLE_UNIT})",
)
COMPACT_PATTERNS = tuple(
    re.compile(SEPARATOR.join(fields) + SEPARATOR + rf"{CONTACT_PREFIX}(?P<contact>[^\r\n]*)", re.IGNORECASE)
    for fields in permutations(COMPACT_FIELDS)
)
RESCHEDULE_TIME = re.compile(SEPARATOR.join(COMPACT_FIELDS[:2]))
BOOKING_REFERENCE = r"B([0-9]{1,12})"
CANCEL_COMMAND = re.compile(rf"取消(?:訂位)?\s*{BOOKING_REFERENCE}", re.IGNORECASE)
RESCHEDULE_COMMAND = re.compile(rf"(?:改期|更改訂位)\s*{BOOKING_REFERENCE}\s+(?:改成|改到|改為)?\s*(.+)", re.IGNORECASE)
EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
PHONE = re.compile(r"\+?[0-9() .-]+")


class ReservationFormatError(ValueError):
    pass


@dataclass(frozen=True)
class ReservationRequest:
    start: datetime
    people: int
    contact: str


@dataclass(frozen=True)
class SplitOffer:
    text: str
    offer_id: str
    options: tuple[tuple[int, ...], ...]


def booking_instructions() -> str:
    return (
        "請在同一則訊息填齊日期、時間、人數和聯絡方式（電話或電子郵件）。\n"
        "日期可填明天、後天、下週一、11月30日，或 YYYY-MM-DD。\n\n"
        "例如：明天晚上六點四個人guest@example.com\n\n"
        "也可以分行填寫：\n日期：明天\n時間：18:00\n人數：4\n聯絡方式：guest@example.com\n\n"
        "管理您的訂位：取消 B000001／改期 B000001 明天晚上六點"
    )


def _number(value: str) -> int:
    if value.isascii():
        return int(value)
    if "十" in value:
        tens, ones = value.split("十")
        return CHINESE_DIGITS.get(tens, 1) * 10 + CHINESE_DIGITS.get(ones, 0)
    return CHINESE_DIGITS[value[-1]]


def _reservation_time(value: str) -> time:
    if re.fullmatch(CLOCK_24, value):
        hour, minute = map(int, value.split(":"))
        return time(hour, minute)

    spoken = SPOKEN_CLOCK.fullmatch(value)
    if spoken is None:
        raise ReservationFormatError("時間請用 24 小時制（例如 18:00），或填像「晚上六點」這樣有時段的寫法。")
    hour = _number(spoken["hour"])
    minute_text = spoken["minute"] or spoken["bare_minute"] or spoken["colon_minute"] or "0"
    minute = 30 if spoken["half"] else _number(minute_text)
    period = spoken["period"]
    if spoken["colon_minute"] and period in ("下午", "傍晚", "晚上") and 13 <= hour <= 23 and minute <= 59:
        return time(hour, minute)
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise ReservationFormatError("請確認時間，例如晚上六點或晚上六點半。")

    if period == "中午":
        if hour not in (11, 12, 1, 2):
            raise ReservationFormatError("請確認中午的時間，也可以用 24 小時格式輸入。")
        hour = hour if hour >= 11 else hour + 12
    elif period == "晚上" and hour == 12:
        hour = 0
    elif period in ("下午", "傍晚", "晚上"):
        hour = hour % 12 + 12
    else:
        hour %= 12
    return time(hour, minute)


def _reservation_date(value: str, today: date) -> date:
    try:
        if value in RELATIVE_DAYS:
            return today + timedelta(days=RELATIVE_DAYS[value])
        weekday = WEEKDAY.fullmatch(value)
        if weekday:
            weekday_index = 6 if weekday["weekday"] in "日天" else CHINESE_DIGITS[weekday["weekday"]] - 1
            if weekday["week"] is None:
                offset = (weekday_index - today.weekday()) % 7
            else:
                offset = weekday_index - today.weekday() + (7 if weekday["week"] == "下" else 0)
            return today + timedelta(days=offset)
        month_day = MONTH_DAY.fullmatch(value)
        if month_day:
            return date(today.year, _number(month_day["month"]), _number(month_day["month_day"]))
        if re.fullmatch(SLASH_DATE, value):
            return date(*map(int, value.split("/")))
        if re.fullmatch(ISO_DATE, value):
            return date.fromisoformat(value)
    except (ValueError, OverflowError) as exc:
        raise ReservationFormatError("這個日期不存在，請檢查年月日。") from exc
    raise ReservationFormatError("日期請填明天、下週一、11月30日，或用 YYYY-MM-DD 格式。")


def _booking_fields(text: str) -> tuple[str, str, str, str]:
    for pattern in COMPACT_PATTERNS:
        compact = pattern.fullmatch(text)
        if compact:
            return compact.group("day", "clock", "people", "contact")

    if not LABEL.match(text):
        match = POSITIONAL.fullmatch(text)
        if match:
            return match.groups()
        raise ReservationFormatError("訂位資料不完整或格式有誤。")

    parts = LABEL.split(text)
    fields = {}
    for label, value in zip(parts[1::2], parts[2::2]):
        name = label if label in FIELD_NAMES else "聯絡方式"
        if name in fields:
            raise ReservationFormatError(f"「{name}」請只填一次。")
        fields[name] = value.strip(" \t\r\n,;、")
    missing = [name for name in FIELD_NAMES if not fields.get(name)]
    if missing:
        raise ReservationFormatError(f"還缺少{'、'.join(missing)}。")
    return tuple(fields[name] for name in FIELD_NAMES)


def _normalize_contact(contact: str) -> str:
    contact = contact.strip().removesuffix("。").strip()
    if not contact:
        raise ReservationFormatError("還缺少聯絡方式。")
    if PHONE.fullmatch(contact):
        digits = re.sub(r"[^0-9]", "", contact)
        if not 7 <= len(digits) <= 15:
            raise ReservationFormatError("請確認電話號碼是否完整。")
        contact = ("+" if contact.startswith("+") else "") + digits
    elif len(contact) > 254 or not EMAIL.fullmatch(contact):
        raise ReservationFormatError("聯絡方式請填有效的電話或電子郵件格式。")
    else:
        address, domain = contact.rsplit("@", 1)
        contact = f"{address}@{domain.lower()}"

    return contact


def parse_reservation(message: str, *, today: date | None = None) -> ReservationRequest:
    text = BOOKING_PREFIX.sub("", unicodedata.normalize("NFKC", message).strip(" \t\r\n,;、"), count=1)
    date_text, time_text, people_text, contact = _booking_fields(text)
    base_date = today if today is not None else datetime.now(TAIPEI_TIME).date()
    day = _reservation_date(date_text, base_date)
    clock = _reservation_time(time_text)
    party = re.fullmatch(rf"{PEOPLE_PREFIX}({NUMBER})(?:\s*{PEOPLE_UNIT})?", people_text)
    people = _number(party[1]) if party else 0
    if people == 0:
        raise ReservationFormatError("人數請填大於 0 的整數。")
    return ReservationRequest(datetime.combine(day, clock, TAIPEI_TIME), people, _normalize_contact(contact))


def _confirmation(
    booking_id: int, start: datetime, people: int, split: tuple[int, ...], *, existing: bool = False,
) -> str:
    title = "這筆訂位已成立" if existing else "訂位成功"
    text = (
        f"{title}\n訂位編號 B{booking_id:06d}\n"
        f"日期 {start:%Y-%m-%d}\n時間 {start:%H:%M}\n人數 {people} 位"
    )
    if len(split) > 1:
        text += "\n拆桌 " + "＋".join(str(group) for group in split)
    return text


def _assign_tables(split: tuple[int, ...], occupied: set[int]) -> tuple[tuple[int, int], ...]:
    small = [table for table in TABLES[2] if table not in occupied]
    large = [table for table in TABLES[4] if table not in occupied]
    large_needed = sum(group > 2 for group in split)
    if large_needed > len(large) or len(split) > len(small) + len(large):
        return ()
    large_tables = iter(large[:large_needed])
    small_tables = iter(small + large[large_needed:])
    return tuple((next(large_tables if group > 2 else small_tables), group) for group in split)


def _split_options(people: int, occupied: set[int]) -> tuple[tuple[int, ...], ...]:
    free_count = sum(len(tables) for tables in TABLES.values()) - len(occupied)
    options = [
        split
        for count in range((people + 3) // 4, min(free_count, people) + 1)
        for split in combinations_with_replacement((1, 2, 3, 4), count)
        if sum(split) == people and _assign_tables(split, occupied)
    ]
    options.sort(key=lambda split: (split.count(1), len(split), split.count(3), split))
    return tuple(options[:MAX_SPLIT_OPTIONS])


class ReservationBook:
    def __init__(self, path: Path, duration_minutes: int = DEFAULT_DURATION_MINUTES):
        if not 1 <= duration_minutes <= 180:
            raise ValueError("用餐時間必須介於 1 到 180 分鐘。")
        self.path = path
        self.duration_minutes = duration_minutes
        self.duration = timedelta(minutes=duration_minutes)
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(reservations)")}
            single_table = "table_id" in columns
            migrate = bool(columns) and "cancelled_at" not in columns
            if migrate:
                if not single_table:
                    db.execute("ALTER TABLE reservation_tables RENAME TO legacy_reservation_tables")
                db.execute("ALTER TABLE reservations RENAME TO legacy_reservations")
                db.execute("DROP INDEX IF EXISTS reservations_start_idx")
            db.execute("""CREATE TABLE IF NOT EXISTS reservations (
                    id INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    customer_id TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    people INTEGER NOT NULL CHECK (people BETWEEN 1 AND 36),
                    contact TEXT NOT NULL,
                    offer_id TEXT UNIQUE,
                    cancelled_at TEXT,
                    CHECK (ends_at > starts_at)
                )""")
            db.execute("CREATE INDEX IF NOT EXISTS reservations_start_idx ON reservations (starts_at)")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS reservations_active_unique
                          ON reservations (customer_id, starts_at, people, contact) WHERE cancelled_at IS NULL""")
            db.execute("""CREATE TABLE IF NOT EXISTS reservation_tables (
                    reservation_id INTEGER NOT NULL REFERENCES reservations(id) ON DELETE CASCADE,
                    table_id INTEGER NOT NULL CHECK (table_id BETWEEN 1 AND 12),
                    people INTEGER NOT NULL CHECK (people BETWEEN 1 AND 4),
                    CHECK (people <= 2 OR table_id >= 7),
                    PRIMARY KEY (reservation_id, table_id)
                )""")
            db.execute("""CREATE TABLE IF NOT EXISTS pending_splits (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    customer_id TEXT NOT NULL,
                    starts_at TEXT NOT NULL,
                    ends_at TEXT NOT NULL,
                    people INTEGER NOT NULL,
                    contact TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    options TEXT NOT NULL,
                    UNIQUE (customer_id, starts_at, people, contact)
                )""")
            db.execute("""CREATE TABLE IF NOT EXISTS reschedule_events (
                    event_id TEXT PRIMARY KEY,
                    reservation_id INTEGER NOT NULL REFERENCES reservations(id)
                )""")
            if migrate:
                offer = "NULL" if single_table else "offer_id"
                db.execute(f"""INSERT INTO reservations (id, event_id, customer_id, starts_at, ends_at, people, contact, offer_id)
                               SELECT id, event_id, customer_id, starts_at, ends_at, people, contact, {offer}
                               FROM legacy_reservations""")
                if single_table:
                    db.execute("""INSERT INTO reservation_tables (reservation_id, table_id, people)
                                  SELECT id, table_id, people FROM legacy_reservations""")
                else:
                    db.execute("""INSERT INTO reservation_tables (reservation_id, table_id, people)
                                  SELECT reservation_id, table_id, people FROM legacy_reservation_tables""")
                    db.execute("DROP TABLE legacy_reservation_tables")
                db.execute("DROP TABLE legacy_reservations")

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def reservations_for_day(self, day: date) -> list[dict]:
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT r.id, r.starts_at, r.ends_at, r.people, t.table_id, t.people
                   FROM reservations AS r
                   LEFT JOIN reservation_tables AS t ON t.reservation_id = r.id
                   WHERE r.cancelled_at IS NULL AND r.starts_at >= ? AND r.starts_at < ?
                   ORDER BY r.starts_at, r.id, t.people, t.table_id""",
                (f"{day.isoformat()}T00", f"{day.isoformat()}T24"),
            ).fetchall()

        bookings = {}
        for booking_id, starts_at, ends_at, people, table_id, group in rows:
            if booking_id not in bookings:
                start = datetime.fromisoformat(starts_at).astimezone(TAIPEI_TIME)
                end = datetime.fromisoformat(ends_at).astimezone(TAIPEI_TIME)
                bookings[booking_id] = {
                    "reference": f"B{booking_id:06d}", "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M"),
                    "period": "lunch" if start.time() < OPEN_PERIODS[1][0] else "dinner",
                    "people": people, "groups": [], "tables": [],
                }
            if group is not None:
                bookings[booking_id]["groups"].append(group)
                bookings[booking_id]["tables"].append({"size": 2 if table_id in TABLES[2] else 4, "people": group})
        return list(bookings.values())

    def upcoming_for_customer(
        self, customer_id: str, *, now: datetime, reference: str | None = None,
    ) -> list[tuple[str, datetime, int]]:
        if not customer_id:
            return []
        match = re.fullmatch(BOOKING_REFERENCE, reference, re.IGNORECASE) if reference else None
        if reference is not None and match is None:
            return []
        booking_id = int(match[1]) if match else None
        with closing(self._connect()) as db:
            rows = db.execute(
                """SELECT id, starts_at, people FROM reservations
                   WHERE customer_id = ? AND cancelled_at IS NULL AND starts_at > ?
                     AND (? IS NULL OR id = ?)
                   ORDER BY starts_at, id LIMIT 13""",
                (customer_id, now.astimezone(TAIPEI_TIME).isoformat(timespec="minutes"), booking_id, booking_id),
            ).fetchall()
        return [(f"B{row_id:06d}", datetime.fromisoformat(start), people) for row_id, start, people in rows]

    @staticmethod
    def _occupied_tables(
        db: sqlite3.Connection, starts_at: str, ends_at: str, *, exclude_id: int | None = None,
    ) -> set[int]:
        return {table for (table,) in db.execute(
            """SELECT reservation_tables.table_id FROM reservations
               JOIN reservation_tables ON reservation_tables.reservation_id = reservations.id
               WHERE reservations.cancelled_at IS NULL AND reservations.starts_at < ?
                 AND reservations.ends_at > ? AND (? IS NULL OR reservations.id != ?)""",
            (ends_at, starts_at, exclude_id, exclude_id),
        )}

    @staticmethod
    def _existing_confirmation(db: sqlite3.Connection, reservation: tuple[int, str, int, str | None]) -> str:
        booking_id, starts_at, people, cancelled_at = reservation
        if cancelled_at:
            return f"訂位 B{booking_id:06d} 已取消。"
        split = tuple(group for (group,) in db.execute(
            "SELECT people FROM reservation_tables WHERE reservation_id = ? ORDER BY people", (booking_id,),
        ))
        return _confirmation(booking_id, datetime.fromisoformat(starts_at), people, split, existing=True)

    @staticmethod
    def _save_booking(
        db: sqlite3.Connection, customer_id: str, event_id: str, request: ReservationRequest,
        ends_at: str, assignments: tuple[tuple[int, int], ...], *, offer_id: str | None = None,
    ) -> str:
        booking_id = db.execute(
            """INSERT INTO reservations (event_id, customer_id, starts_at, ends_at, people, contact, offer_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, customer_id, request.start.isoformat(timespec="minutes"), ends_at,
             request.people, request.contact, offer_id),
        ).lastrowid
        db.executemany(
            "INSERT INTO reservation_tables (reservation_id, table_id, people) VALUES (?, ?, ?)",
            ((booking_id, table, group) for table, group in assignments),
        )
        return _confirmation(booking_id, request.start, request.people, tuple(group for _, group in assignments))

    @staticmethod
    def _offer(offer_id: str, request: ReservationRequest, options: tuple[tuple[int, ...], ...]) -> SplitOffer:
        return SplitOffer(
            f"{request.start:%Y-%m-%d %H:%M} 的 {request.people} 位訂位需要拆桌。\n"
            f"請在 {SPLIT_OFFER_MINUTES} 分鐘內選擇每桌人數。點選後才會確認訂位。",
            offer_id,
            options,
        )

    def reply(
        self, customer_id: str, event_id: str, message: str, *,
        sent_on: date | None = None, now: datetime | None = None,
    ) -> str | SplitOffer:
        now = datetime.now(TAIPEI_TIME) if now is None else now.astimezone(TAIPEI_TIME)
        text = unicodedata.normalize("NFKC", message).strip()
        if text == "訂位":
            return booking_instructions()
        if text.startswith(("取消", "改期", "更改訂位")):
            if not customer_id:
                return "目前無法辨識您的 LINE 帳號，請聯絡餐廳處理。"
            cancel = CANCEL_COMMAND.fullmatch(text)
            if cancel:
                return self.cancel(f"B{cancel[1]}", customer_id=customer_id, now=now)
            reschedule = RESCHEDULE_COMMAND.fullmatch(text)
            if reschedule:
                slot = RESCHEDULE_TIME.fullmatch(reschedule[2].strip())
                if slot:
                    return self.reschedule(f"B{reschedule[1]}", slot["day"], slot["clock"], event_id,
                                           customer_id=customer_id, sent_on=sent_on, now=now)
            return "請輸入「取消 B000001」或「改期 B000001 明天晚上六點」，並換成您的訂位編號。"
        try:
            request = parse_reservation(message, today=sent_on or now.date())
        except ReservationFormatError as exc:
            return f"{exc}\n\n{booking_instructions()}"
        return self._reserve(customer_id, event_id, request, now)

    def _slot_error(self, start: datetime, now: datetime, *, allow_current: bool = False) -> str | None:
        if start < now.replace(second=0, microsecond=0) or (not allow_current and start <= now):
            return "這個訂位時間已過，請重新傳送未來的日期、時間、人數與聯絡方式。"

        start_clock = start.time()
        latest_seatings = [(datetime.combine(start.date(), closes) - self.duration).time() for _, closes in OPEN_PERIODS]
        if not any(opens <= start_clock <= latest for (opens, _), latest in zip(OPEN_PERIODS, latest_seatings)):
            return (
                f"營業時間為 11:30–14:30、17:00–20:00。每桌用餐時間 {self.duration_minutes} 分鐘，"
                f"午餐最晚可訂 {latest_seatings[0]:%H:%M}，晚餐最晚可訂 {latest_seatings[1]:%H:%M}。\n"
                "請重新傳送符合時段的完整訂位訊息。"
            )
        return None

    def add_manual(
        self, request_id: str, day: date, clock: str, people: int, contact: str, *, now: datetime | None = None,
    ) -> str | SplitOffer:
        now = datetime.now(TAIPEI_TIME) if now is None else now.astimezone(TAIPEI_TIME)
        try:
            start = datetime.combine(day, _reservation_time(clock), TAIPEI_TIME)
            clean_contact = _normalize_contact(unicodedata.normalize("NFKC", contact)) if contact.strip() else ""
        except ReservationFormatError as exc:
            return str(exc)
        manual_id = f"manual:{request_id}"
        return self._reserve(manual_id, manual_id, ReservationRequest(start, people, clean_contact), now, allow_current=True)

    def _reserve(
        self, customer_id: str, event_id: str, request: ReservationRequest, now: datetime, *, allow_current: bool = False,
    ) -> str | SplitOffer:
        if not 1 <= request.people <= MAX_PARTY_SIZE:
            return f"每筆訂位請填 1 至 {MAX_PARTY_SIZE} 位（最多 {MAX_PARTY_SIZE} 位）。"
        error = self._slot_error(request.start, now, allow_current=allow_current)
        if error:
            return error

        starts_at = request.start.isoformat(timespec="minutes")
        ends_at = (request.start + self.duration).isoformat(timespec="minutes")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM pending_splits WHERE expires_at <= ?", (now.isoformat(),))
            previous = db.execute(
                """SELECT id, starts_at, people, cancelled_at FROM reservations
                   WHERE event_id = ? OR
                         (cancelled_at IS NULL AND customer_id = ? AND starts_at = ? AND people = ? AND contact = ?)
                   ORDER BY event_id = ? DESC LIMIT 1""",
                (event_id, customer_id, starts_at, request.people, request.contact, event_id),
            ).fetchone()
            if previous:
                return self._existing_confirmation(db, previous)

            occupied = self._occupied_tables(db, starts_at, ends_at)
            if request.people <= 4:
                assignments = _assign_tables((request.people,), occupied)
                if not assignments:
                    return FULL_REPLY
                return self._save_booking(db, customer_id, event_id, request, ends_at, assignments)

            if not customer_id:
                return "拆桌訂位需要辨識您的 LINE 帳號，請使用一對一聊天室重新傳送，或聯絡餐廳。"
            options = _split_options(request.people, occupied)
            if not options:
                return FULL_REPLY

            offer_id = uuid4().hex
            deadline = now + timedelta(minutes=SPLIT_OFFER_MINUTES)
            expires_at = deadline if allow_current and request.start <= now else min(deadline, request.start)
            db.execute(
                """INSERT INTO pending_splits (id, event_id, customer_id, starts_at, ends_at, people, contact, expires_at, options)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (customer_id, starts_at, people, contact) DO UPDATE SET
                       id = excluded.id, event_id = excluded.event_id, ends_at = excluded.ends_at,
                       expires_at = excluded.expires_at, options = excluded.options""",
                (offer_id, event_id, customer_id, starts_at, ends_at, request.people, request.contact,
                 expires_at.isoformat(), json.dumps(options)),
            )
            return self._offer(offer_id, request, options)

    def choose_split(
        self, customer_id: str, offer_id: str, split: tuple[int, ...], *, now: datetime | None = None,
    ) -> str | SplitOffer:
        now = datetime.now(TAIPEI_TIME) if now is None else now.astimezone(TAIPEI_TIME)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT id, starts_at, people, cancelled_at FROM reservations WHERE offer_id = ? AND customer_id = ?",
                (offer_id, customer_id),
            ).fetchone()
            if previous:
                return self._existing_confirmation(db, previous)

            db.execute("DELETE FROM pending_splits WHERE expires_at <= ?", (now.isoformat(),))
            pending = db.execute(
                """SELECT event_id, starts_at, ends_at, people, contact, options FROM pending_splits
                   WHERE id = ? AND customer_id = ?""",
                (offer_id, customer_id),
            ).fetchone()
            if not pending:
                return "這組拆桌選項已失效。請重新傳送日期、時間、人數與聯絡方式。"
            event_id, starts_at, ends_at, people, contact, saved_options = pending
            if list(split) not in json.loads(saved_options):
                return "請點選訊息中的拆桌按鈕。若按鈕已消失，請重傳完整訂位資料。"

            request = ReservationRequest(datetime.fromisoformat(starts_at), people, contact)
            occupied = self._occupied_tables(db, starts_at, ends_at)
            assignments = _assign_tables(split, occupied)
            if not assignments:
                options = _split_options(people, occupied)
                if not options:
                    db.execute("DELETE FROM pending_splits WHERE id = ?", (offer_id,))
                    return FULL_REPLY
                db.execute("UPDATE pending_splits SET options = ? WHERE id = ?", (json.dumps(options), offer_id))
                return SplitOffer("原本的拆法已額滿，請從下方選擇其他可用拆法。", offer_id, options)

            confirmation = self._save_booking(db, customer_id, event_id, request, ends_at, assignments, offer_id=offer_id)
            db.execute("DELETE FROM pending_splits WHERE id = ?", (offer_id,))
            return confirmation

    @staticmethod
    def _reservation_to_change(db: sqlite3.Connection, reference: str, customer_id: str | None) -> tuple | None:
        match = re.fullmatch(BOOKING_REFERENCE, reference.strip(), re.IGNORECASE)
        if match is None:
            return None
        return db.execute(
            """SELECT id, starts_at, people, cancelled_at, customer_id, contact FROM reservations
               WHERE id = ? AND (? IS NULL OR customer_id = ?)""",
            (int(match[1]), customer_id, customer_id),
        ).fetchone()

    def cancel(
        self, reference: str, *, customer_id: str | None = None,
        expected_slot: str | None = None, now: datetime | None = None,
    ) -> str:
        now = datetime.now(TAIPEI_TIME) if now is None else now.astimezone(TAIPEI_TIME)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            reservation = self._reservation_to_change(db, reference, customer_id)
            if not reservation:
                return "找不到可操作的訂位，請確認訂位編號。"
            booking_id, starts_at, _, cancelled_at, _, _ = reservation
            if cancelled_at:
                return f"訂位 B{booking_id:06d} 已取消。"
            if customer_id is not None and datetime.fromisoformat(starts_at) <= now:
                return "訂位時間已到，請聯絡餐廳協助取消。"
            if expected_slot and datetime.fromisoformat(starts_at).strftime("%Y-%m-%dT%H:%M") != expected_slot:
                return "這筆訂位的時間已更新。請從「我的訂位」重新選擇。"
            db.execute("UPDATE reservations SET cancelled_at = ? WHERE id = ?", (now.isoformat(), booking_id))
            return f"訂位 B{booking_id:06d} 已取消，桌位已釋放。"

    def reschedule(
        self, reference: str, day: str, clock: str, event_id: str, *,
        customer_id: str | None = None, sent_on: date | None = None, now: datetime | None = None,
        expected_slot: str | None = None,
    ) -> str:
        now = datetime.now(TAIPEI_TIME) if now is None else now.astimezone(TAIPEI_TIME)
        try:
            start = datetime.combine(_reservation_date(day, sent_on or now.date()), _reservation_time(clock), TAIPEI_TIME)
        except ReservationFormatError as exc:
            return str(exc)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            reservation = self._reservation_to_change(db, reference, customer_id)
            if not reservation:
                return "找不到可操作的訂位，請確認訂位編號。"
            booking_id, old_start, people, cancelled_at, owner, contact = reservation
            previous = db.execute("SELECT reservation_id FROM reschedule_events WHERE event_id = ?", (event_id,)).fetchone()
            if previous:
                if previous[0] != booking_id:
                    return "這個改期請求已用於另一筆訂位，請重新送出。"
                return self._existing_confirmation(db, reservation[:4])
            if cancelled_at:
                return f"訂位 B{booking_id:06d} 已取消。"
            if customer_id is not None and datetime.fromisoformat(old_start) <= now:
                return "訂位時間已到，請聯絡餐廳協助改期。"
            if expected_slot and datetime.fromisoformat(old_start).strftime("%Y-%m-%dT%H:%M") != expected_slot:
                return "這筆訂位的時間已更新。請從「我的訂位」重新選擇。"
            error = self._slot_error(start, now)
            if error:
                return error

            starts_at = start.isoformat(timespec="minutes")
            ends_at = (start + self.duration).isoformat(timespec="minutes")
            duplicate = db.execute(
                """SELECT 1 FROM reservations WHERE id != ? AND cancelled_at IS NULL AND customer_id = ?
                   AND starts_at = ? AND people = ? AND contact = ?""",
                (booking_id, owner, starts_at, people, contact),
            ).fetchone()
            if duplicate:
                return "新時間已有一筆相同的訂位，原訂位仍保留。"
            split = tuple(group for (group,) in db.execute(
                "SELECT people FROM reservation_tables WHERE reservation_id = ? ORDER BY people", (booking_id,),
            ))
            occupied = self._occupied_tables(db, starts_at, ends_at, exclude_id=booking_id)
            assignments = _assign_tables(split, occupied)
            if not assignments:
                return "新時間沒有足夠的桌位容納原本的分桌方式，原訂位仍保留。請選另一個時間。"

            db.execute("UPDATE reservations SET starts_at = ?, ends_at = ? WHERE id = ?", (starts_at, ends_at, booking_id))
            db.execute("DELETE FROM reservation_tables WHERE reservation_id = ?", (booking_id,))
            db.executemany(
                "INSERT INTO reservation_tables (reservation_id, table_id, people) VALUES (?, ?, ?)",
                ((booking_id, table, group) for table, group in assignments),
            )
            db.execute("INSERT INTO reschedule_events (event_id, reservation_id) VALUES (?, ?)", (event_id, booking_id))
            return _confirmation(booking_id, start, people, split).replace("訂位成功", "改期成功", 1)
