"""Benign quotation fixture, separate from the application's runtime."""
def quote(amount: float, discount_percent: float) -> dict:
    if amount < 0 or not 0 <= discount_percent <= 100:
        raise ValueError('Amount/discount outside contract')
    discount_ratio = discount_percent / 100
    total = round(amount * (1 - discount_ratio), 2)
    return {'total': total}
