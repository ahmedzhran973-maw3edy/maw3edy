import os
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock

import psycopg2
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for
from psycopg2.extras import RealDictCursor


app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "local-development-session")

DAY_NAMES = [
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS teachers (
        id BIGSERIAL PRIMARY KEY,
        full_name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        bio TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_types (
        id BIGSERIAL PRIMARY KEY,
        teacher_id BIGINT NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
        price NUMERIC(10, 2) NOT NULL DEFAULT 0 CHECK (price >= 0),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS availability (
        id BIGSERIAL PRIMARY KEY,
        teacher_id BIGINT NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
        day_of_week SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
        start_time TIME NOT NULL,
        end_time TIME NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (teacher_id, day_of_week, start_time, end_time),
        CHECK (start_time < end_time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bookings (
        id BIGSERIAL PRIMARY KEY,
        teacher_id BIGINT NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
        session_type_id BIGINT NOT NULL REFERENCES session_types(id) ON DELETE CASCADE,
        student_name TEXT NOT NULL,
        student_email TEXT NOT NULL,
        starts_at TIMESTAMP NOT NULL,
        ends_at TIMESTAMP NOT NULL,
        notes TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'confirmed'
            CHECK (status IN ('confirmed', 'cancelled')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (starts_at < ends_at)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS bookings_teacher_time_idx
    ON bookings (teacher_id, starts_at, ends_at)
    """,
    """
    ALTER TABLE teachers
    ADD COLUMN IF NOT EXISTS bio TEXT NOT NULL DEFAULT ''
    """,
    """
    ALTER TABLE session_types
    ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''
    """,
    """
    ALTER TABLE session_types
    ADD COLUMN IF NOT EXISTS price NUMERIC(10, 2) NOT NULL DEFAULT 0
    """,
)

_schema_lock = Lock()
_schema_ready = False


class DatabaseUnavailable(RuntimeError):
    """Raised when the Supabase/Postgres connection is not configured or reachable."""


def connect_db():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise DatabaseUnavailable("DATABASE_URL is not configured.")

    try:
        return psycopg2.connect(database_url, connect_timeout=5)
    except psycopg2.Error as exc:
        raise DatabaseUnavailable("The database is not reachable.") from exc


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return

    with _schema_lock:
        if _schema_ready:
            return

        connection = connect_db()
        try:
            with connection:
                with connection.cursor() as cursor:
                    for statement in SCHEMA_STATEMENTS:
                        cursor.execute(statement)
            _schema_ready = True
        finally:
            connection.close()


def fetch_all(statement, params=()):
    connection = connect_db()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement, params)
            return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def json_value(value):
    if isinstance(value, (datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def request_data():
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form


def value(data, key, default=""):
    raw = data.get(key, default)
    return str(raw).strip() if raw is not None else default


def positive_int(data, key):
    try:
        parsed = int(value(data, key))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a positive integer.")
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive integer.")
    return parsed


def wants_json():
    accept = request.headers.get("Accept", "")
    return (
        request.is_json
        or request.args.get("format") == "json"
        or request.form.get("_format") == "json"
        or "application/json" in accept
    )


def success_response(payload, redirect_endpoint, status=201, **redirect_values):
    if wants_json():
        return jsonify(json_value(payload)), status
    flash(payload.get("message", "Saved successfully."), "success")
    return redirect(url_for(redirect_endpoint, **redirect_values))


def error_response(message, status, redirect_endpoint=None, **redirect_values):
    if wants_json() or redirect_endpoint is None:
        return jsonify({"error": message}), status
    flash(message, "error")
    return redirect(url_for(redirect_endpoint, **redirect_values))


def database_error(endpoint=None):
    app.logger.exception("Database operation failed")
    return error_response(
        "The database is temporarily unavailable. Please try again.",
        503,
        endpoint,
    )


def parse_time(raw, field_name):
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must use HH:MM format.") from exc


def parse_start_datetime(raw):
    if not raw:
        raise ValueError("starts_at is required.")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("starts_at must be a valid ISO date and time.") from exc

    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def fetch_dashboard_data():
    teachers = fetch_all(
        """
        SELECT id, full_name, email, bio, created_at
        FROM teachers
        ORDER BY full_name
        """
    )
    session_types = fetch_all(
        """
        SELECT st.id, st.teacher_id, st.title, st.description,
               st.duration_minutes, st.price, st.created_at,
               t.full_name AS teacher_name
        FROM session_types st
        JOIN teachers t ON t.id = st.teacher_id
        ORDER BY t.full_name, st.title
        """
    )
    availability = fetch_all(
        """
        SELECT a.id, a.teacher_id, a.day_of_week, a.start_time, a.end_time,
               t.full_name AS teacher_name
        FROM availability a
        JOIN teachers t ON t.id = a.teacher_id
        ORDER BY t.full_name, a.day_of_week, a.start_time
        """
    )
    for slot in availability:
        slot["day_name"] = DAY_NAMES[slot["day_of_week"]]
    return teachers, session_types, availability


@app.get("/")
def home():
    database_ready = False
    try:
        ensure_schema()
        database_ready = True
    except DatabaseUnavailable:
        app.logger.exception("Homepage database check failed")

    return render_template("index.html", database_ready=database_ready)


@app.get("/dashboard")
def dashboard():
    selected_teacher_id = request.args.get("teacher_id", type=int)
    try:
        ensure_schema()
        teachers, session_types, availability = fetch_dashboard_data()
        database_ready = True
    except DatabaseUnavailable:
        app.logger.exception("Dashboard database load failed")
        teachers, session_types, availability = [], [], []
        database_ready = False

    return render_template(
        "dashboard.html",
        teachers=teachers,
        session_types=session_types,
        availability=availability,
        selected_teacher_id=selected_teacher_id,
        database_ready=database_ready,
    )


@app.get("/book")
def book():
    try:
        ensure_schema()
        session_types = fetch_all(
            """
            SELECT st.id, st.teacher_id, st.title, st.description,
                   st.duration_minutes, st.price, t.full_name AS teacher_name
            FROM session_types st
            JOIN teachers t ON t.id = st.teacher_id
            ORDER BY t.full_name, st.title
            """
        )
        availability = fetch_all(
            """
            SELECT a.teacher_id, a.day_of_week, a.start_time, a.end_time,
                   t.full_name AS teacher_name
            FROM availability a
            JOIN teachers t ON t.id = a.teacher_id
            ORDER BY t.full_name, a.day_of_week, a.start_time
            """
        )
        for slot in availability:
            slot["day_name"] = DAY_NAMES[slot["day_of_week"]]
        database_ready = True
    except DatabaseUnavailable:
        app.logger.exception("Booking page database load failed")
        session_types, availability = [], []
        database_ready = False

    return render_template(
        "book.html",
        session_types=session_types,
        availability=availability,
        database_ready=database_ready,
    )


@app.get("/api/teachers")
def list_teachers():
    try:
        ensure_schema()
        return jsonify(json_value(fetch_all(
            """
            SELECT id, full_name, email, bio, created_at
            FROM teachers
            ORDER BY full_name
            """
        )))
    except DatabaseUnavailable:
        return database_error()


@app.post("/api/teachers")
def register_teacher():
    data = request_data()
    full_name = value(data, "full_name")
    email = value(data, "email").lower()
    bio = value(data, "bio")

    if not full_name or not email or "@" not in email:
        return error_response(
            "A teacher name and valid email are required.",
            400,
            "dashboard",
        )

    try:
        ensure_schema()
        connection = connect_db()
        try:
            with connection:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(
                        """
                        INSERT INTO teachers (full_name, email, bio)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (email) DO UPDATE
                        SET full_name = EXCLUDED.full_name,
                            bio = EXCLUDED.bio
                        RETURNING id, full_name, email, bio, created_at
                        """,
                        (full_name, email, bio),
                    )
                    teacher = dict(cursor.fetchone())
        finally:
            connection.close()

        teacher_id = teacher["id"]
        return success_response(
            {
                "message": "Teacher profile saved.",
                "teacher": teacher,
            },
            "dashboard",
            teacher_id=teacher_id,
        )
    except DatabaseUnavailable:
        return database_error("dashboard")
    except psycopg2.Error:
        app.logger.exception("Teacher registration failed")
        return error_response("Teacher profile could not be saved.", 400, "dashboard")


@app.get("/api/session-types")
def list_session_types():
    try:
        ensure_schema()
        return jsonify(json_value(fetch_all(
            """
            SELECT st.id, st.teacher_id, st.title, st.description,
                   st.duration_minutes, st.price, t.full_name AS teacher_name
            FROM session_types st
            JOIN teachers t ON t.id = st.teacher_id
            ORDER BY t.full_name, st.title
            """
        )))
    except DatabaseUnavailable:
        return database_error()


@app.post("/api/session-types")
def create_session_type():
    data = request_data()
    title = value(data, "title")
    description = value(data, "description")

    try:
        teacher_id = positive_int(data, "teacher_id")
        duration_minutes = positive_int(data, "duration_minutes")
        price = Decimal(value(data, "price", "0") or "0")
        if duration_minutes > 480:
            raise ValueError("duration_minutes cannot exceed 480.")
        if price < 0:
            raise ValueError("price cannot be negative.")
    except (InvalidOperation, ValueError) as exc:
        return error_response(str(exc), 400, "dashboard")

    if not title:
        return error_response("A session title is required.", 400, "dashboard")

    try:
        ensure_schema()
        connection = connect_db()
        try:
            with connection:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(
                        "SELECT id FROM teachers WHERE id = %s",
                        (teacher_id,),
                    )
                    if cursor.fetchone() is None:
                        return error_response(
                            "The selected teacher does not exist.",
                            404,
                            "dashboard",
                        )
                    cursor.execute(
                        """
                        INSERT INTO session_types
                            (teacher_id, title, description, duration_minutes, price)
                        VALUES (%s, %s, %s, %s, %s)
                        RETURNING id, teacher_id, title, description,
                                  duration_minutes, price, created_at
                        """,
                        (
                            teacher_id,
                            title,
                            description,
                            duration_minutes,
                            price,
                        ),
                    )
                    session_type = dict(cursor.fetchone())
        finally:
            connection.close()

        return success_response(
            {
                "message": "Session type created.",
                "session_type": session_type,
            },
            "dashboard",
            teacher_id=teacher_id,
        )
    except DatabaseUnavailable:
        return database_error("dashboard")
    except psycopg2.Error:
        app.logger.exception("Session type creation failed")
        return error_response("Session type could not be created.", 400, "dashboard")


@app.get("/api/availability")
def list_availability():
    teacher_id = request.args.get("teacher_id", type=int)
    try:
        ensure_schema()
        if teacher_id:
            slots = fetch_all(
                """
                SELECT id, teacher_id, day_of_week, start_time, end_time, created_at
                FROM availability
                WHERE teacher_id = %s
                ORDER BY day_of_week, start_time
                """,
                (teacher_id,),
            )
        else:
            slots = fetch_all(
                """
                SELECT id, teacher_id, day_of_week, start_time, end_time, created_at
                FROM availability
                ORDER BY teacher_id, day_of_week, start_time
                """
            )
        return jsonify(json_value(slots))
    except DatabaseUnavailable:
        return database_error()


@app.post("/api/availability")
def set_availability():
    data = request_data()
    try:
        teacher_id = positive_int(data, "teacher_id")
        day_of_week = int(value(data, "day_of_week"))
        if day_of_week not in range(7):
            raise ValueError("day_of_week must be between 0 and 6.")
        start_time = parse_time(value(data, "start_time"), "start_time")
        end_time = parse_time(value(data, "end_time"), "end_time")
        if start_time >= end_time:
            raise ValueError("end_time must be later than start_time.")
    except (TypeError, ValueError) as exc:
        return error_response(str(exc), 400, "dashboard")

    try:
        ensure_schema()
        connection = connect_db()
        try:
            with connection:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(
                        "SELECT id FROM teachers WHERE id = %s",
                        (teacher_id,),
                    )
                    if cursor.fetchone() is None:
                        return error_response(
                            "The selected teacher does not exist.",
                            404,
                            "dashboard",
                        )
                    cursor.execute(
                        """
                        INSERT INTO availability
                            (teacher_id, day_of_week, start_time, end_time)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING id, teacher_id, day_of_week, start_time, end_time
                        """,
                        (teacher_id, day_of_week, start_time, end_time),
                    )
                    slot = cursor.fetchone()
                    if slot is None:
                        return error_response(
                            "That availability window already exists.",
                            409,
                            "dashboard",
                        )
                    slot = dict(slot)
        finally:
            connection.close()

        return success_response(
            {
                "message": "Availability saved.",
                "availability": slot,
            },
            "dashboard",
            teacher_id=teacher_id,
        )
    except DatabaseUnavailable:
        return database_error("dashboard")
    except psycopg2.Error:
        app.logger.exception("Availability creation failed")
        return error_response("Availability could not be saved.", 400, "dashboard")


@app.get("/api/bookings")
def list_bookings():
    try:
        ensure_schema()
        bookings = fetch_all(
            """
            SELECT b.id, b.teacher_id, b.session_type_id, b.student_name,
                   b.student_email, b.starts_at, b.ends_at, b.notes, b.status,
                   st.title AS session_title, t.full_name AS teacher_name
            FROM bookings b
            JOIN session_types st ON st.id = b.session_type_id
            JOIN teachers t ON t.id = b.teacher_id
            ORDER BY b.starts_at
            """
        )
        return jsonify(json_value(bookings))
    except DatabaseUnavailable:
        return database_error()


@app.post("/api/bookings")
def create_booking():
    data = request_data()
    student_name = value(data, "student_name")
    student_email = value(data, "student_email").lower()
    notes = value(data, "notes")

    if not student_name or not student_email or "@" not in student_email:
        return error_response(
            "A student name and valid email are required.",
            400,
            "book",
        )

    try:
        session_type_id = positive_int(data, "session_type_id")
        starts_at = parse_start_datetime(value(data, "starts_at"))
    except ValueError as exc:
        return error_response(str(exc), 400, "book")

    if starts_at <= datetime.now():
        return error_response("Bookings must be scheduled in the future.", 400, "book")

    try:
        ensure_schema()
        connection = connect_db()
        try:
            with connection:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(
                        """
                        SELECT id, teacher_id, duration_minutes, title
                        FROM session_types
                        WHERE id = %s
                        """,
                        (session_type_id,),
                    )
                    session_type = cursor.fetchone()
                    if session_type is None:
                        return error_response(
                            "The selected session type does not exist.",
                            404,
                            "book",
                        )

                    teacher_id = session_type["teacher_id"]
                    ends_at = starts_at + timedelta(
                        minutes=session_type["duration_minutes"]
                    )
                    day_of_week = (starts_at.weekday() + 1) % 7

                    # Serialize bookings for one teacher to prevent double booking
                    # when two students submit the same time at once.
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (teacher_id,),
                    )
                    cursor.execute(
                        """
                        SELECT 1
                        FROM availability
                        WHERE teacher_id = %s
                          AND day_of_week = %s
                          AND start_time <= %s
                          AND end_time >= %s
                        LIMIT 1
                        """,
                        (teacher_id, day_of_week, starts_at.time(), ends_at.time()),
                    )
                    if cursor.fetchone() is None:
                        return error_response(
                            "That time is outside the teacher's availability.",
                            409,
                            "book",
                        )

                    cursor.execute(
                        """
                        SELECT 1
                        FROM bookings
                        WHERE teacher_id = %s
                          AND status = 'confirmed'
                          AND starts_at < %s
                          AND ends_at > %s
                        LIMIT 1
                        """,
                        (teacher_id, ends_at, starts_at),
                    )
                    if cursor.fetchone() is not None:
                        return error_response(
                            "That time is already booked.",
                            409,
                            "book",
                        )

                    cursor.execute(
                        """
                        INSERT INTO bookings
                            (teacher_id, session_type_id, student_name,
                             student_email, starts_at, ends_at, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING id, teacher_id, session_type_id, student_name,
                                  student_email, starts_at, ends_at, notes, status
                        """,
                        (
                            teacher_id,
                            session_type_id,
                            student_name,
                            student_email,
                            starts_at,
                            ends_at,
                            notes,
                        ),
                    )
                    booking = dict(cursor.fetchone())
        finally:
            connection.close()

        return success_response(
            {
                "message": "Booking confirmed.",
                "booking": booking,
            },
            "book",
        )
    except DatabaseUnavailable:
        return database_error("book")
    except psycopg2.Error:
        app.logger.exception("Booking creation failed")
        return error_response("Booking could not be created.", 400, "book")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)