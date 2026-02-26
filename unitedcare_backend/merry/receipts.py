from .models import Receipt


def generate_receipt(contribution):
    receipt = Receipt.objects.create(contribution=contribution)
    return receipt.receipt_number