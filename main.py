import os
import psycopg2
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        return (
            "<h1>⚠️ حدث خطأ في الاتصال:</h1> "
            "<p>تعذر الاتصال بقاعدة البيانات حاليًا. يرجى المحاولة لاحقًا.</p>"
        ), 503

    try:
        # محاولة الاتصال بقاعدة البيانات في Supabase
        conn = psycopg2.connect(database_url, connect_timeout=5)
        conn.close()
        return "<h1>🎉 مبروك يا أحمد! السيرفر يعمل بنجاح وتم الاتصال بقاعدة البيانات!</h1>"
    except Exception:
        return (
            "<h1>⚠️ حدث خطأ في الاتصال:</h1> "
            "<p>تعذر الاتصال بقاعدة البيانات حاليًا. يرجى المحاولة لاحقًا.</p>"
        ), 503

if __name__ == '__main__':
    # تشغيل التطبيق ليراه الجميع
    app.run(host='0.0.0.0', port=5000)