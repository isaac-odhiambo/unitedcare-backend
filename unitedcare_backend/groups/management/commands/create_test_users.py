from datetime import datetime
from decimal import Decimal

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from groups.models import Group, GroupContribution, GroupFund, GroupMembership
from loans.models import LoanProduct, MemberCreditProfile

User = get_user_model()


class Command(BaseCommand):
    help = (
        "Create chama test users and seed backdated rolling 3-month group + savings history "
        "for loan eligibility checks."
    )

    @transaction.atomic
    def handle(self, *args, **kwargs):
        password = "Password@123"

        admin_user = self.create_or_update_user(
            phone="0710000000",
            username="Test Admin",
            password=password,
            role="admin",
            status="approved",
            is_active=True,
            email="testadmin@example.com",
            id_number="900000000",
        )

        group, _ = Group.objects.get_or_create(
            name="Test Chama",
            defaults={
                "group_type": "SAVINGS",
                "description": "System generated test chama for savings and loan testing",
                "objective": "Testing contributions, memberships, savings, and borrowing",
                "created_by": admin_user,
                "visibility": "PUBLIC",
                "join_policy": "APPROVAL",
                "is_active": True,
                "max_members": 50,
                "requires_contributions": True,
                "contribution_amount": Decimal("500.00"),
                "contribution_frequency": "MONTHLY",
            },
        )

        group_changed = False
        if getattr(group, "created_by_id", None) is None:
            group.created_by = admin_user
            group_changed = True
        if hasattr(group, "is_active") and not group.is_active:
            group.is_active = True
            group_changed = True
        if group_changed:
            group.save()

        GroupFund.objects.get_or_create(group=group)

        GroupMembership.objects.get_or_create(
            group=group,
            user=admin_user,
            defaults={"role": "ADMIN", "is_active": True},
        )

        loan_product = LoanProduct.objects.filter(is_default=True).first()
        if not loan_product:
            loan_product, _ = LoanProduct.objects.get_or_create(
                name="Default Weekly Test Loan",
                defaults={
                    "interest_type": "FLAT",
                    "annual_interest_rate": Decimal("12.00"),
                    "repayment_frequency": "WEEKLY",
                    "repayment_weekday": 0,
                    "max_weeks": 12,
                    "late_fee_rate_weekly": Decimal("2.00"),
                    "is_active": True,
                    "is_default": True,
                },
            )

        seeded_users = []
        for i in range(1, 11):
            phone = f"07100000{i:02d}"
            username = f"Test User {i}"
            id_number = str(100000000 + i)

            user = self.create_or_update_user(
                phone=phone,
                username=username,
                password=password,
                role="member",
                status="approved",
                is_active=True,
                email=f"testuser{i}@example.com",
                id_number=id_number,
            )

            GroupMembership.objects.get_or_create(
                group=group,
                user=user,
                defaults={"role": "MEMBER", "is_active": True},
            )

            MemberCreditProfile.objects.get_or_create(
                user=user,
                defaults={
                    "score": 100,
                    "total_loans": 0,
                    "loans_completed": 0,
                    "loans_defaulted": 0,
                    "late_payments": 0,
                },
            )

            seeded_users.append((i, user))

        self.seed_required_group_months(group, seeded_users)
        self.seed_required_savings_months(seeded_users)

        fund = GroupFund.objects.get(group=group)
        seeded_months = ", ".join(dt.strftime("%Y-%m") for dt in self.get_required_month_dates())

        self.stdout.write(self.style.WARNING(
            "⚠️ KYCProfile was not auto-created because your KYC model requires image files."
        ))
        self.stdout.write(self.style.WARNING(
            "Users are active and approved, but has_full_access remains False until KYC is approved."
        ))
        self.stdout.write(self.style.SUCCESS("✅ Test users created successfully"))
        self.stdout.write("Admin login: 0710000000")
        self.stdout.write("Member logins: 0710000001 to 0710000010")
        self.stdout.write(f"Password for all: {password}")
        self.stdout.write(f"Group: {group.name}")
        self.stdout.write(f"Loan product: {loan_product.name}")
        self.stdout.write(f"Group fund balance: {fund.balance}")
        self.stdout.write(f"Seeded required months: {seeded_months}")

    def get_required_month_dates(self):
        """
        Returns aware datetimes for:
        - two months ago
        - one month ago
        - current month
        """
        today = timezone.localdate()
        current_month = today.replace(day=1)
        prev_month = self.prev_month_start(current_month)
        prev2_month = self.prev_month_start(prev_month)

        months = [prev2_month, prev_month, current_month]
        dates = []
        days = [10, 15, 20]

        for month_start, day in zip(months, days):
            dt = datetime(month_start.year, month_start.month, day, 10, 0, 0)
            dates.append(timezone.make_aware(dt))

        return dates

    def prev_month_start(self, d):
        if d.month == 1:
            return d.replace(year=d.year - 1, month=12, day=1)
        return d.replace(month=d.month - 1, day=1)

    def create_or_update_user(
        self,
        *,
        phone,
        username,
        password,
        role,
        status,
        is_active,
        email,
        id_number,
    ):
        user, created = User.objects.get_or_create(
            phone=phone,
            defaults={
                "username": username,
                "email": email,
                "id_number": id_number,
                "role": role,
                "status": status,
                "is_active": is_active,
            },
        )

        changed = False

        if not created:
            if getattr(user, "username", None) != username:
                user.username = username
                changed = True
            if getattr(user, "email", None) != email:
                user.email = email
                changed = True
            if getattr(user, "id_number", None) != id_number:
                user.id_number = id_number
                changed = True
            if getattr(user, "role", None) != role:
                user.role = role
                changed = True
            if getattr(user, "status", None) != status:
                user.status = status
                changed = True
            if getattr(user, "is_active", None) != is_active:
                user.is_active = is_active
                changed = True

        user.set_password(password)
        changed = True

        if changed:
            user.save()

        return user

    def seed_required_group_months(self, group, seeded_users):
        required_dates = self.get_required_month_dates()

        for i, user in seeded_users:
            monthly_amount = Decimal(str(500 + (i * 100)))

            for dt in required_dates:
                month_key = dt.strftime("%Y-%m")
                reference = f"TEST-GRP-{user.phone}-{month_key}"

                exists = GroupContribution.objects.filter(
                    group=group,
                    user=user,
                    reference=reference,
                ).exists()

                if exists:
                    continue

                contribution = GroupContribution.objects.create(
                    group=group,
                    user=user,
                    amount=monthly_amount,
                    source="MANUAL",
                    reference=reference,
                    note=f"Seeded monthly group contribution for {month_key}",
                )

                if hasattr(contribution, "created_at"):
                    GroupContribution.objects.filter(pk=contribution.pk).update(created_at=dt)

    def seed_required_savings_months(self, seeded_users):
        SavingsAccount = self.safe_get_model("savings", "SavingsAccount")
        SavingsDeposit = self.safe_get_model("savings", "SavingsDeposit")
        SavingsTransaction = self.safe_get_model("savings", "SavingsTransaction")
        MpesaTransaction = self.safe_get_model("merry", "MpesaTransaction")
        PaymentLedger = self.safe_get_model("merry", "PaymentLedger")

        required_dates = self.get_required_month_dates()

        savings_accounts = {}
        if SavingsAccount:
            savings_accounts = self.ensure_savings_accounts(SavingsAccount, seeded_users)

        for i, user in seeded_users:
            deposit_amount = Decimal(str(500 + (i * 100)))
            savings_account = savings_accounts.get(user.id)

            for dt in required_dates:
                month_key = dt.strftime("%Y-%m")
                reference = f"TEST-SAV-{user.phone}-{month_key}"

                created_any = False

                if SavingsDeposit:
                    created_any = self.try_create_savings_deposit(
                        SavingsDeposit=SavingsDeposit,
                        user=user,
                        savings_account=savings_account,
                        amount=deposit_amount,
                        reference=reference,
                        dt=dt,
                    ) or created_any

                if SavingsTransaction:
                    created_any = self.try_create_savings_transaction(
                        SavingsTransaction=SavingsTransaction,
                        user=user,
                        savings_account=savings_account,
                        amount=deposit_amount,
                        reference=reference,
                        dt=dt,
                    ) or created_any

                if MpesaTransaction:
                    created_any = self.try_create_mpesa_savings_tx(
                        MpesaTransaction=MpesaTransaction,
                        user=user,
                        amount=deposit_amount,
                        reference=reference,
                        dt=dt,
                    ) or created_any

                if PaymentLedger:
                    created_any = self.try_create_payment_ledger(
                        PaymentLedger=PaymentLedger,
                        user=user,
                        amount=deposit_amount,
                        reference=reference,
                        dt=dt,
                    ) or created_any

                if savings_account:
                    self.bump_savings_account_balance_if_needed(
                        savings_account=savings_account,
                        amount=deposit_amount,
                    )

    def ensure_savings_accounts(self, SavingsAccount, seeded_users):
        model_fields = {f.name for f in SavingsAccount._meta.fields}
        results = {}

        if "user" not in model_fields:
            self.stdout.write(self.style.WARNING(
                "⚠️ SavingsAccount found but no 'user' field. Skipping account seeding."
            ))
            return results

        opened_at = timezone.make_aware(datetime(2025, 12, 15, 9, 0, 0))

        for i, user in seeded_users:
            opening_balance = Decimal(str(1000 + (i * 1000)))
            defaults = {}

            if "name" in model_fields:
                defaults["name"] = f"Test Savings {i}"
            if "account_name" in model_fields:
                defaults["account_name"] = f"Test Savings {i}"
            if "account_type" in model_fields:
                defaults["account_type"] = "FLEXIBLE"
            if "balance" in model_fields:
                defaults["balance"] = opening_balance
            if "available_balance" in model_fields:
                defaults["available_balance"] = opening_balance
            if "reserved_amount" in model_fields:
                defaults["reserved_amount"] = Decimal("0.00")
            if "target_amount" in model_fields:
                defaults["target_amount"] = Decimal("0.00")
            if "status" in model_fields:
                defaults["status"] = "ACTIVE"
            if "is_active" in model_fields:
                defaults["is_active"] = True
            if "created_at" in model_fields:
                defaults["created_at"] = opened_at
            if "opened_at" in model_fields:
                defaults["opened_at"] = opened_at

            try:
                obj, created = SavingsAccount.objects.get_or_create(
                    user=user,
                    defaults=defaults,
                )

                changed = False

                if not created:
                    if "account_type" in model_fields and getattr(obj, "account_type", None) != "FLEXIBLE":
                        obj.account_type = "FLEXIBLE"
                        changed = True
                    if "is_active" in model_fields and not bool(getattr(obj, "is_active", False)):
                        obj.is_active = True
                        changed = True
                    if "status" in model_fields and getattr(obj, "status", None) != "ACTIVE":
                        obj.status = "ACTIVE"
                        changed = True
                    if "reserved_amount" in model_fields and getattr(obj, "reserved_amount", None) is None:
                        obj.reserved_amount = Decimal("0.00")
                        changed = True
                    if "balance" in model_fields and (getattr(obj, "balance", Decimal("0.00")) or Decimal("0.00")) <= 0:
                        obj.balance = opening_balance
                        changed = True
                    if "available_balance" in model_fields and (
                        getattr(obj, "available_balance", Decimal("0.00")) or Decimal("0.00")
                    ) <= 0:
                        obj.available_balance = opening_balance
                        changed = True

                    if changed:
                        obj.save()

                results[user.id] = obj
            except Exception as exc:
                self.stdout.write(self.style.WARNING(
                    f"⚠️ Could not create SavingsAccount for {user.phone}: {exc}"
                ))

        return results

    def try_create_savings_deposit(self, SavingsDeposit, user, savings_account, amount, reference, dt):
        model_fields = {f.name for f in SavingsDeposit._meta.fields}
        lookup = {"reference": reference} if "reference" in model_fields else None

        if lookup and SavingsDeposit.objects.filter(**lookup).exists():
            return False

        payload = {}

        if "user" in model_fields:
            payload["user"] = user
        if "account" in model_fields and savings_account:
            payload["account"] = savings_account
        if "savings_account" in model_fields and savings_account:
            payload["savings_account"] = savings_account
        if "amount" in model_fields:
            payload["amount"] = amount
        if "reference" in model_fields:
            payload["reference"] = reference
        if "source" in model_fields:
            payload["source"] = "MANUAL"
        if "method" in model_fields:
            payload["method"] = "MANUAL"
        if "status" in model_fields:
            payload["status"] = "CONFIRMED"
        if "note" in model_fields:
            payload["note"] = f"Seeded savings deposit for {dt.strftime('%Y-%m')}"
        if "created_at" in model_fields:
            payload["created_at"] = dt
        if "deposited_at" in model_fields:
            payload["deposited_at"] = dt
        if "transaction_date" in model_fields:
            payload["transaction_date"] = dt

        try:
            obj = SavingsDeposit.objects.create(**payload)

            updates = {}
            if "created_at" in model_fields:
                updates["created_at"] = dt
            if "deposited_at" in model_fields:
                updates["deposited_at"] = dt
            if "transaction_date" in model_fields:
                updates["transaction_date"] = dt

            if updates:
                SavingsDeposit.objects.filter(pk=obj.pk).update(**updates)

            return True
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"⚠️ SavingsDeposit create failed for {user.phone} {reference}: {exc}"
            ))
            return False

    def try_create_savings_transaction(self, SavingsTransaction, user, savings_account, amount, reference, dt):
        model_fields = {f.name for f in SavingsTransaction._meta.fields}

        if "reference" in model_fields and SavingsTransaction.objects.filter(reference=reference).exists():
            return False

        payload = {}

        if "user" in model_fields:
            payload["user"] = user

        if "account" in model_fields and savings_account:
            payload["account"] = savings_account
        elif "savings_account" in model_fields and savings_account:
            payload["savings_account"] = savings_account

        if "amount" in model_fields:
            payload["amount"] = amount

        if "reference" in model_fields:
            payload["reference"] = reference

        try:
            obj = SavingsTransaction.objects.create(**payload)
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"⚠️ SavingsTransaction create failed for {user.phone} {reference}: {exc}"
            ))
            return False

        updates = {}

        if "txn_type" in model_fields:
            updates["txn_type"] = "DEPOSIT"
        elif "transaction_type" in model_fields:
            updates["transaction_type"] = "DEPOSIT"
        elif "type" in model_fields:
            updates["type"] = "DEPOSIT"

        if "source" in model_fields:
            updates["source"] = "MANUAL"

        if "method" in model_fields:
            updates["method"] = "MANUAL"

        if "status" in model_fields:
            updates["status"] = "SUCCESS"

        if "note" in model_fields:
            updates["note"] = f"Seeded savings transaction for {dt.strftime('%Y-%m')}"

        if "description" in model_fields:
            updates["description"] = f"Seeded savings transaction for {dt.strftime('%Y-%m')}"

        if "created_at" in model_fields:
            updates["created_at"] = dt

        if "transaction_date" in model_fields:
            updates["transaction_date"] = dt

        try:
            if updates:
                SavingsTransaction.objects.filter(pk=obj.pk).update(**updates)
            return True
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"⚠️ SavingsTransaction update failed for {user.phone} {reference}: {exc}"
            ))
            return False

    def try_create_mpesa_savings_tx(self, MpesaTransaction, user, amount, reference, dt):
        model_fields = {f.name for f in MpesaTransaction._meta.fields}

        if "reference" in model_fields and MpesaTransaction.objects.filter(reference=reference).exists():
            return False

        payload = {}

        if "user" in model_fields:
            payload["user"] = user
        if "phone" in model_fields:
            payload["phone"] = user.phone
        if "amount" in model_fields:
            payload["amount"] = amount
        if "base_amount" in model_fields:
            payload["base_amount"] = amount
        if "transaction_fee" in model_fields:
            payload["transaction_fee"] = Decimal("0.00")
        if "direction" in model_fields:
            payload["direction"] = "IN"
        if "channel" in model_fields:
            payload["channel"] = "C2B"
        if "purpose" in model_fields:
            payload["purpose"] = "SAVINGS_DEPOSIT"
        if "status" in model_fields:
            payload["status"] = "SUCCESS"
        if "reference" in model_fields:
            payload["reference"] = reference
        if "mpesa_receipt_number" in model_fields:
            payload["mpesa_receipt_number"] = f"TEST{user.phone[-4:]}{dt.strftime('%m%d')}"
        if "transaction_date" in model_fields:
            payload["transaction_date"] = dt
        if "created_at" in model_fields:
            payload["created_at"] = dt
        if "allocation_status" in model_fields:
            payload["allocation_status"] = "MANUALLY_ALLOCATED"
        if "allocation_notes" in model_fields:
            payload["allocation_notes"] = f"Seeded for loan month {dt.strftime('%Y-%m')}"
        if "ledger_posted" in model_fields:
            payload["ledger_posted"] = True

        try:
            obj = MpesaTransaction.objects.create(**payload)

            updates = {}
            if "created_at" in model_fields:
                updates["created_at"] = dt
            if "transaction_date" in model_fields:
                updates["transaction_date"] = dt

            if updates:
                MpesaTransaction.objects.filter(pk=obj.pk).update(**updates)

            return True
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"⚠️ MpesaTransaction create failed for {user.phone} {reference}: {exc}"
            ))
            return False

    def try_create_payment_ledger(self, PaymentLedger, user, amount, reference, dt):
        model_fields = {f.name for f in PaymentLedger._meta.fields}

        if "reference" in model_fields:
            filters = {"reference": reference}
            if "user" in model_fields:
                filters["user"] = user
            if "category" in model_fields:
                filters["category"] = "SAVINGS"
            if PaymentLedger.objects.filter(**filters).exists():
                return False

        payload = {}

        if "user" in model_fields:
            payload["user"] = user
        if "entry_type" in model_fields:
            payload["entry_type"] = "CREDIT"
        if "category" in model_fields:
            payload["category"] = "SAVINGS"
        if "amount" in model_fields:
            payload["amount"] = amount
        if "narration" in model_fields:
            payload["narration"] = f"Seeded savings ledger for {dt.strftime('%Y-%m')}"
        if "reference" in model_fields:
            payload["reference"] = reference
        if "created_at" in model_fields:
            payload["created_at"] = dt

        try:
            obj = PaymentLedger.objects.create(**payload)

            if "created_at" in model_fields:
                PaymentLedger.objects.filter(pk=obj.pk).update(created_at=dt)

            return True
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"⚠️ PaymentLedger create failed for {user.phone} {reference}: {exc}"
            ))
            return False

    def bump_savings_account_balance_if_needed(self, savings_account, amount):
        changed = False

        if hasattr(savings_account, "balance"):
            current = getattr(savings_account, "balance", Decimal("0.00")) or Decimal("0.00")
            if current < amount:
                setattr(savings_account, "balance", current + amount)
                changed = True

        if hasattr(savings_account, "available_balance"):
            current = getattr(savings_account, "available_balance", Decimal("0.00")) or Decimal("0.00")
            if current <= 0:
                setattr(savings_account, "available_balance", current + amount)
                changed = True

        if changed:
            savings_account.save()

    def safe_get_model(self, app_label, model_name):
        try:
            return apps.get_model(app_label, model_name)
        except LookupError:
            return None