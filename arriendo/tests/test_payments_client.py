import httpx
import pytest

from app import payments_client
from app.payments_client import PaymentLookupUncertain, PaymentStartRejected, PaymentStartUncertain


def iniciar_cobro_de_prueba():
    return payments_client.iniciar_cobro(
        rental_id=1001,
        customer_id=25,
        amount_rent=50000,
        amount_deposit=100000,
        idempotency_key="charge-saga-1",
    )


def test_iniciar_cobro_envia_datos_y_traduce_respuesta(monkeypatch):
    solicitud = {}

    def fake_post(url, *, headers, json, timeout):
        solicitud.update(
            url=url, headers=headers, json=json, timeout=timeout
        )
        return httpx.Response(
            201,
            json={
                "payment_id": 501,
                "status": "PENDING",
                "redirect_url": "https://webpay.example/pagar",
                "token": "token-501",
            },
        )

    monkeypatch.setattr(payments_client.httpx, "post", fake_post)

    checkout = iniciar_cobro_de_prueba()

    assert solicitud["url"].endswith("/payments/charge")
    assert solicitud["headers"]["Idempotency-Key"] == "charge-saga-1"
    assert solicitud["json"]["amount_rent"] == 50000
    assert solicitud["json"]["amount_deposit"] == 100000
    assert checkout.paymentId == 501
    assert checkout.status == "PENDING"
    assert checkout.redirectUrl == "https://webpay.example/pagar"
    assert checkout.token == "token-501"


def test_400_es_rechazo_confirmado(monkeypatch):
    monkeypatch.setattr(
        payments_client.httpx,
        "post",
        lambda *args, **kwargs: httpx.Response(400),
    )

    with pytest.raises(PaymentStartRejected):
        iniciar_cobro_de_prueba()


def test_timeout_deja_resultado_incierto(monkeypatch):
    def fake_post(*args, **kwargs):
        raise httpx.ReadTimeout("Se perdió la respuesta")

    monkeypatch.setattr(payments_client.httpx, "post", fake_post)

    with pytest.raises(PaymentStartUncertain):
        iniciar_cobro_de_prueba()


def test_consultar_pago_traduce_estado(monkeypatch):
    monkeypatch.setattr(
        payments_client.httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={
                "payment_id": 501, "rental_id": 1001, "amount_total": 150000,
                "status": "CHARGED", "redirect_url": None, "token": None,
            },
        ),
    )
    payment = payments_client.consultar_pago(501)
    assert payment.status == "CHARGED"
    assert payment.rental_id == 1001
    assert payment.amount_total == 150000


def test_consultar_pago_rechaza_identidad_inconsistente(monkeypatch):
    monkeypatch.setattr(
        payments_client.httpx,
        "get",
        lambda *args, **kwargs: httpx.Response(
            200,
            json={"payment_id": 999, "rental_id": 1001, "amount_total": 150000, "status": "CHARGED"},
        ),
    )
    with pytest.raises(PaymentLookupUncertain):
        payments_client.consultar_pago(501)
