from celery import shared_task
from django.db import transaction
from django.utils import timezone

from .models import Loan, MemberCreditProfile
from .services import apply_weekly_late_fees


def _get_or_create_credit_profile_for_loan(loan: Loan) -> MemberCreditProfile:
    """
    MemberCreditProfile requires exactly one context:
    - If loan.merry exists -> profile(merry=loan.merry, group=None)
    - Else -> profile(group=loan.group, merry=None)
    """
    if loan.merry_id:
        profile, _ = MemberCreditProfile.objects.get_or_create(
            user=loan.borrower,
            merry=loan.merry,
            defaults={"group": None},
        )
        return profile

    profile, _ = MemberCreditProfile.objects.get_or_create(
        user=loan.borrower,
        group=loan.group,
        defaults={"merry": None},
    )
    return profile


@shared_task
def apply_late_fees_and_tag_defaulters():
    """
    Runs periodically (daily is ok):
    1) Applies weekly late fees to overdue installments (once per run)
       using services.apply_weekly_late_fees()
    2) Marks loans as DEFAULTED if any installment overdue beyond grace period
    3) Updates credit profile per context (merry/group)
    """
    today = timezone.now().date()

    # 1) Apply weekly late fees (service already sets DEFAULTED on first late fee run)
    # Returns count of installments updated
    updated_installments_count = apply_weekly_late_fees(today=today)

    # 2) Tag defaulters after grace period
    grace_days = 14
    cutoff = today - timezone.timedelta(days=grace_days)

    loans_to_default = (
        Loan.objects.select_related("borrower", "merry", "group")
        .filter(
            status__in=["APPROVED", "DEFAULTED"],  # already might be DEFAULTED by late fee task
            installments__due_date__lt=cutoff,
            installments__is_paid=False,
        )
        .distinct()
    )

    with transaction.atomic():
        for loan in loans_to_default:
            if loan.status != "DEFAULTED":
                loan.status = "DEFAULTED"
                loan.is_defaulter = True
                loan.save(update_fields=["status", "is_defaulter"])

            # Update credit profile in the same context
            profile = _get_or_create_credit_profile_for_loan(loan)
            profile.loans_defaulted += 1
            profile.score = max(profile.score - 20, 0)
            profile.save(update_fields=["loans_defaulted", "score"])

    return {
        "late_fee_installments_updated": updated_installments_count,
        "loans_tagged_defaulted": loans_to_default.count(),
    }   