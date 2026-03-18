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
        "Create chama test users and seed backdated group + savings history "
        "for Jan, Feb, Mar 2026 loan eligibility checks."
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

        if group.created_by_id is None:
            group.created_by = admin_user
            group.save(update_fields=["created_by"])

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
        self.stdout.write("Seeded required months: 2026-01, 2026-02, 2026-03")

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
            if user.username != username:
                user.username = username
                changed = True
            if user.email != email:
                user.email = email
                changed = True
            if user.id_number != id_number:
                user.id_number = id_number
                changed = True
            if user.role != role:
                user.role = role
                changed = True
            if user.status != status:
                user.status = status
                changed = True
            if user.is_active != is_active:
                user.is_active = is_active
                changed = True

        user.set_password(password)
        changed = True

        if changed:
            user.save()

        return user

    def seed_required_group_months(self, group, seeded_users):
        required_dates = [
            timezone.make_aware(datetime(2026, 1, 15, 10, 0, 0)),
            timezone.make_aware(datetime(2026, 2, 15, 10, 0, 0)),
            timezone.make_aware(datetime(2026, 3, 10, 10, 0, 0)),
        ]

        for i, user in seeded_users:
            monthly_amount = Decimal(str(500 + (i * 100)))  # 600 ... 1500

            for dt in required_dates:
                month_key = dt.strftime("%Y-%m")
                reference = f"TEST-GRP-{user.phone}-{month_key}"

                exists = GroupContribution.objects.filter(
                    group=group,
                    user=user,
                    reference=reference,
                ).exists()

                if not exists:
                    contribution = GroupContribution(
                        group=group,
                        user=user,
                        amount=monthly_amount,
                        source="MANUAL",
                        reference=reference,
                        note=f"Seeded monthly group contribution for {month_key}",
                        created_at=dt,
                    )
                    contribution.save()

    def seed_required_savings_months(self, seeded_users):
        """
        Tries to seed savings history for Jan/Feb/Mar 2026 using common model names.
        This is best-effort because your exact savings models were not shared.
        """

        SavingsAccount = self.safe_get_model("savings", "SavingsAccount")
        SavingsDeposit = self.safe_get_model("savings", "SavingsDeposit")
        SavingsTransaction = self.safe_get_model("savings", "SavingsTransaction")
        MpesaTransaction = self.safe_get_model("merry", "MpesaTransaction")
        PaymentLedger = self.safe_get_model("merry", "PaymentLedger")

        required_dates = [
            timezone.make_aware(datetime(2026, 1, 15, 9, 0, 0)),
            timezone.make_aware(datetime(2026, 2, 15, 9, 0, 0)),
            timezone.make_aware(datetime(2026, 3, 10, 9, 0, 0)),
        ]

        savings_accounts = {}
        if SavingsAccount:
            savings_accounts = self.ensure_savings_accounts(SavingsAccount, seeded_users)

        for i, user in seeded_users:
            deposit_amount = Decimal(str(500 + (i * 100)))  # 600 ... 1500
            savings_account = savings_accounts.get(user.id)

            for dt in required_dates:
                month_key = dt.strftime("%Y-%m")
                reference = f"TEST-SAV-{user.phone}-{month_key}"

                created_any = False

                if SavingsDeposit:
                    created_any = self.try_create_savings_deposit(
                        SavingsDeposit= SavingsDeposit,
                        user=user,
                        savings_account=savings_account,
                        amount=deposit_amount,
                        reference=reference,
                        dt=dt,
                    ) or created_any

                if SavingsTransaction:
                    created_any = self.try_create_savings_transaction(
                        SavingsTransaction= SavingsTransaction,
                        user=user,
                        savings_account=savings_account,
                        amount=deposit_amount,
                        reference=reference,
                        dt=dt,
                    ) or created_any

                if MpesaTransaction:
                    created_any = self.try_create_mpesa_savings_tx(
                        MpesaTransaction= MpesaTransaction,
                        user=user,
                        amount=deposit_amount,
                        reference=reference,
                        dt=dt,
                    ) or created_any

                if PaymentLedger:
                    created_any = self.try_create_payment_ledger(
                        PaymentLedger= PaymentLedger,
                        user=user,
                        amount=deposit_amount,
                        reference=reference,
                        dt=dt,
                    ) or created_any

                # If only SavingsAccount exists, at least keep balance healthy
                if savings_account and not created_any:
                    self.bump_savings_account_balance(savings_account, deposit_amount)

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
                obj, _ = SavingsAccount.objects.get_or_create(
                    user=user,
                    defaults=defaults,
                )
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
            SavingsDeposit.objects.create(**payload)
            return True
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"⚠️ SavingsDeposit create failed for {user.phone} {reference}: {exc}"
            ))
            return False

    def try_create_savings_transaction(self, SavingsTransaction, user, savings_account, amount, reference, dt):
        model_fields = {f.name for f in SavingsTransaction._meta.fields}
        lookup = {"reference": reference} if "reference" in model_fields else None

        if lookup and SavingsTransaction.objects.filter(**lookup).exists():
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
        if "transaction_type" in model_fields:
            payload["transaction_type"] = "DEPOSIT"
        if "type" in model_fields:
            payload["type"] = "DEPOSIT"
        if "source" in model_fields:
            payload["source"] = "MANUAL"
        if "method" in model_fields:
            payload["method"] = "MANUAL"
        if "status" in model_fields:
            payload["status"] = "SUCCESS"
        if "note" in model_fields:
            payload["note"] = f"Seeded savings transaction for {dt.strftime('%Y-%m')}"
        if "description" in model_fields:
            payload["description"] = f"Seeded savings transaction for {dt.strftime('%Y-%m')}"
        if "created_at" in model_fields:
            payload["created_at"] = dt
        if "transaction_date" in model_fields:
            payload["transaction_date"] = dt

        try:
            SavingsTransaction.objects.create(**payload)
            return True
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"⚠️ SavingsTransaction create failed for {user.phone} {reference}: {exc}"
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
            MpesaTransaction.objects.create(**payload)
            return True
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"⚠️ MpesaTransaction create failed for {user.phone} {reference}: {exc}"
            ))
            return False

    def try_create_payment_ledger(self, PaymentLedger, user, amount, reference, dt):
        model_fields = {f.name for f in PaymentLedger._meta.fields}

        if "reference" in model_fields and PaymentLedger.objects.filter(
            user=user,
            reference=reference,
            category="SAVINGS" if "category" in model_fields else None,
        ).exists():
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
            PaymentLedger.objects.create(**payload)
            return True
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"⚠️ PaymentLedger create failed for {user.phone} {reference}: {exc}"
            ))
            return False

    def bump_savings_account_balance(self, savings_account, amount):
        changed = False

        if hasattr(savings_account, "balance"):
            current = getattr(savings_account, "balance", Decimal("0.00")) or Decimal("0.00")
            setattr(savings_account, "balance", current + amount)
            changed = True

        if hasattr(savings_account, "available_balance"):
            current = getattr(savings_account, "available_balance", Decimal("0.00")) or Decimal("0.00")
            setattr(savings_account, "available_balance", current + amount)
            changed = True

        if changed:
            savings_account.save()

    def safe_get_model(self, app_label, model_name):
        try:
            return apps.get_model(app_label, model_name)
        except LookupError:
            return None