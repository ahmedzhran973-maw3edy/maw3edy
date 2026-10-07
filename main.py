import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dotenv import load_dotenv
load_dotenv()
import psycopg2
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "local-development-session")
app.permanent_session_lifetime = timedelta(days=14)

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS teachers (
        id BIGSERIAL PRIMARY KEY,
        full_name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        booking_link_slug TEXT NOT NULL UNIQUE,
        phone_number TEXT NOT NULL DEFAULT '',
        bio TEXT NOT NULL DEFAULT '',
        timezone TEXT NOT NULL DEFAULT 'Africa/Cairo',
        country TEXT NOT NULL DEFAULT 'Egypt',
        city TEXT NOT NULL DEFAULT 'Cairo',
        prayer_breaks BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_types (
        id BIGSERIAL PRIMARY KEY,
        teacher_id BIGINT REFERENCES teachers(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL DEFAULT 60 CHECK (duration_minutes > 0),
        link_slug TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        price NUMERIC(10, 2) NOT NULL DEFAULT 0
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
        session_type_id BIGINT REFERENCES session_types(id) ON DELETE CASCADE,
        student_name TEXT NOT NULL,
        student_email TEXT NOT NULL,
        duration_minutes INTEGER NOT NULL DEFAULT 60 CHECK (duration_minutes > 0),
        starts_at TIMESTAMP NOT NULL,
        ends_at TIMESTAMP NOT NULL,
        status TEXT NOT NULL DEFAULT 'confirmed'
            CHECK (status IN ('confirmed', 'cancelled')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CHECK (starts_at < ends_at)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS bookings_teacher_time_idx
    ON bookings (teacher_id, starts_at, ends_at)
    """
)

ALTER_STATEMENTS = (
    "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS full_name TEXT;",
    "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS email TEXT;",
    "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS password_hash TEXT DEFAULT '!';",
    "UPDATE teachers SET password_hash = '!' WHERE password_hash IS NULL;",
    "ALTER TABLE teachers ALTER COLUMN password_hash SET DEFAULT '!';",
    "ALTER TABLE teachers ALTER COLUMN password_hash SET NOT NULL;",
    "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS booking_link_slug TEXT;",
    "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS phone_number TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS bio TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS timezone TEXT NOT NULL DEFAULT 'Africa/Cairo';",
    "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS country TEXT NOT NULL DEFAULT 'Egypt';",
    "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS city TEXT NOT NULL DEFAULT 'Cairo';",
    "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS prayer_breaks BOOLEAN NOT NULL DEFAULT TRUE;",
    "ALTER TABLE session_types ADD COLUMN IF NOT EXISTS link_slug TEXT;",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS duration_minutes INTEGER NOT NULL DEFAULT 60;",
    "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS session_type_id BIGINT;"
)

_schema_lock = Lock()
_schema_ready = False

class DatabaseUnavailable(RuntimeError):
    pass

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
                    for statement in ALTER_STATEMENTS:
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

def active_teacher_id():
    raw_teacher_id = session.get("active_teacher_id")
    if raw_teacher_id is None:
        return None
    try:
        teacher_id = int(raw_teacher_id)
    except (TypeError, ValueError):
        return None
    return teacher_id if teacher_id > 0 else None


def slugify(value, fallback):
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def teacher_booking_slug(email):
    return slugify(email, "teacher")


def get_prayer_times(city, country, date_str):
    try:
        query = urllib.parse.urlencode(
            {"city": city, "country": country, "method": 5}
        )
        url = f"https://api.aladhan.com/v1/timingsByCity/{date_str}?{query}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Maw3edy/1.0"},
        )
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode())
            timings = data.get('data', {}).get('timings', {})
            prayers = ['Fajr', 'Dhuhr', 'Asr', 'Maghrib', 'Isha']
            return [
                str(timings[prayer]).split()[0]
                for prayer in prayers
                if timings.get(prayer)
            ]
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return []


def parse_utc_datetime(raw):
    if not raw:
        raise ValueError("A booking time is required.")
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Booking time must be a valid ISO timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValueError("Booking time must include a timezone.")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def prayer_overlap(starts_at, ends_at, teacher, teacher_timezone):
    local_start = starts_at.replace(tzinfo=timezone.utc).astimezone(teacher_timezone)
    date_string = local_start.strftime("%d-%m-%Y")
    for prayer_time in get_prayer_times(
        teacher["city"],
        teacher["country"],
        date_string,
    ):
        try:
            prayer_start = datetime.strptime(
                f"{local_start.date().isoformat()}T{prayer_time}",
                "%Y-%m-%dT%H:%M",
            ).replace(tzinfo=teacher_timezone)
        except ValueError:
            continue
        prayer_start_utc = prayer_start.astimezone(timezone.utc).replace(tzinfo=None)
        prayer_end_utc = prayer_start_utc + timedelta(minutes=20)
        if starts_at < prayer_end_utc and ends_at > prayer_start_utc:
            return True
    return False

@app.get("/")
def home():
    database_ready = False
    try:
        ensure_schema()
        database_ready = True
    except DatabaseUnavailable:
        pass
    return render_template("index.html", database_ready=database_ready)

@app.get("/login")
def login_page():
    return render_template("login.html")

@app.post("/login")
@app.post("/api/login")
def login_teacher():
    data = request.form or (request.get_json(silent=True) or {})
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    try:
        ensure_schema()
        conn = connect_db()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT * FROM teachers WHERE email = %s", (email,))
                teacher = cursor.fetchone()
        finally:
            conn.close()

        stored_hash = teacher.get("password_hash") if teacher else None
        if stored_hash and check_password_hash(stored_hash, password):
            session.clear()
            session["active_teacher_id"] = teacher["id"]
            session.permanent = True
            return redirect(url_for("dashboard"))
        return render_template("login.html", error_key="auth.invalidCredentials"), 401
    except (DatabaseUnavailable, psycopg2.Error):
        app.logger.exception("Login failed")
        return render_template("login.html", error_key="auth.databaseError"), 503

@app.get("/register")
def register_page():
    return render_template("register.html")

@app.post("/register")
@app.post("/api/register")
def register_teacher():
    data = request.form or (request.get_json(silent=True) or {})
    full_name = str(data.get("full_name", "")).strip()
    email = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))
    phone_number = str(data.get("phone_number", "")).strip()
    country = str(data.get("country", "Egypt")).strip() or "Egypt"
    city = str(data.get("city", "Cairo")).strip() or "Cairo"
    bio = str(data.get("bio", "")).strip()
    prayer_breaks = data.get("prayer_breaks") in ("true", "on", True, 1, "1")

    if not full_name or "@" not in email or len(password) < 8:
        return render_template("register.html", error_key="auth.registrationRequirements"), 400

    hashed_password = generate_password_hash(password)
    booking_link_slug = teacher_booking_slug(email)

    try:
        ensure_schema()
        conn = connect_db()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(
                        """
                        INSERT INTO teachers
                            (full_name, email, password_hash, booking_link_slug,
                             phone_number, bio, country, city, prayer_breaks)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING *
                        """,
                        (
                            full_name,
                            email,
                            hashed_password,
                            booking_link_slug,
                            phone_number,
                            bio,
                            country,
                            city,
                            prayer_breaks,
                        ),
                    )
                    teacher = dict(cursor.fetchone())
        finally:
            conn.close()

        session.clear()
        session["active_teacher_id"] = teacher["id"]
        session.permanent = True
        return redirect(url_for("dashboard"))
    except psycopg2.IntegrityError:
        app.logger.exception("Teacher registration rejected")
        return render_template("register.html", error_key="auth.emailTaken"), 409
    except (DatabaseUnavailable, psycopg2.Error):
        app.logger.exception("Teacher registration failed")
        return render_template("register.html", error_key="auth.databaseError"), 503

@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login_page"))

@app.get("/dashboard")
def dashboard():
    active_id = active_teacher_id()
    if not active_id:
        return redirect(url_for("login_page"))

    try:
        ensure_schema()
        teachers = fetch_all("SELECT * FROM teachers WHERE id = %s", (active_id,))
        availability = fetch_all(
            """
            SELECT a.*, t.full_name AS teacher_name 
            FROM availability a 
            JOIN teachers t ON t.id = a.teacher_id 
            WHERE a.teacher_id = %s
            ORDER BY a.day_of_week, a.start_time
            """,
            (active_id,)
        )
    except DatabaseUnavailable:
        teachers, availability = [], []

    active_teacher = teachers[0] if teachers else None
    if active_teacher is None:
        session.clear()
        return redirect(url_for("login_page"))
    return render_template(
        "dashboard.html",
        active_teacher=active_teacher,
        availability=availability,
    )

@app.post("/api/update-profile")
def update_profile():
    active_id = active_teacher_id()
    if not active_id:
        return redirect(url_for("login_page"))

    full_name = request.form.get("full_name", "").strip()
    phone_number = request.form.get("phone_number", "").strip()
    bio = request.form.get("bio", "").strip()
    country = request.form.get("country", "Egypt").strip()
    city = request.form.get("city", "Cairo").strip()
    prayer_breaks = True if request.form.get("prayer_breaks") in ['true', 'on', True] else False

    try:
        ensure_schema()
        conn = connect_db()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE teachers 
                        SET full_name = %s, bio = %s, phone_number = %s,
                            country = %s, city = %s, prayer_breaks = %s
                        WHERE id = %s
                        """,
                        (
                            full_name,
                            bio,
                            phone_number,
                            country,
                            city,
                            prayer_breaks,
                            active_id,
                        )
                    )
        finally:
            conn.close()
        return redirect(url_for("dashboard"))
    except Exception:
        return redirect(url_for("dashboard"))

@app.get("/book")
def book():
    try:
        ensure_schema()
        teachers = fetch_all("SELECT id, full_name, timezone, country, city, booking_link_slug, prayer_breaks FROM teachers")
        raw_availability = fetch_all("SELECT id, teacher_id, day_of_week, start_time, end_time FROM availability")
        
        # تحويل كائنات الوقت (time) إلى نصوص (strings) لتجنب مشكلة الـ JSON Serialization
        availability = []
        for slot in raw_availability:
            slot_copy = dict(slot)
            if slot_copy.get('start_time'):
                slot_copy['start_time'] = str(slot_copy['start_time'])
            if slot_copy.get('end_time'):
                slot_copy['end_time'] = str(slot_copy['end_time'])
            availability.append(slot_copy)

        bookings = fetch_all(
            """
            SELECT teacher_id, session_type_id, starts_at, ends_at 
            FROM bookings 
            WHERE status = 'confirmed'
            """
        )
        
        # تحويل تواريخ الحجوزات إلى نصوص آمنة أيضاً
        safe_bookings = []
        for b in bookings:
            b_copy = dict(b)
            if b_copy.get('starts_at'):
                b_copy['starts_at'] = b_copy['starts_at'].isoformat()
            if b_copy.get('ends_at'):
                b_copy['ends_at'] = b_copy['ends_at'].isoformat()
            safe_bookings.append(b_copy)

        session_types = fetch_all(
            """
            SELECT id, teacher_id, title, duration_minutes, description 
            FROM session_types 
            ORDER BY teacher_id, title
            """
        )
    except DatabaseUnavailable:
        teachers, availability, safe_bookings, session_types = [], [], [], []

    return render_template(
        "book.html",
        teachers=teachers,
        availability=availability,
        bookings=safe_bookings,
        session_types=session_types,
    )

@app.get("/api/prayer-times")
def api_prayer_times():
    teacher_id = request.args.get("teacher_id", type=int)
    date_str = request.args.get("date", datetime.now().strftime("%d-%m-%Y"))
    if teacher_id:
        try:
            teacher = fetch_all(
                "SELECT city, country FROM teachers WHERE id = %s",
                (teacher_id,),
            )
        except DatabaseUnavailable:
            return jsonify({"error": "The database is unavailable."}), 503
        if not teacher:
            return jsonify({"error": "Teacher not found."}), 404
        city, country = teacher[0]["city"], teacher[0]["country"]
    else:
        city = request.args.get("city", "Cairo")
        country = request.args.get("country", "Egypt")
    times = get_prayer_times(city, country, date_str)
    return jsonify(
        {
            "city": city,
            "country": country,
            "date": date_str,
            "prayer_times": times,
        }
    )

@app.post("/api/availability")
def set_availability():
    teacher_id = active_teacher_id()
    if not teacher_id:
        return redirect(url_for("login_page"))

    day_of_week = int(request.form.get("day_of_week", 0))
    start_time = request.form.get("start_time")
    end_time = request.form.get("end_time")

    try:
        ensure_schema()
        conn = connect_db()
        try:
            with conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO availability (teacher_id, day_of_week, start_time, end_time)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        """,
                        (teacher_id, day_of_week, start_time, end_time)
                    )
        finally:
            conn.close()
        return redirect(url_for("dashboard"))
    except DatabaseUnavailable:
        return jsonify({"error": "خطأ في الاتصال بقاعدة البيانات"}), 503

@app.post("/api/bookings")
def create_booking():
    data = request.form if request.form else (request.get_json(silent=True) or {})

    raw_teacher_id = data.get("teacher_id")
    if not raw_teacher_id:
        return jsonify({"error": "Teacher information is required."}), 400
    try:
        teacher_id = int(raw_teacher_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Teacher information is invalid."}), 400

    student_name = str(data.get("student_name", "")).strip()
    student_email = str(data.get("student_email", "")).strip().lower()
    notes = str(data.get("notes", "")).strip()

    if not student_name or "@" not in student_email:
        return jsonify({"error": "A student name and valid email are required."}), 400

    try:
        starts_at = parse_utc_datetime(data.get("starts_at"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        ensure_schema()
        conn = connect_db()
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute(
                        """
                        SELECT id, city, country, timezone, prayer_breaks
                        FROM teachers
                        WHERE id = %s
                        """,
                        (teacher_id,),
                    )
                    teacher = cursor.fetchone()
                    if teacher is None:
                        return jsonify({"error": "Teacher not found."}), 404

                    raw_session_type_id = data.get("session_type_id")
                    if raw_session_type_id:
                        try:
                            session_type_id = int(raw_session_type_id)
                        except (TypeError, ValueError):
                            return jsonify({"error": "Session type is invalid."}), 400
                        cursor.execute(
                            """
                            SELECT id, duration_minutes
                            FROM session_types
                            WHERE id = %s AND teacher_id = %s
                            """,
                            (session_type_id, teacher_id),
                        )
                        session_type = cursor.fetchone()
                    else:
                        cursor.execute(
                            """
                            SELECT id, duration_minutes
                            FROM session_types
                            WHERE teacher_id = %s
                            ORDER BY id
                            LIMIT 1
                            """,
                            (teacher_id,),
                        )
                        session_type = cursor.fetchone()
                        session_type_id = session_type["id"] if session_type else None

                    if session_type is None:
                        return jsonify({"error": "This teacher has no published session type."}), 400

                    duration_minutes = int(session_type["duration_minutes"])
                    ends_at = starts_at + timedelta(minutes=duration_minutes)
                    teacher_timezone = ZoneInfo(teacher["timezone"] or "UTC")
                    local_start = starts_at.replace(
                        tzinfo=timezone.utc
                    ).astimezone(teacher_timezone)
                    local_end = ends_at.replace(
                        tzinfo=timezone.utc
                    ).astimezone(teacher_timezone)
                    if local_start.date() != local_end.date():
                        return jsonify({"error": "The session must finish on the same local day."}), 409

                    if starts_at <= datetime.now(timezone.utc).replace(tzinfo=None):
                        return jsonify({"error": "Bookings must be scheduled in the future."}), 400

                    day_of_week = (local_start.weekday() + 1) % 7
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
                        (
                            teacher_id,
                            day_of_week,
                            local_start.time(),
                            local_end.time(),
                        ),
                    )
                    if cursor.fetchone() is None:
                        return jsonify({"error": "The selected time is outside the teacher's availability."}), 409

                    if teacher["prayer_breaks"] and prayer_overlap(
                        starts_at,
                        ends_at,
                        teacher,
                        teacher_timezone,
                    ):
                        return jsonify({"error": "The selected time overlaps a prayer break."}), 409

                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (teacher_id,))
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
                        return jsonify({"error": "That time has already been booked."}), 409

                    cursor.execute(
                        """
                        INSERT INTO bookings
                            (teacher_id, session_type_id, student_name, student_email,
                             duration_minutes, starts_at, ends_at, notes)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            teacher_id,
                            session_type_id,
                            student_name,
                            student_email,
                            duration_minutes,
                            starts_at,
                            ends_at,
                            notes,
                        )
                    )
        finally:
            conn.close()
        return jsonify({"success": True, "message": "Booking confirmed."})
    except ZoneInfoNotFoundError:
        return jsonify({"error": "The teacher timezone is invalid."}), 500
    except DatabaseUnavailable:
        return jsonify({"error": "The database is unavailable."}), 503
    except psycopg2.Error:
        app.logger.exception("Booking creation failed")
        return jsonify({"error": "The booking could not be saved."}), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)