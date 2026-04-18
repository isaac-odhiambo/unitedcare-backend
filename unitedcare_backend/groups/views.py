# groups/views.py
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Sum
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    Group,
    GroupContribution,
    GroupFund,
    GroupJoinRequest,
    GroupMemberShare,
    GroupMembership,
)
from .serializers import PostContributionSerializer
from .services import (
    get_or_create_group_fund,
    get_or_create_member_share,
    post_group_contribution,
    require_active_membership,
    require_group_admin,
)

# optional if you still use ledger as source of truth
try:
    from payments.models import PaymentLedger
except Exception:
    PaymentLedger = None

User = get_user_model()


def is_system_admin(user) -> bool:
    """
    System-level admin check.
    Supports:
    - Django superuser/staff
    - custom is_admin flag
    - custom role == 'admin'
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


def serialize_group_basic(group: Group, request_user=None):
    member_count = group.memberships.filter(is_active=True).count()

    payload = {
        "id": group.id,
        "name": group.name,
        "payment_code": (group.payment_code or "").upper(),
        "group_type": group.group_type,
        "group_type_display": group.get_group_type_display(),
        "description": group.description,
        "objective": group.objective,
        "visibility": group.visibility,
        "join_policy": group.join_policy,
        "is_active": group.is_active,
        "max_members": group.max_members,
        "available_slots": group.available_slots(),
        "requires_contributions": group.requires_contributions,
        "contribution_amount": str(group.contribution_amount or Decimal("0.00")),
        "contribution_frequency": group.contribution_frequency,
        "member_count": member_count,
        "created_by": group.created_by_id,
        "created_at": group.created_at,
        "updated_at": group.updated_at,
    }

    if request_user and request_user.is_authenticated:
        membership = GroupMembership.objects.filter(
            group=group,
            user=request_user,
            is_active=True,
        ).first()
        payload["my_membership"] = (
            {
                "role": membership.role,
                "joined_at": membership.joined_at,
            }
            if membership
            else None
        )

    return payload


# ---------------------------------------------------
# Group CRUD
# ---------------------------------------------------
class GroupViewSet(viewsets.ModelViewSet):
    queryset = Group.objects.select_related("created_by").all().order_by("-id")
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()

        only_open = self.request.query_params.get("only_open")
        mine = self.request.query_params.get("mine")
        group_type = self.request.query_params.get("group_type")
        is_active = self.request.query_params.get("is_active")

        if mine in ("1", "true", "True"):
            qs = qs.filter(
                memberships__user=self.request.user,
                memberships__is_active=True,
            )

        if only_open in ("1", "true", "True"):
            qs = qs.filter(is_active=True).exclude(join_policy="CLOSED")

        if group_type:
            qs = qs.filter(group_type=group_type.strip().upper())

        if is_active in ("1", "true", "True"):
            qs = qs.filter(is_active=True)
        elif is_active in ("0", "false", "False"):
            qs = qs.filter(is_active=False)

        return qs.distinct()

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        data = [serialize_group_basic(g, request.user) for g in qs]
        return Response(data, status=status.HTTP_200_OK)

    def retrieve(self, request, *args, **kwargs):
        g = self.get_object()
        return Response(
            serialize_group_basic(g, request.user),
            status=status.HTTP_200_OK,
        )

    def create(self, request, *args, **kwargs):
        if not is_system_admin(request.user):
            raise PermissionDenied("Only system admin can create groups.")

        name = (request.data.get("name") or "").strip()
        group_type = (request.data.get("group_type") or "OTHER").strip().upper()
        description = (request.data.get("description") or "").strip()
        objective = (request.data.get("objective") or "").strip()
        visibility = (request.data.get("visibility") or "PUBLIC").strip().upper()
        join_policy = (request.data.get("join_policy") or "APPROVAL").strip().upper()
        is_active = bool(request.data.get("is_active", True))
        max_members = int(request.data.get("max_members") or 0)
        requires_contributions = bool(request.data.get("requires_contributions", False))
        contribution_amount = Decimal(str(request.data.get("contribution_amount") or "0"))
        contribution_frequency = (request.data.get("contribution_frequency") or "").strip().upper()

        if not name:
            raise ValidationError({"name": "Group name is required."})

        valid_group_types = {choice[0] for choice in Group.GROUP_TYPES}
        valid_visibility = {choice[0] for choice in Group.VISIBILITY_CHOICES}
        valid_join_policies = {choice[0] for choice in Group.JOIN_POLICY_CHOICES}

        if group_type not in valid_group_types:
            raise ValidationError({"group_type": "Invalid group_type."})
        if visibility not in valid_visibility:
            raise ValidationError({"visibility": "Invalid visibility."})
        if join_policy not in valid_join_policies:
            raise ValidationError({"join_policy": "Invalid join_policy."})

        g = Group.objects.create(
            name=name,
            group_type=group_type,
            description=description,
            objective=objective,
            created_by=request.user,
            visibility=visibility,
            join_policy=join_policy,
            is_active=is_active,
            max_members=max_members,
            requires_contributions=requires_contributions,
            contribution_amount=contribution_amount,
            contribution_frequency=contribution_frequency,
        )

        get_or_create_group_fund(g.id)

        GroupMembership.objects.get_or_create(
            group=g,
            user=request.user,
            defaults={"role": "ADMIN", "is_active": True},
        )

        get_or_create_member_share(g.id, request.user.id)

        return Response(
            {
                "message": "Group created successfully.",
                "group": serialize_group_basic(g, request.user),
            },
            status=status.HTTP_201_CREATED,
        )

    def update(self, request, *args, **kwargs):
        g = self.get_object()

        if not is_system_admin(request.user):
            raise PermissionDenied("Only system admin can update groups.")

        data = request.data

        if "name" in data:
            g.name = (data.get("name") or "").strip() or g.name

        if "group_type" in data:
            v = (data.get("group_type") or "").strip().upper()
            if v not in {choice[0] for choice in Group.GROUP_TYPES}:
                raise ValidationError({"group_type": "Invalid group_type."})
            g.group_type = v

        if "description" in data:
            g.description = (data.get("description") or "").strip()

        if "objective" in data:
            g.objective = (data.get("objective") or "").strip()

        if "visibility" in data:
            v = (data.get("visibility") or "").strip().upper()
            if v not in {choice[0] for choice in Group.VISIBILITY_CHOICES}:
                raise ValidationError({"visibility": "Invalid visibility."})
            g.visibility = v

        if "join_policy" in data:
            v = (data.get("join_policy") or "").strip().upper()
            if v not in {choice[0] for choice in Group.JOIN_POLICY_CHOICES}:
                raise ValidationError({"join_policy": "Invalid join_policy."})
            g.join_policy = v

        if "is_active" in data:
            g.is_active = bool(data.get("is_active"))

        if "max_members" in data:
            g.max_members = int(data.get("max_members") or 0)

        if "requires_contributions" in data:
            g.requires_contributions = bool(data.get("requires_contributions"))

        if "contribution_amount" in data:
            g.contribution_amount = Decimal(str(data.get("contribution_amount") or "0"))

        if "contribution_frequency" in data:
            g.contribution_frequency = (
                data.get("contribution_frequency") or ""
            ).strip().upper()

        g.full_clean()
        g.save()

        return Response(
            {
                "message": "Group updated successfully.",
                "group": serialize_group_basic(g, request.user),
            },
            status=status.HTTP_200_OK,
        )

    def destroy(self, request, *args, **kwargs):
        if not is_system_admin(request.user):
            raise PermissionDenied("Only system admin can delete groups.")
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=["get"], url_path="available")
    def available_groups(self, request):
        qs = Group.objects.filter(is_active=True)

        # public groups, plus optionally approval groups
        qs = qs.exclude(join_policy="CLOSED").order_by("-id")

        data = [serialize_group_basic(g, request.user) for g in qs]
        return Response(data, status=status.HTTP_200_OK)


# ---------------------------------------------------
# Membership CRUD
# ---------------------------------------------------
class GroupMembershipViewSet(viewsets.ModelViewSet):
    queryset = GroupMembership.objects.select_related("group", "user").all().order_by("-id")
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()
        group_id = request.query_params.get("group")

        if group_id:
            gid = int(group_id)
            require_active_membership(gid, request.user)
            qs = qs.filter(group_id=gid)

            requester = GroupMembership.objects.filter(
                group_id=gid,
                user=request.user,
                is_active=True,
            ).first()
            is_group_admin = bool(requester and requester.role == "ADMIN")
            can_see_admin_fields = is_system_admin(request.user) or is_group_admin

            data = []
            for m in qs:
                item = {
                    "membership_id": m.id,
                    "group_id": m.group_id,
                    "user_id": m.user_id,
                    "user_name": getattr(m.user, "username", "")
                    or getattr(m.user, "full_name", "")
                    or getattr(m.user, "name", ""),
                    "user_phone": getattr(m.user, "phone", ""),
                }

                if can_see_admin_fields:
                    item.update(
                        {
                            "role": m.role,
                            "is_active": m.is_active,
                            "joined_at": m.joined_at,
                        }
                    )

                data.append(item)

            return Response(data, status=status.HTTP_200_OK)

        if not is_system_admin(request.user):
            qs = qs.filter(user=request.user)

        data = [
            {
                "id": m.id,
                "group_id": m.group_id,
                "group_name": m.group.name,
                "user_id": m.user_id,
                "role": m.role,
                "is_active": m.is_active,
                "joined_at": m.joined_at,
            }
            for m in qs
        ]
        return Response(data, status=status.HTTP_200_OK)

    def create(self, request, *args, **kwargs):
        """
        Direct add by group admin/system admin.
        Mostly for admin use.
        """
        group_id = request.data.get("group")
        user_id = request.data.get("user")
        role = (request.data.get("role") or "MEMBER").strip().upper()

        if not group_id or not user_id:
            raise ValidationError("group and user are required.")
        if role not in ("MEMBER", "ADMIN", "TREASURER", "SECRETARY"):
            raise ValidationError("role must be MEMBER, ADMIN, TREASURER or SECRETARY.")

        group_id = int(group_id)
        user_id = int(user_id)

        if not is_system_admin(request.user):
            require_group_admin(group_id, request.user)

        group = Group.objects.filter(id=group_id).first()
        if not group:
            raise ValidationError("Group not found.")

        ok, reason = group.can_accept_member()
        if not ok:
            raise ValidationError(reason)

        m, created = GroupMembership.objects.get_or_create(
            group_id=group_id,
            user_id=user_id,
            defaults={"role": role, "is_active": True},
        )
        if not created:
            changed = False
            if not m.is_active:
                m.is_active = True
                changed = True
            if m.role != role:
                m.role = role
                changed = True
            if changed:
                m.save(update_fields=["is_active", "role"])

        get_or_create_group_fund(group_id)
        get_or_create_member_share(group_id, user_id)

        return Response(
            {
                "id": m.id,
                "group_id": m.group_id,
                "user_id": m.user_id,
                "role": m.role,
                "is_active": m.is_active,
                "message": "Member added successfully.",
            },
            status=status.HTTP_201_CREATED,
        )

    def destroy(self, request, *args, **kwargs):
        m = self.get_object()

        if not is_system_admin(request.user):
            require_group_admin(m.group_id, request.user)

        if m.is_active:
            m.is_active = False
            m.save(update_fields=["is_active"])

        return Response(
            {"message": "Member removed (deactivated)."},
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------
# Join Request ViewSet
# ---------------------------------------------------
class GroupJoinRequestViewSet(viewsets.ModelViewSet):
    queryset = GroupJoinRequest.objects.select_related(
        "group",
        "user",
        "reviewed_by",
    ).all().order_by("-id")
    permission_classes = [permissions.IsAuthenticated]

    def list(self, request, *args, **kwargs):
        qs = self.get_queryset()

        group_id = request.query_params.get("group")
        mine = request.query_params.get("mine")
        status_param = request.query_params.get("status")

        if group_id:
            gid = int(group_id)
            if not is_system_admin(request.user):
                require_group_admin(gid, request.user)
            qs = qs.filter(group_id=gid)
        elif mine in ("1", "true", "True"):
            qs = qs.filter(user=request.user)
        elif not is_system_admin(request.user):
            qs = qs.filter(user=request.user)

        if status_param:
            qs = qs.filter(status=status_param.strip().upper())

        data = [
            {
                "id": r.id,
                "group_id": r.group_id,
                "group_name": r.group.name,
                "user_id": r.user_id,
                "user_name": getattr(r.user, "username", "")
                or getattr(r.user, "full_name", "")
                or getattr(r.user, "name", ""),
                "note": r.note,
                "status": r.status,
                "reviewed_by": r.reviewed_by_id,
                "reviewed_at": r.reviewed_at,
                "created_at": r.created_at,
            }
            for r in qs
        ]
        return Response(data, status=status.HTTP_200_OK)

    def create(self, request, *args, **kwargs):
        group_id = request.data.get("group") or request.data.get("group_id")
        note = (request.data.get("note") or "").strip()

        if not group_id:
            raise ValidationError({"group": "group/group_id is required."})

        group = Group.objects.filter(id=int(group_id)).first()
        if not group:
            raise ValidationError({"group": "Group not found."})

        ok, reason = group.can_accept_member()
        if not ok:
            raise ValidationError({"detail": reason})

        if group.join_policy == "CLOSED":
            raise ValidationError({"detail": "This group is closed for joining."})

        if GroupMembership.objects.filter(
            group=group,
            user=request.user,
            is_active=True,
        ).exists():
            raise ValidationError(
                {"detail": "You are already an active member of this group."}
            )

        pending = GroupJoinRequest.objects.filter(
            group=group,
            user=request.user,
            status="PENDING",
        ).exists()
        if pending:
            raise ValidationError(
                {"detail": "You already have a pending join request for this group."}
            )

        # OPEN groups can auto-join directly
        if group.join_policy == "OPEN":
            membership, _ = GroupMembership.objects.get_or_create(
                group=group,
                user=request.user,
                defaults={"role": "MEMBER", "is_active": True},
            )
            if not membership.is_active:
                membership.is_active = True
                membership.save(update_fields=["is_active"])

            get_or_create_group_fund(group.id)
            get_or_create_member_share(group.id, request.user.id)

            return Response(
                {
                    "message": "You joined the group successfully.",
                    "group_id": group.id,
                    "membership_id": membership.id,
                    "role": membership.role,
                },
                status=status.HTTP_201_CREATED,
            )

        jr = GroupJoinRequest.objects.create(
            group=group,
            user=request.user,
            note=note,
            status="PENDING",
        )

        return Response(
            {
                "id": jr.id,
                "group_id": jr.group_id,
                "user_id": jr.user_id,
                "status": jr.status,
                "message": "Join request submitted successfully.",
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="approve")
    def approve_request(self, request, pk=None):
        jr = self.get_object()

        if not is_system_admin(request.user):
            require_group_admin(jr.group_id, request.user)

        membership = jr.approve(admin_user=request.user)

        return Response(
            {
                "message": "Join request approved successfully.",
                "request_id": jr.id,
                "membership": {
                    "id": membership.id,
                    "group_id": membership.group_id,
                    "user_id": membership.user_id,
                    "role": membership.role,
                    "is_active": membership.is_active,
                },
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="reject")
    def reject_request(self, request, pk=None):
        jr = self.get_object()

        if not is_system_admin(request.user):
            require_group_admin(jr.group_id, request.user)

        note = (request.data.get("note") or "").strip()
        jr.reject(admin_user=request.user, note=note)

        return Response(
            {
                "message": "Join request rejected successfully.",
                "request_id": jr.id,
                "status": jr.status,
                "note": jr.note,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=["post"], url_path="cancel")
    def cancel_request(self, request, pk=None):
        jr = self.get_object()
        jr.cancel(request.user)

        return Response(
            {
                "message": "Join request cancelled successfully.",
                "request_id": jr.id,
                "status": jr.status,
            },
            status=status.HTTP_200_OK,
        )


# ---------------------------------------------------
# My Group Savings Summary
# ---------------------------------------------------
class MyGroupSavingsSummaryView(APIView):
    """
    GET /api/groups/my-savings/

    Returns groups I belong to + my contribution share.
    Group fund totals only visible to group admin.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        memberships = GroupMembership.objects.filter(
            user=request.user,
            is_active=True,
        ).select_related("group")

        out = []

        for m in memberships:
            fund = GroupFund.objects.filter(group=m.group).first()
            if not fund:
                fund = get_or_create_group_fund(m.group_id)

            share = GroupMemberShare.objects.filter(
                group=m.group,
                user=request.user,
            ).first()
            if not share:
                share = get_or_create_member_share(m.group_id, request.user.id)

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
                    "group": {
                        "id": m.group.id,
                        "name": m.group.name,
                        "group_type": m.group.group_type,
                        "group_type_display": m.group.get_group_type_display(),
                    },
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


# ---------------------------------------------------
# Post Contribution
# ---------------------------------------------------
class PostGroupContributionView(APIView):
    """
    POST /api/groups/contribute/
    Body: { group_id, amount, reference?, note?, source? }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        ser = PostContributionSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        group_id = int(ser.validated_data["group_id"])
        amount = Decimal(str(ser.validated_data["amount"]))
        reference = (ser.validated_data.get("reference") or "").strip() or None
        note = (ser.validated_data.get("note") or "").strip() or None
        source = (ser.validated_data.get("source") or "MANUAL").strip().upper()

        data = post_group_contribution(
            group_id=group_id,
            user=request.user,
            amount=amount,
            reference=reference,
            note=note,
            source=source,
        )
        return Response(data, status=status.HTTP_200_OK)


# ---------------------------------------------------
# Contributions History
# ---------------------------------------------------
class GroupContributionsHistoryView(APIView):
    """
    GET /api/groups/<group_id>/contributions/my/
    GET /api/groups/<group_id>/contributions/all/
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, group_id: int, scope: str):
        group_id = int(group_id)
        require_active_membership(group_id, request.user)

        qs = (
            GroupContribution.objects.filter(group_id=group_id)
            .select_related("user")
            .order_by("-created_at")
        )

        scope = (scope or "").lower().strip()

        if scope == "my":
            qs = qs.filter(user=request.user)

            data = [
                {
                    "id": c.id,
                    "group_id": c.group_id,
                    "user_id": c.user_id,
                    "amount": str(c.amount),
                    "source": getattr(c, "source", None),
                    "reference": c.reference,
                    "note": c.note,
                    "created_at": c.created_at,
                }
                for c in qs[:300]
            ]
            return Response(data, status=status.HTTP_200_OK)

        if scope == "all":
            require_group_admin(group_id, request.user)

            history = [
                {
                    "id": c.id,
                    "group_id": c.group_id,
                    "user_id": c.user_id,
                    "user_name": getattr(c.user, "username", "")
                    or getattr(c.user, "full_name", "")
                    or getattr(c.user, "name", ""),
                    "amount": str(c.amount),
                    "source": getattr(c, "source", None),
                    "reference": c.reference,
                    "note": c.note,
                    "created_at": c.created_at,
                }
                for c in qs[:300]
            ]

            per_member_totals = list(
                GroupMemberShare.objects.filter(group_id=group_id)
                .values("user_id")
                .annotate(total_amount=Sum("total_contributed"))
                .order_by("-total_amount")
            )

            formatted_member_totals = [
                {
                    "user_id": row["user_id"],
                    "total_contributed": str(row["total_amount"] or Decimal("0")),
                }
                for row in per_member_totals
            ]

            fund = GroupFund.objects.filter(group_id=group_id).first()
            fund_total = fund.balance if fund else Decimal("0")

            payload = {
                "group_id": group_id,
                "group_total_from_fund": str(fund_total),
                "per_member_totals": formatted_member_totals,
                "history": history,
            }

            if PaymentLedger is not None:
                group_ref = f"GROUP-{group_id}"

                ledger_member_totals = (
                    PaymentLedger.objects.filter(
                        category="GROUP",
                        entry_type="CREDIT",
                        reference=group_ref,
                    )
                    .values("user_id")
                    .annotate(total_amount=Sum("amount"))
                    .order_by("-total_amount")
                )

                ledger_group_total = (
                    PaymentLedger.objects.filter(
                        category="GROUP",
                        entry_type="CREDIT",
                        reference=group_ref,
                    ).aggregate(t=Sum("amount"))["t"]
                    or Decimal("0")
                )

                payload["ledger_reference"] = group_ref
                payload["group_total_confirmed"] = str(ledger_group_total)
                payload["per_member_totals_confirmed"] = [
                    {
                        "user_id": row["user_id"],
                        "total_contributed_confirmed": str(
                            row["total_amount"] or Decimal("0")
                        ),
                    }
                    for row in ledger_member_totals
                ]

            return Response(payload, status=status.HTTP_200_OK)

        raise ValidationError("Invalid scope. Use 'my' or 'all'.")