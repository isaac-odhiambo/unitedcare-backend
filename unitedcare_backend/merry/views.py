# merry/views.py

from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.utils import timezone
from .models import Contribution

# =======================================
# Payment initiation stub
# This exists only to satisfy the import in urls.py
# =======================================
@api_view(["POST"])
def pay_contribution(request):
    """
    Minimal stub for pay_contribution.
    You can later implement payment initiation here.
    """
    return Response({"message": "pay_contribution endpoint placeholder"})


# =======================================
# M-PESA callback
# =======================================
@api_view(["POST"])
def mpesa_callback(request):
    data = request.data

    print("M-PESA CALLBACK DATA:", data)

    try:
        callback = data["Body"]["stkCallback"]
        result_code = callback["ResultCode"]
        metadata = callback.get("CallbackMetadata", {}).get("Item", [])

        if result_code == 0:
            amount = None
            mpesa_receipt = None
            phone = None

            for item in metadata:
                if item["Name"] == "Amount":
                    amount = item["Value"]
                elif item["Name"] == "MpesaReceiptNumber":
                    mpesa_receipt = item["Value"]
                elif item["Name"] == "PhoneNumber":
                    phone = item["Value"]

            # Use MerchantRequestID as contribution ID
            contribution_id = callback["MerchantRequestID"]

            # Update the contribution as paid
            try:
                contribution = Contribution.objects.get(id=contribution_id)
                contribution.paid = True
                contribution.paid_at = timezone.now()
                contribution.save()
                print("Payment successful")
            except Contribution.DoesNotExist:
                print(f"Contribution with id {contribution_id} not found")

        else:
            print("Payment failed")

    except Exception as e:
        print("Callback error:", str(e))

    return Response({"ResultCode": 0, "ResultDesc": "Accepted"})


# from rest_framework.decorators import api_view
# from rest_framework.response import Response
# from .models import Contribution
# from .mpesa import stk_push
# from .receipts import generate_receipt


# @api_view(["POST"])
# def pay_contribution(request):
#     contribution_id = request.data.get("contribution_id")
#     phone = request.data.get("phone")

#     contribution = Contribution.objects.get(id=contribution_id)

#     response = stk_push(
#         phone=phone,
#         amount=contribution.amount,
#         account_reference=str(contribution.id)
#     )

#     return Response(response)
