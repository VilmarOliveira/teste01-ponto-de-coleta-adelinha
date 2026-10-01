from datetime import date

import pytest

from core import calculate_fees, classify_package, pix_payload


def test_pix_limits_long_holder_name_to_emv_standard():
    payload = pix_payload("38145273000105", "AMOR INFINITO MARKETING E SOLUCOES EMPRESARIAIS", 5)
    assert "5925AMOR INFINITO MARKETING" in payload
    assert "SOLUCOES EMPRESARIAIS" not in payload


@pytest.mark.parametrize("dimensions,weight,expected", [
    ((30, 25, 25), 10, ("Pequeno", 5.0)),
    ((30, 25, 25.01), 10, ("Grande", 10.0)),
    ((50, 50, 50), 20, ("Grande", 10.0)),
])
def test_category_boundaries(dimensions, weight, expected):
    assert classify_package(*dimensions, weight) == expected


@pytest.mark.parametrize("dimensions,weight", [
    ((50, 50, 50.01), 20),
    ((10, 10, 10), 20.01),
    ((0, 10, 10), 1),
])
def test_rejected_packages(dimensions, weight):
    with pytest.raises(ValueError):
        classify_package(*dimensions, weight)


def test_calendar_day_fees_and_pickup_freeze():
    notice = "2026-09-10T10:00:00-03:00"
    assert calculate_fees(5, notice, today=date(2026, 9, 13))["late_fee"] == 0
    assert calculate_fees(5, notice, today=date(2026, 9, 14))["late_fee"] == .5
    assert calculate_fees(5, notice, today=date(2026, 9, 16))["late_fee"] == 1.5
    frozen = calculate_fees(5, notice, "2026-09-14T19:00:00-03:00", today=None)
    assert frozen["total"] == 5.5


def test_pix_payload_has_key_amount_and_valid_crc():
    payload = pix_payload("38145273000105", "Recebedor Teste", 15.50)
    assert payload.startswith("000201")
    assert "38145273000105" in payload
    assert "540515.50" in payload
    assert payload[-8:-4] == "6304"
    assert len(payload[-4:]) == 4
