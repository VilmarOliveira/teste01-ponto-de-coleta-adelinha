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


def register(client, name="Cliente Teste"):
    return client.post("/cadastro", data={"name": name, "cpf": "12345678901", "phone": "16999999999", "signature": signature()})


def login(client):
    with client.session_transaction() as session:
        session["staff"] = "staff"


def test_public_registration_generates_id_and_keeps_sensitive_data_private(client):
    response = register(client)
    assert response.status_code == 200
    assert b"ADL-000001" in response.data
    assert b"12345678901" not in response.data


def test_dashboard_requires_login(client):
    response = client.get("/painel")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


@pytest.mark.parametrize("width,height,length,weight,category", [
    (30, 25, 25, 10, "Pequeno"),
    (30, 25, 25.01, 10, "Grande"),
    (50, 50, 50, 20, "Grande"),
])
def test_package_category_boundaries(app, client, width, height, length, weight, category):
    register(client)
    login(client)
    response = client.post("/pacotes", data={"client_id": 1, "width": width, "height": height, "length": length, "weight": weight, "shelf": "A1"}, follow_redirects=True)
    assert response.status_code == 200
    assert category.encode() in response.data


@pytest.mark.parametrize("dimensions,weight", [((50, 50, 50.01), 20), ((10, 10, 10), 20.01)])
def test_oversized_package_is_blocked(client, dimensions, weight):
    register(client)
    login(client)
    response = client.post("/pacotes", data={"client_id": 1, "width": dimensions[0], "height": dimensions[1], "length": dimensions[2], "weight": weight, "shelf": "A1"}, follow_redirects=True)
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
        client.post("/pacotes", data={"client_id": 1, "width": 10, "height": 10, "length": 10, "weight": 1, "shelf": shelf})
    client.post("/pacotes/1/avisar")
    client.post("/pacotes/2/avisar")
    response = client.post("/retiradas", data={"package_ids": "1", "receiver_name": "Maria", "receiver_document": "RG 1", "pickup_signature": signature()})
    assert response.status_code == 302
    connection = sqlite3.connect(app.config["DATABASE"])
    statuses = [row[0] for row in connection.execute("SELECT status FROM packages ORDER BY id")]
    assert statuses == ["retirado", "aguardando_retirada"]
    response = client.post("/retiradas", data={"package_ids": "1", "receiver_name": "Maria", "receiver_document": "RG 1", "pickup_signature": signature()}, follow_redirects=True)
    assert "Retirada bloqueada" in response.text
