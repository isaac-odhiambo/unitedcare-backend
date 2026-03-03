from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from .models import Group, GroupMembership
from .serializers import GroupSerializer, GroupMembershipSerializer
from .permissions import IsAdmin


class GroupViewSet(viewsets.ModelViewSet):
    queryset = Group.objects.all()
    serializer_class = GroupSerializer

    def get_permissions(self):
        # Only admin can create group
        if self.action == "create":
            return [IsAdmin()]
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

        # Only group admin or system admin can add members
        user = request.user
        is_system_admin = (
            user.is_authenticated
            and user.is_active
            and user.status != "blocked"
            and user.role == "admin"
        )

        if not is_system_admin:
            return Response(
                {"detail": "Only admin can add members."},
                status=status.HTTP_403_FORBIDDEN
            )

        return super().create(request, *args, **kwargs)
from django.shortcuts import render


