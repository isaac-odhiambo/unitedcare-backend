from django.http import HttpResponse
from rest_framework.views import APIView
from rest_framework import permissions
from rest_framework.exceptions import PermissionDenied

from .models import SavingsAccount
from .pdf import build_statement_pdf


class DownloadSavingsStatementPDF(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, account_id: int):
        try:
            account = SavingsAccount.objects.get(id=account_id)
        except SavingsAccount.DoesNotExist:
            raise PermissionDenied("Account not found.")

        if account.user != request.user:
            raise PermissionDenied("Not your account.")

        pdf_bytes = build_statement_pdf(account)

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="savings_statement_{account_id}.pdf"'
        return response