import base64
import hashlib
import hmac
import json

from integrations import adapters, SALE_COMPLETED, SALE_REFUNDED, PAYMENT_REQUESTED
from integrations.mpesa import MpesaDarajaIntegration, normalize_msisdn
from integrations.etims import KraEtimsIntegration, split_tax
from integrations.webhook import WebhookIntegration, dig
from tests.conftest import FakeResponse

SALE = {
    'id': 42, 'items': [
        {'id': 1, 'name': 'Bread', 'price': 65.0, 'quantity': 2, 'subtotal': 130.0},
        {'id': 2, 'name': 'Milk', 'price': 70.0, 'quantity': 1, 'subtotal': 70.0},
    ],
    'subtotal': 200.0, 'discount': 20.0, 'total': 180.0, 'cashier': 'admin',
    'customer_phone': '0712345678', 'payment_method': 'mpesa',
}


def test_registry_has_builtins_and_plugins():
    found = adapters()
    for adapter_id in ('mpesa_daraja', 'kra_etims', 'webhook', 'example_loyalty'):
        assert adapter_id in found


def test_normalize_msisdn():
    assert normalize_msisdn('0712 345 678') == '254712345678'
    assert normalize_msisdn('+254712345678') == '254712345678'
    assert normalize_msisdn('0110345678') == '254110345678'
    assert normalize_msisdn('712345678') == '254712345678'
    assert normalize_msisdn('12345') is None


def test_mpesa_stk_push_sandbox(monkeypatch, fake_http):
    monkeypatch.setenv('MPESA_CONSUMER_KEY', 'ck')
    monkeypatch.setenv('MPESA_CONSUMER_SECRET', 'cs')
    monkeypatch.setenv('MPESA_PASSKEY', 'pk')
    http = fake_http([
        FakeResponse(200, {'access_token': 'tok', 'expires_in': '3599'}),
        FakeResponse(200, {'ResponseCode': '0', 'CheckoutRequestID': 'ws_CO_1', 'CustomerMessage': 'Success'}),
    ])
    mpesa = MpesaDarajaIntegration('mpesa', {'mode': 'sandbox'}, http=http)
    result = mpesa.handle(PAYMENT_REQUESTED, {
        'payment': {'amount': 179.6, 'phone': '0712345678', 'sale_id': 42, 'reference': 'SALE42'},
        'callback_url': 'https://pos.example.com/integrations/mpesa/callback',
    })
    assert result.ok, result.error
    assert result.data['provider_ref'] == 'ws_CO_1'
    auth_call, push_call = http.calls
    assert auth_call['url'].startswith('https://sandbox.safaricom.co.ke/oauth/v1/generate')
    assert auth_call['auth'] == ('ck', 'cs')
    body = push_call['json']
    assert push_call['headers']['Authorization'] == 'Bearer tok'
    assert body['Amount'] == 180 and body['PartyA'] == '254712345678'
    assert base64.b64decode(body['Password']).decode() == f"174379pk{body['Timestamp']}"
    assert result.request['Password'] == '***'  # never logged


def test_mpesa_requires_credentials_outside_mock(fake_http):
    mpesa = MpesaDarajaIntegration('mpesa', {'mode': 'sandbox'}, http=fake_http())
    result = mpesa.handle(PAYMENT_REQUESTED, {'payment': {'amount': 10, 'phone': '0712345678', 'sale_id': 1}})
    assert not result.ok and 'consumer_key' in result.error


def test_mpesa_parses_stk_and_c2b_callbacks():
    mpesa = MpesaDarajaIntegration('mpesa', {'mode': 'mock'})
    ok = mpesa.parse_callback({'Body': {'stkCallback': {
        'MerchantRequestID': 'm', 'CheckoutRequestID': 'ws_CO_1', 'ResultCode': 0, 'ResultDesc': 'ok',
        'CallbackMetadata': {'Item': [{'Name': 'Amount', 'Value': 180}, {'Name': 'MpesaReceiptNumber', 'Value': 'QAB123'},
                                      {'Name': 'PhoneNumber', 'Value': 254712345678}]}}}}, {})
    assert (ok.status, ok.provider_ref, ok.receipt_number, ok.phone) == ('paid', 'ws_CO_1', 'QAB123', '254712345678')
    failed = mpesa.parse_callback({'Body': {'stkCallback': {'CheckoutRequestID': 'ws_CO_2', 'ResultCode': 1032,
                                                            'ResultDesc': 'Request cancelled by user'}}}, {})
    assert failed.status == 'failed'
    c2b = mpesa.parse_callback({'TransID': 'QXY9', 'TransAmount': '180.00', 'BillRefNumber': 'sale42', 'MSISDN': '2547xxx'}, {})
    assert (c2b.status, c2b.reference, c2b.amount) == ('paid', 'SALE42', 180.0)


def test_split_tax():
    assert split_tax(116.0, 'B') == (116.0, 16.0)
    assert split_tax(108.0, 'E') == (108.0, 8.0)
    assert split_tax(100.0, 'A') == (100.0, 0.0)


def test_etims_payload_spreads_discount_and_vat():
    etims = KraEtimsIntegration('kra_etims', {'mode': 'mock'})
    payload = etims.build_sales_payload(SALE)
    assert payload['invcNo'] == 42 and payload['rcptTyCd'] == 'S' and payload['pmtTyCd'] == '06'
    assert payload['totItemCnt'] == 2
    assert payload['totAmt'] == 180.0
    assert payload['taxblAmtB'] == 180.0
    assert payload['taxAmtB'] == round(117.0 * 16 / 116, 2) + round(63.0 * 16 / 116, 2)
    first = payload['itemList'][0]
    assert first['itemCd'] == 'KE1NTXU0000001' and first['dcRt'] == 10.0 and first['dcAmt'] == 13.0


def test_etims_refund_is_credit_note():
    etims = KraEtimsIntegration('kra_etims', {'mode': 'mock'})
    payload = etims.build_sales_payload(SALE, refund=True)
    assert payload['rcptTyCd'] == 'R' and payload['orgInvcNo'] == 42 and payload['invcNo'] == 900000042


def test_etims_mock_signs_invoice():
    result = KraEtimsIntegration('kra_etims', {'mode': 'mock'}).handle(SALE_COMPLETED, {'sale': SALE})
    fiscal = result.data['fiscal']
    assert fiscal['invoice_number'] == 'P000000000A/00/42'
    assert fiscal['signature'] and fiscal['signature'] in fiscal['qr_data']


def test_etims_sandbox_success_and_rejection(monkeypatch, fake_http):
    monkeypatch.setenv('ETIMS_CMC_KEY', 'cmc')
    http = fake_http([FakeResponse(200, {'resultCd': '000', 'resultMsg': 'OK', 'data': {'rcptNo': 7, 'rcptSign': 'SIGN123'}})])
    etims = KraEtimsIntegration('kra_etims', {'mode': 'sandbox', 'tin': 'P051234567X'}, http=http)
    result = etims.handle(SALE_COMPLETED, {'sale': SALE})
    assert result.ok and result.data['fiscal']['signature'] == 'SIGN123'
    call = http.calls[0]
    assert call['url'] == 'https://etims-api-sbx.kra.go.ke/etims-api/saveTrnsSalesOsdc'
    assert call['headers']['cmcKey'] == 'cmc' and call['headers']['tin'] == 'P051234567X'

    http = fake_http([FakeResponse(200, {'resultCd': '910', 'resultMsg': 'Item code not registered'})])
    result = KraEtimsIntegration('kra_etims', {'mode': 'sandbox'}, http=http).handle(SALE_REFUNDED, {'sale': SALE})
    assert not result.ok and 'Item code not registered' in result.error


def test_webhook_template_env_headers_hmac_and_fiscal(monkeypatch, fake_http):
    monkeypatch.setenv('ERP_TOKEN', 'secret-token')
    monkeypatch.setenv('ERP_HMAC', 'shh')
    http = fake_http([FakeResponse(201, {'data': {'invoice': {'no': 'INV-9'}, 'qr': 'https://verify/9'}})])
    hook = WebhookIntegration('erp', {
        'url': 'https://erp.example.com/sales/{{ sale.id }}',
        'method': 'PUT',
        'events': 'sale.completed, sale.refunded',
        'headers': '{"Authorization": "Bearer ${ERP_TOKEN}"}',
        'body_template': '{"ref": "{{ sale.id }}", "amount": {{ sale.total }}, "lines": {{ sale["items"] | tojson }}}',
        'hmac_secret_env': 'ERP_HMAC',
        'invoice_number_path': 'data.invoice.no',
        'qr_path': 'data.qr',
    }, http=http)
    assert hook.handles(SALE_REFUNDED) and not hook.handles(PAYMENT_REQUESTED)
    result = hook.handle(SALE_COMPLETED, {'sale': SALE})
    assert result.ok, result.error
    call = http.calls[0]
    assert call['method'] == 'PUT' and call['url'] == 'https://erp.example.com/sales/42'
    assert call['headers']['Authorization'] == 'Bearer secret-token'
    body = call['data'].decode()
    assert json.loads(body)['amount'] == 180.0
    ts = call['headers']['X-POS-Timestamp']
    expected = hmac.new(b'shh', f'{ts}.{body}'.encode(), hashlib.sha256).hexdigest()
    assert call['headers']['X-POS-Signature'] == f'sha256={expected}'
    assert result.data['fiscal'] == {'invoice_number': 'INV-9', 'qr_data': 'https://verify/9'}
    assert 'secret-token' not in json.dumps(result.request)


def test_webhook_failure_and_dry_run(fake_http):
    http = fake_http([FakeResponse(500, {'error': 'boom'})])
    hook = WebhookIntegration('x', {'url': 'https://x.example.com', 'events': '*'}, http=http)
    assert not hook.handle(SALE_COMPLETED, {'sale': SALE}).ok
    dry = WebhookIntegration('x', {'url': 'https://x.example.com', 'mode': 'dry_run'}, http=fake_http())
    result = dry.handle(SALE_COMPLETED, {'sale': SALE})
    assert result.ok and result.data['dry_run'] and dry.http.calls == []


def test_dig():
    assert dig({'a': [{'b': 1}]}, 'a.0.b') == 1
    assert dig({'a': {}}, 'a.b.c') is None
