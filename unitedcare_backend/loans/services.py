# loans/services.py
# -------------------------------------
# ✅ Matches new Merry models (Seat + Slot dues)
# ✅ MPESA repayment hook supported
# ✅ GROUP share collateral supported (no loan model changes)
# ✅ Backward-compatible with views importing record_loan_payment
# ✅ Approval workflow hardened
# ✅ Overpayment protection added
# ✅ Credit profile updates added

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
    MemberCreditProfile,
)

from merry.models import (
    MerryMember,
    MerryContributionDue,
    MerryPayout,
)

from groups.models import GroupMembership

from groups.services import (
    reserve_group_share_for_loan,
    release_group_share_for_loan,
)

from savings.models import SavingsAccount, SavingsTransaction


# ==========================================================
# POLICY
# ==========================================================
MONEY_QUANT = Decimal("0.01")

LOAN_MULTIPLIER = Decimal("3.0")
REQUIRED_CONSECUTIVE_MONTHS = 3

SECURITY_COVERAGE_RATIO = Decimal("1.00")
BORROWER_SAVINGS_RESERVE_RATIO = Decimal("0.30")

ALLOW_MERRY_CREDIT_SECURITY = True
MERRY_CREDIT_ONLY_IF_SAME_CONTEXT = True

ALLOW_GROUP_SHARE_SECURITY = True
GROUP_SHARE_ONLY_IF_SAME_CONTEXT = True

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
        if not MerryMember.objects.filter(
            merry_id=ctx.merry_id,
            user_id=user.id,
            is_active=True,
        ).exists():
            raise ValidationError("You must join this Merry before requesting a loan.")
    else:
        if not GroupMembership.objects.filter(
            group_id=ctx.group_id,
            user_id=user.id,
            is_active=True,
        ).exists():
            raise ValidationError("You must be an active member of this Group before requesting a loan.")


# -------------------------
# Credit profile helpers
# -------------------------

def get_or_create_credit_profile(*, user, ctx: LoanContext) -> MemberCreditProfile:
    ctx.validate()

    if ctx.merry_id:
        profile, _ = MemberCreditProfile.objects.get_or_create(
            user=user,
            merry_id=ctx.merry_id,
            defaults={"score": 100},
        )
        return profile

    profile, _ = MemberCreditProfile.objects.get_or_create(
        user=user,
        group_id=ctx.group_id,
        defaults={"score": 100},
    )
    return profile


def update_credit_on_approval(loan: Loan) -> None:
    ctx = LoanContext(merry_id=loan.merry_id, group_id=loan.group_id)
    profile = get_or_create_credit_profile(user=loan.borrower, ctx=ctx)
    profile.total_loans = int(profile.total_loans or 0) + 1
    profile.save(update_fields=["total_loans", "updated_at"])


def update_credit_on_completion(loan: Loan) -> None:
    ctx = LoanContext(merry_id=loan.merry_id, group_id=loan.group_id)
    profile = get_or_create_credit_profile(user=loan.borrower, ctx=ctx)
    profile.loans_completed = int(profile.loans_completed or 0) + 1
    profile.score = min(100, int(profile.score or 100) + 3)
    profile.save(update_fields=["loans_completed", "score", "updated_at"])


def update_credit_on_default(loan: Loan) -> None:
    ctx = LoanContext(merry_id=loan.merry_id, group_id=loan.group_id)
    profile = get_or_create_credit_profile(user=loan.borrower, ctx=ctx)
    profile.loans_defaulted = int(profile.loans_defaulted or 0) + 1
    profile.score = max(0, int(profile.score or 100) - 10)
    profile.save(update_fields=["loans_defaulted", "score", "updated_at"])


def update_credit_on_late_payment(loan: Loan) -> None:
    ctx = LoanContext(merry_id=loan.merry_id, group_id=loan.group_id)
    profile = get_or_create_credit_profile(user=loan.borrower, ctx=ctx)
    profile.late_payments = int(profile.late_payments or 0) + 1
    profile.score = max(0, int(profile.score or 100) - 2)
    profile.save(update_fields=["late_payments", "score", "updated_at"])


# -------------------------
# Personal Savings Selection
# -------------------------

def get_primary_savings_account(user) -> SavingsAccount:
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
    return Loan.objects.filter(
        borrower=user,
        status__in=["APPROVED", "DEFAULTED", "UNDER_REVIEW", "PENDING"],
        outstanding_balance__gt=0,
    ).exists()


def validate_loan_eligibility(*, user, ctx: LoanContext, principal: Decimal) -> dict:
    ensure_membership(user, ctx)

    principal = q2(Decimal(principal))
    if principal <= 0:
        raise ValidationError("Principal must be greater than 0.")

    if borrower_has_active_loan(user):
        raise ValidationError("You already have an active loan. Clear it before requesting another loan.")

    account = get_primary_savings_account(user)

    if account.available_balance <= 0:
        raise ValidationError("Loan requires a positive available savings balance.")

    require_three_consecutive_months_saving(account)

    max_allowed = q2(account.available_balance * LOAN_MULTIPLIER)
    if principal > max_allowed:
        raise ValidationError(f"Loan limit exceeded. Max allowed is {max_allowed} (3× your available savings).")

    return {"account": account, "max_allowed": max_allowed}


# -------------------------
# Interest + Totals
# -------------------------

def compute_total_payable(*, principal: Decimal, term_weeks: int, product: LoanProduct) -> Decimal:
    principal = q2(Decimal(principal))
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
        total_due = weekly_due if i < term_weeks else q2(total_payable - running)
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
# Merry Credit Security
# ==========================================================

def get_available_merry_credit(*, user, merry_id: int) -> Decimal:
    member = MerryMember.objects.filter(
        merry_id=merry_id,
        user_id=user.id,
        is_active=True,
    ).first()
    if not member:
        raise ValidationError("You must be a member of this Merry to use Merry credit as security.")

    contrib_total = (
        MerryContributionDue.objects.filter(seat__member=member, seat__is_active=True)
        .aggregate(total=Sum("paid_amount"))
        .get("total")
        or Decimal("0.00")
    )

    payout_total = (
        MerryPayout.objects.filter(seat__member=member, status="PAID")
        .aggregate(total=Sum("amount"))
        .get("total")
        or Decimal("0.00")
    )

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
    amount = q2(Decimal(amount))
    if amount <= 0:
        return

    hold, _ = MerryCreditHold.objects.select_for_update().get_or_create(
        loan=loan,
        defaults={
            "merry_id": merry_id,
            "user": loan.borrower,
            "amount": Decimal("0.00"),
            "is_active": True,
        },
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
# Coverage-Based Reserve / Release
# ==========================================================

def _security_target(principal: Decimal) -> Decimal:
    return q2(Decimal(principal) * SECURITY_COVERAGE_RATIO)


def _borrower_savings_target(principal: Decimal) -> Decimal:
    return q2(Decimal(principal) * BORROWER_SAVINGS_RESERVE_RATIO)


def _weighted_split(total: Decimal, weights: List[Decimal]) -> List[Decimal]:
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
    principal = q2(Decimal(loan.principal))
    if principal <= 0:
        raise ValidationError("Loan principal must be > 0.")

    has_any_guarantor_reserve = LoanGuarantor.objects.filter(loan=loan, reserved_amount__gt=0).exists()
    has_any_merry_hold = MerryCreditHold.objects.filter(loan=loan, is_active=True).exists()

    if (
        Decimal(loan.borrower_reserved_savings or Decimal("0.00")) > 0
        or Decimal(loan.borrower_reserved_merry_credit or Decimal("0.00")) > 0
        or has_any_guarantor_reserve
        or has_any_merry_hold
    ):
        release_reserved_security_for_loan(loan)

    loan.borrower_reserved_savings = Decimal("0.00")
    loan.borrower_reserved_merry_credit = Decimal("0.00")
    loan.security_target = _security_target(principal)
    loan.save(update_fields=["borrower_reserved_savings", "borrower_reserved_merry_credit", "security_target"])

    target = Decimal(loan.security_target)

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

    group_share_reserved = Decimal("0.00")
    if ALLOW_GROUP_SHARE_SECURITY and loan.group_id:
        if (not GROUP_SHARE_ONLY_IF_SAME_CONTEXT) or (loan.group_id is not None):
            remaining_need = q2(target - covered)
            if remaining_need > 0:
                try:
                    hold = reserve_group_share_for_loan(
                        group_id=int(loan.group_id),
                        user=loan.borrower,
                        loan_id=int(loan.id),
                        amount=remaining_need,
                    )
                    group_share_reserved = q2(Decimal(getattr(hold, "amount", Decimal("0.00"))))
                    covered = q2(covered + group_share_reserved)
                except ValidationError:
                    group_share_reserved = Decimal("0.00")

    if ALLOW_MERRY_CREDIT_SECURITY and loan.merry_id:
        if (not MERRY_CREDIT_ONLY_IF_SAME_CONTEXT) or (loan.merry_id is not None):
            available_credit = q2(get_available_merry_credit(user=loan.borrower, merry_id=int(loan.merry_id)))
            remaining_need = q2(target - covered)
            use_credit = q2(min(available_credit, remaining_need))

            if use_credit > 0:
                hold_merry_credit_for_loan(loan=loan, merry_id=int(loan.merry_id), amount=use_credit)
                loan.borrower_reserved_merry_credit = use_credit
                loan.save(update_fields=["borrower_reserved_merry_credit"])
                covered = q2(covered + use_credit)

    remaining_need = q2(target - covered)
    if remaining_need <= 0:
        return {
            "security_target": target,
            "borrower_reserved_savings": loan.borrower_reserved_savings,
            "borrower_reserved_merry_credit": loan.borrower_reserved_merry_credit,
            "borrower_reserved_group_share": group_share_reserved,
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

    weights = [cap for _, _, cap in guarantor_accounts] if WEIGHTED_GUARANTOR_SPLIT else [Decimal("1.0")] * len(guarantor_accounts)
    planned = _weighted_split(remaining_need, weights)

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
            f"Add guarantor(s), increase savings/share, or reduce the loan amount."
        )

    return {
        "security_target": target,
        "borrower_reserved_savings": loan.borrower_reserved_savings,
        "borrower_reserved_merry_credit": loan.borrower_reserved_merry_credit,
        "borrower_reserved_group_share": group_share_reserved,
        "guarantors_reserved_total": guarantors_reserved_total,
        "covered_total": covered,
    }


@transaction.atomic
def release_reserved_security_for_loan(loan: Loan) -> None:
    borrower_acct = get_primary_savings_account(loan.borrower)
    borrower_acct = SavingsAccount.objects.select_for_update().get(id=borrower_acct.id)

    bs = q2(Decimal(loan.borrower_reserved_savings or Decimal("0.00")))
    if bs > 0:
        borrower_acct.reserved_amount = q2(max(Decimal("0.00"), Decimal(borrower_acct.reserved_amount) - bs))
        borrower_acct.save(update_fields=["reserved_amount"])

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

    if loan.group_id:
        release_group_share_for_loan(group_id=int(loan.group_id), loan_id=int(loan.id))

    if Decimal(loan.borrower_reserved_merry_credit or Decimal("0.00")) > 0:
        release_merry_credit_for_loan(loan=loan)

    loan.borrower_reserved_savings = Decimal("0.00")
    loan.borrower_reserved_merry_credit = Decimal("0.00")
    loan.save(update_fields=["borrower_reserved_savings", "borrower_reserved_merry_credit"])


# -------------------------
# Approve Loan
# -------------------------

@transaction.atomic
def approve_loan_and_create_schedule(loan: Loan) -> Loan:
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
    loan.total_paid = q2(loan.total_paid or Decimal("0.00"))
    loan.outstanding_balance = q2(Decimal(loan.total_payable) - Decimal(loan.total_paid))
    loan.save(update_fields=["total_payable", "total_paid", "outstanding_balance"])

    reserve_security_for_loan(loan)
    generate_weekly_installments(loan)

    loan.status = "APPROVED"
    loan.approved_at = timezone.now()
    loan.save(update_fields=["status", "approved_at"])

    update_credit_on_approval(loan)
    return loan


# -------------------------
# Payments
# -------------------------

@transaction.atomic
def create_loan_payment_record(
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

    return LoanPayment.objects.create(
        loan=loan,
        amount=amount,
        method=method,
        reference=reference,
    )


@transaction.atomic
def record_loan_payment(
    loan: Loan,
    amount: Decimal,
    method: str = "MANUAL",
    reference: Optional[str] = None,
) -> LoanPayment:
    """
    Backward-compatible wrapper for existing views.py imports.

    This only creates the LoanPayment row.
    It does NOT apply the payment to installments/balances.
    """
    return create_loan_payment_record(
        loan=loan,
        amount=amount,
        method=method,
        reference=reference,
    )


@transaction.atomic
def apply_payment_to_loan(loan: Loan, amount: Decimal) -> Loan:
    amount = q2(Decimal(amount))
    if amount <= 0:
        raise ValidationError("Payment amount must be greater than 0.")
    if loan.status not in ("APPROVED", "DEFAULTED"):
        raise ValidationError("Payments can only be applied to approved/defaulted loans.")

    current_outstanding = q2(Decimal(loan.outstanding_balance or Decimal("0.00")))
    if current_outstanding <= 0:
        raise ValidationError("This loan has no outstanding balance.")

    amount_to_apply = q2(min(amount, current_outstanding))
    remaining = amount_to_apply

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

    previous_status = loan.status
    loan.total_paid = q2(Decimal(loan.total_paid) + amount_to_apply)
    loan.recompute_balances()
    loan.save(update_fields=["total_paid", "outstanding_balance", "status"])

    if loan.status == "COMPLETED":
        release_reserved_security_for_loan(loan)
        if previous_status != "COMPLETED":
            update_credit_on_completion(loan)

    return loan


@transaction.atomic
def record_and_apply_loan_payment(
    loan: Loan,
    amount: Decimal,
    method: str = "MANUAL",
    reference: Optional[str] = None,
) -> Loan:
    amount = q2(Decimal(amount))
    current_outstanding = q2(Decimal(loan.outstanding_balance or Decimal("0.00")))
    if current_outstanding <= 0:
        raise ValidationError("This loan has no outstanding balance.")

    applied_amount = q2(min(amount, current_outstanding))
    create_loan_payment_record(
        loan=loan,
        amount=applied_amount,
        method=method,
        reference=reference,
    )
    return apply_payment_to_loan(loan, applied_amount)


@transaction.atomic
def apply_mpesa_repayment(*, loan_id: int, amount: Decimal, mpesa_tx) -> Loan:
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

    if LoanPayment.objects.filter(loan=loan, method="MPESA", reference=ref).exists():
        return loan

    outstanding = q2(Decimal(loan.outstanding_balance or Decimal("0.00")))
    if outstanding <= 0:
        raise ValidationError("This loan has no outstanding balance.")

    amt_to_apply = q2(min(amt, outstanding))

    LoanPayment.objects.create(
        loan=loan,
        amount=amt_to_apply,
        method="MPESA",
        reference=ref,
    )

    apply_payment_to_loan(loan, amt_to_apply)
    return loan


# -------------------------
# Late Fees
# -------------------------

@transaction.atomic
def apply_weekly_late_fees(today: Optional[date] = None) -> int:
    """
    This assumes the scheduled task runs weekly.
    For perfect enforcement independent of scheduler frequency,
    add last_late_fee_applied_at to LoanInstallment.
    """
    if today is None:
        today = timezone.now().date()

    count = 0
    touched_loans = set()

    overdue = (
        LoanInstallment.objects.select_for_update()
        .filter(
            is_paid=False,
            due_date__lt=today,
            loan__status__in=["APPROVED", "DEFAULTED"],
        )
        .select_related("loan", "loan__product")
    )

    for inst in overdue:
        loan = inst.loan
        product = loan.product

        overdue_days = (today - inst.due_date).days
        if overdue_days < 7:
            continue

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
            touched_loans.add(loan.id)

            if loan.status == "APPROVED":
                loan.status = "DEFAULTED"
                loan.is_defaulter = True
                loan.save(update_fields=["status", "is_defaulter"])

    for loan_id in touched_loans:
        loan = Loan.objects.filter(id=loan_id).first()
        if loan:
            update_credit_on_late_payment(loan)
            if loan.status == "DEFAULTED":
                update_credit_on_default(loan)

    return count