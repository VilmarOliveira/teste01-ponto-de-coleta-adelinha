import os
import secrets
import sqlite3
import hashlib
import base64
from io import BytesIO
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from core import TZ, calculate_fees, classify_package

ROOT = Path(__file__).parent
TERMS_VERSION = "MINUTA-TESTE-v1"
PIX_KEY = "38145273000105"
ADDRESS = "Rua Afonso Borges de Freitas, 795, Jardim Adelinha, Franca/SP, CEP 14406-848"


def terms_text():
    return """CONDIÇÕES DO SERVIÇO — MINUTA-TESTE-v1
1. Cadastro e identificação: o cliente informa nome, CPF e WhatsApp e recebe um ID único.
2. Pacotes: pequeno, soma até 80 cm e peso até 10 kg, R$ 5,00; grande, soma até 150 cm e peso até 20 kg, R$ 10,00. Acima dos limites não será recebido.
3. Aviso e prazo: o prazo começa após confirmação do aviso. São 4 dias corridos, contando o aviso como primeiro dia. Desde o quinto dia, R$ 0,50 por dia corrido e pacote, inclusive dias fechados. Reenvio não reinicia o prazo.
4. Pagamento e retirada: Pix com conferência manual. Titular ou terceiro com documento próprio pode retirar. Nome, documento e assinatura são registrados. Pode haver retirada parcial.
5. Privacidade: cadastro, aceite, pagamento e retirada são mantidos para operar e comprovar o serviço.
MINUTA PENDENTE DE REVISÃO. Não há nesta versão regra sobre abandono, descarte, indenização ou responsabilidade."""


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", secrets.token_hex(32)),
        DATABASE=os.environ.get("DATABASE", str(ROOT / "data" / "adelinha.db")),
        STAFF_USER=os.environ.get("STAFF_USER", "admin"),
        STAFF_PASSWORD_HASH=os.environ.get("STAFF_PASSWORD_HASH", generate_password_hash("adelinha-teste")),
        PASSWORD_LINK_MINUTES=30,
        LOGIN_LIMIT=5,
        LOGIN_WINDOW_MINUTES=15,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
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

    def client_login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("client_id"):
                return redirect(url_for("client_login"))
            return view(*args, **kwargs)
        return wrapped

    @app.before_request
    def csrf_protection():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        if request.method == "POST" and not secrets.compare_digest(request.form.get("csrf_token", ""), session["csrf_token"]):
            abort(400, "Token CSRF inválido. Atualize a página e tente novamente.")

    @app.context_processor
    def shared_values():
        return {"csrf_token": session.get("csrf_token"), "address": ADDRESS}

    def login_blocked(kind, identifier):
        cutoff = (now() - timedelta(minutes=app.config["LOGIN_WINDOW_MINUTES"])).isoformat()
        count = db().execute("SELECT COUNT(*) FROM login_attempts WHERE kind=? AND identifier=? AND succeeded=0 AND attempted_at>=?", (kind, identifier, cutoff)).fetchone()[0]
        return count >= app.config["LOGIN_LIMIT"]

    def record_login(kind, identifier, succeeded):
        db().execute("INSERT INTO login_attempts(kind,identifier,succeeded,attempted_at) VALUES(?,?,?,?)", (kind, identifier, int(succeeded), now().isoformat()))
        if succeeded:
            db().execute("DELETE FROM login_attempts WHERE kind=? AND identifier=? AND succeeded=0", (kind, identifier))
        db().commit()

    def find_clients(query):
        """Usa a mesma fonte e os mesmos critérios em todas as buscas do painel."""
        query = query.strip()
        if not query:
            return []
        like = f"%{query}%"
        phone = digits(query)
        phone_like = f"%{phone}%" if phone else like
        return db().execute(
            """SELECT id, public_id, name, phone FROM clients
               WHERE name LIKE ? COLLATE NOCASE
                  OR public_id LIKE ? COLLATE NOCASE
                  OR phone LIKE ?
               ORDER BY name LIMIT 30""",
            (like, like, phone_like),
        ).fetchall()

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
        password = request.form.get("password", "")
        if request.form.get("terms_accept") != "yes":
            flash("É necessário aceitar as condições do serviço.", "error")
            return redirect(url_for("index") + "#cadastro")
        if len(name) < 5 or len(cpf) != 11 or len(phone) < 10 or not signature.startswith("data:image/png;base64,") or len(password) < 8 or password != request.form.get("password_confirm"):
            flash("Confira os dados, a assinatura e as senhas (mínimo de 8 caracteres).", "error")
            return redirect(url_for("index") + "#cadastro")
        accepted_at = now().isoformat()
        if db().execute("SELECT 1 FROM clients WHERE cpf=?", (cpf,)).fetchone():
            flash("Já existe cadastro com este CPF. Procure a equipe para recuperar a senha.", "error")
            return redirect(url_for("client_login"))
        cur = db().execute(
            "INSERT INTO clients(name, cpf, phone, terms_version, terms_text, signature, accepted_at, password_hash) VALUES(?,?,?,?,?,?,?,?)",
            (name, cpf, phone, TERMS_VERSION, terms_text(), signature, accepted_at, generate_password_hash(password)),
        )
        client_id = cur.lastrowid
        public_id = f"ADL-{client_id:06d}"
        db().execute("UPDATE clients SET public_id=? WHERE id=?", (public_id, client_id))
        db().commit()
        return render_template("success.html", name=name, public_id=public_id, address=ADDRESS)

    @app.get("/condicoes")
    def terms():
        return render_template("terms.html", version=TERMS_VERSION, exact_text=terms_text())

    @app.get("/login")
    def login():
        return render_template("login.html")

    @app.post("/login")
    def login_post():
        user = request.form.get("user", "")
        if login_blocked("staff", user):
            flash("Muitas tentativas. Aguarde 15 minutos.", "error")
            return redirect(url_for("login"))
        valid = user == app.config["STAFF_USER"] and check_password_hash(app.config["STAFF_PASSWORD_HASH"], request.form.get("password", ""))
        record_login("staff", user, valid)
        if valid:
            session.clear()
            session["staff"] = request.form["user"]
            return redirect(url_for("dashboard"))
        flash("Usuário ou senha inválidos.", "error")
        return redirect(url_for("login"))

    @app.get("/cliente/login")
    def client_login():
        return render_template("client_login.html")

    @app.post("/cliente/login")
    def client_login_post():
        cpf = digits(request.form.get("cpf", ""))
        if login_blocked("client", cpf):
            flash("Muitas tentativas. Aguarde 15 minutos.", "error")
            return redirect(url_for("client_login"))
        client = db().execute("SELECT id,password_hash FROM clients WHERE cpf=?", (cpf,)).fetchone()
        valid = bool(client and client["password_hash"] and check_password_hash(client["password_hash"], request.form.get("password", "")))
        record_login("client", cpf, valid)
        if valid:
            session.clear()
            session["client_id"] = client["id"]
            return redirect(url_for("client_area"))
        flash("CPF ou senha inválidos. Se você ainda não possui senha, procure a equipe.", "error")
        return redirect(url_for("client_login"))

    @app.post("/cliente/sair")
    def client_logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/cliente")
    @client_login_required
    def client_area():
        client = db().execute("SELECT * FROM clients WHERE id=?", (session["client_id"],)).fetchone()
        packages = db().execute("SELECT * FROM packages WHERE client_id=? ORDER BY created_at DESC", (client["id"],)).fetchall()
        pending = []
        for package in packages:
            if package["status"] != "retirado":
                item = {**dict(package), **fees(package)}
                item["deadline"] = (datetime.fromisoformat(package["notified_at"]).astimezone(TZ).date() + timedelta(days=3)).strftime("%d/%m/%Y") if package["notified_at"] else None
                pending.append(item)
        receipts = db().execute("SELECT DISTINCT pu.id,pu.picked_at,pu.total_paid FROM pickups pu JOIN pickup_packages pp ON pp.pickup_id=pu.id JOIN packages p ON p.id=pp.package_id WHERE p.client_id=? ORDER BY pu.picked_at DESC", (client["id"],)).fetchall()
        return render_template("client_area.html", client=client, pending=pending, packages=packages, receipts=receipts)

    @app.get("/cliente/condicoes-aceitas")
    @client_login_required
    def client_accepted_terms():
        client = db().execute("SELECT * FROM clients WHERE id=?", (session["client_id"],)).fetchone()
        return render_template("accepted_terms.html", client=client)

    @app.get("/cliente/comprovantes/<int:pickup_id>")
    @client_login_required
    def client_receipt(pickup_id):
        pickup = db().execute("SELECT pu.* FROM pickups pu WHERE pu.id=? AND EXISTS(SELECT 1 FROM pickup_packages pp JOIN packages p ON p.id=pp.package_id WHERE pp.pickup_id=pu.id AND p.client_id=?)", (pickup_id, session["client_id"])).fetchone()
        if not pickup:
            abort(404)
        packages = db().execute("SELECT p.id package_id,p.public_id,pp.base_amount,pp.late_amount FROM pickup_packages pp JOIN packages p ON p.id=pp.package_id WHERE pp.pickup_id=? AND p.client_id=?", (pickup_id, session["client_id"])).fetchall()
        packages = db().execute("SELECT p.public_id,pp.base_amount,pp.late_amount FROM pickup_packages pp JOIN packages p ON p.id=pp.package_id WHERE pp.pickup_id=? AND p.client_id=?", (pickup_id, session["client_id"])).fetchall()
        return render_template("receipt.html", pickup=pickup, packages=packages, pix=PIX_KEY, client_copy=True)

    @app.post("/sair")
    def logout():
        session.clear()
        return redirect(url_for("index"))

    @app.get("/painel")
    @login_required
    def dashboard():
        query = request.args.get("q", "").strip()
        clients = find_clients(query)
        packages = db().execute("""
            SELECT p.*, c.name client_name, c.public_id client_public_id, c.phone
            FROM packages p JOIN clients c ON c.id=p.client_id
            ORDER BY CASE p.status WHEN 'aguardando_aviso' THEN 0 WHEN 'aguardando_retirada' THEN 1 ELSE 2 END, p.created_at DESC
        """).fetchall()
        rows = [{**dict(p), **fees(p)} for p in packages]
        return render_template("dashboard.html", clients=clients, packages=rows, q=query, pix=PIX_KEY)

    @app.get("/painel/clientes/busca")
    @login_required
    def client_search():
        clients = find_clients(request.args.get("q", ""))
        return {"clients": [dict(client) for client in clients]}

    @app.get("/painel/clientes/<int:client_id>")
    @login_required
    def staff_client(client_id):
        client = db().execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            abort(404)
        packages = db().execute("SELECT * FROM packages WHERE client_id=? ORDER BY created_at DESC", (client_id,)).fetchall()
        receipts = db().execute("SELECT DISTINCT pu.* FROM pickups pu JOIN pickup_packages pp ON pp.pickup_id=pu.id JOIN packages p ON p.id=pp.package_id WHERE p.client_id=? ORDER BY pu.picked_at DESC", (client_id,)).fetchall()
        return render_template("staff_client.html", client=client, packages=packages, receipts=receipts)

    @app.get("/painel/pacotes/<int:package_id>")
    @login_required
    def package_detail(package_id):
        package = db().execute("""SELECT p.*,c.name client_name,c.public_id client_public_id,c.id owner_id
            FROM packages p JOIN clients c ON c.id=p.client_id WHERE p.id=?""", (package_id,)).fetchone()
        if not package:
            abort(404)
        withdrawal = db().execute("""SELECT pu.*,pp.package_public_id,pp.client_id snapshot_client_id,
            pp.client_name snapshot_client_name,pp.client_public_id snapshot_client_public_id,pp.service_amount,pp.late_days,pp.late_amount,pp.total_amount
            FROM pickup_packages pp JOIN pickups pu ON pu.id=pp.pickup_id WHERE pp.package_id=?""", (package_id,)).fetchone()
        return render_template("package_detail.html", package=package, withdrawal=withdrawal)

    @app.post("/painel/clientes/<int:client_id>/link-senha")
    @login_required
    def password_link(client_id):
        if request.form.get("identity_checked") != "yes" or not db().execute("SELECT 1 FROM clients WHERE id=?", (client_id,)).fetchone():
            abort(400, "Confirme a conferência manual da identidade.")
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        expires = (now() + timedelta(minutes=app.config["PASSWORD_LINK_MINUTES"])).isoformat()
        db().execute("UPDATE password_tokens SET used_at=? WHERE client_id=? AND used_at IS NULL", (now().isoformat(), client_id))
        db().execute("INSERT INTO password_tokens(client_id,token_hash,expires_at,created_at,created_by) VALUES(?,?,?,?,?)", (client_id, token_hash, expires, now().isoformat(), session["staff"]))
        db().commit()
        link = url_for("set_password", token=raw, _external=True)
        return render_template("password_link.html", link=link, expires=expires)

    @app.route("/definir-senha/<token>", methods=["GET", "POST"])
    def set_password(token):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = db().execute("SELECT * FROM password_tokens WHERE token_hash=?", (token_hash,)).fetchone()
        valid = row and not row["used_at"] and datetime.fromisoformat(row["expires_at"]) > now()
        if not valid:
            return render_template("set_password.html", invalid=True), 410
        if request.method == "POST":
            password = request.form.get("password", "")
            if len(password) < 8 or password != request.form.get("password_confirm"):
                flash("As senhas devem ser iguais e ter ao menos 8 caracteres.", "error")
                return redirect(request.url)
            db().execute("UPDATE clients SET password_hash=? WHERE id=?", (generate_password_hash(password), row["client_id"]))
            db().execute("UPDATE password_tokens SET used_at=? WHERE id=?", (now().isoformat(), row["id"]))
            db().commit()
            flash("Senha definida. Você já pode entrar.", "success")
            return redirect(url_for("client_login"))
        return render_template("set_password.html", invalid=False)

    @app.post("/pacotes")
    @login_required
    def add_package():
        try:
            dimensions = [float(request.form[k].replace(",", ".")) for k in ("width", "height", "length")]
            weight = float(request.form["weight"].replace(",", "."))
        except (ValueError, KeyError):
            flash("Informe medidas e peso válidos.", "error")
            return redirect(url_for("dashboard"))
        try:
            client_id = int(request.form.get("client_id", ""))
        except ValueError:
            flash("Selecione um cliente válido antes de registrar o pacote.", "error")
            return redirect(url_for("dashboard"))
        selected_client = db().execute("SELECT id,name,public_id FROM clients WHERE id=?", (client_id,)).fetchone()
        if not selected_client:
            flash("Selecione um cliente válido antes de registrar o pacote.", "error")
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
        cur = db().execute("INSERT INTO pickups(receiver_name, receiver_document, signature, staff, picked_at, base_total, late_total, total_paid, payment_method, payment_confirmed) VALUES(?,?,?,?,?,?,?,?,?,1)",
            (receiver, document, signature, session["staff"], picked_at, sum(x["base"] for x in totals), sum(x["late_fee"] for x in totals), sum(x["total"] for x in totals), "Pix"))
        pickup_id = cur.lastrowid
        for package, total in zip(packages, totals):
            owner = db().execute("SELECT id,name,public_id FROM clients WHERE id=?", (package["client_id"],)).fetchone()
            db().execute("""INSERT INTO pickup_packages(
                pickup_id,package_id,base_amount,late_amount,package_public_id,client_id,client_name,client_public_id,
                service_amount,late_days,total_amount) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (pickup_id, package["id"], total["base"], total["late_fee"], package["public_id"], owner["id"], owner["name"], owner["public_id"], total["base"], total["late_days"], total["total"]))
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
        packages = db().execute("SELECT p.id package_id,p.public_id, pp.base_amount, pp.late_amount FROM pickup_packages pp JOIN packages p ON p.id=pp.package_id WHERE pp.pickup_id=?", (pickup_id,)).fetchall()
        return render_template("receipt.html", pickup=pickup, packages=packages, pix=PIX_KEY)

    @app.get("/comprovantes/<int:pickup_id>/pacotes/<int:package_id>.pdf")
    def receipt_pdf(pickup_id, package_id):
        row = db().execute("""SELECT pu.*,pp.package_public_id,pp.client_id snapshot_client_id,p.client_id owner_id,
            pp.client_name snapshot_client_name,pp.client_public_id snapshot_client_public_id,pp.service_amount,pp.late_days,pp.late_amount,pp.total_amount
            FROM pickup_packages pp JOIN pickups pu ON pu.id=pp.pickup_id JOIN packages p ON p.id=pp.package_id
            WHERE pp.pickup_id=? AND pp.package_id=?""", (pickup_id, package_id)).fetchone()
        if not row:
            abort(404)
        if not session.get("staff") and session.get("client_id") not in (row["snapshot_client_id"], row["owner_id"]):
            abort(403)
        pdf = build_receipt_pdf(row)
        filename = f"comprovante-{row['package_public_id'] or package_id}.pdf"
        return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=filename)

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
    CREATE TABLE IF NOT EXISTS clients (id INTEGER PRIMARY KEY, public_id TEXT UNIQUE, name TEXT NOT NULL, cpf TEXT NOT NULL, phone TEXT NOT NULL, terms_version TEXT NOT NULL, terms_text TEXT, signature TEXT NOT NULL, accepted_at TEXT NOT NULL, password_hash TEXT);
    CREATE TABLE IF NOT EXISTS packages (id INTEGER PRIMARY KEY, public_id TEXT UNIQUE, client_id INTEGER NOT NULL REFERENCES clients(id), tracking TEXT, width REAL NOT NULL, height REAL NOT NULL, length REAL NOT NULL, weight REAL NOT NULL, shelf TEXT, category TEXT NOT NULL, base_price REAL NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, notified_at TEXT, picked_at TEXT);
    CREATE TABLE IF NOT EXISTS pickups (id INTEGER PRIMARY KEY, receiver_name TEXT NOT NULL, receiver_document TEXT NOT NULL, signature TEXT NOT NULL, staff TEXT NOT NULL, picked_at TEXT NOT NULL, base_total REAL NOT NULL, late_total REAL NOT NULL, total_paid REAL NOT NULL, payment_method TEXT, payment_confirmed INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS pickup_packages (pickup_id INTEGER REFERENCES pickups(id), package_id INTEGER UNIQUE REFERENCES packages(id), base_amount REAL NOT NULL, late_amount REAL NOT NULL, package_public_id TEXT, client_id INTEGER, client_name TEXT, client_public_id TEXT, service_amount REAL, late_days INTEGER, total_amount REAL, PRIMARY KEY(pickup_id, package_id));
    CREATE TABLE IF NOT EXISTS pickups (id INTEGER PRIMARY KEY, receiver_name TEXT NOT NULL, receiver_document TEXT NOT NULL, signature TEXT NOT NULL, staff TEXT NOT NULL, picked_at TEXT NOT NULL, base_total REAL NOT NULL, late_total REAL NOT NULL, total_paid REAL NOT NULL, payment_confirmed INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS pickup_packages (pickup_id INTEGER REFERENCES pickups(id), package_id INTEGER UNIQUE REFERENCES packages(id), base_amount REAL NOT NULL, late_amount REAL NOT NULL, PRIMARY KEY(pickup_id, package_id));
    CREATE TABLE IF NOT EXISTS password_tokens (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id), token_hash TEXT UNIQUE NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, used_at TEXT);
    CREATE TABLE IF NOT EXISTS login_attempts (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, identifier TEXT NOT NULL, succeeded INTEGER NOT NULL, attempted_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(kind,identifier,attempted_at);
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(clients)")}
    if "password_hash" not in columns:
        connection.execute("ALTER TABLE clients ADD COLUMN password_hash TEXT")
    if "terms_text" not in columns:
        connection.execute("ALTER TABLE clients ADD COLUMN terms_text TEXT")
    ensure_columns(connection, "pickups", {"payment_method": "TEXT"})
    ensure_columns(connection, "pickup_packages", {
        "package_public_id": "TEXT", "client_id": "INTEGER", "client_name": "TEXT", "client_public_id": "TEXT",
        "service_amount": "REAL", "late_days": "INTEGER", "total_amount": "REAL",
    })
    connection.commit()
    connection.close()


def ensure_columns(connection, table, definitions):
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, data_type in definitions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {data_type}")


def display(value, money=False):
    if value is None or value == "":
        return "Não registrado"
    return f"R$ {float(value):.2f}".replace(".", ",") if money else str(value)


def build_receipt_pdf(withdrawal):
    """Cria uma cópia imutável usando exclusivamente os valores salvos na retirada."""
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4, pageCompression=0)
    width, height = A4
    pdf.setTitle(f"Comprovante {display(withdrawal['package_public_id'])}")
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(45, height - 50, "PONTO DE COLETA ADELINHA")
    pdf.setFont("Helvetica", 9)
    lines = [
        "CNPJ: 38.145.273/0001-05",
        ADDRESS,
        "WhatsApp: (16) 98800-4966",
        "",
        f"Pacote: {display(withdrawal['package_public_id'])}",
        f"Cliente: {display(withdrawal['snapshot_client_name'])}",
        f"ID do cliente: {display(withdrawal['snapshot_client_public_id'])}",
        f"Retirado por: {display(withdrawal['receiver_name'])}",
        f"Documento: {display(withdrawal['receiver_document'])}",
        f"Data e hora: {display(withdrawal['picked_at'])} (America/Sao_Paulo)",
        f"Valor do serviço: {display(withdrawal['service_amount'], True)}",
        f"Dias de atraso: {display(withdrawal['late_days'])}",
        f"Taxa extra: {display(withdrawal['late_amount'], True)}",
        f"Total pago: {display(withdrawal['total_amount'], True)}",
        f"Forma de pagamento: {display(withdrawal['payment_method'])}",
        f"Pagamento confirmado pelo atendente: {'Sim' if withdrawal['payment_confirmed'] == 1 else 'Não registrado'}",
        f"Atendente: {display(withdrawal['staff'])}",
    ]
    y = height - 72
    for line in lines:
        pdf.drawString(45, y, line)
        y -= 16
    statement = "Confirmo que retirei o pacote identificado neste comprovante e que o pagamento informado foi realizado."
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(45, y - 10, statement)
    y -= 55
    pdf.setFont("Helvetica", 9)
    pdf.drawString(45, y, "Assinatura de quem retirou:")
    signature = withdrawal["signature"]
    if signature and signature.startswith("data:image/png;base64,"):
        image_data = base64.b64decode(signature.split(",", 1)[1])
        pdf.drawImage(ImageReader(BytesIO(image_data)), 45, y - 100, width=260, height=90, preserveAspectRatio=True, anchor="sw")
    else:
        pdf.drawString(45, y - 20, "Não registrado")
    pdf.save()
    output.seek(0)
    return output


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
