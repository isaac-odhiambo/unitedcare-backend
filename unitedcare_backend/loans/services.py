# loans/services.py (COMPLETE + UPDATED)
# -------------------------------------
# ✅ Updated to match your NEW Merry models (Seat + Slot dues):
#   - Replaced old MerryContribution usage with MerryContributionDue (paid_amount allocations)
#   - Updated payout aggregation to use seat-based payouts (MerryPayout.seat -> member)
# ✅ Adds MPESA repayment hook for centralized payments app:
#   - apply_mpesa_repayment(...) called by payments/services.py on STK SUCCESS (LOAN_REPAYMENT)
#   - Idempotent (prevents double-applying the same MpesaTransaction)

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, List, Dict, Tuple

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from loans.models import (
    Loan,
    LoanGuarantor,
    LoanInstallment,
    LoanPayment,
    LoanProduct,
    MerryCreditHold,
)

# ✅ UPDATED IMPORTS (MerryContribution removed)
from merry.models import (
    MerryMember,
    MerryContributionDue,
    MerryPayout,
)

from groups.models import GroupMembership
from savings.models import SavingsAccount, SavingsTransaction

# ==========================================================
# ✅ ADJUST HERE (POLICY)
# ==========================================================
MONEY_QUANT = Decimal("0.01")

# Eligibility cap (requesting stage)
LOAN_MULTIPLIER = Decimal("3.0")
REQUIRED_CONSECUTIVE_MONTHS = 3

# Approval-time security requirement:
# 1.00 = must secure 100% of principal before approval
# 1.10 = must secure 110% (more conservative)
SECURITY_COVERAGE_RATIO = Decimal("1.00")

# Borrower reserves from PERSONAL SAVINGS first:
# E.g 0.30 means target is 30% of principal (capped by available_balance)
BORROWER_SAVINGS_RESERVE_RATIO = Decimal("0.30")

# Use Merry credit (paid contributions not yet received) as additional security?
ALLOW_MERRY_CREDIT_SECURITY = True

# Only use Merry credit if the loan itself is a Merry loan
# (recommended safest default)
MERRY_CREDIT_ONLY_IF_SAME_CONTEXT = True

# Split guarantor shares weighted by their available savings (recommended)
WEIGHTED_GUARANTOR_SPLIT = True


def q2(x: Decimal) -> Decimal:
    return Decimal(x).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


# -------------------------
# Context
# -------------------------

@dataclass(frozen=True)
class LoanContext:
    merry_id: Optional[int] = None
    group_id: Optional[int] = None

    def validate(self) -> None:
        if bool(self.merry_id) == bool(self.group_id):
            raise ValidationError("Provide either merry_id or group_id (not both).")


def ensure_membership(user, ctx: LoanContext) -> None:
    ctx.validate()

    if ctx.merry_id:
        if not MerryMember.objects.filter(merry_id=ctx.merry_id, user_id=user.id, is_active=True).exists():
            raise ValidationError("You must join this Merry before requesting a loan.")
    else:
        if not GroupMembership.objects.filter(group_id=ctx.group_id, user_id=user.id, is_active=True).exists():
            raise ValidationError("You must be an active member of this Group before requesting a loan.")


# -------------------------
# Personal Savings Selection
# -------------------------

def get_primary_savings_account(user) -> SavingsAccount:
    """
    Primary account used for loan qualification and reserving funds.
    Policy: use the earliest active FLEXIBLE account.
    """
    acct = (
        SavingsAccount.objects.filter(user=user, is_active=True, account_type="FLEXIBLE")
        .order_by("id")
        .first()
    )
    if not acct:
        raise ValidationError("You need an active FLEXIBLE savings account to request a loan.")
    return acct


# -------------------------
# 3 Consecutive Months Deposit Check
# -------------------------

def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month_start(d: date) -> date:
    if d.month == 12:
        return d.replace(year=d.year + 1, month=1, day=1)
    return d.replace(month=d.month + 1, day=1)


def _prev_month_start(d: date) -> date:
    if d.month == 1:
        return d.replace(year=d.year - 1, month=12, day=1)
    return d.replace(month=d.month - 1, day=1)


def _has_deposit_in_month(account: SavingsAccount, month_start: date) -> bool:
    month_end = _next_month_start(month_start)
    return SavingsTransaction.objects.filter(
        account=account,
        txn_type="DEPOSIT",
        created_at__date__gte=month_start,
        created_at__date__lt=month_end,
    ).exists()


def require_three_consecutive_months_saving(account: SavingsAccount) -> None:
    today = timezone.now().date()
    m0 = _month_start(today)
    m1 = _prev_month_start(m0)
    m2 = _prev_month_start(m1)

    missing = []
    if not _has_deposit_in_month(account, m2):
        missing.append(m2.strftime("%Y-%m"))
    if not _has_deposit_in_month(account, m1):
        missing.append(m1.strftime("%Y-%m"))
    if not _has_deposit_in_month(account, m0):
        missing.append(m0.strftime("%Y-%m"))

    if missing:
        raise ValidationError(
            f"Loan requires deposits for {REQUIRED_CONSECUTIVE_MONTHS} consecutive months. "
            f"Missing deposit month(s): {', '.join(missing)}."
        )


# -------------------------
# Eligibility (request stage)
# -------------------------

def borrower_has_active_loan(user) -> bool:
    """
    Prevent multiple concurrent loans for the same borrower (safer for reserves/release logic).
    """
    return Loan.objects.filter(
        borrower=user,
        status__in=["APPROVED", "DEFAULTED", "UNDER_REVIEW", "PENDING"],
        outstanding_balance__gt=0,
    ).exists()


def validate_loan_eligibility(*, user, ctx: LoanContext, principal: Decimal) -> dict:
    ensure_membership(user, ctx)

    if Decimal(principal) <= 0:
        raise ValidationError("Principal must be greater than 0.")

    if borrower_has_active_loan(user):
        raise ValidationError("You already have an active loan. Clear it before requesting another loan.")

    account = get_primary_savings_account(user)

    if account.available_balance <= 0:
        raise ValidationError("Loan requires a positive available savings balance.")

    require_three_consecutive_months_saving(account)

    max_allowed = q2(account.available_balance * LOAN_MULTIPLIER)
    if Decimal(principal) > max_allowed:
        raise ValidationError(f"Loan limit exceeded. Max allowed is {max_allowed} (3× your available savings).")

    return {"account": account, "max_allowed": max_allowed}


# -------------------------
# Interest + Totals
# -------------------------

def compute_total_payable(*, principal: Decimal, term_weeks: int, product: LoanProduct) -> Decimal:
    principal = Decimal(principal)
    annual_rate = Decimal(product.annual_interest_rate) / Decimal("100.0")

    if term_weeks <= 0:
        raise ValidationError("term_weeks must be greater than 0.")

    if product.interest_type == "FLAT":
        interest = principal * annual_rate * (Decimal(term_weeks) / Decimal("52"))
        return q2(principal + interest)

    if product.interest_type == "REDUCING":
        weekly_rate = annual_rate / Decimal("52")
        weekly_principal = principal / Decimal(term_weeks)

        total_interest = Decimal("0.00")
        balance = principal
        for _ in range(term_weeks):
            total_interest += balance * weekly_rate
            balance -= weekly_principal

        return q2(principal + total_interest)

    raise ValidationError("Unsupported interest type.")


# -------------------------
# Weekly Schedule Generation
# -------------------------

def next_weekday(d: date, weekday: int) -> date:
    if weekday < 0 or weekday > 6:
        raise ValidationError("Invalid weekday. Must be 0..6 (Mon..Sun).")
    return d + timedelta(days=(weekday - d.weekday()) % 7)


@transaction.atomic
def generate_weekly_installments(loan: Loan) -> List[LoanInstallment]:
    if loan.product.repayment_frequency != "WEEKLY":
        raise ValidationError("Only WEEKLY repayment schedule is supported.")

    LoanInstallment.objects.filter(loan=loan).delete()

    term_weeks = int(loan.term_weeks)
    if term_weeks <= 0:
        raise ValidationError("term_weeks must be > 0.")

    total_payable = Decimal(loan.total_payable)
    if total_payable <= 0:
        raise ValidationError("Loan total_payable must be set before generating schedule.")

    start_date = timezone.now().date()
    first_due = next_weekday(start_date, int(loan.product.repayment_weekday))

    weekly_due = q2(total_payable / Decimal(term_weeks))

    running = Decimal("0.00")
    rows: List[LoanInstallment] = []

    for i in range(1, term_weeks + 1):
        due_date = first_due + timedelta(days=7 * (i - 1))
        if i < term_weeks:
            total_due = weekly_due
        else:
            total_due = q2(total_payable - running)  # rounding fix
        running += total_due

        rows.append(
            LoanInstallment(
                loan=loan,
                installment_no=i,
                due_date=due_date,
                principal_due=Decimal("0.00"),
                interest_due=Decimal("0.00"),
                total_due=total_due,
                late_fee=Decimal("0.00"),
                paid_amount=Decimal("0.00"),
                is_paid=False,
            )
        )

    LoanInstallment.objects.bulk_create(rows)
    return list(LoanInstallment.objects.filter(loan=loan).order_by("installment_no"))


# ==========================================================
# ✅ Merry Credit Security (UPDATED for your NEW Merry models)
# ==========================================================

def get_available_merry_credit(*, user, merry_id: int) -> Decimal:
    """
    ✅ NEW MODEL LOGIC

    available credit = (total allocated into dues for this member)
                     - (total PAID payouts for this member's seats)
                     - (total active holds)

    Notes:
    - Contributions are represented by MerryContributionDue.paid_amount (allocated confirmed payments).
    - Payouts are seat-based: MerryPayout.seat -> seat.member.
    """
    member = MerryMember.objects.filter(
        merry_id=merry_id, user_id=user.id, is_active=True
    ).first()
    if not member:
        raise ValidationError("You must be a member of this Merry to use Merry credit as security.")

    # Total "contributed" = total allocated money into dues for ALL seats of this member (all periods)
    contrib_total = (
        MerryContributionDue.objects.filter(seat__member=member, seat__is_active=True)
        .aggregate(total=Sum("paid_amount"))
        .get("total")
        or Decimal("0.00")
    )

    # Total payouts already received = sum of PAID payouts for this member's seats
    payout_total = (
        MerryPayout.objects.filter(seat__member=member, status="PAID")
        .aggregate(total=Sum("amount"))
        .get("total")
        or Decimal("0.00")
    )

    # Existing credit holds for active loans
    held_total = (
        MerryCreditHold.objects.filter(user_id=user.id, merry_id=merry_id, is_active=True)
        .aggregate(total=Sum("amount"))
        .get("total")
        or Decimal("0.00")
    )

    available = q2(Decimal(contrib_total) - Decimal(payout_total) - Decimal(held_total))
    return max(Decimal("0.00"), available)


@transaction.atomic
def hold_merry_credit_for_loan(*, loan: Loan, merry_id: int, amount: Decimal) -> None:
    """
    Create/update a hold record so payout logic can block cashout while loan active.
    """
    amount = q2(Decimal(amount))
    if amount <= 0:
        return

    hold, _ = MerryCreditHold.objects.select_for_update().get_or_create(
        loan=loan,
        defaults={"merry_id": merry_id, "user": loan.borrower, "amount": Decimal("0.00"), "is_active": True},
    )
    hold.merry_id = merry_id
    hold.user = loan.borrower
    hold.amount = q2(Decimal(hold.amount) + amount)
    hold.is_active = True
    hold.save(update_fields=["merry_id", "user", "amount", "is_active"])


@transaction.atomic
def release_merry_credit_for_loan(*, loan: Loan) -> None:
    hold = MerryCreditHold.objects.select_for_update().filter(loan=loan, is_active=True).first()
    if not hold:
        return
    hold.is_active = False
    hold.released_at = timezone.now()
    hold.save(update_fields=["is_active", "released_at"])


# ==========================================================
# Coverage-Based Reserve / Release (Best Practice)
# ==========================================================

def _security_target(principal: Decimal) -> Decimal:
    return q2(Decimal(principal) * SECURITY_COVERAGE_RATIO)


def _borrower_savings_target(principal: Decimal) -> Decimal:
    return q2(Decimal(principal) * BORROWER_SAVINGS_RESERVE_RATIO)


def _weighted_split(total: Decimal, weights: List[Decimal]) -> List[Decimal]:
    """
    Split 'total' across weights, preserving cents and making sums match exactly.
    """
    total = q2(total)
    if total <= 0:
        return [Decimal("0.00") for _ in weights]

    wsum = sum([Decimal(w) for w in weights], Decimal("0.00"))
    if wsum <= 0:
        n = len(weights)
        if n == 0:
            return []
        base = q2(total / Decimal(n))
        parts = [base] * n
        parts[-1] = q2(total - sum(parts[:-1], Decimal("0.00")))
        return parts

    parts = []
    running = Decimal("0.00")
    for i, w in enumerate(weights):
        if i < len(weights) - 1:
            p = q2(total * (Decimal(w) / wsum))
            parts.append(p)
            running += p
        else:
            parts.append(q2(total - running))
    return parts


@transaction.atomic
def reserve_security_for_loan(loan: Loan) -> Dict[str, Decimal]:
    """
    Approval-time security reserve:
    1) security_target = SECURITY_COVERAGE_RATIO × principal
    2) Reserve borrower savings up to BORROWER_SAVINGS_RESERVE_RATIO × principal (capped by available)
    3) Optionally hold Merry credit
    4) Remaining gap covered by 1+ accepted guarantors
    5) Store exact allocations for audit + accurate release
    """
    principal = q2(Decimal(loan.principal))
    if principal <= 0:
        raise ValidationError("Loan principal must be > 0.")

    # Reset stored allocations (safe in same atomic flow)
    loan.borrower_reserved_savings = Decimal("0.00")
    loan.borrower_reserved_merry_credit = Decimal("0.00")
    loan.security_target = _security_target(principal)
    loan.save(update_fields=["borrower_reserved_savings", "borrower_reserved_merry_credit", "security_target"])

    target = Decimal(loan.security_target)

    # --- Borrower savings reserve ---
    borrower_acct = get_primary_savings_account(loan.borrower)
    borrower_acct = SavingsAccount.objects.select_for_update().get(id=borrower_acct.id)

    borrower_target = min(_borrower_savings_target(principal), q2(borrower_acct.available_balance))
    borrower_target = q2(borrower_target)

    if borrower_target > 0:
        borrower_acct.reserved_amount = q2(Decimal(borrower_acct.reserved_amount) + borrower_target)
        borrower_acct.full_clean()
        borrower_acct.save(update_fields=["reserved_amount"])
        loan.borrower_reserved_savings = borrower_target
        loan.save(update_fields=["borrower_reserved_savings"])

    covered = q2(loan.borrower_reserved_savings)

    # --- Merry credit hold (optional) ---
    if ALLOW_MERRY_CREDIT_SECURITY:
        if loan.merry_id:
            if (not MERRY_CREDIT_ONLY_IF_SAME_CONTEXT) or (loan.merry_id is not None):
                available_credit = q2(get_available_merry_credit(user=loan.borrower, merry_id=int(loan.merry_id)))
                remaining_need = q2(target - covered)
                use_credit = q2(min(available_credit, remaining_need))

                if use_credit > 0:
                    hold_merry_credit_for_loan(loan=loan, merry_id=int(loan.merry_id), amount=use_credit)
                    loan.borrower_reserved_merry_credit = use_credit
                    loan.save(update_fields=["borrower_reserved_merry_credit"])
                    covered = q2(covered + use_credit)

    # --- Guarantors cover the remainder ---
    remaining_need = q2(target - covered)
    if remaining_need <= 0:
        return {
            "security_target": target,
            "borrower_reserved_savings": loan.borrower_reserved_savings,
            "borrower_reserved_merry_credit": loan.borrower_reserved_merry_credit,
            "guarantors_reserved_total": Decimal("0.00"),
            "covered_total": target,
        }

    accepted_qs = (
        LoanGuarantor.objects.select_related("guarantor")
        .select_for_update()
        .filter(loan=loan, accepted=True)
    )
    accepted = list(accepted_qs)
    if not accepted:
        raise ValidationError("At least one guarantor must accept before approval.")

    guarantor_accounts: List[Tuple[LoanGuarantor, SavingsAccount, Decimal]] = []
    for g in accepted:
        g_acct = get_primary_savings_account(g.guarantor)
        g_acct = SavingsAccount.objects.select_for_update().get(id=g_acct.id)
        cap = q2(g_acct.available_balance)
        guarantor_accounts.append((g, g_acct, cap))

    total_capacity = sum([cap for _, _, cap in guarantor_accounts], Decimal("0.00"))
    if total_capacity <= 0:
        raise ValidationError("Accepted guarantors have no available savings to secure this loan.")

    if WEIGHTED_GUARANTOR_SPLIT:
        weights = [cap for _, _, cap in guarantor_accounts]
        planned = _weighted_split(remaining_need, weights)
    else:
        planned = _weighted_split(remaining_need, [Decimal("1.0")] * len(guarantor_accounts))

    guarantors_reserved_total = Decimal("0.00")

    for (g, g_acct, cap), share in zip(guarantor_accounts, planned):
        use = q2(min(share, cap))
        if use <= 0:
            continue

        g_acct.reserved_amount = q2(Decimal(g_acct.reserved_amount) + use)
        g_acct.full_clean()
        g_acct.save(update_fields=["reserved_amount"])

        g.reserved_amount = q2(Decimal(g.reserved_amount) + use)
        g.save(update_fields=["reserved_amount"])

        guarantors_reserved_total = q2(guarantors_reserved_total + use)

    covered = q2(covered + guarantors_reserved_total)

    if covered < target:
        short = q2(target - covered)
        raise ValidationError(
            f"Insufficient security coverage. Need additional {short}. "
            f"Add guarantor(s), increase savings, or reduce the loan amount."
        )

    return {
        "security_target": target,
        "borrower_reserved_savings": loan.borrower_reserved_savings,
        "borrower_reserved_merry_credit": loan.borrower_reserved_merry_credit,
        "guarantors_reserved_total": guarantors_reserved_total,
        "covered_total": covered,
    }


@transaction.atomic
def release_reserved_security_for_loan(loan: Loan) -> None:
    """
    Releases EXACT reserved amounts (best practice).
    """
    # borrower savings
    borrower_acct = get_primary_savings_account(loan.borrower)
    borrower_acct = SavingsAccount.objects.select_for_update().get(id=borrower_acct.id)

    bs = q2(Decimal(loan.borrower_reserved_savings or Decimal("0.00")))
    if bs > 0:
        borrower_acct.reserved_amount = q2(max(Decimal("0.00"), Decimal(borrower_acct.reserved_amount) - bs))
        borrower_acct.save(update_fields=["reserved_amount"])

    # guarantors
    accepted = (
        LoanGuarantor.objects.select_related("guarantor")
        .select_for_update()
        .filter(loan=loan, accepted=True)
    )
    for g in accepted:
        amt = q2(Decimal(g.reserved_amount or Decimal("0.00")))
        if amt <= 0:
            continue

        g_acct = get_primary_savings_account(g.guarantor)
        g_acct = SavingsAccount.objects.select_for_update().get(id=g_acct.id)
        g_acct.reserved_amount = q2(max(Decimal("0.00"), Decimal(g_acct.reserved_amount) - amt))
        g_acct.save(update_fields=["reserved_amount"])

        g.reserved_amount = Decimal("0.00")
        g.save(update_fields=["reserved_amount"])

    # merry hold
    if Decimal(loan.borrower_reserved_merry_credit or Decimal("0.00")) > 0:
        release_merry_credit_for_loan(loan=loan)

    # clear loan allocations
    loan.borrower_reserved_savings = Decimal("0.00")
    loan.borrower_reserved_merry_credit = Decimal("0.00")
    loan.save(update_fields=["borrower_reserved_savings", "borrower_reserved_merry_credit"])


# -------------------------
# Approve Loan
# -------------------------

@transaction.atomic
def approve_loan_and_create_schedule(loan: Loan) -> Loan:
    """
    Approval workflow:
    - require accepted guarantor(s)
    - re-check eligibility
    - compute totals
    - reserve security (borrower savings + optional merry credit + guarantors)
    - generate weekly installments
    """
    if loan.status not in ("PENDING", "UNDER_REVIEW"):
        raise ValidationError("Only pending/review loans can be approved.")

    if not LoanGuarantor.objects.filter(loan=loan, accepted=True).exists():
        raise ValidationError("At least one guarantor must accept before approval.")

    ctx = LoanContext(merry_id=loan.merry_id, group_id=loan.group_id)
    validate_loan_eligibility(user=loan.borrower, ctx=ctx, principal=loan.principal)

    loan.total_payable = compute_total_payable(
        principal=loan.principal,
        term_weeks=loan.term_weeks,
        product=loan.product,
    )
    loan.total_paid = loan.total_paid or Decimal("0.00")
    loan.outstanding_balance = q2(Decimal(loan.total_payable) - Decimal(loan.total_paid))

    loan.status = "APPROVED"
    loan.approved_at = timezone.now()
    loan.save()

    reserve_security_for_loan(loan)
    generate_weekly_installments(loan)
    return loan


# -------------------------
# Payments (manual + MPESA hook)
# -------------------------

@transaction.atomic
def record_loan_payment(
    loan: Loan,
    amount: Decimal,
    method: str = "MANUAL",
    reference: Optional[str] = None,
) -> LoanPayment:
    amount = q2(Decimal(amount))
    if amount <= 0:
        raise ValidationError("Payment amount must be greater than 0.")
    if loan.status not in ("APPROVED", "DEFAULTED"):
        raise ValidationError("You can only pay an approved/defaulted loan.")

    return LoanPayment.objects.create(loan=loan, amount=amount, method=method, reference=reference)


@transaction.atomic
def apply_payment_to_loan(loan: Loan, amount: Decimal) -> Loan:
    amount = q2(Decimal(amount))
    if amount <= 0:
        raise ValidationError("Payment amount must be greater than 0.")
    if loan.status not in ("APPROVED", "DEFAULTED"):
        raise ValidationError("Payments can only be applied to approved/defaulted loans.")

    remaining = amount
    installments = LoanInstallment.objects.select_for_update().filter(loan=loan).order_by("installment_no")

    for inst in installments:
        if remaining <= 0:
            break
        if inst.is_paid:
            continue

        due = q2(Decimal(inst.total_due) + Decimal(inst.late_fee) - Decimal(inst.paid_amount))
        if due <= 0:
            inst.is_paid = True
            inst.save(update_fields=["is_paid"])
            continue

        pay = due if remaining >= due else remaining
        inst.paid_amount = q2(Decimal(inst.paid_amount) + pay)
        remaining = q2(remaining - pay)

        new_due = q2(Decimal(inst.total_due) + Decimal(inst.late_fee) - Decimal(inst.paid_amount))
        if new_due <= 0:
            inst.is_paid = True

        inst.save(update_fields=["paid_amount", "is_paid"])

    loan.total_paid = q2(Decimal(loan.total_paid) + amount)
    loan.recompute_balances()
    loan.save(update_fields=["total_paid", "outstanding_balance", "status"])

    if loan.status == "COMPLETED":
        release_reserved_security_for_loan(loan)

    return loan


@transaction.atomic
def apply_mpesa_repayment(*, loan_id: int, amount: Decimal, mpesa_tx) -> Loan:
    """
    ✅ Centralized MPESA repayment hook used by payments/services.py:

    Called after STK SUCCESS for purpose=LOAN_REPAYMENT.

    - Idempotent: prevents applying the same MpesaTransaction twice
    - Creates LoanPayment(method="MPESA") linked by reference MPESA_TX#<id>
    - Applies the amount to installments + updates balances
    - Releases reserved security if loan completes

    Note:
      We accept mpesa_tx as an object to avoid hard import cycles.
      (payments.models.MpesaTransaction instance)
    """
    loan = Loan.objects.select_for_update().select_related("product", "borrower").filter(id=loan_id).first()
    if not loan:
        raise ValidationError("Loan not found.")

    amt = q2(Decimal(amount))
    if amt <= 0:
        raise ValidationError("Repayment amount must be greater than 0.")

    if loan.status not in ("APPROVED", "DEFAULTED"):
        raise ValidationError("You can only repay an approved/defaulted loan.")

    tx_id = getattr(mpesa_tx, "id", None)
    if not tx_id:
        raise ValidationError("Invalid mpesa_tx supplied (missing id).")

    ref = f"MPESA_TX#{tx_id}"

    # ✅ Idempotency guard
    if LoanPayment.objects.filter(loan=loan, method="MPESA", reference=ref).exists():
        return loan

    # Record payment row (audit)
    LoanPayment.objects.create(
        loan=loan,
        amount=amt,
        method="MPESA",
        reference=ref,
    )

    # Apply to schedule
    apply_payment_to_loan(loan, amt)

    return loan


# -------------------------
# Late Fees
# -------------------------

@transaction.atomic
def apply_weekly_late_fees(today: Optional[date] = None) -> int:
    """
    Adds weekly late fee to each overdue installment (once per run).
    """
    if today is None:
        today = timezone.now().date()

    count = 0
    overdue = (
        LoanInstallment.objects.select_for_update()
        .filter(is_paid=False, due_date__lt=today, loan__status__in=["APPROVED", "DEFAULTED"])
        .select_related("loan", "loan__product")
    )

    for inst in overdue:
        loan = inst.loan
        product = loan.product

        rate = Decimal(product.late_fee_rate_weekly) / Decimal("100.0")
        remaining_due = q2(Decimal(inst.total_due) + Decimal(inst.late_fee) - Decimal(inst.paid_amount))

        if remaining_due <= 0:
            inst.is_paid = True
            inst.save(update_fields=["is_paid"])
            continue

        fee = q2(remaining_due * rate)
        if fee > 0:
            inst.late_fee = q2(Decimal(inst.late_fee) + fee)
            inst.save(update_fields=["late_fee"])
            count += 1

            if loan.status == "APPROVED":
                loan.status = "DEFAULTED"
                loan.is_defaulter = True
                loan.save(update_fields=["status", "is_defaulter"])

    return count