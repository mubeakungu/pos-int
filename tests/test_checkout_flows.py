import json

import integrations.base
from tests.conftest import FakeHttp, FakeResponse


def add_to_cart(client, product_id=1, quantity=2):
    client.post('/sales', data={'product_id': product_id, 'quantity': quantity})


def enable(m, key, **settings):
    config = m.IntegrationConfig.query.filter_by(key=key).first()
    config.enabled = True
    config.settings = {**config.settings, **settings}
    m.db.session.commit()
    return config


def test_cash_sale_gets_etims_invoice(client, m):
    add_to_cart(client)
    resp = client.post('/complete_sale', data={'payment_method': 'cash', 'customer_pin': 'a123456789z'})
    assert resp.status_code == 302 and '/view_receipt/' in resp.location
    sale = m.Sale.query.one()
    assert sale.kra_invoice_id == f'P000000000A/00/{sale.id}'
    assert sale.meta.customer_pin == 'A123456789Z'
    assert sale.payments[0].status == 'paid' and sale.payments[0].method == 'cash'
    log = m.IntegrationLog.query.filter_by(integration_key='kra_etims').one()
    assert log.status == 'success' and json.loads(log.request_json)['custTin'] == 'A123456789Z'
    assert m.Product.query.get(1).quantity == 8
    page = client.get(resp.location)
    assert b'test invoice, not sent to KRA' in page.data


def test_mpesa_stk_flow_with_callback(client, m):
    enable(m, 'mpesa')
    add_to_cart(client)
    resp = client.post('/complete_sale', data={'payment_method': 'mpesa', 'payment_phone': '0712345678'})
    payment = m.Payment.query.one()
    assert resp.location.endswith(f'/payments/{payment.id}')
    assert payment.status == 'pending' and payment.provider_ref.startswith('ws_CO_MOCK_')
    sale = payment.sale
    assert sale.kra_invoice_id is None  # not invoiced until paid
    assert client.get(f'/payments/{payment.id}/status').json['status'] == 'pending'

    callback = {'Body': {'stkCallback': {
        'CheckoutRequestID': payment.provider_ref, 'ResultCode': 0, 'ResultDesc': 'ok',
        'CallbackMetadata': {'Item': [{'Name': 'MpesaReceiptNumber', 'Value': 'QAB123'}, {'Name': 'Amount', 'Value': 130}]}}}}
    anon = client.application.test_client()
    ack = anon.post('/integrations/mpesa/callback', json=callback)
    assert ack.json['ResultCode'] == 0
    m.db.session.refresh(payment)
    assert payment.status == 'paid' and payment.receipt_number == 'QAB123'
    assert payment.sale.kra_invoice_id is not None
    assert client.get(f'/payments/{payment.id}/status').json['status'] == 'paid'
    # A duplicate callback must not invoice the sale twice
    anon.post('/integrations/mpesa/callback', json=callback)
    assert m.IntegrationLog.query.filter_by(integration_key='kra_etims', event='sale.completed').count() == 1


def test_mpesa_callback_token_is_enforced(client, m, monkeypatch):
    enable(m, 'mpesa')
    monkeypatch.setenv('MPESA_CALLBACK_TOKEN', 'tok123')
    anon = client.application.test_client()
    assert anon.post('/integrations/mpesa/callback', json={}).status_code == 403
    assert anon.post('/integrations/mpesa/callback?token=tok123', json={}).status_code == 200


def test_failed_stk_then_cash(client, m):
    enable(m, 'mpesa')
    add_to_cart(client)
    client.post('/complete_sale', data={'payment_method': 'mpesa', 'payment_phone': '0712345678'})
    payment = m.Payment.query.one()
    client.post(f'/payments/{payment.id}/simulate', data={'outcome': 'failed'})
    m.db.session.refresh(payment)
    assert payment.status == 'failed'
    client.post(f'/payments/{payment.id}/cash')
    m.db.session.refresh(payment)
    assert payment.status == 'paid' and payment.method == 'cash'
    assert payment.sale.kra_invoice_id is not None


def test_c2b_payment_matches_sale_reference(client, m):
    enable(m, 'mpesa')
    add_to_cart(client)
    client.post('/complete_sale', data={'payment_method': 'mpesa', 'payment_phone': '0712345678'})
    payment = m.Payment.query.one()
    anon = client.application.test_client()
    anon.post('/integrations/mpesa/callback',
              json={'TransID': 'QC2B1', 'TransAmount': '130', 'BillRefNumber': f'SALE{payment.sale_id}', 'MSISDN': '254712345678'})
    m.db.session.refresh(payment)
    assert payment.status == 'paid' and payment.receipt_number == 'QC2B1'


def test_cancel_unpaid_sale_restocks(client, m):
    enable(m, 'mpesa')
    add_to_cart(client, quantity=3)
    client.post('/complete_sale', data={'payment_method': 'mpesa', 'payment_phone': '0712345678'})
    assert m.Product.query.get(1).quantity == 7
    payment = m.Payment.query.one()
    client.post(f'/payments/{payment.id}/cancel')
    assert m.Product.query.get(1).quantity == 10
    m.db.session.refresh(payment)
    assert payment.status == 'cancelled' and payment.sale.refunded


def test_refund_sends_credit_note(client, m):
    add_to_cart(client)
    client.post('/complete_sale', data={'payment_method': 'cash'})
    sale = m.Sale.query.one()
    client.post(f'/receipt/{sale.id}/refund', data={'refund_reason': 'damaged'})
    log = m.IntegrationLog.query.filter_by(event='sale.refunded').one()
    assert log.status == 'success' and json.loads(log.request_json)['rcptTyCd'] == 'R'


def test_admin_adds_webhook_and_it_fires(client, m, monkeypatch):
    http = FakeHttp([FakeResponse(200, {'ok': True})])
    monkeypatch.setattr(integrations.base.requests, 'Session', lambda: http)
    resp = client.post('/integrations/new', data={'adapter': 'webhook', 'name': 'My ERP'})
    assert resp.location.endswith('/integrations/my_erp')
    client.post('/integrations/my_erp', data={
        'enabled': 'on', 'display_name': 'My ERP', 'mode': 'live', 'url': 'https://erp.example.com/hook',
        'method': 'POST', 'events': 'sale.completed', 'headers': '{}', 'body_template': '', 'hmac_secret_env': '',
        'timeout': '5', 'invoice_number_path': '', 'qr_path': '',
    })
    add_to_cart(client)
    client.post('/complete_sale', data={'payment_method': 'cash'})
    assert [c['url'] for c in http.calls] == ['https://erp.example.com/hook']
    sent = json.loads(http.calls[0]['data'])
    assert sent['event'] == 'sale.completed' and sent['data']['sale']['total'] == 130.0


def test_failed_call_can_be_retried(client, m, monkeypatch):
    http = FakeHttp([FakeResponse(503, {}), FakeResponse(200, {})])
    monkeypatch.setattr(integrations.base.requests, 'Session', lambda: http)
    m.db.session.add(m.IntegrationConfig(key='erp', adapter='webhook', name='ERP', enabled=True,
                                         settings={'url': 'https://erp.example.com', 'events': 'sale.completed'}))
    m.db.session.commit()
    add_to_cart(client)
    client.post('/complete_sale', data={'payment_method': 'cash'})
    log = m.IntegrationLog.query.filter_by(integration_key='erp').one()
    assert log.status == 'failed'
    client.post(f'/integrations/logs/{log.id}/retry')
    m.db.session.refresh(log)
    assert log.status == 'success' and log.attempts == 2


def test_admin_pages_render(client, m):
    assert client.get('/integrations').status_code == 200
    for key in ('kra_etims', 'mpesa'):
        page = client.get(f'/integrations/{key}')
        assert page.status_code == 200
    assert b'MPESA_CONSUMER_KEY' in client.get('/integrations/mpesa').data
    assert client.post('/integrations/kra_etims/test').status_code == 302
    assert client.post('/integrations/kra_etims/action/initialize_device').status_code == 302
    assert b'M-Pesa' not in client.get('/sales').data.split(b'payment_method')[1][:300]
    enable(m, 'mpesa')
    assert b'M-Pesa (STK push)' in client.get('/sales').data
