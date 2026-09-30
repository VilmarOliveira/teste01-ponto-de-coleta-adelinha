"""Cria clientes e pacotes fictícios para demonstração, sem apagar dados existentes."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import create_app

app = create_app()
with app.test_request_context("/"):
    import sqlite3
    db = sqlite3.connect(app.config["DATABASE"])
    existing = db.execute("SELECT id FROM clients WHERE public_id='ADL-000001'").fetchone()
    if not existing:
        db.execute("INSERT INTO clients(id,public_id,name,cpf,phone,terms_version,signature,accepted_at) VALUES(1,'ADL-000001','Ana Demonstração','00000000000','16999990000','MINUTA-TESTE-v1','data:image/png;base64,TESTE',?)", (datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(),))
        db.execute("INSERT INTO packages(id,public_id,client_id,width,height,length,weight,shelf,category,base_price,status,created_at,notified_at) VALUES(1,'PCT-000001',1,20,20,20,2,'A-01','Pequeno',5,'aguardando_retirada',?,?)", ((datetime.now(ZoneInfo("America/Sao_Paulo"))-timedelta(days=5)).isoformat(), (datetime.now(ZoneInfo("America/Sao_Paulo"))-timedelta(days=5)).isoformat()))
        db.commit()
        print("Dados fictícios criados.")
    else:
        print("Dados de demonstração já existem.")
    db.close()
