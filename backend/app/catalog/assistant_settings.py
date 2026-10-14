from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .access import resolve_org
from .models import OrganizationMembership
from .services.assistant_tools import tool_settings, validate_overrides


@api_view(['GET', 'PATCH'])
@permission_classes([IsAuthenticated])
def assistant_tool_preferences(request):
    org = resolve_org(request.user)
    with transaction.atomic():
        membership = get_object_or_404(OrganizationMembership.objects.select_for_update().select_related('user'), organization=org, user=request.user)
        if request.method == 'PATCH':
            if not isinstance(request.data, dict) or set(request.data) != {'preferences'}:
                raise ValidationError('Only your own tool preferences can be updated here.')
            try:
                patch = validate_overrides(request.data['preferences'])
            except ValueError as exc:
                raise ValidationError(str(exc))
            membership.assistant_tool_preferences = {**membership.assistant_tool_preferences, **patch}
            membership.save(update_fields=['assistant_tool_preferences'])
        return Response({'tools': tool_settings(org, membership)})
