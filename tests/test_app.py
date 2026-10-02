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
    return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def token(client):
    with client.session_transaction() as session:
        session.setdefault("csrf_token", "test-csrf")
        return session["csrf_token"]


def register(client, name="Cliente Teste"):
    return client.post("/cadastro", data={"name": name, "cpf": "12345678901", "phone": "16999999999", "email": "cliente@example.com", "signature": signature(), "password": "senha-segura", "password_confirm": "senha-segura", "terms_accept": "yes", "residential_cep": "14400000", "residential_street": "Rua da Cliente", "residential_number": "123", "residential_district": "Centro", "residential_city": "Franca", "residential_state": "SP", "residential_complement": "Apto 2", "csrf_token": token(client)})


def test_registration_requires_and_saves_residential_address_separately(app, client):
    response = client.post("/cadastro", data={"name": "Cliente Sem Endereco", "cpf": "12345678901",
        "phone": "16999999999", "signature": signature(), "password": "senha-segura",
        "password_confirm": "senha-segura", "terms_accept": "yes", "csrf_token": token(client)})
    assert response.status_code == 400
    assert "endereço residencial obrigatório" in response.text
    response = register(client)
    assert response.status_code == 200
    connection = sqlite3.connect(app.config["DATABASE"])
    address = connection.execute("SELECT residential_cep,residential_street,residential_number,residential_district,residential_city,residential_state,residential_complement,email FROM clients").fetchone()
    assert address == ("14400000", "Rua da Cliente", "123", "Centro", "Franca", "SP", "Apto 2", "cliente@example.com")
    assert b"RUA AFONSO BORGES DE FREITAS" in response.data


def test_staff_edits_and_client_sees_both_addresses(app, client):
    register(client)
    login(client)
    response = client.post("/painel/clientes/1/editar", data={"name": "Cliente Teste", "cpf": "12345678901",
        "phone": "16999999999", "residential_cep": "14000001", "residential_street": "Rua Nova",
        "residential_number": "45", "residential_district": "Bairro Novo", "residential_city": "Franca",
        "residential_state": "sp", "residential_complement": "", "csrf_token": token(client)}, follow_redirects=True)
    assert "Cadastro atualizado" in response.text
    connection = sqlite3.connect(app.config["DATABASE"])
    assert connection.execute("SELECT residential_street,residential_state FROM clients WHERE id=1").fetchone() == ("Rua Nova", "SP")
    with client.session_transaction() as session:
        session.clear(); session["client_id"] = 1; session["client_session_version"] = 1; session["csrf_token"] = "test-csrf"
    page = client.get("/cliente").text
    assert "Rua Nova" in page
    assert "Endereço Pickup (retirada)" in page
    assert "RUA AFONSO BORGES DE FREITAS" in page


def test_client_edits_only_own_contact_and_residential_address(app, client):
    register(client)
    with client.session_transaction() as session:
        session.clear(); session["client_id"] = 1; session["client_session_version"] = 1; session["csrf_token"] = "test-csrf"
    response = client.post("/cliente/editar", data={"phone": "16977776666", "email": "novo@example.com",
        "residential_cep": "14000002", "residential_street": "Rua Própria", "residential_number": "9",
        "residential_district": "Jardim", "residential_city": "Franca", "residential_state": "SP",
        "residential_complement": "Casa", "csrf_token": token(client)}, follow_redirects=True)
    assert "Dados atualizados com sucesso" in response.text
    connection = sqlite3.connect(app.config["DATABASE"])
    row = connection.execute("SELECT name,cpf,phone,email,residential_street FROM clients WHERE id=1").fetchone()
    assert row == ("Cliente Teste", "12345678901", "16977776666", "novo@example.com", "Rua Própria")


def test_client_edit_rejects_invalid_email_and_requires_login(app, client):
    register(client)
    with client.session_transaction() as session: session.clear()
    assert client.get("/cliente/editar").status_code == 302
    with client.session_transaction() as session:
        session["client_id"] = 1; session["client_session_version"] = 1; session["csrf_token"] = "test-csrf"
    response = client.post("/cliente/editar", data={"phone": "16977776666", "email": "email-invalido",
        "residential_cep": "14000002", "residential_street": "Rua Própria", "residential_number": "9",
        "residential_district": "Jardim", "residential_city": "Franca", "residential_state": "SP",
        "csrf_token": token(client)})
    assert response.status_code == 400
    assert sqlite3.connect(app.config["DATABASE"]).execute("SELECT email FROM clients WHERE id=1").fetchone()[0] == "cliente@example.com"


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
        "shelf": "B02", "tracking": "TRK001",
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
            "width": "20", "height": "20", "length": "20", "weight": "2", "shelf": "B02", "tracking": "TRK001",
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
    response = client.post("/pacotes", data={"client_id": 1, "width": width, "height": height, "length": length, "weight": weight, "shelf": "A1", "tracking": "TRK001", "csrf_token": token(client)}, follow_redirects=True)
    assert response.status_code == 200
    assert category.encode() in response.data


@pytest.mark.parametrize("dimensions,weight", [((50, 50, 50.01), 20), ((10, 10, 10), 20.01)])
def test_oversized_package_is_blocked(client, dimensions, weight):
    register(client)
    login(client)
    response = client.post("/pacotes", data={"client_id": 1, "width": dimensions[0], "height": dimensions[1], "length": dimensions[2], "weight": weight, "shelf": "A1", "tracking": "TRK001", "csrf_token": token(client)}, follow_redirects=True)
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
        client.post("/pacotes", data={"client_id": 1, "width": 10, "height": 10, "length": 10, "weight": 1, "shelf": shelf, "tracking": "TRK001", "csrf_token": token(client)})
    client.post("/pacotes/1/avisar", data={"csrf_token": token(client)})
    client.post("/pacotes/2/avisar", data={"csrf_token": token(client)})
    response = client.post("/retiradas", data={"package_ids": "1", "receiver_name": "Maria", "receiver_document": "12345678901", "document_type": "CPF", "payment_method": "Dinheiro", "payment_confirmed": "yes", "pickup_signature": signature(), "csrf_token": token(client)})
    assert response.status_code == 302
    connection = sqlite3.connect(app.config["DATABASE"])
    statuses = [row[0] for row in connection.execute("SELECT status FROM packages ORDER BY id")]
    assert statuses == ["retirado", "aguardando_retirada"]
    response = client.post("/retiradas", data={"package_ids": "1", "receiver_name": "Maria", "receiver_document": "12345678901", "document_type": "CPF", "payment_method": "Dinheiro", "payment_confirmed": "yes", "pickup_signature": signature(), "csrf_token": token(client)}, follow_redirects=True)
    assert "já possui uma retirada registrada" in response.text


def test_duplicate_package_ids_are_processed_once(app, client):
    register(client)
    login(client)
    client.post("/pacotes", data={"client_id": 1, "width": 10, "height": 10, "length": 10, "weight": 1, "shelf": "A1", "tracking": "TRK001", "csrf_token": token(client)})
    client.post("/pacotes/1/avisar", data={"csrf_token": token(client)})

    response = client.post("/retiradas", data={
        "package_ids": ["1", "1"], "receiver_name": "Cliente Teste",
        "receiver_document": "12345678901", "document_type": "CPF", "payment_method": "Dinheiro", "payment_confirmed": "yes", "pickup_signature": signature(),
        "csrf_token": token(client),
    })
    assert response.status_code == 302
    connection = sqlite3.connect(app.config["DATABASE"])
    assert connection.execute("SELECT COUNT(*) FROM pickups").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM pickup_packages").fetchone()[0] == 1
    assert connection.execute("SELECT status FROM packages WHERE id=1").fetchone()[0] == "retirado"


def test_dashboard_renders_one_package_section_and_one_package_card(client):
    register(client)
    login(client)
    client.post("/pacotes", data={"client_id": 1, "width": 10, "height": 10, "length": 10, "weight": 1, "shelf": "A1", "tracking": "TRK001", "csrf_token": token(client)})
    response = client.get("/painel")
    assert response.text.count("<h2>Pacotes</h2>") == 1
    # O ID aparece no cartão e no link/URL, mas existe somente um article do pacote.
    assert response.text.count('<article class="package">') == 1


def test_second_pickup_shows_existing_receipt_without_new_rows(app, client):
    register(client)
    login(client)
    client.post("/pacotes", data={"client_id": 1, "width": 10, "height": 10, "length": 10, "weight": 1, "shelf": "A1", "tracking": "TRK001", "csrf_token": token(client)})
    client.post("/pacotes/1/avisar", data={"csrf_token": token(client)})
    payload = {"package_ids": "1", "receiver_name": "Cliente Teste", "receiver_document": "12345678901", "document_type": "CPF", "payment_method": "Dinheiro", "payment_confirmed": "yes", "pickup_signature": signature(), "csrf_token": token(client)}
    client.post("/retiradas", data=payload)

    response = client.post("/retiradas", data=payload, follow_redirects=True)
    assert "já possui uma retirada registrada" in response.text
    assert "Baixar comprovante em PDF" in response.text
    connection = sqlite3.connect(app.config["DATABASE"])
    assert connection.execute("SELECT COUNT(*) FROM pickups").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM pickup_packages").fetchone()[0] == 1


def test_normal_pickup_of_multiple_distinct_packages_is_atomic(app, client):
    register(client)
    login(client)
    for shelf in ("A1", "A2"):
        client.post("/pacotes", data={"client_id": 1, "width": 10, "height": 10, "length": 10, "weight": 1, "shelf": shelf, "tracking": "TRK001", "csrf_token": token(client)})
    for package_id in (1, 2):
        client.post(f"/pacotes/{package_id}/avisar", data={"csrf_token": token(client)})

    response = client.post("/retiradas", data={
        "package_ids": ["1", "2"], "receiver_name": "Terceiro",
        "receiver_document": "12345678901", "document_type": "CPF", "payment_method": "Dinheiro", "payment_confirmed": "yes", "pickup_signature": signature(),
        "csrf_token": token(client),
    })
    assert response.status_code == 302
    connection = sqlite3.connect(app.config["DATABASE"])
    assert connection.execute("SELECT COUNT(*) FROM pickups").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM pickup_packages").fetchone()[0] == 2
    assert connection.execute("SELECT COUNT(*) FROM packages WHERE status='retirado'").fetchone()[0] == 2


def test_pickup_rolls_back_every_change_when_a_link_fails(app, client):
    register(client)
    login(client)
    for shelf in ("A1", "A2"):
        client.post("/pacotes", data={"client_id": 1, "width": 10, "height": 10, "length": 10, "weight": 1, "shelf": shelf, "tracking": "TRK001", "csrf_token": token(client)})
    for package_id in (1, 2):
        client.post(f"/pacotes/{package_id}/avisar", data={"csrf_token": token(client)})

    connection = sqlite3.connect(app.config["DATABASE"])
    connection.execute("""CREATE TRIGGER fail_second_pickup_link BEFORE INSERT ON pickup_packages
        WHEN NEW.package_id=2 BEGIN SELECT RAISE(ABORT, 'falha simulada'); END""")
    connection.commit()

    response = client.post("/retiradas", data={
        "package_ids": ["1", "2"], "receiver_name": "Cliente Teste",
        "receiver_document": "12345678901", "document_type": "CPF", "payment_method": "Dinheiro", "payment_confirmed": "yes", "pickup_signature": signature(),
        "csrf_token": token(client),
    }, follow_redirects=True)
    assert "Nenhuma alteração foi salva" in response.text
    assert connection.execute("SELECT COUNT(*) FROM pickups").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM pickup_packages").fetchone()[0] == 0
    statuses = [row[0] for row in connection.execute("SELECT status FROM packages ORDER BY id")]
    assert statuses == ["aguardando_retirada", "aguardando_retirada"]


def test_csrf_is_required(client):
    assert client.post("/cadastro", data={}).status_code == 400


def test_package_error_preserves_form_and_edit_is_blocked_after_notice(app, client):
    register(client)
    login(client)
    invalid = {"client_id": 1, "client_label": "Cliente Teste · ADL-000001", "tracking": "000123", "shelf": "A01",
        "width": "100", "height": "40", "length": "20", "weight": "2", "csrf_token": token(client)}
    response = client.post("/pacotes", data=invalid, follow_redirects=True)
    assert "Pacote recusado: Limite de 150 cm na soma e 20 kg." in response.text
    assert 'value="000123"' in response.text and 'value="100"' in response.text
    assert sqlite3.connect(app.config["DATABASE"]).execute("SELECT COUNT(*) FROM packages").fetchone()[0] == 0

    valid = {**invalid, "width": "20", "height": "20", "length": "20", "weight": "2", "csrf_token": token(client)}
    client.post("/pacotes", data=valid)
    client.post("/pacotes/1/avisar", data={"csrf_token": token(client)})
    response = client.post("/painel/pacotes/1/editar", data=valid, follow_redirects=True)
    assert "não pode mais ser editado" in response.text


def test_admin_edits_client_changes_password_and_deactivates_with_history(app, client):
    register(client)
    login(client)
    response = client.post("/painel/clientes/1/editar", data={"name": "Nome Atualizado", "cpf": "123.456.789-01", "phone": "16988887777", "residential_cep": "14400000", "residential_street": "Rua da Cliente", "residential_number": "123", "residential_district": "Centro", "residential_city": "Franca", "residential_state": "SP", "csrf_token": token(client)}, follow_redirects=True)
    assert "Cadastro atualizado" in response.text
    client.post("/painel/clientes/1/senha", data={"password": "senha-nova", "password_confirm": "senha-nova", "csrf_token": token(client)})
    response = client.post("/painel/clientes/1/remover", data={"confirmation": "ADL-000001", "csrf_token": token(client)}, follow_redirects=True)
    assert "Cliente desativado" in response.text
    connection = sqlite3.connect(app.config["DATABASE"])
    assert connection.execute("SELECT name,active,session_version FROM clients").fetchone() == ("Nome Atualizado", 0, 3)
    actions = [row[0] for row in connection.execute("SELECT action FROM audit_logs ORDER BY id")]
    assert actions == ["editar", "alterar_senha", "desativar"]


def test_cash_pickup_saves_document_and_payment_confirmation(app, client):
    register(client)
    login(client)
    client.post("/pacotes", data={"client_id": 1, "tracking": "001ABC", "shelf": "A1", "width": 10, "height": 10, "length": 10, "weight": 1, "csrf_token": token(client)})
    client.post("/pacotes/1/avisar", data={"csrf_token": token(client)})
    response = client.post("/retiradas", data={"package_ids": "1", "receiver_name": "Terceiro", "document_type": "RG",
        "receiver_document": "12.345.678-X", "payment_method": "Dinheiro", "payment_confirmed": "yes",
        "pickup_signature": signature(), "csrf_token": token(client)})
    assert response.status_code == 302
    row = sqlite3.connect(app.config["DATABASE"]).execute("SELECT document_type,receiver_document,payment_method,payment_confirmed_at,payment_confirmed_by FROM pickups").fetchone()
    assert row[:3] == ("RG", "12.345.678-X", "Dinheiro")
    assert row[3] and row[4] == "staff"


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
    client.post("/cadastro", data={"name": "Segundo Cliente", "cpf": "98765432100", "phone": "16988888888", "signature": signature(), "password": "outra-senha", "password_confirm": "outra-senha", "terms_accept": "yes", "residential_cep": "14400000", "residential_street": "Rua Dois", "residential_number": "2", "residential_district": "Centro", "residential_city": "Franca", "residential_state": "SP", "csrf_token": token(client)})
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.execute("INSERT INTO pickups(id,receiver_name,receiver_document,signature,staff,picked_at,base_total,late_total,total_paid,payment_confirmed) VALUES(1,'X','1',?,'staff','2026-01-01T10:00:00-03:00',5,0,5,1)", (signature(),))
    connection.execute("INSERT INTO packages(id,public_id,client_id,width,height,length,weight,category,base_price,status,created_at) VALUES(1,'PCT-000001',2,10,10,10,1,'Pequeno',5,'retirado','2026-01-01T09:00:00-03:00')")
    connection.execute("INSERT INTO pickup_packages(pickup_id,package_id,base_amount,late_amount) VALUES(1,1,5,0)"); connection.commit()
    with client.session_transaction() as session:
        session["client_id"] = 1
        session["client_session_version"] = 1
    assert client.get("/cliente/comprovantes/1").status_code == 404


def test_existing_database_migration_and_one_time_password_link(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE clients (id INTEGER PRIMARY KEY, public_id TEXT, name TEXT, cpf TEXT, phone TEXT, terms_version TEXT, signature TEXT, accepted_at TEXT)")
    connection.execute("INSERT INTO clients VALUES(1,'ADL-000001','Antigo','11111111111','16999999999','antiga','assinatura','2025-01-01T10:00:00-03:00')"); connection.commit(); connection.close()
    migrated = create_app({"TESTING": True, "SECRET_KEY": "x", "DATABASE": str(path)})
    migrated_columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(clients)")}
    assert {"email", "residential_cep", "residential_street", "residential_number"} <= migrated_columns
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


@pytest.mark.parametrize("receiver,document", [
    ("Cliente Teste", "12345678901"),
    ("Terceiro Autorizado", "98765432100"),
])
def test_withdrawal_snapshot_detail_restart_and_pdf(app, client, receiver, document):
    register(client)
    login(client)
    client.post("/pacotes", data={
        "client_id": 1, "width": 10, "height": 10, "length": 10,
        "weight": 1, "shelf": "A1", "tracking": "TRK001", "csrf_token": token(client),
    })
    client.post("/pacotes/1/avisar", data={"csrf_token": token(client)})
    response = client.post("/retiradas", data={
        "package_ids": "1", "receiver_name": receiver,
        "receiver_document": document, "document_type": "CPF", "payment_method": "Dinheiro", "payment_confirmed": "yes", "pickup_signature": signature(),
        "csrf_token": token(client),
    })
    assert response.status_code == 302

    connection = sqlite3.connect(app.config["DATABASE"])
    snapshot = connection.execute("""SELECT package_public_id,client_id,client_name,client_public_id,service_amount,
        late_days,late_amount,total_amount FROM pickup_packages""").fetchone()
    assert snapshot == ("PCT-000001", 1, "Cliente Teste", "ADL-000001", 5, 0, 0, 5)
    connection.execute("UPDATE clients SET name='Nome alterado' WHERE id=1")
    connection.execute("UPDATE packages SET base_price=99 WHERE id=1")
    connection.commit()

    restarted = create_app({"TESTING": True, "SECRET_KEY": "restart", "DATABASE": app.config["DATABASE"]})
    restarted_client = restarted.test_client()
    with restarted_client.session_transaction() as session:
        session["staff"] = "staff"
    detail = restarted_client.get("/painel/pacotes/1")
    assert detail.status_code == 200
    assert b"Cliente Teste" in detail.data
    assert receiver.encode() in detail.data
    assert document.encode() in detail.data
    assert b"R$ 5,00" in detail.data

    pdf = restarted_client.get("/comprovantes/1/pacotes/1.pdf")
    assert pdf.status_code == 200
    assert pdf.mimetype == "application/pdf"
    assert pdf.data.startswith(b"%PDF-")
    assert b"PCT-000001" in pdf.data
    assert b"Cliente Teste" in pdf.data
    assert receiver.encode() in pdf.data
    assert b"Confirmo que retirei o pacote" in pdf.data
    assert b"/Subtype /Image" in pdf.data


def test_client_cannot_download_another_clients_pdf(app, client):
    # Reaproveita uma retirada mínima gravada diretamente para verificar autorização.
    register(client)
    connection = sqlite3.connect(app.config["DATABASE"])
    connection.execute("INSERT INTO clients(id,public_id,name,cpf,phone,terms_version,signature,accepted_at) VALUES(2,'ADL-000002','Outro','99999999999','16999999999','v',?,'2026-01-01')", (signature(),))
    connection.execute("INSERT INTO packages(id,public_id,client_id,width,height,length,weight,category,base_price,status,created_at) VALUES(1,'PCT-000001',2,10,10,10,1,'Pequeno',5,'retirado','2026-01-01')")
    connection.execute("INSERT INTO pickups(id,receiver_name,receiver_document,signature,staff,picked_at,base_total,late_total,total_paid,payment_method,payment_confirmed) VALUES(1,'Outro','RG',?,'admin','2026-01-01',5,0,5,'Pix',1)", (signature(),))
    connection.execute("INSERT INTO pickup_packages(pickup_id,package_id,base_amount,late_amount,package_public_id,client_id,client_name,client_public_id,service_amount,late_days,total_amount) VALUES(1,1,5,0,'PCT-000001',2,'Outro','ADL-000002',5,0,5)")
    connection.commit()
    with client.session_transaction() as session:
        session["client_id"] = 1
    assert client.get("/comprovantes/1/pacotes/1.pdf").status_code == 403
