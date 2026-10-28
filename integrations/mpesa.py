"""
Safaricom M-Pesa Daraja adapter.

Supports:
  * Lipa Na M-Pesa Online (STK push): the customer gets a PIN prompt on their
    phone at checkout, and Safaricom calls our callback URL with the outcome.
  * STK push status query, for when a callback is slow or lost.
  * C2B confirmation callbacks: a customer pays the Till/Paybill manually
    using the sale reference (e.g. "SALE42") as the account number.

Docs: https://developer.safaricom.co.ke/APIs
"""
import base64
import datetime
import re
import uuid

from .base import Integration, Field, Result, CallbackResult, PAYMENT_REQUESTED
from .registry import register

BASE_URLS = {
    'sandbox': 'https://sandbox.safaricom.co.ke',
    'production': 'https://api.safaricom.co.ke',
}


def normalize_msisdn(phone):
    """Turn 07xx / 01xx / +2547xx / 2547xx into Daraja's 2547xxxxxxxx format."""
    if not phone:
        return None
    digits = re.sub(r'\D', '', str(phone))
    if digits.startswith('0') and len(digits) == 10:
        digits = '254' + digits[1:]
    elif len(digits) == 9 and digits[0] in '71':
        digits = '254' + digits
    if not re.fullmatch(r'254[17]\d{8}', digits):
        return None
    return digits


@register
class MpesaDarajaIntegration(Integration):
    adapter = 'mpesa_daraja'
    name = 'M-Pesa (Daraja)'
    description = 'STK push at checkout, payment callbacks, status queries and C2B confirmations via Safaricom Daraja.'
    category = 'payments'
    events = (PAYMENT_REQUESTED,)
    payment_method_label = 'M-Pesa (STK push)'
    payment_method_code = 'mpesa'
    fields = (
        Field('mode', 'Mode', default='mock', type='choice', choices=('mock', 'sandbox', 'production'),
              help='mock needs no credentials; sandbox uses Safaricom test servers.'),
        Field('shortcode', 'Business shortcode', default='174379', env='MPESA_SHORTCODE',
              help='Paybill number, or the Head Office/store number for a Till. 174379 is the Daraja sandbox shortcode.'),
        Field('transaction_type', 'Transaction type', default='CustomerPayBillOnline', type='choice',
              choices=('CustomerPayBillOnline', 'CustomerBuyGoodsOnline')),
        Field('party_b', 'Till number (Buy Goods only)', default='',
              help='Leave blank for Paybill; for Buy Goods enter the Till number that receives the money.'),
        Field('account_reference_prefix', 'Account reference prefix', default='SALE'),
        Field('consumer_key', 'Consumer key', secret=True, env='MPESA_CONSUMER_KEY'),
        Field('consumer_secret', 'Consumer secret', secret=True, env='MPESA_CONSUMER_SECRET'),
        Field('passkey', 'Lipa Na M-Pesa passkey', secret=True, env='MPESA_PASSKEY'),
        Field('callback_token', 'Callback token', secret=True, env='MPESA_CALLBACK_TOKEN', required=False,
              help='Random string added to the callback URL so only Safaricom (who knows the URL) can post to it.'),
    )

    _token = None
    _token_expires = None

    @property
    def base_url(self):
        return BASE_URLS.get(self.mode, BASE_URLS['sandbox'])

    # --- auth -----------------------------------------------------------
    def access_token(self):
        now = datetime.datetime.utcnow()
        if self._token and self._token_expires and now < self._token_expires:
            return self._token
        resp = self.http.get(
            f'{self.base_url}/oauth/v1/generate',
            params={'grant_type': 'client_credentials'},
            auth=(self.setting('consumer_key'), self.setting('consumer_secret')),
            timeout=15,
        )
        data = self._json(resp)
        if not resp.ok or 'access_token' not in data:
            raise RuntimeError(f'Daraja auth failed ({resp.status_code}): {data}')
        self._token = data['access_token']
        self._token_expires = now + datetime.timedelta(seconds=int(data.get('expires_in', 3599)) - 60)
        return self._token

    def _password(self, timestamp):
        raw = f"{self.setting('shortcode')}{self.setting('passkey')}{timestamp}"
        return base64.b64encode(raw.encode()).decode()

    @staticmethod
    def _timestamp():
        # Daraja expects East Africa Time (UTC+3)
        return (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime('%Y%m%d%H%M%S')

    def _headers(self):
        return {'Authorization': f'Bearer {self.access_token()}'}

    # --- events ---------------------------------------------------------
    def handle(self, event, payload):
        if event != PAYMENT_REQUESTED:
            return Result.skip(f'Not handled: {event}')
        return self.stk_push(payload['payment'], payload.get('callback_url'))

    def stk_push(self, payment, callback_url):
        phone = normalize_msisdn(payment.get('phone'))
        if not phone:
            return Result.failure('Enter a valid Safaricom number, e.g. 0712 345 678.')
        amount = max(1, int(round(float(payment['amount']))))  # Daraja only takes whole shillings
        reference = payment.get('reference') or f"{self.setting('account_reference_prefix')}{payment.get('sale_id')}"
        timestamp = self._timestamp()
        shortcode = self.setting('shortcode')
        party_b = self.setting('party_b') if self.setting('transaction_type') == 'CustomerBuyGoodsOnline' and self.setting('party_b') else shortcode

        body = {
            'BusinessShortCode': shortcode,
            'Password': self._password(timestamp),
            'Timestamp': timestamp,
            'TransactionType': self.setting('transaction_type'),
            'Amount': amount,
            'PartyA': phone,
            'PartyB': party_b,
            'PhoneNumber': phone,
            'CallBackURL': callback_url,
            'AccountReference': reference[:12],
            'TransactionDesc': f'Payment {reference}'[:13],
        }
        logged = {**body, 'Password': '***'}

        if self.is_mock:
            checkout_id = f'ws_CO_MOCK_{uuid.uuid4().hex[:12]}'
            return Result.success(
                {'provider_ref': checkout_id, 'status': 'pending', 'message': 'Mock STK push sent.'},
                request=logged,
                response={'CheckoutRequestID': checkout_id, 'ResponseCode': '0', 'mock': True},
            )

        missing = self.missing_settings()
        if missing:
            return Result.failure(f'Missing M-Pesa settings: {", ".join(missing)}', request=logged)
        try:
            resp = self.http.post(f'{self.base_url}/mpesa/stkpush/v1/processrequest',
                                  json=body, headers=self._headers(), timeout=30)
        except Exception as e:
            return Result.failure(f'Could not reach Daraja: {e}', request=logged)
        data = self._json(resp)
        if not resp.ok or str(data.get('ResponseCode')) != '0':
            return Result.failure(data.get('errorMessage') or data.get('ResponseDescription') or f'HTTP {resp.status_code}',
                                  request=logged, response=data)
        return Result.success(
            {'provider_ref': data['CheckoutRequestID'], 'status': 'pending',
             'message': data.get('CustomerMessage', 'STK push sent.')},
            request=logged, response=data,
        )

    def query_payment(self, provider_ref):
        if self.is_mock:
            return Result.success({'status': 'pending'})
        timestamp = self._timestamp()
        body = {
            'BusinessShortCode': self.setting('shortcode'),
            'Password': self._password(timestamp),
            'Timestamp': timestamp,
            'CheckoutRequestID': provider_ref,
        }
        try:
            resp = self.http.post(f'{self.base_url}/mpesa/stkpushquery/v1/query',
                                  json=body, headers=self._headers(), timeout=30)
        except Exception as e:
            return Result.failure(f'Could not reach Daraja: {e}')
        data = self._json(resp)
        code = data.get('ResultCode')
        if code is None:
            # Daraja answers "The transaction is being processed" with an error while it's still pending
            return Result.success({'status': 'pending', 'message': data.get('errorMessage')}, response=data)
        status = 'paid' if str(code) == '0' else 'failed'
        return Result.success({'status': status, 'message': data.get('ResultDesc')}, response=data)

    def parse_callback(self, body, args):
        """Handles both STK push callbacks and C2B confirmation callbacks."""
        body = body or {}
        stk = (body.get('Body') or {}).get('stkCallback')
        if stk:
            meta = {i.get('Name'): i.get('Value') for i in (stk.get('CallbackMetadata') or {}).get('Item', [])}
            paid = str(stk.get('ResultCode')) == '0'
            return CallbackResult(
                provider_ref=stk.get('CheckoutRequestID'),
                status='paid' if paid else 'failed',
                receipt_number=meta.get('MpesaReceiptNumber'),
                amount=meta.get('Amount'),
                phone=str(meta['PhoneNumber']) if meta.get('PhoneNumber') else None,
                message=stk.get('ResultDesc'),
            )
        if 'TransID' in body:  # C2B confirmation
            return CallbackResult(
                reference=(body.get('BillRefNumber') or '').strip().upper(),
                status='paid',
                receipt_number=body.get('TransID'),
                amount=float(body.get('TransAmount') or 0),
                phone=body.get('MSISDN'),
                message='C2B payment received',
            )
        return CallbackResult(status='pending', message='Unrecognised callback body')

    def register_c2b_urls(self, confirmation_url, validation_url):
        """One-time setup so Safaricom sends manual Till/Paybill payments to us."""
        body = {
            'ShortCode': self.setting('party_b') or self.setting('shortcode'),
            'ResponseType': 'Completed',
            'ConfirmationURL': confirmation_url,
            'ValidationURL': validation_url,
        }
        if self.is_mock:
            return Result.success({'message': 'Mock C2B URLs registered.'}, request=body)
        try:
            resp = self.http.post(f'{self.base_url}/mpesa/c2b/v1/registerurl',
                                  json=body, headers=self._headers(), timeout=30)
        except Exception as e:
            return Result.failure(f'Could not reach Daraja: {e}', request=body)
        data = self._json(resp)
        if not resp.ok:
            return Result.failure(data.get('errorMessage') or f'HTTP {resp.status_code}', request=body, response=data)
        return Result.success({'message': data.get('ResponseDescription', 'Registered')}, request=body, response=data)

    def test_connection(self):
        if self.is_mock:
            return Result.success({'message': 'Mock mode: no credentials needed. Switch to sandbox to test real Daraja calls.'})
        missing = self.missing_settings()
        if missing:
            return Result.failure(f'Set these environment variables first: {", ".join(missing)}')
        try:
            self.access_token()
        except Exception as e:
            return Result.failure(str(e))
        return Result.success({'message': f'Authenticated with Daraja ({self.mode}).'})
