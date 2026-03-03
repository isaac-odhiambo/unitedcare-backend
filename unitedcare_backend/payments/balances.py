# payments/balances.py
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Dict

from django.db.models import Sum
from django.contrib.auth.base_user import AbstractBaseUser

from .models import PaymentLedger


def _sum_amount(qs) -> Decimal:
    v = qs.aggregate(s=Sum("amount"))["s"]
    return v if v is not None else Decimal("0")


def get_user_balance(
    *,
    user: AbstractBaseUser,
    category: Optional[str] = None,
) -> Decimal:
    """
    Net balance = credits - debits from PaymentLedger.

    If category is provided (e.g. "SAVINGS", "MERRY"), we compute only that category.
    Note: WITHDRAWAL debits will reduce the balance in their own category;
          you should decide whether withdrawals should be deducted from SAVINGS
          (recommended) or from WITHDRAWAL category (not recommended).
    """
    qs = PaymentLedger.objects.filter(user=user)

    if category:
        qs = qs.filter(category=category)

    credits = _sum_amount(qs.filter(entry_type="CREDIT"))
    debits = _sum_amount(qs.filter(entry_type="DEBIT"))
    return credits - debits


def get_user_balances_breakdown(*, user: AbstractBaseUser) -> Dict[str, Decimal]:
    """
    Helpful for dashboards / admin checks.
    """
    categories = ["SAVINGS", "LOANS", "MERRY", "GROUP"]
    return {c: get_user_balance(user=user, category=c) for c in categories}