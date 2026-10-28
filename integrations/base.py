"""
Core building blocks for POS integrations.

An integration is a small class that knows how to talk to one external API.
The POS never calls an API directly: it emits events ("sale.completed",
"payment.requested", ...) and every enabled integration that subscribes to
that event gets a chance to handle it. Adding a new API means writing one
subclass of `Integration` (or configuring the generic webhook adapter),
not touching the checkout code.
"""
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import requests


# --- Events the POS emits -------------------------------------------------
SALE_COMPLETED = 'sale.completed'
SALE_REFUNDED = 'sale.refunded'
PAYMENT_REQUESTED = 'payment.requested'
PAYMENT_COMPLETED = 'payment.completed'
PAYMENT_FAILED = 'payment.failed'

ALL_EVENTS = (SALE_COMPLETED, SALE_REFUNDED, PAYMENT_REQUESTED, PAYMENT_COMPLETED, PAYMENT_FAILED)


class IntegrationError(Exception):
    """Raised by an adapter when the external API rejects a request."""


@dataclass
class Field:
    """
    One setting an integration needs.

    Secret fields are never stored in the database: their value is always read
    from the environment variable named in `env`. Non-secret fields are stored
    in the database but can still be overridden by setting `env`.
    """
    name: str
    label: str
    default: Any = ''
    secret: bool = False
    env: Optional[str] = None
    help: str = ''
    choices: tuple = ()
    type: str = 'text'  # text | textarea | bool | choice
    required: bool = True  # only checked for secret fields outside mock mode


@dataclass
class Result:
    """What an adapter hands back to the POS after handling an event."""
    ok: bool
    data: dict = field(default_factory=dict)
    error: Optional[str] = None
    request: Any = None   # what was sent, for the audit log (never include secrets)
    response: Any = None  # what came back, for the audit log
    skipped: bool = False

    @classmethod
    def success(cls, data=None, request=None, response=None):
        return cls(ok=True, data=data or {}, request=request, response=response)

    @classmethod
    def failure(cls, error, request=None, response=None):
        return cls(ok=False, error=str(error), request=request, response=response)

    @classmethod
    def skip(cls, reason):
        return cls(ok=True, skipped=True, data={'reason': reason})


@dataclass
class CallbackResult:
    """A payment provider's callback, translated into POS terms."""
    provider_ref: Optional[str] = None  # e.g. M-Pesa CheckoutRequestID
    reference: Optional[str] = None     # the account/bill reference the customer used
    status: str = 'pending'             # paid | failed | pending
    receipt_number: Optional[str] = None
    amount: Optional[float] = None
    phone: Optional[str] = None
    message: Optional[str] = None
    ack: dict = field(default_factory=lambda: {'ResultCode': 0, 'ResultDesc': 'Accepted'})


class Integration:
    """
    Base class for every adapter.

    Subclasses set the class attributes below and override the hooks they
    need. Each configured instance gets its own `settings` dict, so the same
    adapter (for example the generic webhook) can be set up several times.
    """
    adapter = ''           # unique adapter id, e.g. "mpesa_daraja"
    name = ''              # human name shown in the admin page
    description = ''
    category = 'other'     # payments | tax | messaging | webhook | other
    events = ()            # events this adapter reacts to
    fields = ()            # tuple of Field
    allow_multiple = False  # can admins add more than one instance?
    payment_method_label = None  # set on payment adapters, shown at checkout
    payment_method_code = None   # short name stored on payments, e.g. "mpesa"

    def __init__(self, key, settings=None, http=None):
        self.key = key
        self._stored = dict(settings or {})
        self.http = http or requests.Session()

    # --- settings -------------------------------------------------------
    def setting(self, name):
        spec = next((f for f in self.fields if f.name == name), None)
        if spec is not None and spec.env and os.environ.get(spec.env):
            return os.environ[spec.env]
        if spec is not None and spec.secret:
            return spec.default or None
        value = self._stored.get(name)
        if value in (None, '') and spec is not None:
            return spec.default
        return value

    def bool_setting(self, name):
        value = self.setting(name)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ('1', 'true', 'yes', 'on')

    @property
    def mode(self):
        """'mock' lets the whole flow run without real credentials."""
        return (self.setting('mode') or 'mock').lower()

    @property
    def is_mock(self):
        return self.mode == 'mock'

    def missing_settings(self):
        """Names of required settings that have no value (ignored in mock mode)."""
        if self.is_mock:
            return []
        return [f.name for f in self.fields if f.secret and f.required and not self.setting(f.name)]

    @classmethod
    def describe_fields(cls):
        return [
            {
                'name': f.name, 'label': f.label, 'secret': f.secret, 'env': f.env,
                'help': f.help, 'choices': f.choices, 'type': f.type, 'default': f.default,
                'env_set': bool(f.env and os.environ.get(f.env)),
            }
            for f in cls.fields
        ]

    # --- hooks ----------------------------------------------------------
    def handles(self, event):
        return event in self.events

    def handle(self, event, payload):
        """React to a POS event. Return a Result."""
        return Result.skip(f'{self.adapter} does not handle {event}')

    def test_connection(self):
        """Check credentials/reachability from the admin page."""
        return Result.success({'message': 'No connection test for this integration.'})

    # Payment adapters also implement these:
    def parse_callback(self, body, args):
        raise NotImplementedError

    def query_payment(self, provider_ref):
        raise NotImplementedError

    # --- helpers --------------------------------------------------------
    def _json(self, resp):
        try:
            return resp.json()
        except ValueError:
            return {'raw': resp.text[:2000]}
