from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework import status

from .models import OTP, KYCProfile, User
from .serializers import MemberRegisterSerializer, KYCUploadSerializer


def send_otp(phone, code):
    # Replace with SMS provider later
    print(f"OTP for {phone}: {code}")


class MemberRegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = MemberRegisterSerializer(data=request.data)
        if serializer.is_valid():
            user = serializer.save()

            code = OTP.generate()
            OTP.objects.create(phone=user.phone, code=code)
            send_otp(user.phone, code)

            return Response(
                {"message": "Registration successful. OTP sent."},
                status=status.HTTP_201_CREATED
            )
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class VerifyOTPView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        phone = request.data.get("phone")
        code = request.data.get("code")

        try:
            otp = OTP.objects.filter(
                phone=phone, code=code, is_used=False
            ).latest("created_at")
        except OTP.DoesNotExist:
            return Response({"error": "Invalid OTP"}, status=400)

        if otp.is_expired():
            return Response({"error": "OTP expired"}, status=400)

        otp.is_used = True
        otp.save()

        return Response({"message": "Phone verified successfully"})


class KYCUploadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = KYCUploadSerializer(data=request.data)
        if serializer.is_valid():
            KYCProfile.objects.update_or_create(
                user=request.user,
                defaults={
                    **serializer.validated_data,
                    "status": "submitted",
                }
            )
            return Response({"message": "KYC submitted successfully"})
        return Response(serializer.errors, status=400)
