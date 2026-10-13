"""The caller demonstrates a cross-file API contract for review."""
from .pricing import quote

def checkout_total(amount: float, discount_percent: float) -> float:
    return quote(amount, discount_percent)['total']
