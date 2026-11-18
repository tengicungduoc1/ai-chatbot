"""Optional PostgreSQL calendar store; selected explicitly, never by fallback."""
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import threading
from uuid import uuid4

from calendar_store import (
    TZ, FIELDS, ValidationError, ConflictError, _date, _identifier, _integer, _payload,
)


class SupabaseCalendarStore:
    blocks_event_loop = True

    def __init__(self, url, driver):
        self._db = driver.connect(url, autocommit=True, row_factory=driver.rows.dict_row,
                                  options="-c timezone=Asia/Ho_Chi_Minh")
        self._mutex = threading.RLock()
        try:
            self._verify_backend_role()
        except BaseException:
            self._db.close()
            raise

    def _verify_backend_role(self):
        cursor = self._db.cursor()
        try:
            cursor.execute(
                "SELECT pg_has_role(current_user, %s, 'member') AS is_member",
                ("app_calendar_backend",),
            )
            row = cursor.fetchone()
            member = row.get("is_member") if isinstance(row, dict) else row[0] if row else None
            if member is not True:
                raise RuntimeError("Database login is not a member of app_calendar_backend")
        finally:
            cursor.close() if hasattr(cursor, 'close') else None

    @staticmethod
    def _check_id(id):
        _identifier(id)
        return id

    @staticmethod
    def _user_id(user_id):
        if not isinstance(user_id, str) or not user_id:
            raise ValidationError("ID người dùng không hợp lệ.")
        return user_id

    def close(self):
        self._db.close()

    def _fetch(self, sql, params=(), one=False, user_id="default"):
        user_id = self._user_id(user_id)
        with self._mutex:
            cursor = self._db.cursor()
            cursor.execute("BEGIN")
            try:
                cursor.execute("SET LOCAL ROLE app_calendar_backend")
                cursor.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
                cursor.execute(sql, params)
                result = cursor.fetchone() if one else cursor.fetchall()
                cursor.execute("COMMIT")
                return result
            except BaseException:
                cursor.execute("ROLLBACK")
                raise
            finally:
                cursor.close() if hasattr(cursor, 'close') else None

    @contextmanager
    def _write(self, user_id):
        # ponytail: one connection per store; add a pool only if measured concurrency demands it.
        with self._mutex:
            cursor = self._db.cursor()
            cursor.execute("BEGIN")
            try:
                cursor.execute("SET LOCAL ROLE app_calendar_backend")
                cursor.execute("SELECT set_config('app.user_id', %s, true)", (user_id,))
                # The lock precedes every overlap, revision and reminder read in this transaction.
                cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (user_id,))
                yield cursor
                cursor.execute("COMMIT")
            except BaseException:
                cursor.execute("ROLLBACK")
                raise
            finally:
                cursor.close() if hasattr(cursor, 'close') else None
                # ponytail: explicit SQL transaction on autocommit; use driver.transaction() if pooling is added.

    @staticmethod
    def _event(row):
        return {key: row[key] for key in row if key != "user_id"} if row else None

    def list_events(self, start_date, end_date, user_id="default"):
        if _date(start_date) > _date(end_date):
            raise ValidationError("Khoảng ngày không hợp lệ.")
        user_id = self._user_id(user_id)
        rows = self._fetch('SELECT * FROM public.events WHERE user_id = %s AND date BETWEEN %s AND %s '
                           'ORDER BY date, start, id', (user_id, start_date, end_date), user_id=user_id)
        return [self._event(row) for row in rows]

    def get_event(self, id, user_id="default"):
        _identifier(id)
        user_id = self._user_id(user_id)
        return self._event(self._fetch('SELECT * FROM public.events WHERE id = %s AND user_id = %s',
                                       (id, user_id), one=True, user_id=user_id))

    def _current(self, cursor, id, revision, user_id):
        _integer(revision, minimum=1)
        cursor.execute('SELECT * FROM public.events WHERE id = %s AND user_id = %s', (id, user_id))
        row = cursor.fetchone()
        if row is None or row['revision'] != revision:
            raise ConflictError("Lịch đã thay đổi hoặc đã bị xóa. Hãy tải lại.")
        return self._event(row)

    @staticmethod
    def _check_overlap(cursor, event, user_id, exclude=""):
        cursor.execute('SELECT id FROM public.events WHERE user_id = %s AND date = %s '
                       'AND start < %s AND "end" > %s AND id != %s LIMIT 1',
                       (user_id, event['date'], event['end'], event['start'], exclude))
        if cursor.fetchone():
            raise ConflictError("Khoảng giờ bị trùng với lịch hiện có.")

    def _insert(self, cursor, values, user_id):
        self._check_overlap(cursor, values, user_id)
        event = values | {'id': str(uuid4()), 'revision': 1}
        cursor.execute('INSERT INTO public.events '
                       '(id, title, date, start, "end", note, remind_minutes, revision, user_id) '
                       'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
                       tuple(event[k] for k in ('id', 'title', 'date', 'start', 'end', 'note',
                                                 'remind_minutes', 'revision')) + (user_id,))
        return event

    def create_event(self, payload, user_id="default"):
        values = _payload(payload)
        user_id = self._user_id(user_id)
        with self._write(user_id) as cursor:
            return self._insert(cursor, values, user_id)

    def update_event(self, id, payload, expected_revision, user_id="default"):
        self._check_id(id)
        user_id = self._user_id(user_id)
        with self._write(user_id) as cursor:
            current = self._current(cursor, id, expected_revision, user_id)
            values = _payload(payload, current)
            self._check_overlap(cursor, values, user_id, exclude=id)
            event = values | {'id': id, 'revision': current['revision'] + 1}
            cursor.execute('UPDATE public.events SET title=%s, date=%s, start=%s, "end"=%s, '
                           'note=%s, remind_minutes=%s, revision=%s WHERE id=%s AND user_id=%s',
                           tuple(event[k] for k in FIELDS) + (event['revision'], id, user_id))
            cursor.execute('UPDATE public.reminders SET active = false WHERE event_id = %s AND user_id = %s',
                           (id, user_id))
            return event

    def delete_event(self, id, expected_revision, user_id="default"):
        self._check_id(id)
        user_id = self._user_id(user_id)
        with self._write(user_id) as cursor:
            self._current(cursor, id, expected_revision, user_id)
            cursor.execute('DELETE FROM public.events WHERE id = %s AND user_id = %s', (id, user_id))
            cursor.execute('UPDATE public.reminders SET active = false WHERE event_id = %s AND user_id = %s',
                           (id, user_id))

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
        with self._write(user_id) as cursor:
            cursor.execute('SELECT * FROM public.events WHERE user_id = %s AND date BETWEEN %s AND %s '
                           'ORDER BY date, start, id', (user_id, source_monday, source_end.isoformat()))
            originals = [self._event(row) for row in cursor.fetchall()]
            copies = []
            for event in originals:
                values = {k: event[k] for k in FIELDS}
                values['date'] = (target + (_date(event['date']) - source)).isoformat()
                copies.append(self._insert(cursor, values, user_id))
            return copies

    @staticmethod
    def _next_timestamp(cursor, user_id):
        cursor.execute('SELECT updated_at FROM public.reminders WHERE user_id = %s '
                       'ORDER BY updated_at DESC LIMIT 1', (user_id,))
        row = cursor.fetchone()
        now = datetime.now(TZ)
        if row is not None:
            latest = row['updated_at']
            if isinstance(latest, str):
                latest = datetime.fromisoformat(latest)
            if now.astimezone(timezone.utc) <= latest.astimezone(timezone.utc):
                now = (latest + timedelta(microseconds=1)).astimezone(TZ)
        return now.isoformat()

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
        with self._write(user_id) as cursor:
            cursor.execute('SELECT e.* FROM public.events e LEFT JOIN public.reminders r '
                           'ON r.event_id = e.id AND r.revision = e.revision AND r.user_id = e.user_id '
                           "WHERE e.user_id = %s AND e.date BETWEEN %s AND %s "
                           "AND (r.status IS NULL OR r.status != 'played') ORDER BY e.date, e.start, e.id",
                           (user_id, today.isoformat(), tomorrow.isoformat()))
            for row in cursor.fetchall():
                event = self._event(row)
                start = datetime.fromisoformat(f'{event["date"]}T{event["start"]}').replace(tzinfo=TZ)
                end = datetime.fromisoformat(f'{event["date"]}T{event["end"]}').replace(tzinfo=TZ)
                if now >= end or start - now > timedelta(minutes=event['remind_minutes']):
                    continue
                reminder = {
                    'id': f'{event["id"]}:{event["revision"]}', 'event': event,
                    'text': f'Nhắc lịch: {event["title"]}, {event["start"]} ngày {event["date"]}.',
                    'expires_at': end.isoformat(),
                }
                cursor.execute('SELECT 1 FROM public.reminders WHERE id = %s AND user_id = %s',
                               (reminder['id'], user_id))
                if cursor.fetchone() is None:
                    cursor.execute('INSERT INTO public.reminders '
                                   '(id, event_id, revision, payload, status, active, updated_at, user_id) '
                                   "VALUES (%s, %s, %s, %s::jsonb, 'pending', true, %s, %s)",
                                   (reminder['id'], event['id'], event['revision'],
                                    json.dumps(reminder, ensure_ascii=False),
                                    self._next_timestamp(cursor, user_id), user_id))
                due.append(reminder)
        return due

    def mark_reminder(self, id, status, user_id="default"):
        _identifier(id)
        if status not in ('played', 'failed', 'cancelled'):
            raise ValidationError("Trạng thái phát không hợp lệ.")
        user_id = self._user_id(user_id)
        with self._write(user_id) as cursor:
            cursor.execute('SELECT r.status FROM public.reminders r JOIN public.events e '
                           'ON e.id = r.event_id AND e.revision = r.revision AND e.user_id = r.user_id '
                           'WHERE r.id = %s AND r.user_id = %s AND r.active = true', (id, user_id))
            row = cursor.fetchone()
            if row is None:
                raise ValidationError("Nhắc lịch không tồn tại hoặc đã hết hiệu lực.")
            if row['status'] != 'played':
                cursor.execute('UPDATE public.reminders SET status = %s, updated_at = %s '
                               'WHERE id = %s AND user_id = %s',
                               (status, self._next_timestamp(cursor, user_id), id, user_id))

    def reminder_history(self, limit=30, user_id="default"):
        _integer(limit)
        user_id = self._user_id(user_id)
        rows = self._fetch('SELECT payload, status, active, updated_at FROM public.reminders '
                           'WHERE user_id = %s ORDER BY updated_at DESC, id DESC LIMIT %s',
                           (user_id, limit), user_id=user_id)
        return [(row['payload'] if isinstance(row['payload'], dict) else json.loads(row['payload'])) | {
            'status': row['status'], 'active': bool(row['active']),
            'updated_at': row['updated_at'].isoformat() if isinstance(row['updated_at'], datetime) else row['updated_at'],
        } for row in rows]
