from pathlib import PurePosixPath
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter
from django.contrib.auth.models import Group
from rest_framework import serializers

from catalog.models import OrganizationMembership
from .models import KnowledgeSource


class SourceSerializer(serializers.ModelSerializer):
    allowed_user_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)
    allowed_group_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)

    class Meta:
        model = KnowledgeSource
        fields = ['id', 'name', 'kind', 'config', 'is_active', 'access', 'allowed_user_ids', 'allowed_group_ids', 'cron', 'timezone', 'last_run_at', 'next_run_at']
        read_only_fields = ['id', 'last_run_at', 'next_run_at']
        validators = []

    def validate(self, attrs):
        org = self.context['org']
        kind = attrs.get('kind', self.instance.kind if self.instance else 'upload')
        if self.instance and kind != self.instance.kind:
            raise serializers.ValidationError({'kind': 'Create a new source to change its kind.'})
        name = attrs.get('name', self.instance.name if self.instance else '')
        if not name.strip() or KnowledgeSource.objects.filter(organization=org, name=name).exclude(pk=self.instance.pk if self.instance else None).exists():
            raise serializers.ValidationError({'name': 'Use a unique, nonempty source name.'})
        config = attrs.get('config', self.instance.config if self.instance else {})
        if not isinstance(config, dict):
            raise serializers.ValidationError({'config': 'Expected an object.'})
        if kind == 'snapshot':
            path = config.get('path', '')
            if set(config) - {'path'} or not isinstance(path, str) or not path or '\\' in path or ':' in path or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
                raise serializers.ValidationError({'config': 'Use a relative manifest path beneath the service import directory.'})
        elif config:
            raise serializers.ValidationError({'config': 'Upload sources do not accept connector credentials or paths.'})
        cron = attrs.get('cron', self.instance.cron if self.instance else '')
        if cron and (len(cron.split()) != 5 or not croniter.is_valid(cron)):
            raise serializers.ValidationError({'cron': 'Use a valid five-field cron expression.'})
        try:
            ZoneInfo(attrs.get('timezone', self.instance.timezone if self.instance else 'UTC'))
        except (ValueError, ZoneInfoNotFoundError):
            raise serializers.ValidationError({'timezone': 'Use a valid IANA timezone.'})
        if 'allowed_user_ids' in attrs:
            ids = set(attrs['allowed_user_ids'])
            if ids != set(OrganizationMembership.objects.filter(organization=org, user_id__in=ids).values_list('user_id', flat=True)):
                raise serializers.ValidationError({'allowed_user_ids': 'Every reader must belong to this organization.'})
        if 'allowed_group_ids' in attrs:
            ids = set(attrs['allowed_group_ids'])
            if ids != set(Group.objects.filter(id__in=ids, name__in=['Company', 'Analytics']).values_list('id', flat=True)):
                raise serializers.ValidationError({'allowed_group_ids': 'Choose an existing application access group.'})
        return attrs

    def create(self, validated_data):
        users = validated_data.pop('allowed_user_ids', [self.context['request'].user.pk])
        groups = validated_data.pop('allowed_group_ids', [])
        source = KnowledgeSource.objects.create(organization=self.context['org'], **validated_data)
        source.allowed_users.set(users)
        source.allowed_groups.set(groups)
        return source

    def update(self, instance, validated_data):
        users = validated_data.pop('allowed_user_ids', None)
        groups = validated_data.pop('allowed_group_ids', None)
        instance = super().update(instance, validated_data)
        if users is not None:
            instance.allowed_users.set(users)
        if groups is not None:
            instance.allowed_groups.set(groups)
        return instance

    def to_representation(self, instance):
        result = super().to_representation(instance)
        # .all() uses the list endpoint's prefetch cache; values_list() bypasses it.
        result['allowed_user_ids'] = [user.pk for user in instance.allowed_users.all()]
        result['allowed_group_ids'] = [group.pk for group in instance.allowed_groups.all()]
        return result
