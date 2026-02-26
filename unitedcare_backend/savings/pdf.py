from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from .models import SavingsAccount, SavingsTransaction


def build_statement_pdf(account: SavingsAccount) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    y = height - 60
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, y, "SAVINGS STATEMENT")
    y -= 25

    c.setFont("Helvetica", 11)
    c.drawString(50, y, f"Member: {account.user}")
    y -= 18
    c.drawString(50, y, f"Account: {account.name} ({account.account_type})")
    y -= 18
    c.drawString(50, y, f"Current Balance: {account.balance}")
    y -= 30

    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, y, "Date")
    c.drawString(160, y, "Type")
    c.drawString(260, y, "Amount")
    c.drawString(360, y, "Reference")
    y -= 15

    c.setFont("Helvetica", 10)

    txns = SavingsTransaction.objects.filter(account=account).order_by("-created_at")[:200]
    for t in txns:
        if y < 80:
            c.showPage()
            y = height - 60
            c.setFont("Helvetica", 10)

        c.drawString(50, y, t.created_at.strftime("%Y-%m-%d %H:%M"))
        c.drawString(160, y, t.txn_type)
        c.drawString(260, y, str(t.amount))
        c.drawString(360, y, (t.reference or "")[:25])
        y -= 14

    c.showPage()
    c.save()
    pdf = buffer.getvalue()
    buffer.close()
    return pdf