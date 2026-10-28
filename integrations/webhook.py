"""
Generic HTTP / webhook adapter: connect the POS to any REST API without code.

Each instance is configured on the admin page with:
  * the URL and HTTP method to call,
  * which POS events trigger it,
  * optional headers (use ${ENV_VAR} to pull secrets from the environment),
  * an optional Jinja body template (default: the whole event as JSON),
  * an optional HMAC secret so the receiver can verify the call came from us,
  * optional response paths, so a third-party tax/invoicing API can put its
    invoice number and QR code on the receipt.
"""
import hashlib
import hmac
import json
import os
import re
import time

from jinja2.sandbox import SandboxedEnvironment

from .base import Integration, Field, Result, ALL_EVENTS
from .registry import register

_jinja = SandboxedEnvironment(autoescape=False)
_jinja.filters['tojson'] = lambda v: json.dumps(v, default=str)
_ENV_REF = re.compile(r'\$\{([A-Z0-9_]+)\}')


def _expand_env(value):
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ''), value)


def dig(data, path):
    """Read a dotted path like 'data.invoice.number' or 'items.0.id' from nested JSON."""
    if not path:
        return None
    current = data
    for part in path.split('.'):
        if isinstance(current, list) and part.isdigit():
            idx = int(part)
            current = current[idx] if idx < len(current) else None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
        if current is None:
            return None
    return current


@register
class WebhookIntegration(Integration):
    adapter = 'webhook'
    name = 'Custom API / webhook'
    description = 'Call any HTTP API when something happens in the POS: ERPs, accounting, loyalty, third-party tax providers, Slack, Zapier and so on.'
    category = 'webhook'
    allow_multiple = True
    fields = (
        Field('mode', 'Mode', default='live', type='choice', choices=('live', 'dry_run'),
              help='dry_run builds and logs the request without sending it.'),
        Field('url', 'URL', default='', help='Placeholders like {{ sale.id }} work here too.'),
        Field('method', 'HTTP method', default='POST', type='choice', choices=('POST', 'PUT', 'PATCH', 'GET')),
        Field('events', 'Events', default='sale.completed',
              help='Comma-separated: ' + ', '.join(ALL_EVENTS) + ' (or "*" for all).'),
        Field('headers', 'Headers (JSON)', default='{"Content-Type": "application/json"}', type='textarea',
              help='Use ${MY_API_KEY} to read a secret from an environment variable, e.g. {"Authorization": "Bearer ${MY_API_KEY}"}.'),
        Field('body_template', 'Body template (Jinja, optional)', default='', type='textarea',
              help='Leave blank to send {"event": ..., "data": ...}. Variables: event, sale, payment, plus the tojson filter.'),
        Field('hmac_secret_env', 'HMAC secret env var (optional)', default='',
              help='Name of an environment variable holding a shared secret; adds an X-POS-Signature header (sha256).'),
        Field('timeout', 'Timeout (seconds)', default='15'),
        Field('invoice_number_path', 'Response path to invoice number (optional)', default='',
              help='e.g. data.invoiceNumber. If set, it replaces the receipt invoice number.'),
        Field('qr_path', 'Response path to QR / verify URL (optional)', default='', help='e.g. data.qrUrl'),
    )

    @property
    def mode(self):
        return (self.setting('mode') or 'live').lower()

    @property
    def is_mock(self):
        return self.mode == 'dry_run'

    @property
    def subscribed_events(self):
        raw = (self.setting('events') or '').strip()
        if raw == '*':
            return set(ALL_EVENTS)
        return {e.strip() for e in raw.split(',') if e.strip()}

    def handles(self, event):
        return event in self.subscribed_events

    def build_request(self, event, payload):
        context = {'event': event, **payload}
        url = _jinja.from_string(self.setting('url') or '').render(**context).strip()
        if not url:
            raise ValueError('No URL configured.')
        try:
            headers = json.loads(self.setting('headers') or '{}')
        except ValueError as e:
            raise ValueError(f'Headers are not valid JSON: {e}')
        headers = {str(k): _expand_env(str(v)) for k, v in headers.items()}

        template = (self.setting('body_template') or '').strip()
        if template:
            body = _jinja.from_string(template).render(**context)
        else:
            body = json.dumps({'event': event, 'data': payload}, default=str)

        secret_env = (self.setting('hmac_secret_env') or '').strip()
        if secret_env and os.environ.get(secret_env):
            ts = str(int(time.time()))
            sig = hmac.new(os.environ[secret_env].encode(), f'{ts}.{body}'.encode(), hashlib.sha256).hexdigest()
            headers['X-POS-Timestamp'] = ts
            headers['X-POS-Signature'] = f'sha256={sig}'
        headers.setdefault('X-POS-Event', event)
        return url, headers, body

    def handle(self, event, payload):
        try:
            url, headers, body = self.build_request(event, payload)
        except Exception as e:
            return Result.failure(f'Could not build the request: {e}')

        # Never write header values to the log: they usually carry API keys.
        logged = {'method': self.setting('method'), 'url': url, 'headers': sorted(headers), 'body': body[:4000]}
        if self.is_mock:
            return Result.success({'dry_run': True}, request=logged)

        method = (self.setting('method') or 'POST').upper()
        try:
            resp = self.http.request(method, url, data=body.encode() if method != 'GET' else None,
                                     headers=headers, timeout=float(self.setting('timeout') or 15))
        except Exception as e:
            return Result.failure(f'Request failed: {e}', request=logged)
        data = self._json(resp)
        if not resp.ok:
            return Result.failure(f'{url} returned HTTP {resp.status_code}', request=logged, response=data)

        out = {'status_code': resp.status_code}
        invoice = dig(data, self.setting('invoice_number_path'))
        if invoice:
            out['fiscal'] = {'invoice_number': str(invoice), 'qr_data': dig(data, self.setting('qr_path'))}
        return Result.success(out, request=logged, response=data)

    def test_connection(self):
        sample = {
            'sale': {'id': 0, 'items': [{'id': 1, 'name': 'Test item', 'price': 100, 'quantity': 1, 'subtotal': 100}],
                     'subtotal': 100, 'discount': 0, 'total': 100, 'test': True},
        }
        event = sorted(self.subscribed_events)[0] if self.subscribed_events else 'sale.completed'
        result = self.handle(event, sample)
        if result.ok:
            result.data.setdefault('message', f'Sent a test {event} event successfully.')
        return result
