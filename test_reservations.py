from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
import unittest
from uuid import uuid4

from reservations import parse_reservation, ReservationBook, SplitOffer, TAIPEI_TIME


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 27, 10, tzinfo=TAIPEI_TIME)
        self.path = Path(__file__).resolve().parent / ".runtime" / f"restaurant-test-{uuid4().hex}.sqlite3"
        self.addCleanup(self.path.unlink, missing_ok=True)
        self.book = ReservationBook(self.path)

    def reserve(self, key, hour, people, *, day="2026-11-28"):
        return self.book.reply(key, f"event-{key}", f"{day} {hour} {people} 0912345678", now=self.now)

    def test_required_fields_and_contact(self):
        accepted = parse_reservation("人數：２ 人，日期：２０２６-１１-２８，時間：１２：００，電話：0912-345-678")
        self.assertEqual((accepted.people, accepted.contact), (2, "0912345678"))
        self.assertEqual(parse_reservation("2026-11-28 12:00  2   a@EXAMPLE.COM").contact, "a@example.com")

        invalid_messages = (
            ("日期:2026-11-28 時間:12:00 人數:2", "還缺少聯絡方式"),
            ("2026-02-30 12:00 2 0912345678", "日期不存在"),
            ("2026-11-28 27:00 2 0912345678", "24 小時制"),
            ("2026-11-28 12:00 0 0912345678", "大於 0"),
            ("2026-11-28 12:00 2 invalid@example", "電話或電子郵件"),
        )
        for message, expected in invalid_messages:
            with self.subTest(message=message):
                reply = self.book.reply("customer", "invalid", message, now=self.now)
                self.assertIn(expected, reply)
                self.assertIn("請在同一則訊息填齊", reply)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 0)

    def test_hours_party_size_and_duration(self):
        for i, time in enumerate(("11:29", "13:01", "14:30", "16:59", "18:31", "20:00")):
            with self.subTest(time=time):
                self.assertIn("營業時間", self.reserve(f"invalid-{i}", time, 2))
        for i, time in enumerate(("11:30", "13:00", "17:00", "18:30")):
            with self.subTest(time=time):
                self.assertIn("訂位成功", self.reserve(f"valid-{i}", time, 2))
        self.assertIn("時間已過", self.reserve("past", "12:00", 2, day="2026-11-26"))
        self.assertIn("最多 36 位", self.reserve("large", "12:00", 37))
        self.book = ReservationBook(self.path, duration_minutes=60)
        self.assertIn("訂位成功", self.reserve("shorter", "19:00", 2))

    def test_spoken_dates_and_year_boundary(self):
        today = date(2026, 12, 31)
        for spoken, expected in (("今天", today), ("明天", date(2027, 1, 1)), ("後天", date(2027, 1, 2))):
            with self.subTest(spoken=spoken):
                request = parse_reservation(f"日期：{spoken} 時間：18:00 人數：2 聯絡方式：guest@example.com", today=today)
                self.assertEqual(request.start.date(), expected)

        reply = self.book.reply("guest", "relative", "明天 18:00 2 guest@example.com", now=self.now)
        self.assertIn("日期 2026-11-28", reply)
        closed = self.book.reply("guest", "closed", "後天 15:00 2 guest@example.com", now=self.now)
        self.assertIn("營業時間", closed)
        missing = self.book.reply("guest", "missing", "日期：明天 時間：18:00 人數：2", now=self.now)
        self.assertIn("還缺少聯絡方式", missing)

    def test_delayed_message_keeps_date_and_group_choices(self):
        after_midnight = datetime(2027, 1, 1, 0, 1, tzinfo=TAIPEI_TIME)
        offer = self.book.reply(
            "guest", "late-message", "明天 18:00 8 guest@example.com",
            sent_on=date(2026, 12, 31), now=after_midnight,
        )
        self.assertIsInstance(offer, SplitOffer)
        self.assertIn("2027-01-01 18:00", offer.text)
        confirmation = self.book.choose_split("guest", offer.offer_id, (4, 4), now=after_midnight)
        self.assertIn("日期 2027-01-01", confirmation)
        self.assertIn("拆桌 4＋4", confirmation)

    def test_compact_spoken_date_time_and_party(self):
        request = parse_reservation("明天晚上六點八個人guest@example.com", today=self.now.date())
        self.assertEqual(request.start, datetime(2026, 11, 28, 18, tzinfo=TAIPEI_TIME))
        self.assertEqual((request.people, request.contact), (8, "guest@example.com"))
        offer = self.book.reply("guest", "compact", "明天晚上六點八個人guest@example.com", now=self.now)
        self.assertIsInstance(offer, SplitOffer)
        self.assertIn("2026-11-28 18:00 的 8 位訂位", offer.text)
        self.assertIn((4, 4), offer.options)

        cases = (
            ("後天晚上六點半，兩位，0912-345-678", 18, 30, 2),
            ("日期：後天 時間：下午六點十五分 人數：十二位 聯絡方式：guest@example.com", 18, 15, 12),
            ("明天18:008個人guest@example.com", 18, 0, 8),
            ("明天 上午十一點半 三位 guest@example.com", 11, 30, 3),
        )
        for message, hour, minute, people in cases:
            with self.subTest(message=message):
                parsed = parse_reservation(message, today=self.now.date())
                self.assertEqual((parsed.start.hour, parsed.start.minute, parsed.people), (hour, minute, people))

        for message in ("明天晚上六點八個人", "明天 六點 8 guest@example.com", "明天晚上六點六十分八個人guest@example.com"):
            with self.subTest(message=message):
                reply = self.book.reply("guest", "invalid-compact", message, now=self.now)
                self.assertIn("請在同一則訊息填齊", reply)
        midnight = self.book.reply("guest", "midnight", "明天晚上十二點八個人guest@example.com", now=self.now)
        self.assertIn("營業時間", midnight)

    def test_common_conversational_formats(self):
        cases = (
            ("明天晚上6:30，兩位，0912-345-678", 18, 30, 2, "0912345678"),
            ("明天晚上6點30 8位 0912345678", 18, 30, 8, "0912345678"),
            ("我要訂位明天晚上六點八個人，電話：0912345678", 18, 0, 8, "0912345678"),
            ("幫我訂八位 明天晚上六點 guest@example.com", 18, 0, 8, "guest@example.com"),
            ("請幫我預約明天晚上一共八位", None, None, None, None),
            ("明天 18:00 2 手機是 +886 912 345 678", 18, 0, 2, "+886912345678"),
            ("明天晚上18:00共八位emailguest@example.com", 18, 0, 8, "emailguest@example.com"),
            ("明天晚上六點八個人，Email 是 guest@example.com。", 18, 0, 8, "guest@example.com"),
            ("我要訂位 日期：明天 時間：晚上六點 人數：八位 信箱：guest@example.com", 18, 0, 8, "guest@example.com"),
            ("日期：明天時間：晚上六點人數：八位電話：0912345678", 18, 0, 8, "0912345678"),
        )
        for message, hour, minute, people, contact in cases:
            with self.subTest(message=message):
                if hour is None:
                    self.assertIn("請在同一則訊息填齊", self.book.reply("guest", "incomplete", message, now=self.now))
                    continue
                parsed = parse_reservation(message, today=self.now.date())
                self.assertEqual(parsed.start.date(), date(2026, 11, 28))
                self.assertEqual((parsed.start.hour, parsed.start.minute, parsed.people, parsed.contact),
                                 (hour, minute, people, contact))

    def test_conversational_dates_and_unclear_requests(self):
        cases = (
            ("下禮拜一晚上六點兩位guest@example.com", date(2026, 11, 30)),
            ("這週日中午十二點四位0912345678", date(2026, 11, 29)),
            ("11月30日中午十二點四位0912345678", date(2026, 11, 30)),
            ("十一月二十八號 18:00 兩位 guest@example.com", date(2026, 11, 28)),
            ("2026/11/28晚上六點兩位guest@example.com", date(2026, 11, 28)),
            ("大後天晚上六點兩位guest@example.com", date(2026, 11, 30)),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertEqual(parse_reservation(message, today=self.now.date()).start.date(), expected)

        examples = (
            ("明天六點兩位guest@example.com", "24 小時制"),
            ("明天晚上六點兩位", "還缺少聯絡方式"),
            ("明天上午18:00兩位guest@example.com", "確認時間"),
            ("週末晚上六點兩位guest@example.com", "訂位資料不完整"),
        )
        for message, expected in examples:
            with self.subTest(message=message):
                reply = self.book.reply("guest", "ambiguous", message, now=self.now)
                self.assertIn(expected, reply)
                self.assertIn("請在同一則訊息填齊", reply)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 0)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM pending_splits").fetchone()[0], 0)

    def test_concurrent_requests_cannot_overbook_four_person_tables(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            replies = list(pool.map(lambda i: self.reserve(f"large-{i}", "11:30", 4), range(8)))
        self.assertEqual(sum("訂位成功" in reply for reply in replies), 6)
        self.assertEqual(sum("桌位已滿" in reply for reply in replies), 2)

        for i in range(6):
            self.assertIn("訂位成功", self.reserve(f"small-{i}", "11:30", 2))
        self.assertIn("桌位已滿", self.reserve("overlapping", "12:59", 2))
        self.assertIn("訂位成功", self.reserve("next-seating", "13:00", 4))

    def test_small_parties_use_two_person_tables_first_and_retries_are_idempotent(self):
        first = self.reserve("first", "11:30", 2)
        self.book = ReservationBook(self.path)
        replay = self.reserve("first", "11:30", 2)
        self.assertEqual(first.splitlines()[1], replay.splitlines()[1])
        self.assertIn("這筆訂位已成立", replay)
        manual_repeat = self.book.reply("first", "different-event", "2026-11-28 11:30 2 0912-345-678", now=self.now)
        self.assertIn(first.splitlines()[1], manual_repeat)

        for i in range(1, 12):
            self.assertIn("訂位成功", self.reserve(f"small-{i}", "11:30", 2))
        self.assertIn("桌位已滿", self.reserve("no-table", "11:30", 2))
        with closing(sqlite3.connect(self.path)) as database:
            assigned = database.execute("SELECT table_id FROM reservation_tables ORDER BY reservation_id").fetchall()
        self.assertEqual(assigned, [(table,) for table in range(1, 13)])

    def test_group_is_booked_only_after_customer_chooses(self):
        offer = self.reserve("group", "18:00", 8)
        self.assertIsInstance(offer, SplitOffer)
        self.assertIn((4, 4), offer.options)
        self.assertIn((2, 2, 4), offer.options)
        self.assertLessEqual(len(offer.options), 13)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 0)

        self.book = ReservationBook(self.path)
        wrong_customer = self.book.choose_split("other", offer.offer_id, (2, 2, 4), now=self.now)
        self.assertIn("已失效", wrong_customer)
        wrong_choice = self.book.choose_split("group", offer.offer_id, (4, 3), now=self.now)
        self.assertIn("請點選", wrong_choice)
        confirmation = self.book.choose_split("group", offer.offer_id, (2, 2, 4), now=self.now)
        self.assertIn("拆桌 2＋2＋4", confirmation)
        repeat = self.book.choose_split("group", offer.offer_id, (4, 4), now=self.now)
        self.assertIn("拆桌 2＋2＋4", repeat)
        self.assertEqual(confirmation.splitlines()[1], repeat.splitlines()[1])
        self.assertEqual(confirmation.splitlines()[1], self.reserve("group", "18:00", 8).splitlines()[1])
        with closing(sqlite3.connect(self.path)) as database:
            rows = database.execute("SELECT table_id, people FROM reservation_tables ORDER BY table_id").fetchall()
            self.assertEqual(rows, [(1, 2), (2, 2), (7, 4)])

    def test_capacity_is_rechecked_after_offers(self):
        offers = [self.reserve(f"group-{i}", "18:00", 8) for i in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(
                lambda i: self.book.choose_split(f"group-{i}", offers[i].offer_id, (4, 4), now=self.now), range(4),
            ))
        self.assertEqual(sum(isinstance(result, str) and "訂位成功" in result for result in outcomes), 3)
        remaining = [result for result in outcomes if isinstance(result, SplitOffer)]
        self.assertEqual(len(remaining), 1)
        self.assertNotIn((4, 4), remaining[0].options)
        self.assertIn((2, 2, 2, 2), remaining[0].options)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM reservation_tables").fetchone()[0], 6)

    def test_expired_choices_cannot_book(self):
        offer = self.reserve("group", "18:00", 8)
        result = self.book.choose_split("group", offer.offer_id, (4, 4), now=self.now + timedelta(minutes=10))
        self.assertIn("已失效", result)
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 0)

    def test_migration_preserves_single_table_reservations(self):
        self.path.unlink()
        with closing(sqlite3.connect(self.path)) as database:
            database.executescript("""
                CREATE TABLE reservations (
                    id INTEGER PRIMARY KEY, event_id TEXT, customer_id TEXT, starts_at TEXT,
                    ends_at TEXT, people INTEGER, contact TEXT, table_id INTEGER
                );
                CREATE INDEX reservations_start_idx ON reservations (starts_at);
                INSERT INTO reservations VALUES (42, 'old-event', 'old-customer', '2026-11-28T18:00+08:00',
                    '2026-11-28T19:30+08:00', 4, 'guest@example.com', 7);
            """)
        self.book = ReservationBook(self.path)
        old_booking = self.book.reply("old-customer", "old-event", "2026-11-28 18:00 4 guest@example.com", now=self.now)
        self.assertIn("B000042", old_booking)
        new_booking = self.reserve("new", "18:00", 4)
        self.assertIn("B000043", new_booking)
        with closing(sqlite3.connect(self.path)) as database:
            rows = database.execute("SELECT reservation_id, table_id FROM reservation_tables ORDER BY reservation_id").fetchall()
            self.assertEqual(rows, [(42, 7), (43, 8)])

    def test_dashboard_lists_only_confirmed_reservations_for_selected_day(self):
        self.reserve("lunch", "12:00", 2)
        offer = self.reserve("dinner", "18:00", 8)
        self.book.choose_split("dinner", offer.offer_id, (2, 2, 4), now=self.now)
        self.reserve("pending", "17:30", 5)
        self.reserve("following-day", "12:00", 4, day="2026-11-29")

        records = self.book.reservations_for_day(date(2026, 11, 28))
        self.assertEqual([(row["period"], row["people"], row["groups"]) for row in records],
                         [("lunch", 2, [2]), ("dinner", 8, [2, 2, 4])])
        self.assertEqual((records[0]["start"], records[0]["end"]), ("12:00", "13:30"))
        self.assertEqual(records[1]["reference"], "B000002")
        self.assertNotIn("customer_id", records[0])
        self.assertNotIn("contact", records[0])

    def test_cancel_checks_line_owner_and_releases_tables(self):
        for i in range(6):
            self.assertIn("訂位成功", self.reserve(f"filled-{i}", "18:00", 4))
        self.assertIn("找不到", self.book.reply("different", "wrong-cancel", "取消 B000001", now=self.now))
        self.assertIn("桌位已滿", self.reserve("full", "18:00", 4))
        result = self.book.reply("filled-0", "cancel-event", "取消 B000001", now=self.now)
        self.assertIn("桌位已釋放", result)
        self.assertIn("已取消", self.reserve("filled-0", "18:00", 4))
        new_booking = self.book.reply("filled-0", "new-event", "2026-11-28 18:00 4 0912345678", now=self.now)
        self.assertIn("B000007", new_booking)
        self.assertEqual(len(self.book.reservations_for_day(date(2026, 11, 28))), 6)

    def test_reschedule_preserves_original_on_conflict_and_same_reference_on_success(self):
        self.reserve("move-owner", "18:00", 4)
        for i in range(6):
            self.reserve(f"full-next-{i}", "18:00", 4, day="2026-11-29")
        command = "改期 B000001 後天晚上六點"
        self.assertIn("找不到", self.book.reply("different", "wrong-move", command, now=self.now))
        self.assertIn("原訂位仍保留", self.book.reply("move-owner", "conflict", command, now=self.now))
        self.assertEqual(self.book.reservations_for_day(date(2026, 11, 28))[0]["reference"], "B000001")
        self.assertIn("桌位已釋放", self.book.cancel("B000002", now=self.now))
        result = self.book.reply("move-owner", "first-move", command, now=self.now)
        self.assertIn("改期成功", result)
        self.assertIn("B000001", result)
        self.assertEqual(self.book.reservations_for_day(date(2026, 11, 28)), [])
        self.assertIn("改期成功", self.book.reschedule("B000001", "大後天", "18:00", "second-move", now=self.now))
        self.book = ReservationBook(self.path)
        stale = self.book.reply("move-owner", "first-move", command, now=self.now)
        self.assertIn("2026-11-30", stale)
        self.assertEqual(self.book.reservations_for_day(date(2026, 11, 30))[0]["reference"], "B000001")

    def test_manual_booking_and_physical_table_size_breakdown(self):
        day = date(2026, 11, 28)
        for i in range(6):
            self.reserve(f"small-{i}", "18:00", 2)
        manual = self.book.add_manual("staff-1", day, "18:00", 2, "", now=self.now)
        self.assertIn("訂位成功", manual)
        self.assertIn("這筆訂位已成立", self.book.add_manual("staff-1", day, "18:00", 2, "", now=self.now))
        booking = self.book.reservations_for_day(day)[-1]
        self.assertEqual(booking["tables"], [{"size": 4, "people": 2}])
        self.assertNotIn("contact", booking)
        offer = self.book.add_manual("staff-2", day, "11:30", 8, "", now=self.now)
        self.assertIsInstance(offer, SplitOffer)
        result = self.book.choose_split("manual:staff-2", offer.offer_id, (2, 2, 4), now=self.now)
        self.assertIn("訂位成功", result)
        self.assertEqual(self.book.reservations_for_day(day)[0]["tables"],
                         [{"size": 2, "people": 2}, {"size": 2, "people": 2}, {"size": 4, "people": 4}])

    def test_migration_preserves_previous_reservation_and_split_offer(self):
        self.path.unlink()
        with closing(sqlite3.connect(self.path)) as database:
            database.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE reservations (
                    id INTEGER PRIMARY KEY, event_id TEXT UNIQUE, customer_id TEXT, starts_at TEXT,
                    ends_at TEXT, people INTEGER, contact TEXT, offer_id TEXT UNIQUE,
                    UNIQUE (customer_id, starts_at, people, contact)
                );
                CREATE INDEX reservations_start_idx ON reservations (starts_at);
                CREATE TABLE reservation_tables (reservation_id INTEGER REFERENCES reservations(id), table_id INTEGER, people INTEGER);
                INSERT INTO reservations VALUES (72, 'old-event', 'old-customer', '2026-11-28T18:00+08:00',
                    '2026-11-28T19:30+08:00', 8, 'guest@example.com', 'old-offer');
                INSERT INTO reservation_tables VALUES (72, 7, 4), (72, 8, 4);
            """)
        self.book = ReservationBook(self.path)
        kept = self.book.choose_split("old-customer", "old-offer", (4, 4), now=self.now)
        self.assertIn("B000072", kept)
        self.assertEqual(self.book.reservations_for_day(date(2026, 11, 28))[0]["tables"],
                         [{"size": 4, "people": 4}, {"size": 4, "people": 4}])
        with closing(sqlite3.connect(self.path)) as database:
            self.assertEqual(database.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
