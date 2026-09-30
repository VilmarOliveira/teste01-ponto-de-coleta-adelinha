import os
import secrets
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from core import TZ, calculate_fees, classify_package

ROOT = Path(__file__).parent
TERMS_VERSION = "MINUTA-TESTE-v1"
PIX_KEY = "38145273000105"
ADDRESS = "Rua Afonso Borges de Freitas, 795, Jardim Adelinha, Franca/SP, CEP 14406-848"


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", secrets.token_hex(32)),
        DATABASE=os.environ.get("DATABASE", str(ROOT / "data" / "adelinha.db")),
        STAFF_USER=os.environ.get("STAFF_USER", "admin"),
        STAFF_PASSWORD_HASH=os.environ.get("STAFF_PASSWORD_HASH", generate_password_hash("adelinha-teste")),
    )
    if test_config:
        app.config.update(test_config)

    def db():
        if "db" not in request.environ:
            connection = sqlite3.connect(app.config["DATABASE"])
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            request.environ["db"] = connection
        return request.environ["db"]

    @app.teardown_request
    def close_db(_error=None):
        connection = request.environ.pop("db", None)
        if connection:
            connection.close()

    def now():
        return datetime.now(TZ)

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("staff"):
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)
        return wrapped

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        """Endpoint simples usado pela hospedagem para confirmar que o app iniciou."""
        db().execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.post("/cadastro")
    def register():
        name = request.form.get("name", "").strip()
        cpf = digits(request.form.get("cpf", ""))
        phone = digits(request.form.get("phone", ""))
        signature = request.form.get("signature", "")
        if len(name) < 5 or len(cpf) != 11 or len(phone) < 10 or not signature.startswith("data:image/png;base64,"):
            flash("Confira nome, CPF, telefone e assinatura.", "error")
            return redirect(url_for("index") + "#cadastro")
        accepted_at = now().isoformat()
        cur = db().execute(
            "INSERT INTO clients(name, cpf, phone, terms_version, signature, accepted_at) VALUES(?,?,?,?,?,?)",
            (name, cpf, phone, TERMS_VERSION, signature, accepted_at),
        )
        client_id = cur.lastrowid
        public_id = f"ADL-{client_id:06d}"
        db().execute("UPDATE clients SET public_id=? WHERE id=?", (public_id, client_id))
        db().commit()
        return render_template("success.html", name=name, public_id=public_id, address=ADDRESS)

    @app.get("/condicoes")
    def terms():
        return render_template("terms.html", version=TERMS_VERSION)

    @app.get("/login")
    def login():
        return render_template("login.html")

    @app.post("/login")
    def login_post():
        if request.form.get("user") == app.config["STAFF_USER"] and check_password_hash(app.config["STAFF_PASSWORD_HASH"], request.form.get("password", "")):
            session.clear()
            session["staff"] = request.form["user"]
            return redirect(url_for("dashboard"))
        flash("Usuário ou senha inválidos.", "error")
        return redirect(url_for("login"))

    @app.post("/sair")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/painel")
    @login_required
    def dashboard():
        query = request.args.get("q", "").strip()
        clients = []
        if query:
            like = f"%{query}%"
            clients = db().execute("SELECT id, public_id, name, phone FROM clients WHERE name LIKE ? OR public_id LIKE ? OR phone LIKE ? ORDER BY name LIMIT 30", (like, like, like)).fetchall()
        packages = db().execute("""
            SELECT p.*, c.name client_name, c.public_id client_public_id, c.phone
            FROM packages p JOIN clients c ON c.id=p.client_id
            ORDER BY CASE p.status WHEN 'aguardando_aviso' THEN 0 WHEN 'aguardando_retirada' THEN 1 ELSE 2 END, p.created_at DESC
        """).fetchall()
        rows = [{**dict(p), **fees(p)} for p in packages]
        return render_template("dashboard.html", clients=clients, packages=rows, q=query, pix=PIX_KEY)

    @app.post("/pacotes")
    @login_required
    def add_package():
        try:
            dimensions = [float(request.form[k].replace(",", ".")) for k in ("width", "height", "length")]
            weight = float(request.form["weight"].replace(",", "."))
            client_id = int(request.form["client_id"])
        except (ValueError, KeyError):
            flash("Informe medidas e peso válidos.", "error")
            return redirect(url_for("dashboard"))
        try:
            category, price = classify_package(*dimensions, weight)
        except ValueError as error:
            flash(f"Pacote recusado: {error}", "error")
            return redirect(url_for("dashboard"))
        cur = db().execute("""INSERT INTO packages(client_id, tracking, width, height, length, weight, shelf, category, base_price, status, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,'aguardando_aviso',?)""", (client_id, request.form.get("tracking", "").strip(), *dimensions, weight, request.form.get("shelf", "").strip(), category, price, now().isoformat()))
        package_id = f"PCT-{cur.lastrowid:06d}"
        db().execute("UPDATE packages SET public_id=? WHERE id=?", (package_id, cur.lastrowid))
        db().commit()
        flash(f"{package_id} registrado como {category}.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/pacotes/<int:package_id>/avisar")
    @login_required
    def notify(package_id):
        package = db().execute("SELECT status FROM packages WHERE id=?", (package_id,)).fetchone()
        if not package:
            abort(404)
        if package["status"] == "aguardando_aviso":
            db().execute("UPDATE packages SET status='aguardando_retirada', notified_at=? WHERE id=?", (now().isoformat(), package_id))
            db().commit()
        flash("Aviso confirmado. O prazo foi iniciado sem alterar avisos anteriores.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/retiradas")
    @login_required
    def pickup():
        ids = [int(x) for x in request.form.getlist("package_ids")]
        if not ids:
            flash("Selecione ao menos um pacote.", "error")
            return redirect(url_for("dashboard"))
        placeholders = ",".join("?" * len(ids))
        packages = db().execute(f"SELECT * FROM packages WHERE id IN ({placeholders}) AND status='aguardando_retirada'", ids).fetchall()
        if len(packages) != len(set(ids)):
            flash("Retirada bloqueada: pacote inválido ou já retirado.", "error")
            return redirect(url_for("dashboard"))
        receiver = request.form.get("receiver_name", "").strip()
        document = request.form.get("receiver_document", "").strip()
        signature = request.form.get("pickup_signature", "")
        if not receiver or not document or not signature.startswith("data:image/png;base64,"):
            flash("Informe quem retirou, documento e assinatura.", "error")
            return redirect(url_for("dashboard"))
        totals = [fees(p) for p in packages]
        picked_at = now().isoformat()
        cur = db().execute("INSERT INTO pickups(receiver_name, receiver_document, signature, staff, picked_at, base_total, late_total, total_paid, payment_confirmed) VALUES(?,?,?,?,?,?,?,?,1)",
            (receiver, document, signature, session["staff"], picked_at, sum(x["base"] for x in totals), sum(x["late_fee"] for x in totals), sum(x["total"] for x in totals)))
        pickup_id = cur.lastrowid
        for package, total in zip(packages, totals):
            db().execute("INSERT INTO pickup_packages(pickup_id, package_id, base_amount, late_amount) VALUES(?,?,?,?)", (pickup_id, package["id"], total["base"], total["late_fee"]))
            db().execute("UPDATE packages SET status='retirado', picked_at=? WHERE id=?", (picked_at, package["id"]))
        db().commit()
        return redirect(url_for("receipt", pickup_id=pickup_id))

    @app.get("/comprovantes/<int:pickup_id>")
    @login_required
    def receipt(pickup_id):
        pickup = db().execute("SELECT * FROM pickups WHERE id=?", (pickup_id,)).fetchone()
        if not pickup:
            abort(404)
        packages = db().execute("SELECT p.public_id, pp.base_amount, pp.late_amount FROM pickup_packages pp JOIN packages p ON p.id=pp.package_id WHERE pp.pickup_id=?", (pickup_id,)).fetchall()
        return render_template("receipt.html", pickup=pickup, packages=packages, pix=PIX_KEY)

    with app.app_context():
        init_db(app.config["DATABASE"])
    return app


def digits(value):
    return "".join(c for c in value if c.isdigit())


def fees(package, today=None):
    return calculate_fees(package["base_price"], package["notified_at"], package["picked_at"], today)


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE IF NOT EXISTS clients (id INTEGER PRIMARY KEY, public_id TEXT UNIQUE, name TEXT NOT NULL, cpf TEXT NOT NULL, phone TEXT NOT NULL, terms_version TEXT NOT NULL, signature TEXT NOT NULL, accepted_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS packages (id INTEGER PRIMARY KEY, public_id TEXT UNIQUE, client_id INTEGER NOT NULL REFERENCES clients(id), tracking TEXT, width REAL NOT NULL, height REAL NOT NULL, length REAL NOT NULL, weight REAL NOT NULL, shelf TEXT, category TEXT NOT NULL, base_price REAL NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, notified_at TEXT, picked_at TEXT);
    CREATE TABLE IF NOT EXISTS pickups (id INTEGER PRIMARY KEY, receiver_name TEXT NOT NULL, receiver_document TEXT NOT NULL, signature TEXT NOT NULL, staff TEXT NOT NULL, picked_at TEXT NOT NULL, base_total REAL NOT NULL, late_total REAL NOT NULL, total_paid REAL NOT NULL, payment_confirmed INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS pickup_packages (pickup_id INTEGER REFERENCES pickups(id), package_id INTEGER UNIQUE REFERENCES packages(id), base_amount REAL NOT NULL, late_amount REAL NOT NULL, PRIMARY KEY(pickup_id, package_id));
    """)
    connection.commit()
    connection.close()


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
