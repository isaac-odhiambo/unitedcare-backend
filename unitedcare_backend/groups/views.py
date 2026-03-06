# groups/views.py (COMPLETE + UPDATED)
# - ✅ ONLY SYSTEM ADMIN can create groups
# - ✅ ONLY GROUP ADMIN can add/remove members
# - ✅ Members CANNOT see GroupFund totals
# - ✅ Only group ADMIN can see GroupFund totals
#
# Endpoints:
#   GET  /api/groups/my-savings/
#   POST /api/groups/contribute/
#   GET  /api/groups/<group_id>/contributions/my/
#   GET  /api/groups/<group_id>/contributions/all/   (admin only)

from decimal import Decimal

# ✅ ADDED (only for totals per member)
from django.db.models import Sum  # ✅ ADDED

from rest_framework import permissions, status, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.exceptions import ValidationError, PermissionDenied

from .models import (
    Group,
    GroupMembership,
    GroupFund,
    GroupMemberShare,
    GroupContribution,
)
from .serializers import PostContributionSerializer
from .services import (
    get_or_create_group_fund,
    get_or_create_member_share,
    post_group_contribution,
    require_active_membership,
    require_group_admin,
)

# ✅ ADDED (ledger is source-of-truth for "confirmed" contributions)
from payments.models import PaymentLedger  # ✅ ADDED


def is_system_admin(user) -> bool:
    """
    System-level admin check.
    Supports:
    - Django staff/superuser
    - Custom role field: user.role == "admin"
    - Custom boolean: user.is_admin == True
    """
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    if getattr(user, "is_admin", False):
        return True
    if getattr(user, "role", None) == "admin":
        return True
    return False


# -----------------------------
# Group CRUD
# -----------------------------
class GroupViewSet(viewsets.ModelViewSet):
    queryset = Group.objects.all().order_by("-id")
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        data = [{"id": g.id, "name": g.name, "created_at": g.created_at} for g in qs]
        return Response(data, status=status.HTTP_200_OK)

    def retrieve(self, request, *args, **kwargs):
        g = self.get_object()
        return Response(
            {"id": g.id, "name": g.name, "created_at": g.created_at},
            status=status.HTTP_200_OK,
        )

    def create(self, request, *args, **kwargs):
        # ✅ ONLY SYSTEM ADMIN can create groups
        if not is_system_admin(request.user):
            raise PermissionDenied("Only admin can create groups.")

        name = (request.data.get("name") or "").strip()
        if not name:
            raise ValidationError("Group name is required.")

        g = Group.objects.create(name=name)

        # ✅ Auto create fund
        get_or_create_group_fund(g.id)

        # ✅ Creator becomes group ADMIN
        GroupMembership.objects.get_or_create(
            group=g,
            user=request.user,
            defaults={"role": "ADMIN", "is_active": True},
        )

        return Response(
            {"id": g.id, "name": g.name, "message": "Group created successfully."},
            status=status.HTTP_201_CREATED,
        )

    def destroy(self, request, *args, **kwargs):
        # optional: restrict delete to system admin
        if not is_system_admin(request.user):
            raise PermissionDenied("Only admin can delete groups.")
        return super().destroy(request, *args, **kwargs)


# -----------------------------
# Membership CRUD (simple)
# -----------------------------
class GroupMembershipViewSet(viewsets.ModelViewSet):
    queryset = GroupMembership.objects.select_related("group", "user").all().order_by("-id")
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request, *args, **kwargs):
        """
        ✅ Updated rules:
        - System admin: can list all memberships (optionally filtered)
        - Any active group member: can list members of THAT group using ?group=<id>
        - Without ?group=..., user sees ONLY their memberships (safe default)

        Response fields:
        - Normal members: limited fields (no is_active, role)
        - Admins (group/system): include role + is_active
        """
        qs = self.get_queryset()

        group_id = request.query_params.get("group")

        # ✅ If group filter is given => list members in that group (for active members)
        if group_id:
            gid = int(group_id)
            require_active_membership(gid, request.user)

            qs = qs.filter(group_id=gid).select_related("user", "group")

            # admin view?
            is_group_admin = False
            try:
                requester = GroupMembership.objects.filter(
                    group_id=gid, user=request.user, is_active=True
                ).first()
                is_group_admin = bool(requester and requester.role == "ADMIN")
            except Exception:
                is_group_admin = False

            can_see_admin_fields = is_system_admin(request.user) or is_group_admin

            data = []
            for m in qs:
                base = {
                    "membership_id": m.id,
                    "group_id": m.group_id,
                    "user_id": m.user_id,
                    # ✅ optional but practical for frontend display
                    "user_name": getattr(m.user, "username", "") or getattr(m.user, "name", ""),
                    "user_phone": getattr(m.user, "phone", ""),
                }

                # ✅ Only admins can see role/is_active
                if can_see_admin_fields:
                    base.update(
                        {
                            "role": m.role,
                            "is_active": m.is_active,
                            "joined_at": m.joined_at,
                        }
                    )

                data.append(base)

            return Response(data, status=status.HTTP_200_OK)

        # ✅ No group filter:
        # - system admin can see all memberships
        # - normal users see only their memberships
        if not is_system_admin(request.user):
            qs = qs.filter(user=request.user)

        data = []
        for m in qs:
            data.append(
                {
                    "id": m.id,
                    "group_id": m.group_id,
                    "user_id": m.user_id,
                    "role": m.role,
                    "is_active": m.is_active,
                    "joined_at": m.joined_at,
                }
            )
        return Response(data, status=status.HTTP_200_OK)

    def create(self, request, *args, **kwargs):
        """
        Only GROUP ADMIN can add members to their group.
        """
        group_id = request.data.get("group")
        user_id = request.data.get("user")
        role = (request.data.get("role") or "MEMBER").strip().upper()

        if not group_id or not user_id:
            raise ValidationError("group and user are required.")
        if role not in ("MEMBER", "ADMIN"):
            raise ValidationError("role must be MEMBER or ADMIN.")

        group_id = int(group_id)
        user_id = int(user_id)

        # ✅ Only group admin can add members
        require_group_admin(group_id, request.user)

        m, created = GroupMembership.objects.get_or_create(
            group_id=group_id,
            user_id=user_id,
            defaults={"role": role, "is_active": True},
        )
        if not created:
            if not m.is_active or m.role != role:
                m.is_active = True
                m.role = role
                m.save(update_fields=["is_active", "role"])

        # ✅ Ensure share row exists
        get_or_create_member_share(group_id, user_id)

        return Response(
            {
                "id": m.id,
                "group_id": m.group_id,
                "user_id": m.user_id,
                "role": m.role,
                "is_active": m.is_active,
            },
            status=status.HTTP_201_CREATED,
        )

    def destroy(self, request, *args, **kwargs):
        """
        Only GROUP ADMIN or SYSTEM ADMIN can remove a member.
        Soft remove: set is_active=False.
        """
        m = self.get_object()

        if not is_system_admin(request.user):
            require_group_admin(m.group_id, request.user)

        if m.is_active:
            m.is_active = False
            m.save(update_fields=["is_active"])

        return Response({"message": "Member removed (deactivated)."}, status=status.HTTP_200_OK)


# -----------------------------
# ✅ Group Savings Endpoints
# -----------------------------
class MyGroupSavingsSummaryView(APIView):
    """
    GET /api/groups/my-savings/
    Returns groups I belong to + my share.

    ✅ IMPORTANT:
    - Members do NOT see GroupFund totals
    - Only group ADMIN sees GroupFund totals
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        memberships = GroupMembership.objects.filter(user=request.user, is_active=True).select_related("group")
        out = []

        for m in memberships:
            fund = GroupFund.objects.filter(group=m.group).first()
            if not fund:
                fund = get_or_create_group_fund(m.group_id)

            share = GroupMemberShare.objects.filter(group=m.group, user=request.user).first()
            if not share:
                share = get_or_create_member_share(m.group_id, request.user.id)

            # ✅ Hide fund totals from non-admin members
            if m.role == "ADMIN":
                fund_payload = {
                    "balance": str(fund.balance),
                    "reserved_amount": str(fund.reserved_amount),
                    "available_balance": str(fund.available_balance),
                }
            else:
                fund_payload = {
                    "balance": None,
                    "reserved_amount": None,
                    "available_balance": None,
                    "visibility": "admins_only",
                }

            out.append(
                {
                    "group": {"id": m.group.id, "name": m.group.name},
                    "my_role": m.role,
                    "fund": fund_payload,
                    "my_share": {
                        "total_contributed": str(share.total_contributed),
                        "reserved_share": str(share.reserved_share),
                        "available_share": str(share.available_share),
                    },
                }
            )

        return Response(out, status=status.HTTP_200_OK)


class PostGroupContributionView(APIView):
    """
    POST /api/groups/contribute/
    Body: { group_id, amount, reference?, note? }

    Member contribution into group fund.
    - Member must belong to group.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        ser = PostContributionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        group_id = int(ser.validated_data["group_id"])
        amount = Decimal(str(ser.validated_data["amount"]))
        reference = (ser.validated_data.get("reference") or "").strip() or None
        note = (ser.validated_data.get("note") or "").strip() or None

        data = post_group_contribution(
            group_id=group_id,
            user=request.user,
            amount=amount,
            reference=reference,
            note=note,
        )
        return Response(data, status=status.HTTP_200_OK)


class GroupContributionsHistoryView(APIView):
    """
    GET /api/groups/<group_id>/contributions/my/
      - member sees their own contributions

    GET /api/groups/<group_id>/contributions/all/
      - group admin sees full group contributions
      - ✅ ALSO returns per-member confirmed totals (from PaymentLedger)
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, group_id: int, scope: str):
        group_id = int(group_id)
        require_active_membership(group_id, request.user)

        qs = GroupContribution.objects.filter(group_id=group_id).order_by("-created_at")

        scope = (scope or "").lower().strip()
        if scope == "my":
            qs = qs.filter(user=request.user)

            data = [
                {
                    "id": c.id,
                    "group_id": c.group_id,
                    "user_id": c.user_id,
                    "amount": str(c.amount),
                    "reference": c.reference,
                    "note": c.note,
                    "created_at": c.created_at,
                }
                for c in qs[:300]
            ]
            return Response(data, status=status.HTTP_200_OK)

        elif scope == "all":
            require_group_admin(group_id, request.user)

            # ✅ Keep your existing history list
            history = [
                {
                    "id": c.id,
                    "group_id": c.group_id,
                    "user_id": c.user_id,
                    "amount": str(c.amount),
                    "reference": c.reference,
                    "note": c.note,
                    "created_at": c.created_at,
                }
                for c in qs[:300]
            ]

            # ✅ NEW: totals from PaymentLedger (confirmed money)
            # IMPORTANT: your payments flow must use reference="GROUP-<group_id>" for group contributions
            group_ref = f"GROUP-{group_id}"

            totals_qs = (
                PaymentLedger.objects.filter(
                    category="GROUP",
                    entry_type="CREDIT",
                    reference=group_ref,
                )
                .values("user_id")
                .annotate(total_amount=Sum("amount"))
                .order_by("-total_amount")
            )

            per_member_totals = [
                {
                    "user_id": row["user_id"],
                    "total_contributed_confirmed": str(row["total_amount"] or Decimal("0")),
                }
                for row in totals_qs
            ]

            # ✅ NEW: group total confirmed
            group_total = (
                PaymentLedger.objects.filter(
                    category="GROUP",
                    entry_type="CREDIT",
                    reference=group_ref,
                ).aggregate(t=Sum("amount"))["t"]
                or Decimal("0")
            )

            return Response(
                {
                    "group_id": group_id,
                    "reference": group_ref,
                    "group_total_confirmed": str(group_total),
                    "per_member_totals": per_member_totals,
                    "history": history,
                },
                status=status.HTTP_200_OK,
            )

        else:
            raise ValidationError("Invalid scope. Use 'my' or 'all'.")