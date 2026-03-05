# groups/views.py
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from .models import Group, GroupMembership
from .serializers import GroupSerializer, GroupMembershipSerializer
from .permissions import IsAdmin, IsSuperAdmin


class GroupViewSet(viewsets.ModelViewSet):
    queryset = Group.objects.all()
    serializer_class = GroupSerializer

    def get_permissions(self):
        # ✅ Only SUPER ADMIN can create group
        if self.action == "create":
            return [IsSuperAdmin()]
        return [IsAuthenticated()]

    def perform_create(self, serializer):
        serializer.save()


class GroupMembershipViewSet(viewsets.ModelViewSet):
    queryset = GroupMembership.objects.all()
    serializer_class = GroupMembershipSerializer
    permission_classes = [IsAuthenticated]

    def create(self, request, *args, **kwargs):
        group_id = request.data.get("group")

        if not group_id:
            return Response(
                {"detail": "Group ID is required."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # ✅ Keep your existing rule: only admin can add members
        # Admin = superuser OR staff OR role=admin (via user.is_admin)
        if not IsAdmin().has_permission(request, self):
            return Response(
                {"detail": "Only admin can add members."},
                status=status.HTTP_403_FORBIDDEN
            )

        return super().create(request, *args, **kwargs)