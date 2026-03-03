# payments/utils.py
from decimal import Decimal

def calculate_b2c_fee(amount: Decimal) -> Decimal:
    if amount <= 1000:
        return Decimal("50")
    elif amount <= 10000:
        return Decimal("175")
    elif amount <= 25000:
        return Decimal("200")
    elif amount <= 50000:
        return Decimal("300")
    elif amount <= 70000:
        return Decimal("400")
    else:
        return amount * Decimal("0.005")  # 0.5% for very large payouts