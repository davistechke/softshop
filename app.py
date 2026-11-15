import os, re, time, shutil, sqlite3, secrets, hmac, zipfile, uuid
from datetime import datetime
from functools import wraps
from flask import (Flask, request, session, redirect, url_for, render_template,
                   jsonify, send_file, abort, flash, g)

def _load_env():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)
_load_env()
import gateway

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.abspath(os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data")))
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

def _secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    p = os.path.join(DATA_DIR, ".secret_key")
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(secrets.token_hex(32))
    return open(p).read().strip()

app = Flask(__name__)
app.secret_key = _secret_key()
app.config["MAX_CONTENT_LENGTH"] = int(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# All credentials come from .env (or the host's environment variables) - none are hardcoded.
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MAX_DOWNLOADS = int(os.environ.get("MAX_DOWNLOADS", "5"))

# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT DEFAULT '',
  price INTEGER NOT NULL, stored_name TEXT NOT NULL, original_name TEXT NOT NULL,
  size INTEGER DEFAULT 0, active INTEGER DEFAULT 1, created_at TEXT);
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT UNIQUE NOT NULL, product_id INTEGER NOT NULL,
  phone TEXT, amount INTEGER NOT NULL, status TEXT DEFAULT 'pending', reference TEXT,
  receipt TEXT, downloads INTEGER DEFAULT 0, created_at TEXT, paid_at TEXT);
"""
with sqlite3.connect(os.path.join(DATA_DIR, "shop.db")) as _c:
    _c.executescript(SCHEMA)

def db():
    if "db" not in g:
        g.db = sqlite3.connect(os.path.join(DATA_DIR, "shop.db"))
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def _close(_):
    d = g.pop("db", None)
    if d:
        d.close()

def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

@app.template_filter("kes")
def kes(n):
    return f"KES {int(n):,}"

@app.template_filter("filesize")
def filesize(n):
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024

# ---------------------------------------------------------------- helpers
def normalize_phone(raw):
    p = re.sub(r"[^\d]", "", raw or "")
    if p.startswith("0") and len(p) == 10:
        p = "254" + p[1:]
    elif len(p) == 9 and p[0] in "17":
        p = "254" + p
    return p if re.fullmatch(r"254[17]\d{8}", p) else None

def clean_relpath(p):
    parts = [s for s in (p or "").replace("\\", "/").split("/") if s not in ("", ".", "..")]
    return "/".join(parts)

def mark_paid(order, receipt=None):
    db().execute("UPDATE orders SET status='paid', receipt=COALESCE(?,receipt), paid_at=? "
                 "WHERE id=? AND status!='paid'", (receipt, now(), order["id"]))
    db().commit()

def mark_failed(order):
    db().execute("UPDATE orders SET status='failed' WHERE id=? AND status='pending'", (order["id"],))
    db().commit()

def get_order(token):
    return db().execute("SELECT o.*, p.name AS pname, p.original_name, p.stored_name, p.size "
                        "FROM orders o JOIN products p ON p.id=o.product_id WHERE o.token=?",
                        (token,)).fetchone()

# ---------------------------------------------------------------- storefront (no login)
@app.route("/")
def index():
    products = db().execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC").fetchall()
    return render_template("index.html", products=products)

@app.route("/p/<int:pid>")
def product(pid):
    p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        abort(404)
    return render_template("product.html", p=p)

@app.route("/buy/<int:pid>", methods=["POST"])
def buy(pid):
    p = db().execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        abort(404)
    phone = normalize_phone(request.form.get("phone"))
    if not phone:
        flash("Enter a valid M-Pesa number, e.g. 0712345678.", "error")
        return redirect(url_for("product", pid=pid))
    token = secrets.token_urlsafe(24)
    db().execute("INSERT INTO orders(token,product_id,phone,amount,created_at) VALUES(?,?,?,?,?)",
                 (token, pid, phone, p["price"], now()))
    db().commit()
    order = get_order(token)
    try:
        ref = gateway.initiate(token=token, phone=phone, amount=p["price"],
                               description=f"{p['name']}"[:40])
        db().execute("UPDATE orders SET reference=? WHERE id=?", (ref, order["id"]))
        db().commit()
    except gateway.GatewayError as e:
        mark_failed(order)
        flash(str(e), "error")
        return redirect(url_for("product", pid=pid))
    return redirect(url_for("order_page", token=token))

@app.route("/order/<token>")
def order_page(token):
    o = get_order(token)
    if not o:
        abort(404)
    return render_template("order.html", o=o, simulate=gateway.SIMULATE,
                           max_downloads=MAX_DOWNLOADS)

@app.route("/order/<token>/status")
def order_status(token):
    o = get_order(token)
    if not o:
        abort(404)
    if o["status"] == "pending" and o["reference"]:
        st, receipt, amt = gateway.check(o["reference"])      # server-side confirmation
        if st == "paid" and (not amt or int(float(amt)) >= o["amount"]):
            mark_paid(o, receipt)
        elif st == "failed":
            mark_failed(o)
        o = get_order(token)
    return jsonify(status=o["status"])

@app.route("/order/<token>/simulate", methods=["POST"])
def simulate(token):
    if not gateway.SIMULATE:
        abort(404)
    o = get_order(token)
    if not o:
        abort(404)
    mark_paid(o, "SIMULATED")
    return redirect(url_for("order_page", token=token))

@app.route("/webhook/makamesco", methods=["GET", "POST"])
@app.route("/callback", methods=["GET", "POST"])
@app.route("/webhook/makamsco", methods=["GET", "POST"])      # legacy spelling
def webhook():
    """Makamesco payment notification. Register in the Makamesco dashboard as
    https://<your-domain>/callback?token=<MAKAMESCO_CALLBACK_TOKEN>"""
    if not gateway.webhook_ok(request):
        abort(403)
    if request.method == "GET":
        return jsonify(ok=True)
    data = request.get_json(silent=True) or request.form.to_dict()
    token, ref, status, receipt, amount = gateway.parse_webhook(data)
    row = None
    if token:
        row = get_order(token)
    if not row and ref:
        r = db().execute("SELECT token FROM orders WHERE reference=?", (ref,)).fetchone()
        row = get_order(r["token"]) if r else None
    if not row:
        return jsonify(ok=True)                     # unknown order: acknowledge, ignore
    if status == "paid":
        if amount and int(float(amount)) < row["amount"]:
            return jsonify(ok=True)                 # underpaid: do not unlock
        mark_paid(row, receipt)                     # unlocks the download immediately
    elif status == "failed":
        mark_failed(row)
    return jsonify(ok=True)

@app.route("/download/<token>")
def download(token):
    o = get_order(token)
    if not o or o["status"] != "paid":
        abort(403)
    if MAX_DOWNLOADS and o["downloads"] >= MAX_DOWNLOADS:
        return render_template("order.html", o=o, simulate=False, max_downloads=MAX_DOWNLOADS,
                               limit_reached=True), 403
    path = os.path.join(UPLOAD_DIR, o["stored_name"])
    if not os.path.isfile(path):
        abort(404)
    db().execute("UPDATE orders SET downloads=downloads+1 WHERE id=?", (o["id"],))
    db().commit()
    mime = "application/vnd.android.package-archive" if o["original_name"].lower().endswith(".apk") else None
    return send_file(path, as_attachment=True, download_name=o["original_name"], mimetype=mime)

# ---------------------------------------------------------------- admin (/admin)
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]
app.jinja_env.globals["csrf_token"] = csrf_token

def admin_only(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get("admin"):
            return redirect(url_for("admin"))
        if request.method == "POST" and not hmac.compare_digest(request.form.get("csrf", ""), csrf_token()):
            abort(400)
        return f(*a, **k)
    return w

FAILED_LOGINS = {}

@app.route("/admin", methods=["GET", "POST"])
def admin():
    if request.method == "POST" and not session.get("admin"):
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        tries = [t for t in FAILED_LOGINS.get(ip, []) if time.time() - t < 600]
        if not (ADMIN_USERNAME and ADMIN_PASSWORD):
            flash("Admin login is not configured. Set ADMIN_USERNAME and ADMIN_PASSWORD in .env.", "error")
        elif len(tries) >= 5:
            flash("Too many attempts. Try again in 10 minutes.", "error")
        elif (hmac.compare_digest(request.form.get("username", "").strip(), ADMIN_USERNAME) &
              hmac.compare_digest(request.form.get("password", ""), ADMIN_PASSWORD)):
            session["admin"] = True
            FAILED_LOGINS.pop(ip, None)
            return redirect(url_for("admin"))
        else:
            FAILED_LOGINS[ip] = tries + [time.time()]
            flash("Wrong username or password.", "error")
    if not session.get("admin"):
        return render_template("admin_login.html")
    products = db().execute(
        "SELECT p.*, (SELECT COUNT(*) FROM orders o WHERE o.product_id=p.id AND o.status='paid') AS sales "
        "FROM products p ORDER BY id DESC").fetchall()
    orders = db().execute("SELECT o.*, p.name AS pname FROM orders o JOIN products p ON p.id=o.product_id "
                          "ORDER BY o.id DESC LIMIT 100").fetchall()
    revenue = db().execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid'").fetchone()[0]
    return render_template("admin.html", products=products, orders=orders, revenue=revenue)

@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("admin"))

@app.route("/admin/upload", methods=["POST"])
@admin_only
def admin_upload():
    files = request.files.getlist("files")
    paths = request.form.getlist("paths")
    name = request.form.get("name", "").strip()
    desc = request.form.get("description", "").strip()
    try:
        price = int(request.form.get("price", "0"))
    except ValueError:
        price = 0
    if not files or price < 1:
        return jsonify(error="Choose at least one file and set a price of KES 1 or more."), 400
    paths = [clean_relpath(paths[i] if i < len(paths) and paths[i] else f.filename)
             for i, f in enumerate(files)]
    uid = uuid.uuid4().hex
    single = len(files) == 1 and "/" not in paths[0]
    if single:
        original = paths[0] or "software.bin"
        stored = uid + os.path.splitext(original)[1].lower()
        files[0].save(os.path.join(UPLOAD_DIR, stored))
    else:
        tops = {p.split("/")[0] for p in paths if "/" in p}
        base = tops.pop() if len(tops) == 1 and all("/" in p for p in paths) else "software"
        original, stored = f"{base}.zip", uid + ".zip"
        with zipfile.ZipFile(os.path.join(UPLOAD_DIR, stored), "w", zipfile.ZIP_DEFLATED) as zf:
            for f, p in zip(files, paths):
                if p:
                    with zf.open(p, "w", force_zip64=True) as dest:
                        shutil.copyfileobj(f.stream, dest)
    size = os.path.getsize(os.path.join(UPLOAD_DIR, stored))
    db().execute("INSERT INTO products(name,description,price,stored_name,original_name,size,created_at) "
                 "VALUES(?,?,?,?,?,?,?)",
                 (name or os.path.splitext(original)[0], desc, price, stored, original, size, now()))
    db().commit()
    return jsonify(ok=True)

@app.route("/admin/product/<int:pid>/update", methods=["POST"])
@admin_only
def admin_update(pid):
    try:
        price = max(1, int(request.form.get("price", "1")))
    except ValueError:
        price = 1
    db().execute("UPDATE products SET name=?, price=?, description=?, active=? WHERE id=?",
                 (request.form.get("name", "").strip() or "Untitled", price,
                  request.form.get("description", "").strip(),
                  1 if request.form.get("active") else 0, pid))
    db().commit()
    flash("Saved.", "ok")
    return redirect(url_for("admin"))

@app.route("/admin/product/<int:pid>/delete", methods=["POST"])
@admin_only
def admin_delete(pid):
    p = db().execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if p:
        paid = db().execute("SELECT COUNT(*) FROM orders WHERE product_id=? AND status='paid'", (pid,)).fetchone()[0]
        if paid:
            db().execute("UPDATE products SET active=0 WHERE id=?", (pid,))
            flash("Product has paid orders, so it was hidden instead of deleted (buyers keep access).", "ok")
        else:
            db().execute("DELETE FROM orders WHERE product_id=?", (pid,))
            db().execute("DELETE FROM products WHERE id=?", (pid,))
            try:
                os.remove(os.path.join(UPLOAD_DIR, p["stored_name"]))
            except OSError:
                pass
            flash("Deleted.", "ok")
        db().commit()
    return redirect(url_for("admin"))

if __name__ == "__main__":
    app.run(debug=True, port=5000)
