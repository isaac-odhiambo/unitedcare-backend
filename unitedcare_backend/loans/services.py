from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, List, Optional, Sequence

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
    LoanSecurityAllocation,
    MemberCreditProfile,
)
from merry.models import MerryContributionDue, MerryMember, MerryPayout
from savings.models import SavingsAccount, SavingsTransaction


# ==========================================================
# POLICY
# ==========================================================
MONEY_QUANT = Decimal("0.01")

REQUIRED_CONSECUTIVE_MONTHS = 3

# Borrowing policy
LOAN_MULTIPLIER = Decimal("3.0")  # max principal = 3 x available personal savings
SECURITY_COVERAGE_RATIO = Decimal("1.00")  # require 100% coverage
BORROWER_SAVINGS_RESERVE_RATIO = Decimal("0.30")  # reserve 30% of principal from own savings first

# Guarantor policy
MIN_GUARANTORS_FOR_APPROVAL = 1
GUARANTOR_MAX_EXPOSURE_RATIO = Decimal("0.70")  # up to 70% of guarantor available savings can support loans

# Security source toggles
ALLOW_BORROWER_SAVINGS_SECURITY = True
ALLOW_BORROWER_MERRY_CREDIT_SECURITY = True
ALLOW_BORROWER_GROUP_SHARE_SECURITY = True
ALLOW_GUARANTOR_SAVINGS_SECURITY = True

# Order of security application:
# 1) borrower savings
# 2) borrower merry credit
# 3) borrower group share (adapter)
# 4) guarantor savings


# ==========================================================
# Utils
# ==========================================================
def q2(x: Decimal | str | int | float) -> Decimal:
    return Decimal(x).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


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


def _security_target(principal: Decimal) -> Decimal:
    return q2(Decimal(principal) * SECURITY_COVERAGE_RATIO)


def _borrower_savings_target(principal: Decimal) -> Decimal:
    return q2(Decimal(principal) * BORROWER_SAVINGS_RESERVE_RATIO)


def next_weekday(d: date, weekday: int) -> date:
    if weekday < 0 or weekday > 6:
        raise ValidationError("Invalid weekday. Must be 0..6 (Mon..Sun).")
    return d + timedelta(days=(weekday - d.weekday()) % 7)


# ==========================================================
# Data Shapes
# ==========================================================
@dataclass(frozen=True)
class EligibilityPreview:
    eligible: bool
    max_allowed: Decimal
    available_savings: Decimal
    has_active_loan: bool
    missing_deposit_months: List[str]
    reason: str = ""


# ==========================================================
# Credit Profile
# ==========================================================
def get_or_create_credit_profile(*, user) -> MemberCreditProfile:
    profile, _ = MemberCreditProfile.objects.get_or_create(
        user=user,
        defaults={"score": 100},
    )
    return profile


def update_credit_on_approval(loan: Loan) -> None:
    profile = get_or_create_credit_profile(user=loan.borrower)
    profile.total_loans = int(profile.total_loans or 0) + 1
    profile.save(update_fields=["total_loans", "updated_at"])


def update_credit_on_completion(loan: Loan) -> None:
    profile = get_or_create_credit_profile(user=loan.borrower)
    profile.loans_completed = int(profile.loans_completed or 0) + 1
    profile.score = min(100, int(profile.score or 100) + 3)
    profile.save(update_fields=["loans_completed", "score", "updated_at"])


def update_credit_on_default(loan: Loan) -> None:
    profile = get_or_create_credit_profile(user=loan.borrower)
    profile.loans_defaulted = int(profile.loans_defaulted or 0) + 1
    profile.score = max(0, int(profile.score or 100) - 10)
    profile.save(update_fields=["loans_defaulted", "score", "updated_at"])


def update_credit_on_late_payment(loan: Loan) -> None:
    profile = get_or_create_credit_profile(user=loan.borrower)
    profile.late_payments = int(profile.late_payments or 0) + 1
    profile.score = max(0, int(profile.score or 100) - 2)
    profile.save(update_fields=["late_payments", "score", "updated_at"])


# ==========================================================
# Product
# ==========================================================
def get_default_loan_product() -> LoanProduct:
    product = (
        LoanProduct.objects.filter(is_active=True, is_default=True)
        .order_by("id")
        .first()
    )
    if product:
        return product

    product = LoanProduct.objects.filter(is_active=True).order_by("id").first()
    if not product:
        raise ValidationError("No active loan product is configured.")
    return product


# ==========================================================
# Personal Savings
# ==========================================================
def get_primary_savings_account(user) -> SavingsAccount:
    acct = (
        SavingsAccount.objects.filter(user=user, is_active=True, account_type="FLEXIBLE")
        .order_by("id")
        .first()
    )
    if not acct:
        raise ValidationError("You need an active FLEXIBLE savings account to request a loan.")
    return acct


def _has_deposit_in_month(account: SavingsAccount, month_start: date) -> bool:
    month_end = _next_month_start(month_start)
    return SavingsTransaction.objects.filter(
        account=account,
        txn_type="DEPOSIT",
        created_at__date__gte=month_start,
        created_at__date__lt=month_end,
    ).exists()


def get_missing_consecutive_deposit_months(account: SavingsAccount) -> List[str]:
    today = timezone.now().date()
    m0 = _month_start(today)
    m1 = _prev_month_start(m0)
    m2 = _prev_month_start(m1)

    missing: List[str] = []
    if not _has_deposit_in_month(account, m2):
        missing.append(m2.strftime("%Y-%m"))
    if not _has_deposit_in_month(account, m1):
        missing.append(m1.strftime("%Y-%m"))
    if not _has_deposit_in_month(account, m0):
        missing.append(m0.strftime("%Y-%m"))
    return missing


def require_three_consecutive_months_saving(account: SavingsAccount) -> None:
    missing = get_missing_consecutive_deposit_months(account)
    if missing:
        raise ValidationError(
            f"Loan requires deposits for {REQUIRED_CONSECUTIVE_MONTHS} consecutive months. "
            f"Missing deposit month(s): {', '.join(missing)}."
        )


# ==========================================================
# Borrower Eligibility
# ==========================================================
def borrower_has_active_loan(user) -> bool:
    return Loan.objects.filter(
        borrower=user,
        status__in=["PENDING", "UNDER_REVIEW", "APPROVED", "DEFAULTED"],
        outstanding_balance__gt=0,
    ).exists()


def validate_platform_loan_eligibility(*, user, principal: Decimal) -> dict:
    principal = q2(principal)
    if principal <= 0:
        raise ValidationError("Principal must be greater than 0.")

    if borrower_has_active_loan(user):
        raise ValidationError("You already have an active loan. Clear it before requesting another loan.")

    account = get_primary_savings_account(user)

    available_savings = q2(getattr(account, "available_balance", Decimal("0.00")))
    if available_savings <= 0:
        raise ValidationError("Loan requires a positive available savings balance.")

    require_three_consecutive_months_saving(account)

    max_allowed = q2(available_savings * LOAN_MULTIPLIER)
    if principal > max_allowed:
        raise ValidationError(f"Loan limit exceeded. Max allowed is {max_allowed} (3× your available savings).")

    return {
        "account": account,
        "available_savings": available_savings,
        "max_allowed": max_allowed,
    }


def get_loan_eligibility_preview(*, user) -> EligibilityPreview:
    try:
        account = get_primary_savings_account(user)
    except ValidationError as e:
        return EligibilityPreview(
            eligible=False,
            max_allowed=Decimal("0.00"),
            available_savings=Decimal("0.00"),
            has_active_loan=False,
            missing_deposit_months=[],
            reason=str(e.detail[0]) if hasattr(e, "detail") else str(e),
        )

    available_savings = q2(getattr(account, "available_balance", Decimal("0.00")))
    missing = get_missing_consecutive_deposit_months(account)
    active_loan = borrower_has_active_loan(user)
    max_allowed = q2(max(Decimal("0.00"), available_savings * LOAN_MULTIPLIER))

    reason = ""
    eligible = True

    if available_savings <= 0:
        eligible = False
        reason = "Loan requires a positive available savings balance."
    elif missing:
        eligible = False
        reason = f"Missing deposit month(s): {', '.join(missing)}."
    elif active_loan:
        eligible = False
        reason = "You already have an active loan."

    return EligibilityPreview(
        eligible=eligible,
        max_allowed=max_allowed,
        available_savings=available_savings,
        has_active_loan=active_loan,
        missing_deposit_months=missing,
        reason=reason,
    )


# ==========================================================
# Guarantor Helpers
# ==========================================================
def _user_is_globally_eligible_guarantor(user) -> bool:
    if hasattr(user, "is_active") and not bool(user.is_active):
        return False
    if hasattr(user, "is_approved") and not bool(user.is_approved):
        return False
    return True


def get_guarantor_available_savings_capacity(user) -> Decimal:
    """
    Platform-level guarantor capacity.
    Uses available savings and policy cap.
    """
    try:
        acct = get_primary_savings_account(user)
    except ValidationError:
        return Decimal("0.00")

    available = q2(getattr(acct, "available_balance", Decimal("0.00")))
    if available <= 0:
        return Decimal("0.00")

    return q2(available * GUARANTOR_MAX_EXPOSURE_RATIO)


def validate_guarantor_candidates(*, borrower, guarantor_ids: Sequence[int]) -> List:
    guarantor_ids = [int(x) for x in guarantor_ids if str(x).strip()]
    if not guarantor_ids:
        return []

    if borrower.id in guarantor_ids:
        raise ValidationError("Borrower cannot be their own guarantor.")

    User = type(borrower)
    guarantors = list(User.objects.filter(id__in=guarantor_ids))
    found_ids = {g.id for g in guarantors}
    missing = [gid for gid in guarantor_ids if gid not in found_ids]
    if missing:
        raise ValidationError(f"Guarantor(s) not found: {', '.join(map(str, missing))}.")

    bad = [g for g in guarantors if not _user_is_globally_eligible_guarantor(g)]
    if bad:
        raise ValidationError("One or more selected guarantors are not eligible to guarantee a loan.")

    return guarantors


# ==========================================================
# Merry Credit Helpers
# ==========================================================
def _active_merry_credit_allocations_total_for_user(*, user, merry_id: int) -> Decimal:
    total = (
        LoanSecurityAllocation.objects.filter(
            owner_user=user,
            merry_id=merry_id,
            is_active=True,
            source_type__in=["BORROWER_MERRY_CREDIT", "GUARANTOR_MERRY_CREDIT"],
        )
        .aggregate(total=Sum("amount"))
        .get("total")
        or Decimal("0.00")
    )
    return q2(total)


def get_available_merry_credit_breakdown(*, user) -> List[dict]:
    """
    Returns per-merry available credit rows.
    Requires your merry app with:
    - MerryMember
    - MerryContributionDue
    - MerryPayout
    """
    rows: List[dict] = []

    memberships = MerryMember.objects.filter(user=user, is_active=True).select_related("merry")

    for membership in memberships:
        contrib_total = (
            MerryContributionDue.objects.filter(seat__member=membership, seat__is_active=True)
            .aggregate(total=Sum("paid_amount"))
            .get("total")
            or Decimal("0.00")
        )

        payout_total = (
            MerryPayout.objects.filter(seat__member=membership, status="PAID")
            .aggregate(total=Sum("amount"))
            .get("total")
            or Decimal("0.00")
        )

        held_total = _active_merry_credit_allocations_total_for_user(
            user=user,
            merry_id=membership.merry_id,
        )

        available = q2(Decimal(contrib_total) - Decimal(payout_total) - Decimal(held_total))
        if available > 0:
            rows.append(
                {
                    "merry": membership.merry,
                    "merry_id": membership.merry_id,
                    "available": available,
                }
            )

    return rows


def get_total_available_merry_credit(*, user) -> Decimal:
    total = sum((row["available"] for row in get_available_merry_credit_breakdown(user=user)), Decimal("0.00"))
    return q2(total)


# ==========================================================
# Group Share Security Adapter
# ==========================================================
def reserve_group_share_security_for_loan(*, loan: Loan, user, amount: Decimal, guarantor_link: LoanGuarantor | None = None) -> Decimal:
    """
    Adapter point for your groups module.

    Right now this returns 0 safely because your exact group-share reserve
    implementation was not provided in this turn.

    When you are ready, connect this to your real group hold logic and also
    create matching LoanSecurityAllocation rows with source_type:
    - BORROWER_GROUP_SHARE
    - GUARANTOR_GROUP_SHARE
    """
    _ = (loan, user, amount, guarantor_link)
    return Decimal("0.00")


def release_group_share_security_for_loan(*, loan: Loan) -> None:
    """
    Adapter release hook for group-share security.
    """
    _ = loan
    return None


# ==========================================================
# Loan Request Creation
# ==========================================================
@transaction.atomic
def request_global_loan(
    *,
    borrower,
    principal: Decimal,
    term_weeks: int,
    guarantor_ids: Optional[Sequence[int]] = None,
    product: Optional[LoanProduct] = None,
    member_note: str = "",
) -> Loan:
    principal = q2(principal)

    if term_weeks <= 0:
        raise ValidationError("term_weeks must be greater than 0.")

    eligibility = validate_platform_loan_eligibility(user=borrower, principal=principal)
    _ = eligibility  # keeps intention clear

    if product is None:
        product = get_default_loan_product()

    guarantors = validate_guarantor_candidates(
        borrower=borrower,
        guarantor_ids=guarantor_ids or [],
    )

    loan = Loan.objects.create(
        borrower=borrower,
        product=product,
        principal=principal,
        term_weeks=term_weeks,
        status="PENDING",
        member_note=member_note or "",
        total_payable=Decimal("0.00"),
        total_paid=Decimal("0.00"),
        outstanding_balance=Decimal("0.00"),
        security_target=_security_target(principal),
        security_reserved_total=Decimal("0.00"),
    )

    LoanGuarantor.objects.bulk_create(
        [
            LoanGuarantor(
                loan=loan,
                guarantor=g,
            )
            for g in guarantors
        ]
    )

    return loan


# ==========================================================
# Interest + Totals
# ==========================================================
def compute_total_payable(*, principal: Decimal, term_weeks: int, product: LoanProduct) -> Decimal:
    principal = q2(principal)
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


# ==========================================================
# Weekly Schedule
# ==========================================================
@transaction.atomic
def generate_weekly_installments(loan: Loan) -> List[LoanInstallment]:
    if loan.product.repayment_frequency != "WEEKLY":
        raise ValidationError("Only WEEKLY repayment schedule is supported.")

    LoanInstallment.objects.filter(loan=loan).delete()

    term_weeks = int(loan.term_weeks)
    if term_weeks <= 0:
        raise ValidationError("term_weeks must be > 0.")

    total_payable = Decimal(loan.total_payable or Decimal("0.00"))
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
# Security Allocation Helpers
# ==========================================================
def _create_security_allocation(
    *,
    loan: Loan,
    source_type: str,
    owner_user,
    amount: Decimal,
    savings_account: SavingsAccount | None = None,
    merry=None,
    group=None,
    guarantor_link: LoanGuarantor | None = None,
) -> LoanSecurityAllocation:
    allocation = LoanSecurityAllocation.objects.create(
        loan=loan,
        source_type=source_type,
        owner_user=owner_user,
        guarantor_link=guarantor_link,
        savings_account=savings_account,
        merry=merry,
        group=group,
        amount=q2(amount),
        is_active=True,
    )
    return allocation


@transaction.atomic
def release_reserved_security_for_loan(loan: Loan) -> None:
    allocations = (
        LoanSecurityAllocation.objects.select_for_update()
        .filter(loan=loan, is_active=True)
        .select_related("savings_account", "guarantor_link")
    )

    for alloc in allocations:
        if alloc.savings_account_id:
            acct = SavingsAccount.objects.select_for_update().get(id=alloc.savings_account_id)
            acct.reserved_amount = q2(max(Decimal("0.00"), Decimal(acct.reserved_amount or Decimal("0.00")) - Decimal(alloc.amount)))
            acct.save(update_fields=["reserved_amount"])

        if alloc.guarantor_link_id and alloc.source_type.startswith("GUARANTOR_"):
            gl = LoanGuarantor.objects.select_for_update().get(id=alloc.guarantor_link_id)
            gl.reserved_amount = q2(max(Decimal("0.00"), Decimal(gl.reserved_amount or Decimal("0.00")) - Decimal(alloc.amount)))
            gl.save(update_fields=["reserved_amount"])

        alloc.release()

    release_group_share_security_for_loan(loan=loan)

    loan.security_reserved_total = Decimal("0.00")
    loan.save(update_fields=["security_reserved_total"])


@transaction.atomic
def reserve_security_for_loan(loan: Loan) -> dict:
    principal = q2(loan.principal)
    if principal <= 0:
        raise ValidationError("Loan principal must be > 0.")

    # clear any previous holds first
    if LoanSecurityAllocation.objects.filter(loan=loan, is_active=True).exists():
        release_reserved_security_for_loan(loan)

    loan.security_target = _security_target(principal)
    loan.security_reserved_total = Decimal("0.00")
    loan.save(update_fields=["security_target", "security_reserved_total"])

    target = q2(loan.security_target)
    covered = Decimal("0.00")

    # ------------------------------------------------------
    # 1) Borrower savings
    # ------------------------------------------------------
    if ALLOW_BORROWER_SAVINGS_SECURITY:
        borrower_acct = get_primary_savings_account(loan.borrower)
        borrower_acct = SavingsAccount.objects.select_for_update().get(id=borrower_acct.id)

        cap = q2(getattr(borrower_acct, "available_balance", Decimal("0.00")))
        desired = _borrower_savings_target(principal)
        use = q2(min(cap, desired, q2(target - covered)))

        if use > 0:
            borrower_acct.reserved_amount = q2(Decimal(borrower_acct.reserved_amount or Decimal("0.00")) + use)
            borrower_acct.full_clean()
            borrower_acct.save(update_fields=["reserved_amount"])

            _create_security_allocation(
                loan=loan,
                source_type="BORROWER_SAVINGS",
                owner_user=loan.borrower,
                amount=use,
                savings_account=borrower_acct,
            )
            covered = q2(covered + use)

    # ------------------------------------------------------
    # 2) Borrower merry credit
    # ------------------------------------------------------
    if ALLOW_BORROWER_MERRY_CREDIT_SECURITY and covered < target:
        merry_rows = get_available_merry_credit_breakdown(user=loan.borrower)

        for row in merry_rows:
            remaining_need = q2(target - covered)
            if remaining_need <= 0:
                break

            use = q2(min(row["available"], remaining_need))
            if use <= 0:
                continue

            _create_security_allocation(
                loan=loan,
                source_type="BORROWER_MERRY_CREDIT",
                owner_user=loan.borrower,
                amount=use,
                merry=row["merry"],
            )
            covered = q2(covered + use)

    # ------------------------------------------------------
    # 3) Borrower group share (adapter)
    # ------------------------------------------------------
    if ALLOW_BORROWER_GROUP_SHARE_SECURITY and covered < target:
        remaining_need = q2(target - covered)
        reserved_group_amt = q2(
            reserve_group_share_security_for_loan(
                loan=loan,
                user=loan.borrower,
                amount=remaining_need,
                guarantor_link=None,
            )
        )
        if reserved_group_amt > 0:
            covered = q2(covered + reserved_group_amt)

    # ------------------------------------------------------
    # 4) Guarantor savings
    # ------------------------------------------------------
    if ALLOW_GUARANTOR_SAVINGS_SECURITY and covered < target:
        accepted = list(
            LoanGuarantor.objects.select_related("guarantor")
            .select_for_update()
            .filter(loan=loan, accepted=True)
        )

        if len(accepted) < MIN_GUARANTORS_FOR_APPROVAL:
            raise ValidationError("At least one guarantor must accept before approval.")

        for g in accepted:
            remaining_need = q2(target - covered)
            if remaining_need <= 0:
                break

            try:
                g_acct = get_primary_savings_account(g.guarantor)
            except ValidationError:
                continue

            g_acct = SavingsAccount.objects.select_for_update().get(id=g_acct.id)
            cap = q2(get_guarantor_available_savings_capacity(g.guarantor))
            use = q2(min(cap, remaining_need))

            if use <= 0:
                continue

            g_acct.reserved_amount = q2(Decimal(g_acct.reserved_amount or Decimal("0.00")) + use)
            g_acct.full_clean()
            g_acct.save(update_fields=["reserved_amount"])

            g.reserved_amount = q2(Decimal(g.reserved_amount or Decimal("0.00")) + use)
            g.save(update_fields=["reserved_amount"])

            _create_security_allocation(
                loan=loan,
                source_type="GUARANTOR_SAVINGS",
                owner_user=g.guarantor,
                amount=use,
                savings_account=g_acct,
                guarantor_link=g,
            )

            covered = q2(covered + use)

    if covered < target:
        short = q2(target - covered)
        raise ValidationError(
            f"Insufficient security coverage. Need additional {short}. "
            f"Add guarantor(s), increase savings, or reduce the loan amount."
        )

    loan.recompute_reserved_security_total()
    loan.save(update_fields=["security_reserved_total"])

    return {
        "security_target": q2(target),
        "covered_total": q2(covered),
        "security_reserved_total": q2(loan.security_reserved_total),
    }


# ==========================================================
# Approval
# ==========================================================
@transaction.atomic
def approve_loan_and_create_schedule(loan: Loan) -> Loan:
    if loan.status not in ("PENDING", "UNDER_REVIEW"):
        raise ValidationError("Only pending/review loans can be approved.")

    validate_platform_loan_eligibility(user=loan.borrower, principal=loan.principal)

    if not LoanGuarantor.objects.filter(loan=loan, accepted=True).exists():
        raise ValidationError("At least one guarantor must accept before approval.")

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


# ==========================================================
# Payments
# ==========================================================
@transaction.atomic
def create_loan_payment_record(
    loan: Loan,
    amount: Decimal,
    method: str = "MANUAL",
    reference: Optional[str] = None,
) -> LoanPayment:
    amount = q2(amount)
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
    return create_loan_payment_record(
        loan=loan,
        amount=amount,
        method=method,
        reference=reference,
    )


@transaction.atomic
def apply_payment_to_loan(loan: Loan, amount: Decimal) -> Loan:
    amount = q2(amount)
    if amount <= 0:
        raise ValidationError("Payment amount must be greater than 0.")

    if loan.status not in ("APPROVED", "DEFAULTED"):
        raise ValidationError("Payments can only be applied to approved/defaulted loans.")

    current_outstanding = q2(loan.outstanding_balance or Decimal("0.00"))
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
    loan.total_paid = q2(Decimal(loan.total_paid or Decimal("0.00")) + amount_to_apply)
    loan.recompute_balances()
    loan.save(update_fields=["total_paid", "outstanding_balance", "status", "completed_at"])

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
    amount = q2(amount)
    current_outstanding = q2(loan.outstanding_balance or Decimal("0.00"))
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
    loan = (
        Loan.objects.select_for_update()
        .select_related("product", "borrower")
        .filter(id=loan_id)
        .first()
    )
    if not loan:
        raise ValidationError("Loan not found.")

    amt = q2(amount)
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

    outstanding = q2(loan.outstanding_balance or Decimal("0.00"))
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


# ==========================================================
# Late Fees
# ==========================================================
@transaction.atomic
def apply_weekly_late_fees(today: Optional[date] = None) -> int:
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