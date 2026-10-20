import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, render_template, request, redirect, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "super-secret-key-maw3edy-2024")

# الاتصال بقاعدة البيانات مباشرة
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres.vdqovdrjrvgesuvivppc:f6L4pbjf2SMOy11U@db.vdqovdrjrvgesuvivppc.supabase.co:5432/postgres")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

# ==========================================
# 1. صفحات الموقع الأساسية
# ==========================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        # استقبال البيانات الجديدة
        full_name = request.form.get('full_name')
        email = request.form.get('email')
        password = request.form.get('password')
        phone_number = request.form.get('phone_number') # رقم الواتساب
        country = request.form.get('country')
        city = request.form.get('city')
        timezone = request.form.get('timezone')
        profession = request.form.get('profession', 'معلم أونلاين')
        bio = request.form.get('bio', '')
        
        slug = email.split('@')[0].lower().replace('.', '-')
        password_hash = generate_password_hash(password)

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO teachers (full_name, email, password_hash, booking_link_slug, phone_number, country, city, timezone, profession, bio, prayer_breaks)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, False)
            """, (full_name, email, password_hash, slug, phone_number, country, city, timezone, profession, bio))
            conn.commit()
            flash('تم إنشاء الحساب بنجاح! يرجى تسجيل الدخول.', 'success')
            return redirect('/login')
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            return render_template('register.html', error_key="auth.emailExists")
        finally:
            cur.close()
            conn.close()

    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT * FROM teachers WHERE email = %s", (email,))
        teacher = cur.fetchone()
        cur.close()
        conn.close()

        if teacher:
            try:
                if check_password_hash(teacher['password_hash'], password):
                    session['teacher_id'] = teacher['id']
                    return redirect('/dashboard')
            except:
                pass
        
        return render_template('login.html', error_key="auth.invalidCredentials")
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('teacher_id', None)
    return redirect('/login')

# ==========================================
# 2. لوحة التحكم (Dashboard)
# ==========================================

@app.route('/dashboard')
def dashboard():
    if 'teacher_id' not in session:
        return redirect('/login')
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM teachers WHERE id = %s", (session['teacher_id'],))
    teacher = cur.fetchone()

    # جلب الحجوزات القادمة
    cur.execute("""
        SELECT student_name, student_whatsapp, duration_minutes, starts_at, notes 
        FROM bookings 
        WHERE teacher_id = %s ORDER BY starts_at ASC
    """, (session['teacher_id'],))
    bookings = cur.fetchall()
    
    cur.close()
    conn.close()
    
    return render_template('dashboard.html', teacher=teacher, bookings=bookings)

@app.route('/api/toggle_prayer', methods=['POST'])
def toggle_prayer():
    if 'teacher_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    prayer_breaks = data.get('prayer_breaks', False)
    
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE teachers SET prayer_breaks = %s WHERE id = %s", (prayer_breaks, session['teacher_id']))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({"success": True})

# ==========================================
# 3. نظام الحجز للطالب (Booking Engine)
# ==========================================

@app.route('/book/<slug>')
def book_page(slug):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, full_name, profession, bio, timezone, city, country, prayer_breaks FROM teachers WHERE booking_link_slug = %s", (slug,))
    teacher = cur.fetchone()
    cur.close()
    conn.close()

    if not teacher:
        return "المعلم غير موجود", 404

    return render_template('book.html', teacher=teacher)

@app.route('/api/availability/<int:teacher_id>')
def api_availability(teacher_id):
    conn = get_db_connection()
    cur = conn.cursor()
    
    # جلب المعلم لمعرفة دولته ومدينته
    cur.execute("SELECT city, country, timezone, prayer_breaks FROM teachers WHERE id = %s", (teacher_id,))
    teacher = cur.fetchone()

    # جلب أيام وأوقات العمل
    cur.execute("SELECT day_of_week, start_time, end_time FROM availability WHERE teacher_id = %s", (teacher_id,))
    availability = cur.fetchall()
    
    for row in availability:
        row['start_time'] = str(row['start_time'])
        row['end_time'] = str(row['end_time'])

    # جلب مواقيت الصلاة من الـ API إذا كانت الخاصية مفعلة
    prayer_times = []
    if teacher and teacher.get('prayer_breaks'):
        try:
            city = teacher['city']
            country = teacher['country']
            res = requests.get(f"http://api.aladhan.com/v1/timingsByCity?city={city}&country={country}&method=5").json()
            timings = res['data']['timings']
            # نرسل أهم الأوقات للواجهة لاستبعادها
            prayer_times = [timings['Dhuhr'], timings['Asr'], timings['Maghrib'], timings['Isha']]
        except Exception as e:
            print("Error fetching prayers:", e)

    cur.close()
    conn.close()

    return jsonify({
        'availability': availability,
        'prayer_times': prayer_times,
        'timezone': teacher['timezone'] if teacher else 'UTC'
    })

@app.route('/api/bookings', methods=['POST'])
def create_booking():
    data = request.json
    teacher_id = data.get('teacher_id')
    student_name = data.get('student_name')
    student_email = data.get('student_email')
    student_whatsapp = data.get('student_whatsapp')
    duration_minutes = data.get('duration_minutes')
    starts_at = data.get('starts_at') # سيأتي بصيغة ISO Time
    notes = data.get('notes', '')

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO bookings (teacher_id, student_name, student_email, student_whatsapp, duration_minutes, starts_at, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (teacher_id, student_name, student_email, student_whatsapp, duration_minutes, starts_at, notes))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"success": False, "error": str(e)}), 400
    finally:
        cur.close()
        conn.close()

if __name__ == '__main__':
    app.run(debug=True)