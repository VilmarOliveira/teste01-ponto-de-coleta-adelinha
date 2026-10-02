import os
import secrets
import sqlite3
import hashlib
import base64
import math
import re
from email.utils import parseaddr
from io import BytesIO
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_file, session, url_for
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
import qrcode
from werkzeug.security import check_password_hash, generate_password_hash

from core import TZ, calculate_fees, classify_package, pix_payload

ROOT = Path(__file__).parent
TERMS_VERSION = "MINUTA-TESTE-v1"
PIX_KEY = "38145273000105"
PIX_RECEIVER_NAME = "AMOR INFINITO MARKETING E SOLUCOES EMPRESARIAIS"
# O CEP não é exibido aqui porque não foi confirmado para o endereço de retirada.
PICKUP_ADDRESS = "RUA AFONSO BORGES DE FREITAS, 795, JARDIM ADELINHA, FRANCA – SP"
ADDRESS = PICKUP_ADDRESS  # compatibilidade com comprovantes e integrações existentes


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
            client = db().execute("SELECT active,session_version FROM clients WHERE id=?", (session.get("client_id"),)).fetchone() if session.get("client_id") else None
            if not client or not client["active"] or client["session_version"] != session.get("client_session_version"):
                session.clear()
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
        return {"csrf_token": session.get("csrf_token"), "address": PICKUP_ADDRESS,
                "pickup_address": PICKUP_ADDRESS, "pix_receiver_name_full": PIX_RECEIVER_NAME}

    @app.template_filter("local_datetime")
    def local_datetime(value):
        if not value:
            return "Não registrado"
        return datetime.fromisoformat(value).astimezone(TZ).strftime("%d/%m/%Y %H:%M")

    def audit(action, entity_type, entity_id, details=""):
        db().execute("INSERT INTO audit_logs(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (session.get("staff", "sistema"), action, entity_type, entity_id, details, now().isoformat()))

    def package_form_data():
        return {key: request.form.get(key, "") for key in ("client_id", "client_label", "tracking", "shelf", "weight", "width", "height", "length")}

    def residential_address_form():
        return {key: request.form.get(key, "").strip() for key in
                ("residential_cep", "residential_street", "residential_number",
                 "residential_district", "residential_city", "residential_state",
                 "residential_complement")}

    def valid_residential_address(data):
        required = ("residential_street", "residential_number", "residential_district", "residential_city")
        return (len(digits(data["residential_cep"])) == 8
                and all(data[field] for field in required)
                and bool(re.fullmatch(r"[A-Za-z]{2}", data["residential_state"])))

    def valid_email(value):
        """E-mail é opcional, mas, quando informado, deve ter formato completo."""
        if not value:
            return True
        parsed = parseaddr(value)[1]
        return parsed == value and len(value) <= 254 and bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value))

    def validate_package_form():
        data = package_form_data()
        tracking, shelf = data["tracking"].strip(), data["shelf"].strip()
        if not tracking or not shelf or not re.fullmatch(r"[A-Za-z0-9]+", tracking) or not re.fullmatch(r"[A-Za-z0-9]+", shelf):
            raise ValueError("Rastreio e prateleira são obrigatórios e devem conter somente letras e números.")
        try:
            client_id = int(data["client_id"])
            numbers = [float(data[key].replace(",", ".")) for key in ("width", "height", "length", "weight")]
        except (ValueError, TypeError):
            raise ValueError("Selecione um cliente válido e informe peso e medidas numéricos.")
        if not all(math.isfinite(value) and value > 0 for value in numbers):
            raise ValueError("Peso e medidas devem ser números maiores que zero.")
        client = db().execute("SELECT id,name,public_id FROM clients WHERE id=? AND active=1", (client_id,)).fetchone()
        if not client:
            raise ValueError("Selecione um cliente válido e ativo.")
        width, height, length, weight = numbers
        category, price = classify_package(width, height, length, weight)
        return data, client, (width, height, length, weight), category, price

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
        email = request.form.get("email", "").strip().lower()
        signature = request.form.get("signature", "")
        password = request.form.get("password", "")
        residential = residential_address_form()
        if request.form.get("terms_accept") != "yes":
            flash("É necessário aceitar as condições do serviço.", "error")
            return redirect(url_for("index") + "#cadastro")
        if len(name) < 5 or len(cpf) != 11 or len(phone) < 10 or not valid_email(email) or not signature.startswith("data:image/png;base64,") or len(password) < 8 or password != request.form.get("password_confirm") or not valid_residential_address(residential):
            flash("Confira os dados, o e-mail, o endereço residencial obrigatório, a assinatura e as senhas (mínimo de 8 caracteres).", "error")
            form = {"name": name, "cpf": request.form.get("cpf", ""), "phone": request.form.get("phone", ""), "email": email, **residential}
            return render_template("index.html", registration_form=form), 400
        accepted_at = now().isoformat()
        if db().execute("SELECT 1 FROM clients WHERE cpf=?", (cpf,)).fetchone():
            flash("Já existe cadastro com este CPF. Procure a equipe para recuperar a senha.", "error")
            return redirect(url_for("client_login"))
        cur = db().execute(
            """INSERT INTO clients(name, cpf, phone, terms_version, terms_text, signature, accepted_at, password_hash,
               residential_cep,residential_street,residential_number,residential_district,residential_city,residential_state,residential_complement,email)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (name, cpf, phone, TERMS_VERSION, terms_text(), signature, accepted_at, generate_password_hash(password),
             digits(residential["residential_cep"]), residential["residential_street"], residential["residential_number"],
             residential["residential_district"], residential["residential_city"], residential["residential_state"].upper(),
             residential["residential_complement"] or None, email or None),
        )
        client_id = cur.lastrowid
        public_id = f"ADL-{client_id:06d}"
        db().execute("UPDATE clients SET public_id=? WHERE id=?", (public_id, client_id))
        db().commit()
        return render_template("success.html", name=name, public_id=public_id, address=ADDRESS)

    @app.get("/condicoes")
    def terms():
        back = request.args.get("next", "")
        if not back.startswith("/") or back.startswith("//"):
            back = url_for("index")
        return render_template("terms.html", version=TERMS_VERSION, exact_text=terms_text(), back=back)

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
        client = db().execute("SELECT id,password_hash,active,session_version FROM clients WHERE cpf=?", (cpf,)).fetchone()
        valid = bool(client and client["active"] and client["password_hash"] and check_password_hash(client["password_hash"], request.form.get("password", "")))
        record_login("client", cpf, valid)
        if valid:
            session.clear()
            session["client_id"] = client["id"]
            session["client_session_version"] = client["session_version"]
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
        return render_template("client_area.html", client=client, pending=pending, packages=packages)

    @app.route("/cliente/editar", methods=["GET", "POST"])
    @client_login_required
    def client_edit():
        client = db().execute("SELECT * FROM clients WHERE id=?", (session["client_id"],)).fetchone()
        if request.method == "GET":
            return render_template("client_edit.html", client=client)
        phone = digits(request.form.get("phone", ""))
        email = request.form.get("email", "").strip().lower()
        residential = residential_address_form()
        if len(phone) < 10 or not valid_email(email) or not valid_residential_address(residential):
            flash("Confira o WhatsApp, o e-mail e todos os campos obrigatórios do endereço residencial.", "error")
            values = {**dict(client), **residential, "phone": request.form.get("phone", ""), "email": email}
            return render_template("client_edit.html", client=values), 400
        db().execute("""UPDATE clients SET phone=?,email=?,residential_cep=?,residential_street=?,residential_number=?,
            residential_district=?,residential_city=?,residential_state=?,residential_complement=? WHERE id=?""",
            (phone, email or None, digits(residential["residential_cep"]), residential["residential_street"],
             residential["residential_number"], residential["residential_district"], residential["residential_city"],
             residential["residential_state"].upper(), residential["residential_complement"] or None, session["client_id"]))
        audit("editar_proprio_cadastro", "cliente", session["client_id"], "telefone, e-mail ou endereço residencial atualizados")
        db().commit()
        flash("Dados atualizados com sucesso", "success")
        return redirect(url_for("client_area"))

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
        package_rows = db().execute("""
            SELECT p.*, c.name client_name, c.public_id client_public_id, c.phone
            FROM packages p JOIN clients c ON c.id=p.client_id
            ORDER BY CASE p.status WHEN 'aguardando_aviso' THEN 0 WHEN 'aguardando_retirada' THEN 1 ELSE 2 END, p.created_at DESC
        """).fetchall()
        # Uma única coleção alimenta a única seção "Pacotes" da tela. A chave
        # também protege a interface caso uma futura consulta ganhe outro JOIN.
        packages = {package["id"]: package for package in package_rows}.values()
        rows = [{**dict(package), **fees(package)} for package in packages]
        package_form = session.pop("package_form", {})
        settings = db().execute("SELECT pix_receiver_name FROM settings WHERE id=1").fetchone()
        return render_template("dashboard.html", clients=clients, packages=rows, q=query, pix=PIX_KEY, package_form=package_form, pix_receiver_name=settings["pix_receiver_name"])

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
        pending_count = db().execute("SELECT COUNT(*) FROM packages WHERE client_id=? AND status!='retirado'", (client_id,)).fetchone()[0]
        linked_count = db().execute("SELECT COUNT(*) FROM packages WHERE client_id=?", (client_id,)).fetchone()[0]
        return render_template("staff_client.html", client=client, packages=packages, pending_count=pending_count, linked_count=linked_count)

    @app.post("/painel/clientes/<int:client_id>/editar")
    @login_required
    def edit_client(client_id):
        client = db().execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client:
            abort(404)
        name = request.form.get("name", "").strip()
        cpf, phone = digits(request.form.get("cpf", "")), digits(request.form.get("phone", ""))
        email = request.form.get("email", "").strip().lower()
        residential = residential_address_form()
        duplicate = db().execute("SELECT 1 FROM clients WHERE cpf=? AND id!=?", (cpf, client_id)).fetchone()
        if len(name) < 5 or len(cpf) != 11 or len(phone) < 10 or not valid_email(email) or duplicate or not valid_residential_address(residential):
            flash("Confira nome, CPF, WhatsApp, e-mail e todos os campos obrigatórios do endereço residencial. O CPF não pode estar em outro cadastro.", "error")
            return redirect(url_for("staff_client", client_id=client_id))
        db().execute("""UPDATE clients SET name=?,cpf=?,phone=?,email=?,residential_cep=?,residential_street=?,residential_number=?,
            residential_district=?,residential_city=?,residential_state=?,residential_complement=? WHERE id=?""",
            (name, cpf, phone, email or None, digits(residential["residential_cep"]), residential["residential_street"],
             residential["residential_number"], residential["residential_district"], residential["residential_city"],
             residential["residential_state"].upper(), residential["residential_complement"] or None, client_id))
        audit("editar", "cliente", client_id, "nome, CPF ou telefone atualizados")
        db().commit()
        flash("Cadastro atualizado. Contratos e comprovantes históricos não foram alterados.", "success")
        return redirect(url_for("staff_client", client_id=client_id))

    @app.post("/painel/clientes/<int:client_id>/senha")
    @login_required
    def admin_client_password(client_id):
        password = request.form.get("password", "")
        if len(password) < 8 or password != request.form.get("password_confirm"):
            flash("As senhas devem ser iguais e ter ao menos 8 caracteres.", "error")
            return redirect(url_for("staff_client", client_id=client_id))
        if not db().execute("SELECT 1 FROM clients WHERE id=?", (client_id,)).fetchone():
            abort(404)
        db().execute("UPDATE clients SET password_hash=?,session_version=session_version+1 WHERE id=?", (generate_password_hash(password), client_id))
        db().execute("UPDATE password_tokens SET used_at=? WHERE client_id=? AND used_at IS NULL", (now().isoformat(), client_id))
        audit("alterar_senha", "cliente", client_id, "sessões anteriores invalidadas")
        db().commit()
        flash("Nova senha definida e sessões anteriores invalidadas.", "success")
        return redirect(url_for("staff_client", client_id=client_id))

    @app.post("/painel/clientes/<int:client_id>/remover")
    @login_required
    def remove_client(client_id):
        client = db().execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if not client or request.form.get("confirmation") != client["public_id"]:
            flash("Confirmação inválida. Digite o ID exibido.", "error")
            return redirect(url_for("staff_client", client_id=client_id))
        pending = db().execute("SELECT COUNT(*) FROM packages WHERE client_id=? AND status!='retirado'", (client_id,)).fetchone()[0]
        linked = db().execute("SELECT COUNT(*) FROM packages WHERE client_id=?", (client_id,)).fetchone()[0]
        if pending:
            flash("Não é possível excluir ou desativar enquanto houver pacotes pendentes.", "error")
        elif linked or client["signature"] or client["terms_version"]:
            db().execute("UPDATE clients SET active=0,session_version=session_version+1 WHERE id=?", (client_id,))
            audit("desativar", "cliente", client_id, "histórico preservado")
            db().commit()
            flash("Cliente desativado. O histórico foi preservado e o acesso foi bloqueado.", "success")
        else:
            audit("excluir", "cliente", client_id, "sem registros vinculados")
            db().execute("DELETE FROM password_tokens WHERE client_id=?", (client_id,))
            db().execute("DELETE FROM clients WHERE id=?", (client_id,))
            db().commit()
            flash("Cliente excluído definitivamente.", "success")
            return redirect(url_for("dashboard"))
        return redirect(url_for("staff_client", client_id=client_id))

    @app.post("/painel/configuracoes/pix")
    @login_required
    def pix_settings():
        db().execute("UPDATE settings SET pix_receiver_name=? WHERE id=1", (PIX_RECEIVER_NAME,))
        audit("configurar_pix", "configuracao", 1, "nome do recebedor atualizado")
        db().commit()
        flash("Configuração Pix atualizada.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/painel/pacotes/<int:package_id>")
    @login_required
    def package_detail(package_id):
        package = db().execute("""SELECT p.*,c.name client_name,c.public_id client_public_id,c.id owner_id
            FROM packages p JOIN clients c ON c.id=p.client_id WHERE p.id=?""", (package_id,)).fetchone()
        if not package:
            abort(404)
        withdrawal = db().execute("""SELECT pu.*,pp.package_public_id,pp.client_id snapshot_client_id,
            pp.client_name snapshot_client_name,pp.client_public_id snapshot_client_public_id,pp.tracking_code snapshot_tracking,
            pp.package_category snapshot_category,pp.notified_at snapshot_notified_at,pp.service_amount,pp.late_days,pp.late_amount,pp.total_amount
            FROM pickup_packages pp JOIN pickups pu ON pu.id=pp.pickup_id WHERE pp.package_id=?""", (package_id,)).fetchone()
        pickup_packages = []
        if withdrawal:
            pickup_packages = db().execute("SELECT * FROM pickup_packages WHERE pickup_id=? ORDER BY package_id", (withdrawal["id"],)).fetchall()
        return render_template("package_detail.html", package=package, withdrawal=withdrawal, pickup_packages=pickup_packages)

    @app.get("/cliente/pacotes/<int:package_id>")
    @client_login_required
    def client_package_detail(package_id):
        package = db().execute("""SELECT p.*,c.name client_name,c.public_id client_public_id,c.id owner_id
            FROM packages p JOIN clients c ON c.id=p.client_id WHERE p.id=? AND p.client_id=?""", (package_id, session["client_id"])).fetchone()
        if not package:
            abort(404)
        withdrawal = db().execute("""SELECT pu.*,pp.package_public_id,pp.client_id snapshot_client_id,
            pp.client_name snapshot_client_name,pp.client_public_id snapshot_client_public_id,pp.tracking_code snapshot_tracking,
            pp.package_category snapshot_category,pp.notified_at snapshot_notified_at,pp.service_amount,pp.late_days,pp.late_amount,pp.total_amount
            FROM pickup_packages pp JOIN pickups pu ON pu.id=pp.pickup_id WHERE pp.package_id=?""", (package_id,)).fetchone()
        pickup_packages = db().execute("SELECT * FROM pickup_packages WHERE pickup_id=? AND client_id=? ORDER BY package_id", (withdrawal["id"], session["client_id"])).fetchall() if withdrawal else []
        return render_template("package_detail.html", package=package, withdrawal=withdrawal, pickup_packages=pickup_packages, client_view=True)

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
            db().execute("UPDATE clients SET password_hash=?,session_version=session_version+1 WHERE id=?", (generate_password_hash(password), row["client_id"]))
            db().execute("UPDATE password_tokens SET used_at=? WHERE id=?", (now().isoformat(), row["id"]))
            db().commit()
            flash("Senha definida. Você já pode entrar.", "success")
            return redirect(url_for("client_login"))
        return render_template("set_password.html", invalid=False)

    @app.post("/pacotes")
    @login_required
    def add_package():
        try:
            data, selected_client, numbers, category, price = validate_package_form()
        except ValueError as error:
            saved_form = package_form_data()
            saved_form["has_error"] = True
            session["package_form"] = saved_form
            message = str(error)
            if "Limite de 150" in message:
                message = "Pacote recusado: Limite de 150 cm na soma e 20 kg."
            flash(message, "error")
            return redirect(url_for("dashboard") + "#novo-pacote")
        width, height, length, weight = numbers
        cur = db().execute("""INSERT INTO packages(client_id, tracking, width, height, length, weight, shelf, category, base_price, status, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,'aguardando_aviso',?)""", (selected_client["id"], data["tracking"].strip(), width, height, length, weight, data["shelf"].strip(), category, price, now().isoformat()))
        package_id = f"PCT-{cur.lastrowid:06d}"
        db().execute("UPDATE packages SET public_id=? WHERE id=?", (package_id, cur.lastrowid))
        audit("criar", "pacote", cur.lastrowid, package_id)
        db().commit()
        flash(f"{package_id} registrado como {category}.", "success")
        return redirect(url_for("dashboard"))

    @app.route("/painel/pacotes/<int:package_id>/editar", methods=["GET", "POST"])
    @login_required
    def edit_package(package_id):
        package = db().execute("""SELECT p.*,c.name client_name,c.public_id client_public_id FROM packages p JOIN clients c ON c.id=p.client_id WHERE p.id=?""", (package_id,)).fetchone()
        if not package:
            abort(404)
        if package["status"] != "aguardando_aviso" or db().execute("SELECT 1 FROM pickup_packages WHERE package_id=?", (package_id,)).fetchone():
            flash("Este pacote não pode mais ser editado porque o aviso ou a retirada já foi registrado.", "error")
            return redirect(url_for("package_detail", package_id=package_id))
        form_data = session.pop(f"edit_package_{package_id}", None)
        if request.method == "POST":
            try:
                data, selected_client, numbers, category, price = validate_package_form()
            except ValueError as error:
                saved_form = package_form_data()
                saved_form["has_error"] = True
                session[f"edit_package_{package_id}"] = saved_form
                message = "Pacote recusado: Limite de 150 cm na soma e 20 kg." if "Limite de 150" in str(error) else str(error)
                flash(message, "error")
                return redirect(url_for("edit_package", package_id=package_id))
            width, height, length, weight = numbers
            updated = db().execute("""UPDATE packages SET client_id=?,tracking=?,width=?,height=?,length=?,weight=?,shelf=?,category=?,base_price=? WHERE id=? AND status='aguardando_aviso' AND NOT EXISTS(SELECT 1 FROM pickup_packages WHERE package_id=?)""",
                (selected_client["id"], data["tracking"].strip(), width, height, length, weight, data["shelf"].strip(), category, price, package_id, package_id))
            if updated.rowcount != 1:
                db().rollback()
                flash("O status mudou e o pacote não pode mais ser editado.", "error")
                return redirect(url_for("package_detail", package_id=package_id))
            audit("editar", "pacote", package_id, package["public_id"])
            db().commit()
            flash("Pacote atualizado e categoria recalculada.", "success")
            return redirect(url_for("package_detail", package_id=package_id))
        return render_template("package_edit.html", package=package, form_data=form_data or dict(package))

    @app.route("/painel/pacotes/<int:package_id>/excluir", methods=["GET", "POST"])
    @login_required
    def delete_package(package_id):
        package = db().execute("""SELECT p.*,c.name client_name FROM packages p JOIN clients c ON c.id=p.client_id WHERE p.id=?""", (package_id,)).fetchone()
        if not package:
            abort(404)
        allowed = package["status"] == "aguardando_aviso" and not db().execute("SELECT 1 FROM pickup_packages WHERE package_id=?", (package_id,)).fetchone()
        if request.method == "POST":
            if not allowed:
                flash("O pacote não pode ser excluído após a confirmação do aviso.", "error")
                return redirect(url_for("package_detail", package_id=package_id))
            if request.form.get("confirmation") != package["public_id"]:
                flash("Confirmação inválida. Digite o ID do pacote.", "error")
                return redirect(request.url)
            audit("excluir", "pacote", package_id, f"{package['public_id']} · {package['tracking']}")
            deleted = db().execute("DELETE FROM packages WHERE id=? AND status='aguardando_aviso' AND NOT EXISTS(SELECT 1 FROM pickup_packages WHERE package_id=?)", (package_id, package_id))
            if deleted.rowcount != 1:
                db().rollback()
                flash("O status mudou e o pacote não pode mais ser excluído.", "error")
                return redirect(url_for("package_detail", package_id=package_id))
            db().commit()
            flash("Pacote excluído. O cliente e os demais pacotes foram preservados.", "success")
            return redirect(url_for("dashboard"))
        return render_template("package_delete.html", package=package, allowed=allowed)

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

    @app.post("/painel/retirada/resumo")
    @login_required
    def pickup_summary():
        try:
            ids = list(dict.fromkeys(int(value) for value in request.form.getlist("package_ids")))
        except ValueError:
            return {"error": "Seleção inválida."}, 400
        if not ids:
            return {"error": "Selecione ao menos um pacote."}, 400
        placeholders = ",".join("?" * len(ids))
        packages = db().execute(f"SELECT * FROM packages WHERE id IN ({placeholders}) AND status='aguardando_retirada'", ids).fetchall()
        if len(packages) != len(ids):
            return {"error": "Há pacote indisponível para retirada."}, 400
        totals = [fees(package) for package in packages]
        total = round(sum(item["total"] for item in totals), 2)
        setting = db().execute("SELECT pix_receiver_name FROM settings WHERE id=1").fetchone()
        receiver = setting["pix_receiver_name"]
        payload = pix_payload(PIX_KEY, receiver, total) if receiver else None
        qr_data = qr_code_data_url(payload) if payload else None
        return {"total": total, "total_display": f"R$ {total:.2f}".replace(".", ","), "pix_code": payload, "qr_code": qr_data,
            "pix_configured": bool(receiver)}

    @app.post("/retiradas")
    @login_required
    def pickup():
        raw_ids = request.form.getlist("package_ids")
        try:
            # O navegador pode enviar o mesmo checkbox mais de uma vez. Remova
            # repetições mantendo a ordem antes de calcular ou persistir valores.
            ids = list(dict.fromkeys(int(value) for value in raw_ids))
        except ValueError:
            flash("A seleção contém um pacote inválido.", "error")
            return redirect(url_for("dashboard"))
        if not ids:
            flash("Selecione ao menos um pacote.", "error")
            return redirect(url_for("dashboard"))
        receiver = request.form.get("receiver_name", "").strip()
        document_type = request.form.get("document_type", "")
        document = request.form.get("receiver_document", "").strip().upper()
        payment_method = request.form.get("payment_method", "")
        signature = request.form.get("pickup_signature", "")
        payment_confirmed = request.form.get("payment_confirmed") == "yes"
        if not receiver or not valid_document(document_type, document) or payment_method not in ("Pix", "Dinheiro") or not payment_confirmed or not signature.startswith("data:image/png;base64,"):
            flash("Informe nome, tipo e número do documento, pagamento confirmado e assinatura.", "error")
            return redirect(url_for("dashboard"))
        placeholders = ",".join("?" * len(ids))
        connection = db()
        try:
            # Serializa a conferência e a gravação para que duas confirmações
            # simultâneas não consigam retirar o mesmo pacote.
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                f"SELECT pp.package_id,pp.pickup_id FROM pickup_packages pp WHERE pp.package_id IN ({placeholders}) LIMIT 1",
                ids,
            ).fetchone()
            if existing:
                connection.rollback()
                flash("Este pacote já possui uma retirada registrada. Consulte o comprovante existente abaixo.", "error")
                return redirect(url_for("package_detail", package_id=existing["package_id"]))

            packages = connection.execute(
                f"SELECT * FROM packages WHERE id IN ({placeholders}) AND status='aguardando_retirada' ORDER BY id",
                ids,
            ).fetchall()
            if len(packages) != len(ids):
                connection.rollback()
                flash("Retirada bloqueada: há pacote inválido ou que não está aguardando retirada.", "error")
                return redirect(url_for("dashboard"))

            totals = [fees(package) for package in packages]
            picked_at = now().isoformat()
            if payment_method == "Pix" and not connection.execute("SELECT pix_receiver_name FROM settings WHERE id=1 AND pix_receiver_name IS NOT NULL AND trim(pix_receiver_name)!=''").fetchone():
                connection.rollback()
                flash("Configure o nome do recebedor Pix antes de confirmar um pagamento Pix.", "error")
                return redirect(url_for("dashboard"))
            cur = connection.execute("""INSERT INTO pickups(receiver_name,receiver_document,document_type,signature,staff,picked_at,
                base_total,late_total,total_paid,payment_method,payment_confirmed,payment_confirmed_at,payment_confirmed_by)
                VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (receiver, document, document_type, signature, session["staff"], picked_at, sum(x["base"] for x in totals), sum(x["late_fee"] for x in totals), sum(x["total"] for x in totals), payment_method, picked_at, session["staff"]))
            pickup_id = cur.lastrowid
            for package, total in zip(packages, totals):
                owner = connection.execute("SELECT id,name,public_id FROM clients WHERE id=?", (package["client_id"],)).fetchone()
                connection.execute("""INSERT INTO pickup_packages(
                    pickup_id,package_id,base_amount,late_amount,package_public_id,client_id,client_name,client_public_id,
                    tracking_code,package_category,notified_at,service_amount,late_days,total_amount) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pickup_id, package["id"], total["base"], total["late_fee"], package["public_id"], owner["id"], owner["name"], owner["public_id"], package["tracking"], package["category"], package["notified_at"], total["base"], total["late_days"], total["total"]))
                connection.execute("UPDATE packages SET status='retirado', picked_at=? WHERE id=?", (picked_at, package["id"]))
            connection.commit()
        except sqlite3.IntegrityError:
            connection.rollback()
            existing = connection.execute(
                f"SELECT package_id,pickup_id FROM pickup_packages WHERE package_id IN ({placeholders}) LIMIT 1",
                ids,
            ).fetchone()
            if existing:
                flash("Este pacote já possui uma retirada registrada. Consulte o comprovante existente abaixo.", "error")
                return redirect(url_for("package_detail", package_id=existing["package_id"]))
            flash("Não foi possível concluir a retirada. Nenhuma alteração foi salva.", "error")
            return redirect(url_for("dashboard"))
        except Exception:
            connection.rollback()
            raise
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
            pp.client_name snapshot_client_name,pp.client_public_id snapshot_client_public_id,pp.tracking_code snapshot_tracking,
            pp.package_category snapshot_category,pp.notified_at snapshot_notified_at,pp.service_amount,pp.late_days,pp.late_amount,pp.total_amount
            FROM pickup_packages pp JOIN pickups pu ON pu.id=pp.pickup_id JOIN packages p ON p.id=pp.package_id
            WHERE pp.pickup_id=? AND pp.package_id=?""", (pickup_id, package_id)).fetchone()
        if not row:
            abort(404)
        if not session.get("staff") and session.get("client_id") not in (row["snapshot_client_id"], row["owner_id"]):
            abort(403)
        if session.get("staff"):
            receipt_packages = db().execute("SELECT * FROM pickup_packages WHERE pickup_id=? ORDER BY package_id", (pickup_id,)).fetchall()
        else:
            receipt_packages = db().execute("SELECT * FROM pickup_packages WHERE pickup_id=? AND client_id=? ORDER BY package_id", (pickup_id, session["client_id"])).fetchall()
        pdf = build_receipt_pdf(row, receipt_packages)
        filename = f"comprovante-{row['package_public_id'] or package_id}.pdf"
        return send_file(pdf, mimetype="application/pdf", as_attachment=True, download_name=filename)

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
    CREATE TABLE IF NOT EXISTS clients (id INTEGER PRIMARY KEY, public_id TEXT UNIQUE, name TEXT NOT NULL, cpf TEXT NOT NULL, phone TEXT NOT NULL, email TEXT, terms_version TEXT NOT NULL, terms_text TEXT, signature TEXT NOT NULL, accepted_at TEXT NOT NULL, password_hash TEXT, active INTEGER NOT NULL DEFAULT 1, session_version INTEGER NOT NULL DEFAULT 1, residential_cep TEXT, residential_street TEXT, residential_number TEXT, residential_district TEXT, residential_city TEXT, residential_state TEXT, residential_complement TEXT);
    CREATE TABLE IF NOT EXISTS packages (id INTEGER PRIMARY KEY, public_id TEXT UNIQUE, client_id INTEGER NOT NULL REFERENCES clients(id), tracking TEXT, width REAL NOT NULL, height REAL NOT NULL, length REAL NOT NULL, weight REAL NOT NULL, shelf TEXT, category TEXT NOT NULL, base_price REAL NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, notified_at TEXT, picked_at TEXT);
    CREATE TABLE IF NOT EXISTS pickups (id INTEGER PRIMARY KEY, receiver_name TEXT NOT NULL, receiver_document TEXT NOT NULL, document_type TEXT, signature TEXT NOT NULL, staff TEXT NOT NULL, picked_at TEXT NOT NULL, base_total REAL NOT NULL, late_total REAL NOT NULL, total_paid REAL NOT NULL, payment_method TEXT, payment_confirmed INTEGER NOT NULL, payment_confirmed_at TEXT, payment_confirmed_by TEXT);
    CREATE TABLE IF NOT EXISTS pickup_packages (pickup_id INTEGER REFERENCES pickups(id), package_id INTEGER UNIQUE REFERENCES packages(id), base_amount REAL NOT NULL, late_amount REAL NOT NULL, package_public_id TEXT, client_id INTEGER, client_name TEXT, client_public_id TEXT, tracking_code TEXT, package_category TEXT, notified_at TEXT, service_amount REAL, late_days INTEGER, total_amount REAL, PRIMARY KEY(pickup_id, package_id));
    CREATE TABLE IF NOT EXISTS password_tokens (id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id), token_hash TEXT UNIQUE NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL, used_at TEXT);
    CREATE TABLE IF NOT EXISTS login_attempts (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, identifier TEXT NOT NULL, succeeded INTEGER NOT NULL, attempted_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(kind,identifier,attempted_at);
    CREATE TABLE IF NOT EXISTS audit_logs (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER, details TEXT, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), pix_receiver_name TEXT);
    INSERT OR IGNORE INTO settings(id,pix_receiver_name) VALUES(1,'AMOR INFINITO MARKETING E SOLUCOES EMPRESARIAIS');
    """)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(clients)")}
    if "password_hash" not in columns:
        connection.execute("ALTER TABLE clients ADD COLUMN password_hash TEXT")
    if "terms_text" not in columns:
        connection.execute("ALTER TABLE clients ADD COLUMN terms_text TEXT")
    ensure_columns(connection, "clients", {"active": "INTEGER NOT NULL DEFAULT 1", "session_version": "INTEGER NOT NULL DEFAULT 1",
        "residential_cep": "TEXT", "residential_street": "TEXT", "residential_number": "TEXT",
        "residential_district": "TEXT", "residential_city": "TEXT", "residential_state": "TEXT",
        "residential_complement": "TEXT", "email": "TEXT"})
    ensure_columns(connection, "pickups", {"payment_method": "TEXT", "document_type": "TEXT", "payment_confirmed_at": "TEXT", "payment_confirmed_by": "TEXT"})
    ensure_columns(connection, "pickup_packages", {
        "package_public_id": "TEXT", "client_id": "INTEGER", "client_name": "TEXT", "client_public_id": "TEXT",
        "tracking_code": "TEXT", "package_category": "TEXT", "notified_at": "TEXT",
        "service_amount": "REAL", "late_days": "INTEGER", "total_amount": "REAL",
    })
    # O nome completo é mostrado na interface; pix_payload aplica o limite EMV de 25 caracteres.
    connection.execute("UPDATE settings SET pix_receiver_name=? WHERE id=1", (PIX_RECEIVER_NAME,))
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


def valid_document(document_type, number):
    if document_type == "CPF":
        return len(digits(number)) == 11
    if document_type == "RG":
        return bool(re.fullmatch(r"[A-Z0-9.\-]{5,20}", number))
    if document_type == "CNH":
        return len(digits(number)) == 11
    return False


def qr_code_data_url(payload):
    image = qrcode.make(payload)
    output = BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def build_receipt_pdf(withdrawal, packages):
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
        f"Pacote consultado: {display(withdrawal['package_public_id'])}",
        f"Cliente: {display(withdrawal['snapshot_client_name'])}",
        f"ID do cliente: {display(withdrawal['snapshot_client_public_id'])}",
        f"Retirado por: {display(withdrawal['receiver_name'])}",
        f"Documento: {display(withdrawal['document_type'])} {display(withdrawal['receiver_document'])}",
        f"Data e hora: {format_local(withdrawal['picked_at'])}",
        f"Valor do serviço: {display(withdrawal['service_amount'], True)}",
        f"Dias de atraso: {display(withdrawal['late_days'])}",
        f"Taxa extra: {display(withdrawal['late_amount'], True)}",
        f"Total pago: {display(withdrawal['total_amount'], True)}",
        f"Forma de pagamento: {display(withdrawal['payment_method'])}",
        f"Pagamento confirmado: {format_local(withdrawal['payment_confirmed_at'])}",
        f"Atendente: {display(withdrawal['payment_confirmed_by'] or withdrawal['staff'])}",
    ]
    y = height - 72
    for line in lines:
        pdf.drawString(45, y, line)
        y -= 16
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(45, y, "Pacotes desta retirada:")
    y -= 16
    pdf.setFont("Helvetica", 9)
    for item in packages:
        pdf.drawString(45, y, f"{display(item['package_public_id'])} | rastreio {display(item['tracking_code'])} | {display(item['package_category'])} | total {display(item['total_amount'], True)}")
        y -= 14
    pdf.drawString(45, y, f"TOTAL DA RETIRADA: {display(withdrawal['total_paid'], True)}")
    y -= 20
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


def format_local(value):
    if not value:
        return "Não registrado"
    return datetime.fromisoformat(value).astimezone(TZ).strftime("%d/%m/%Y %H:%M")


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
