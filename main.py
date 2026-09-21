import os
import psycopg2
from flask import Flask

app = Flask(__name__)

# جلب رابط قاعدة البيانات المحمي من أداة Secrets
DATABASE_URL = os.environ.get("DATABASE_URL")

@app.route('/')
def home():
    try:
        # محاولة الاتصال بقاعدة البيانات في Supabase
        conn = psycopg2.connect(DATABASE_URL)
        conn.close()
        return "<h1>🎉 مبروك يا أحمد! السيرفر يعمل بنجاح وتم الاتصال بقاعدة البيانات!</h1>"
    except Exception as e:
        return f"<h1>⚠️ حدث خطأ في الاتصال:</h1> <p>{e}</p>"

if __name__ == '__main__':
    # تشغيل التطبيق ليراه الجميع
    app.run(host='0.0.0.0', port=8080)