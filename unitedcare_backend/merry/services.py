import random
from .models import Member, Payout


def generate_payout_schedule(merry):
    members = list(merry.members.all())

    if merry.payout_order_type == "random":
        random.shuffle(members)

    for index, member in enumerate(members, start=1):
        member.payout_position = index
        member.save()

        Payout.objects.create(
            merry=merry,
            member=member,
            week_number=index,
            amount=merry.total_pool()
        )