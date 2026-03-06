from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import SavingsAccount, SavingsTransaction

MONEY_QUANT = Decimal("0.01")


def q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _extract_id(reference: str, prefix: str) -> Optional[int]:
    ref = (reference or "").strip()
    if not ref.startswith(prefix):
        return None
    try:
        return int(ref.replace(prefix, "").strip())
    except Exception:
        return None


def get_default_savings_account(user) -> SavingsAccount:
    """
    Policy: earliest active FLEXIBLE account is the user's wallet.
    """
    acct = SavingsAccount.objects.filter(user=user, is_active=True, account_type="FLEXIBLE").order_by("id").first()
    if not acct:
        raise ValidationError("You need an active FLEXIBLE savings account.")
    return acct


def get_account_or_404_for_user(account_id: int, user) -> SavingsAccount:
    acct = SavingsAccount.objects.filter(id=account_id, user=user, is_active=True).first()
    if not acct:
        raise ValidationError("Savings account not found.")
    return acct


@transaction.atomic
def create_account(*, user, name: str, account_type: str, locked_until=None, target_amount=None, target_deadline=None):
    name = (name or "").strip()
    if not name:
        raise ValidationError("Account name is required.")
    if account_type not in ("FLEXIBLE", "FIXED", "TARGET"):
        raise ValidationError("Invalid account_type.")

    acct = SavingsAccount.objects.create(
        user=user,
        name=name,
        account_type=account_type,
        locked_until=locked_until or None,
        target_amount=target_amount or None,
        target_deadline=target_deadline or None,
        balance=Decimal("0.00"),
        reserved_amount=Decimal("0.00"),
        is_active=True,
    )
    return acct


@transaction.atomic
def manual_deposit(*, user, account_id: int, amount: Decimal, reference: str | None = None, note: str | None = None):
    amt = q2(Decimal(str(amount)))
    if amt <= 0:
        raise ValidationError("amount must be greater than 0.")

    acct = SavingsAccount.objects.select_for_update().filter(id=account_id, user=user, is_active=True).first()
    if not acct:
        raise ValidationError("Savings account not found.")

    acct.balance = q2(Decimal(acct.balance) + amt)
    acct.full_clean()
    acct.save(update_fields=["balance"])

    SavingsTransaction.objects.create(
        account=acct,
        txn_type="DEPOSIT",
        amount=amt,
        reference=(reference or None),
        note=(note or None),
    )
    return acct


# ==========================================================
# ✅ Called by payments/services.py on STK SUCCESS
# payments uses:
#   apply_mpesa_deposit(user=tx.user, amount=tx.amount, mpesa_tx=tx, reference=tx.reference)
#
# Recommended STK reference from frontend:
#   reference="SAVINGS-<account_id>"
# If not provided -> deposits into default FLEXIBLE account.
# ==========================================================
@transaction.atomic
def apply_mpesa_deposit(*, user, amount: Decimal, mpesa_tx, reference: str = "") -> SavingsAccount:
    if not user:
        raise ValidationError("Savings deposit requires authenticated user.")

    tx_id = getattr(mpesa_tx, "id", None)
    if not tx_id:
        raise ValidationError("Invalid mpesa_tx (missing id).")

    amt = q2(Decimal(str(amount)))
    if amt <= 0:
        raise ValidationError("amount must be greater than 0.")

    account_id = _extract_id(reference or "", "SAVINGS-")

    if account_id:
        acct = SavingsAccount.objects.select_for_update().filter(id=account_id, user=user, is_active=True).first()
        if not acct:
            acct = SavingsAccount.objects.select_for_update().get(id=get_default_savings_account(user).id)
    else:
        acct = SavingsAccount.objects.select_for_update().get(id=get_default_savings_account(user).id)

    mpesa_ref = f"MPESA_TX#{tx_id}"

    # ✅ idempotency guard
    if SavingsTransaction.objects.filter(account=acct, txn_type="DEPOSIT", reference=mpesa_ref).exists():
        return acct

    acct.balance = q2(Decimal(acct.balance) + amt)
    acct.full_clean()
    acct.save(update_fields=["balance"])

    SavingsTransaction.objects.create(
        account=acct,
        txn_type="DEPOSIT",
        amount=amt,
        reference=mpesa_ref,
        note=f"MPesa savings deposit ({(reference or '').strip()})",
        created_at=timezone.now(),
    )

    return acct


# ==========================================================
# ✅ Called by payments/services.py on B2C SUCCESS
# Deducts from the savings account and records withdrawal transaction
#
# WithdrawalRequest reference in payments: "WD#<id>"
# We use SavingsTransaction.reference="WD#<id>" (or MPESA_TX#<id>) for idempotency
# ==========================================================
@transaction.atomic
def apply_mpesa_withdrawal_payout(
    *,
    user,
    requested_amount: Decimal,
    withdrawal_ref: str,
    target_object: Optional[object],
    mpesa_tx,
) -> SavingsAccount:
    """
    - requested_amount is the FULL amount requested by user (before fee),
      because that is what you debit from savings balance.
    - payout_amount is what was sent to user phone (tx.amount) and may be less due to fee.
    """
    if not user:
        raise ValidationError("Withdrawal requires authenticated user.")

    amt = q2(Decimal(str(requested_amount)))
    if amt <= 0:
        raise ValidationError("requested_amount must be > 0.")

    tx_id = getattr(mpesa_tx, "id", None)
    if not tx_id:
        raise ValidationError("Invalid mpesa_tx (missing id).")

    # Determine account from target_object if present; else default wallet
    acct: SavingsAccount
    if target_object and isinstance(target_object, SavingsAccount):
        acct = SavingsAccount.objects.select_for_update().filter(id=target_object.id, user=user, is_active=True).first()
        if not acct:
            acct = SavingsAccount.objects.select_for_update().get(id=get_default_savings_account(user).id)
    else:
        acct = SavingsAccount.objects.select_for_update().get(id=get_default_savings_account(user).id)

    # ✅ idempotency: if we already recorded this WD ref, don't do again
    wd_ref = (withdrawal_ref or "").strip() or f"WD_UNKNOWN_TX#{tx_id}"
    if SavingsTransaction.objects.filter(account=acct, txn_type="WITHDRAWAL", reference=wd_ref).exists():
        return acct

    # Re-check available balance (balance can change after approval)
    if not acct.is_active:
        raise ValidationError("Savings account is inactive.")
    if not acct.can_withdraw_now():
        raise ValidationError("This savings account is locked.")
    if amt > acct.available_balance:
        raise ValidationError("Insufficient available balance (some funds may be reserved).")

    acct.balance = q2(Decimal(acct.balance) - amt)
    acct.full_clean()
    acct.save(update_fields=["balance"])

    SavingsTransaction.objects.create(
        account=acct,
        txn_type="WITHDRAWAL",
        amount=amt,
        reference=wd_ref,
        note=f"MPesa withdrawal paid (mpesa_tx={tx_id})",
        created_at=timezone.now(),
    )

    return acct