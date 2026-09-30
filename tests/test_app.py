import sqlite3
from datetime import date

import pytest

pytest.importorskip("flask", reason="Flask não está instalado; execute scripts/setup.sh")

from app import create_app, fees


@pytest.fixture()
def app(tmp_path):
    return create_app({"TESTING": True, "SECRET_KEY": "test", "DATABASE": str(tmp_path / "test.db"), "STAFF_USER": "staff", "STAFF_PASSWORD_HASH": "scrypt:32768:8:1$QyMzw3Iyd33tjJsf$900213144d569c99835ddcd16d66a2f251ec5d758bdfb944e2af125162ec9e74c34633bc09e634dca472af32b14615b140d57e6f722339446353a0d6a908b784"})


@pytest.fixture()
def client(app):
    return app.test_client()


def signature():
    return "data:image/png;base64,aGVsbG8="


def token(client):
    with client.session_transaction() as session:
        session.setdefault("csrf_token", "test-csrf")
        return session["csrf_token"]


def register(client, name="Cliente Teste"):
    return client.post("/cadastro", data={"name": name, "cpf": "12345678901", "phone": "16999999999", "signature": signature(), "password": "senha-segura", "password_confirm": "senha-segura", "terms_accept": "yes", "csrf_token": token(client)})


def login(client):
    with client.session_transaction() as session:
        session["staff"] = "staff"
        session.setdefault("csrf_token", "test-csrf")


def test_public_registration_generates_id_and_keeps_sensitive_data_private(client):
    response = register(client)
    assert response.status_code == 200
    assert b"ADL-000001" in response.data
    assert b"12345678901" not in response.data


def test_dashboard_requires_login(client):
    response = client.get("/painel")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_package_form_search_select_and_register_flow(app, client):
    register(client, "Maria da Silva")
    login(client)

    for query in ("Maria", "da Silva", "ADL-000001", "(16) 99999-9999"):
        response = client.get("/painel/clientes/busca", query_string={"q": query})
        assert response.status_code == 200
        assert response.json["clients"][0]["id"] == 1
        assert response.json["clients"][0]["name"] == "Maria da Silva"
        assert response.json["clients"][0]["public_id"] == "ADL-000001"

    selected_id = client.get("/painel/clientes/busca", query_string={"q": "Maria"}).json["clients"][0]["id"]
    response = client.post("/pacotes", data={
        "client_id": selected_id,
        "width": "20",
        "height": "20",
        "length": "20",
        "weight": "2",
        "shelf": "B-02",
        "csrf_token": token(client),
    }, follow_redirects=True)
    assert "PCT-000001 registrado como Pequeno" in response.text
    connection = sqlite3.connect(app.config["DATABASE"])
    assert connection.execute("SELECT client_id FROM packages WHERE public_id='PCT-000001'").fetchone()[0] == 1


def test_package_registration_rejects_missing_or_unknown_client(client):
    login(client)
    for client_id in ("", "999999"):
        response = client.post("/pacotes", data={
            "client_id": client_id,
            "width": "20", "height": "20", "length": "20", "weight": "2", "shelf": "B-02",
            "csrf_token": token(client),
        }, follow_redirects=True)
        assert "cliente válido" in response.text


@pytest.mark.parametrize("width,height,length,weight,category", [
    (30, 25, 25, 10, "Pequeno"),
    (30, 25, 25.01, 10, "Grande"),
    (50, 50, 50, 20, "Grande"),
])
def test_package_category_boundaries(app, client, width, height, length, weight, category):
    register(client)
    login(client)
    response = client.post("/pacotes", data={"client_id": 1, "width": width, "height": height, "length": length, "weight": weight, "shelf": "A1", "csrf_token": token(client)}, follow_redirects=True)
    assert response.status_code == 200
    assert category.encode() in response.data


@pytest.mark.parametrize("dimensions,weight", [((50, 50, 50.01), 20), ((10, 10, 10), 20.01)])
def test_oversized_package_is_blocked(client, dimensions, weight):
    register(client)
    login(client)
    response = client.post("/pacotes", data={"client_id": 1, "width": dimensions[0], "height": dimensions[1], "length": dimensions[2], "weight": weight, "shelf": "A1", "csrf_token": token(client)}, follow_redirects=True)
    assert "Pacote recusado" in response.text


def test_late_fee_counts_calendar_dates():
    package = {"base_price": 5, "notified_at": "2026-09-10T10:00:00-03:00", "picked_at": None}
    assert fees(package, date(2026, 9, 13))["late_fee"] == 0
    assert fees(package, date(2026, 9, 14))["late_fee"] == .5
    assert fees(package, date(2026, 9, 16))["late_fee"] == 1.5


def test_partial_pickup_and_duplicate_protection(app, client):
    register(client)
    login(client)
    for shelf in ("A1", "A2"):
        client.post("/pacotes", data={"client_id": 1, "width": 10, "height": 10, "length": 10, "weight": 1, "shelf": shelf, "csrf_token": token(client)})
    client.post("/pacotes/1/avisar", data={"csrf_token": token(client)})
    client.post("/pacotes/2/avisar", data={"csrf_token": token(client)})
    response = client.post("/retiradas", data={"package_ids": "1", "receiver_name": "Maria", "receiver_document": "RG 1", "pickup_signature": signature(), "csrf_token": token(client)})
    assert response.status_code == 302
    connection = sqlite3.connect(app.config["DATABASE"])
    statuses = [row[0] for row in connection.execute("SELECT status FROM packages ORDER BY id")]
    assert statuses == ["retirado", "aguardando_retirada"]
    response = client.post("/retiradas", data={"package_ids": "1", "receiver_name": "Maria", "receiver_document": "RG 1", "pickup_signature": signature(), "csrf_token": token(client)}, follow_redirects=True)
    assert "Retirada bloqueada" in response.text


def test_csrf_is_required(client):
    assert client.post("/cadastro", data={}).status_code == 400


def test_terms_acceptance_is_required(client):
    response = client.post("/cadastro", data={"name": "Sem Aceite", "cpf": "12345678901", "phone": "16999999999", "signature": signature(), "password": "senha-segura", "password_confirm": "senha-segura", "csrf_token": token(client)}, follow_redirects=True)
    assert "necessário aceitar" in response.text


def test_client_login_is_rate_limited(client):
    for _ in range(5):
        client.post("/cliente/login", data={"cpf": "12345678901", "password": "errada", "csrf_token": token(client)})
    response = client.post("/cliente/login", data={"cpf": "12345678901", "password": "errada", "csrf_token": token(client)}, follow_redirects=True)
    assert "Muitas tentativas" in response.text


def test_client_login_logout_and_password_is_hashed(app, client):
    register(client)
    connection = sqlite3.connect(app.config["DATABASE"])
    stored = connection.execute("SELECT password_hash FROM clients").fetchone()[0]
    assert stored != "senha-segura"
    response = client.post("/cliente/login", data={"cpf": "123.456.789-01", "password": "senha-segura", "csrf_token": token(client)}, follow_redirects=True)
    assert "Olá, Cliente Teste" in response.text
    response = client.post("/cliente/sair", data={"csrf_token": token(client)}, follow_redirects=True)
    assert "Fazer meu cadastro" in response.text


def test_client_cannot_access_another_receipt(app, client):
    register(client, "Primeiro Cliente")
    with client.session_transaction() as session:
        session.clear(); session["csrf_token"] = "test-csrf"
    client.post("/cadastro", data={"name": "Segundo Cliente", "cpf": "98765432100", "phone": "16988888888", "signature": signature(), "password": "outra-senha", "password_confirm": "outra-senha", "terms_accept": "yes", "csrf_token": token(client)})
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.execute("INSERT INTO pickups(id,receiver_name,receiver_document,signature,staff,picked_at,base_total,late_total,total_paid,payment_confirmed) VALUES(1,'X','1',?,'staff','2026-01-01T10:00:00-03:00',5,0,5,1)", (signature(),))
    connection.execute("INSERT INTO packages(id,public_id,client_id,width,height,length,weight,category,base_price,status,created_at) VALUES(1,'PCT-000001',2,10,10,10,1,'Pequeno',5,'retirado','2026-01-01T09:00:00-03:00')")
    connection.execute("INSERT INTO pickup_packages VALUES(1,1,5,0)"); connection.commit()
    with client.session_transaction() as session:
        session["client_id"] = 1
    assert client.get("/cliente/comprovantes/1").status_code == 404


def test_existing_database_migration_and_one_time_password_link(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE clients (id INTEGER PRIMARY KEY, public_id TEXT, name TEXT, cpf TEXT, phone TEXT, terms_version TEXT, signature TEXT, accepted_at TEXT)")
    connection.execute("INSERT INTO clients VALUES(1,'ADL-000001','Antigo','11111111111','16999999999','antiga','assinatura','2025-01-01T10:00:00-03:00')"); connection.commit(); connection.close()
    migrated = create_app({"TESTING": True, "SECRET_KEY": "x", "DATABASE": str(path)})
    c = migrated.test_client()
    with c.session_transaction() as session:
        session["staff"] = "staff"; session["csrf_token"] = "test-csrf"
    response = c.post("/painel/clientes/1/link-senha", data={"identity_checked": "yes", "csrf_token": "test-csrf"})
    assert response.status_code == 200
    link = response.text.split('/definir-senha/')[1].split('<')[0]
    url = "/definir-senha/" + link
    c.get(url)
    with c.session_transaction() as session: csrf = session["csrf_token"]
    assert c.post(url, data={"password": "nova-senha", "password_confirm": "nova-senha", "csrf_token": csrf}).status_code == 302
    assert c.get(url).status_code == 410


def test_expired_password_link_is_rejected(app, client):
    register(client)
    login(client)
    response = client.post("/painel/clientes/1/link-senha", data={"identity_checked": "yes", "csrf_token": token(client)})
    raw = response.text.split('/definir-senha/')[1].split('<')[0]
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.execute("UPDATE password_tokens SET expires_at='2020-01-01T00:00:00-03:00'")
    connection.commit()
    assert client.get("/definir-senha/" + raw).status_code == 410
