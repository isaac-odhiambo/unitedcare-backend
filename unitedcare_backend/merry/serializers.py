from rest_framework import serializers
from .models import MerryGoRound, Member, Contribution


class MerrySerializer(serializers.ModelSerializer):
    class Meta:
        model = MerryGoRound
        fields = "__all__"


class ContributionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contribution
        fields = "__all__"