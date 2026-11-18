"""Synchronous SQLite calendar and durable, revision-bound reminders."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import re
import sqlite3
from uuid import uuid4



TZ = timezone(timedelta(hours=7), "Asia/Ho_Chi_Minh")
FIELDS = ("title", "date", "start", "end", "note", "remind_minutes")


class ValidationError(ValueError):
    pass


class ConflictError(ValidationError):
    pass


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValidationError("Ngày phải có dạng YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("Ngày không hợp lệ.") from exc


def _identifier(value):
    if not isinstance(value, str) or not value:
        raise ValidationError("ID không hợp lệ.")


def _integer(value, minimum=0, maximum=2 ** 63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValidationError("Giá trị số nguyên không hợp lệ.")


def _payload(payload, current=None):
    if not isinstance(payload, dict) or payload.keys() - set(FIELDS):
        raise ValidationError("Dữ liệu lịch chứa trường không hợp lệ.")
    values = ({k: current[k] for k in FIELDS} if current else {"note": "", "remind_minutes": 0})
    values.update(payload)
    if values.keys() != set(FIELDS):
        raise ValidationError("Thiếu tiêu đề, ngày hoặc giờ.")
    if (not isinstance(values["title"], str) or not values["title"].strip()
            or len(values["title"]) > 120 or "\x00" in values["title"]):
        raise ValidationError("Tiêu đề phải có 1–120 ký tự.")
    _date(values["date"])
    for key in ("start", "end"):
        if not isinstance(values[key], str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", values[key]):
            raise ValidationError("Giờ phải có dạng HH:MM.")
    if values["end"] <= values["start"]:
        raise ValidationError("Giờ kết thúc phải sau giờ bắt đầu trong cùng ngày.")
    if not isinstance(values["note"], str) or len(values["note"]) > 2000 or "\x00" in values["note"]:
        raise ValidationError("Ghi chú tối đa 2000 ký tự.")
    _integer(values["remind_minutes"], maximum=1440)
    return values


class CalendarStore:
    def __init__(self, path):
        self._db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.execute("PRAGMA synchronous = NORMAL")
            self._db.execute("PRAGMA journal_mode = WAL")
            self._db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL CHECK(length(title) BETWEEN 1 AND 120),
                    date TEXT NOT NULL,
                    start TEXT NOT NULL,
                    end TEXT NOT NULL CHECK(end > start),
                    note TEXT NOT NULL CHECK(length(note) <= 2000),
                    remind_minutes INTEGER NOT NULL CHECK(remind_minutes BETWEEN 0 AND 1440),
                    revision INTEGER NOT NULL CHECK(revision >= 1)
                );
                CREATE INDEX IF NOT EXISTS events_chronological ON events(date, start, end);
                CREATE TABLE IF NOT EXISTS reminders (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'played', 'failed', 'cancelled')),
                    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_id, revision)
                );
            """)
            for table in ("events", "reminders"):
                columns = {row[1] for row in self._db.execute(f"PRAGMA table_info({table})")}
                if "user_id" not in columns:
                    self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'"
                    )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS events_user_chronological "
                "ON events(user_id, date, start, end)"
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS reminders_user_history "
                "ON reminders(user_id, updated_at)"
            )
            self._db.commit()
        except BaseException:
            self._db.close()
            raise

    @staticmethod
    def _event(row):
        return {key: row[key] for key in row.keys() if key != "user_id"}

    @staticmethod
    def _user_id(user_id):
        if not isinstance(user_id, str) or not user_id:
            raise ValidationError("ID người dùng không hợp lệ.")
        return user_id


    def close(self):
        self._db.close()

    @contextmanager
    def _write(self):
        # Acquire the write lock BEFORE overlap/revision reads, including other connections.
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            yield

    def list_events(self, start_date, end_date, user_id="default"):
        if _date(start_date) > _date(end_date):
            raise ValidationError("Khoảng ngày không hợp lệ.")
        user_id = self._user_id(user_id)
        rows = self._db.execute(
            "SELECT * FROM events WHERE user_id = ? AND date BETWEEN ? AND ? ORDER BY date, start, id",
            (user_id, start_date, end_date),
        )
        return [self._event(row) for row in rows]

    def get_event(self, id, user_id="default"):
        _identifier(id)
        user_id = self._user_id(user_id)
        row = self._db.execute("SELECT * FROM events WHERE id = ? AND user_id = ?", (id, user_id)).fetchone()
        return self._event(row) if row else None

    def _current(self, id, expected_revision, user_id="default"):
        _integer(expected_revision, minimum=1)
        event = self.get_event(id, user_id)
        if event is None or event["revision"] != expected_revision:
            raise ConflictError("Lịch đã thay đổi hoặc đã bị xóa. Hãy tải lại.")
        return event

    def _check_overlap(self, event, user_id="default", exclude=""):
        row = self._db.execute(
            "SELECT id FROM events WHERE user_id = ? AND date = ? AND start < ? AND end > ? AND id != ? LIMIT 1",
            (user_id, event["date"], event["end"], event["start"], exclude),
        ).fetchone()
        if row:
            raise ConflictError("Khoảng giờ bị trùng với lịch hiện có.")

    def _insert(self, values, user_id="default"):
        self._check_overlap(values, user_id)
        event = values | {"id": str(uuid4()), "revision": 1}
        self._db.execute(
            "INSERT INTO events (id, title, date, start, end, note, remind_minutes, revision, user_id) "
            "VALUES (:id, :title, :date, :start, :end, :note, :remind_minutes, :revision, :user_id)",
            event | {"user_id": user_id},
        )
        return event

    def create_event(self, payload, user_id="default"):
        values = _payload(payload)
        user_id = self._user_id(user_id)
        with self._write():
            return self._insert(values, user_id)

    def _invalidate(self, id, user_id="default"):
        self._db.execute("UPDATE reminders SET active = 0 WHERE event_id = ? AND user_id = ?", (id, user_id))

    def _next_reminder_timestamp(self, user_id):
        now = datetime.now(TZ)
        timestamps = self._db.execute(
            "SELECT updated_at FROM reminders WHERE user_id = ?", (user_id,),
        )
        latest = max((datetime.fromisoformat(row[0]).astimezone(timezone.utc) for row in timestamps),
                     default=None)
        if latest is not None and now.astimezone(timezone.utc) <= latest:
            now = (latest + timedelta(microseconds=1)).astimezone(TZ)
        return now.isoformat()

    def update_event(self, id, payload, expected_revision, user_id="default"):
        """Apply editable fields; omitted fields retain their current values."""
        user_id = self._user_id(user_id)
        with self._write():
            current = self._current(id, expected_revision, user_id)
            values = _payload(payload, current)
            self._check_overlap(values, user_id, exclude=id)
            event = values | {"id": id, "revision": current["revision"] + 1}
            self._db.execute(
                "UPDATE events SET title=:title, date=:date, start=:start, end=:end, "
                "note=:note, remind_minutes=:remind_minutes, revision=:revision "
                "WHERE id=:id AND user_id=:user_id", event | {"user_id": user_id},
            )
            self._invalidate(id, user_id)
            return event

    def delete_event(self, id, expected_revision, user_id="default"):
        user_id = self._user_id(user_id)
        with self._write():
            self._current(id, expected_revision, user_id)
            self._db.execute("DELETE FROM events WHERE id = ? AND user_id = ?", (id, user_id))
            self._invalidate(id, user_id)

    def copy_week(self, source_monday, target_monday, user_id="default"):
        user_id = self._user_id(user_id)
        source, target = _date(source_monday), _date(target_monday)
        if source.weekday() or target.weekday():
            raise ValidationError("Tuần phải bắt đầu vào thứ Hai.")
        try:
            source_end = source + timedelta(days=6)
            target + timedelta(days=6)
        except OverflowError as exc:
            raise ValidationError("Tuần vượt quá phạm vi ngày hỗ trợ.") from exc
        with self._write():
            copies = []
            for event in self.list_events(source_monday, source_end.isoformat(), user_id=user_id):
                values = {k: event[k] for k in FIELDS}
                values["date"] = (target + (_date(event["date"]) - source)).isoformat()
                copies.append(self._insert(values, user_id))
            return copies

    def due_reminders(self, now, user_id="default"):
        user_id = self._user_id(user_id)
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValidationError("Thời điểm phải kèm múi giờ.")
        try:
            now = now.astimezone(TZ)
        except (OverflowError, ValueError) as exc:
            raise ValidationError("Thời điểm ngoài phạm vi hỗ trợ.") from exc
        today = now.date()
        tomorrow = date.fromordinal(min(today.toordinal() + 1, date.max.toordinal()))
        due = []
        with self._write():
            # Lead time is capped at 24 hours; older dates have already expired.
            events = self._db.execute(
                "SELECT e.* FROM events e LEFT JOIN reminders r "
                "ON r.event_id = e.id AND r.revision = e.revision AND r.user_id = e.user_id "
                "WHERE e.user_id = ? AND e.date BETWEEN ? AND ? "
                "AND (r.status IS NULL OR r.status != 'played') "
                "ORDER BY e.date, e.start, e.id", (user_id, today.isoformat(), tomorrow.isoformat()),
            ).fetchall()
            for row in events:
                event = self._event(row)
                start = datetime.fromisoformat(f'{event["date"]}T{event["start"]}').replace(tzinfo=TZ)
                end = datetime.fromisoformat(f'{event["date"]}T{event["end"]}').replace(tzinfo=TZ)
                if now >= end or start - now > timedelta(minutes=event["remind_minutes"]):
                    continue
                reminder = {
                    "id": f'{event["id"]}:{event["revision"]}',
                    "event": event,
                    "text": f'Nhắc lịch: {event["title"]}, {event["start"]} ngày {event["date"]}.',
                    "expires_at": end.isoformat(),
                }
                exists = self._db.execute(
                    "SELECT 1 FROM reminders WHERE id = ? AND user_id = ?",
                    (reminder["id"], user_id),
                ).fetchone()
                if exists is None:
                    self._db.execute(
                        "INSERT INTO reminders (id, event_id, revision, payload, status, active, updated_at, user_id) "
                        "VALUES (?, ?, ?, ?, 'pending', 1, ?, ?)",
                        (reminder["id"], event["id"], event["revision"],
                         json.dumps(reminder, ensure_ascii=False),
                         self._next_reminder_timestamp(user_id), user_id),
                    )
                due.append(reminder)
        return due

    def mark_reminder(self, id, status, user_id="default"):
        """Only a successful playback is terminal; ignore later failure ACKs."""
        _identifier(id)
        if status not in ("played", "failed", "cancelled"):
            raise ValidationError("Trạng thái phát không hợp lệ.")
        user_id = self._user_id(user_id)
        with self._write():
            row = self._db.execute(
                "SELECT r.status FROM reminders r JOIN events e "
                "ON e.id = r.event_id AND e.revision = r.revision AND e.user_id = r.user_id "
                "WHERE r.id = ? AND r.user_id = ? AND r.active = 1", (id, user_id),
            ).fetchone()
            if row is None:
                raise ValidationError("Nhắc lịch không tồn tại hoặc đã hết hiệu lực.")
            if row["status"] != "played":
                self._db.execute(
                    "UPDATE reminders SET status = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (status, self._next_reminder_timestamp(user_id), id, user_id),
                )

    def reminder_history(self, limit=30, user_id="default"):
        _integer(limit)
        user_id = self._user_id(user_id)
        rows = self._db.execute(
            "SELECT payload, status, active, updated_at FROM reminders WHERE user_id = ? "
            "ORDER BY updated_at DESC, rowid DESC LIMIT ?", (user_id, limit),
        )
        return [json.loads(row["payload"]) | {
            "status": row["status"], "active": bool(row["active"]), "updated_at": row["updated_at"],
        } for row in rows]
