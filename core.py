"""Regras de negócio sem dependências externas.

Manter os cálculos aqui permite testá-los mesmo em um ambiente sem Flask.
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import unicodedata

TZ = ZoneInfo("America/Sao_Paulo")


def classify_package(width: float, height: float, length: float, weight: float):
    """Retorna (categoria, preço) ou levanta ValueError para pacote recusado."""
    dimensions = (width, height, length)
    if min(dimensions) <= 0 or weight <= 0:
        raise ValueError("Medidas e peso devem ser maiores que zero.")
    dimension_sum = sum(dimensions)
    if dimension_sum > 150 or weight > 20:
        raise ValueError("Limite de 150 cm na soma e 20 kg.")
    if dimension_sum <= 80 and weight <= 10:
        return "Pequeno", 5.0
    return "Grande", 10.0


def calculate_fees(base_price, notified_at=None, picked_at=None, today: date | None = None):
    """Calcula atraso por datas locais; o dia do aviso é o primeiro dos quatro."""
    base = float(base_price)
    late_days = 0
    if notified_at:
        notice_date = datetime.fromisoformat(notified_at).astimezone(TZ).date()
        last_free_date = notice_date + timedelta(days=3)
        reference = today or (
            datetime.fromisoformat(picked_at).astimezone(TZ).date()
            if picked_at
            else datetime.now(TZ).date()
        )
        late_days = max(0, (reference - last_free_date).days)
    late_fee = late_days * 0.5
    return {"base": base, "late_days": late_days, "late_fee": late_fee, "total": base + late_fee}


def _emv_field(identifier, value):
    return f"{identifier}{len(value):02d}{value}"


def pix_payload(key, receiver_name, amount):
    """Gera um BR Code Pix estático com CRC16-CCITT."""
    if not receiver_name or amount <= 0:
        raise ValueError("Nome do recebedor e valor são obrigatórios.")
    receiver = unicodedata.normalize("NFKD", receiver_name).encode("ascii", "ignore").decode().strip().upper()[:25]
    merchant = _emv_field("00", "BR.GOV.BCB.PIX") + _emv_field("01", key)
    payload = "".join((_emv_field("00", "01"), _emv_field("26", merchant), _emv_field("52", "0000"),
        _emv_field("53", "986"), _emv_field("54", f"{amount:.2f}"), _emv_field("58", "BR"),
        _emv_field("59", receiver), _emv_field("60", "FRANCA"), _emv_field("62", _emv_field("05", "***")), "6304"))
    crc = 0xFFFF
    for byte in payload.encode("utf-8"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return payload + f"{crc:04X}"
