from django.contrib.auth.signals import (
    user_logged_in, user_logged_out, user_login_failed,
)
from django.dispatch import receiver

from .models import UserActivityLog


def _client_ip(request):
    if request is None:
        return None
    xff = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _user_agent(request):
    if request is None:
        return ''
    return (request.META.get('HTTP_USER_AGENT', '') or '')[:500]


@receiver(user_logged_in)
def on_user_logged_in(sender, request, user, **kwargs):
    UserActivityLog.objects.create(
        user=user,
        email=getattr(user, 'email', '') or '',
        event='login',
        path=request.path if request else '',
        method=(request.method or '') if request else '',
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )


@receiver(user_logged_out)
def on_user_logged_out(sender, request, user, **kwargs):
    UserActivityLog.objects.create(
        user=user,
        email=getattr(user, 'email', '') or '' if user else '',
        event='logout',
        path=request.path if request else '',
        method=(request.method or '') if request else '',
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )


@receiver(user_login_failed)
def on_user_login_failed(sender, credentials, request=None, **kwargs):
    # `credentials` may use either USERNAME_FIELD ('email') or 'username'.
    email = (credentials or {}).get('email') or (credentials or {}).get('username') or ''
    UserActivityLog.objects.create(
        user=None,
        email=email[:255],
        event='login_failed',
        path=request.path if request else '',
        method=(request.method or '') if request else '',
        ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
