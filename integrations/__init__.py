"""
Pluggable integrations for the POS. See README.md ("Integrations") for how
to configure the built-in adapters and how to add your own.
"""
from .base import (  # noqa: F401
    Integration, Field, Result, CallbackResult, IntegrationError,
    SALE_COMPLETED, SALE_REFUNDED, PAYMENT_REQUESTED, PAYMENT_COMPLETED, PAYMENT_FAILED, ALL_EVENTS,
)
from .registry import adapters, get_adapter, register  # noqa: F401
