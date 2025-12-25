from rest_framework import serializers
from .models import User, KYCProfile


class MemberRegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ("phone", "username", "id_number", "password")

    def validate(self, data):
        phone = data.get("phone")
        id_number = data.get("id_number")

        # 🔐 ID IS REQUIRED FOR MEMBERS
        if not id_number:
            raise serializers.ValidationError(
                {"id_number": "ID number is required for members."}
            )

        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError(
                {"phone": "Phone number already registered."}
            )

        if User.objects.filter(id_number=id_number).exists():
            raise serializers.ValidationError(
                {"id_number": "ID number already registered."}
            )

        return data

    def create(self, validated_data):
        user = User.objects.create_user(
            phone=validated_data["phone"],
            username=validated_data["username"],
            password=validated_data["password"],
        )

        # Assign member-only fields
        user.id_number = validated_data["id_number"]
        user.role = "member"
        user.status = "pending"
        user.save()

        return user


class KYCUploadSerializer(serializers.ModelSerializer):
    class Meta:
        model = KYCProfile
        fields = ("passport_photo", "id_front", "id_back")
