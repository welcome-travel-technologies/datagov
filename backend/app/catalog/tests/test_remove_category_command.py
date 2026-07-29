from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.db import connection
from django.utils import timezone

from catalog.models import (
    Category,
    DataPerson,
    GovernanceTask,
    Item,
    ItemGroup,
    Organization,
    StatusChangeLog,
)


@pytest.fixture
def allow_legacy_category(db):
    """Temporarily model a database that still contains the legacy marker."""
    with connection.cursor() as cursor:
        cursor.execute(
            'ALTER TABLE "catalog_category" '
            'DROP CONSTRAINT "category_name_not_reserved"'
        )
    # pytest-django rolls the surrounding test transaction back, including
    # this transactional DDL, so the constraint is restored without issuing
    # ALTER TABLE while related FK trigger events are still pending.
    yield


def _group_with_item(
        org, key, category, steward, status, *, deleted_at=None):
    group = ItemGroup.objects.create(
        group_key=key,
        kind=ItemGroup.KIND_MEASURE_NAME,
        organization=org,
        category=category,
        steward=steward,
        status=status,
        deleted=False,
        deleted_at=deleted_at,
    )
    item = Item.objects.create(
        item_id=f'{key}-item',
        item_name=f'{key} item',
        item_type='PB_MEASURE',
        group_id=key,
        item_group=group,
        organization=org,
        status=status,
        deleted=False,
    )
    ItemGroup.objects.filter(pk=group.pk).update(primary_item=item)
    return group, item


@pytest.mark.django_db
def test_legacy_deletion_category_preview_and_apply_preserve_intent(
        allow_legacy_category):
    org = Organization.objects.create(name='Marker org')
    foreign_org = Organization.objects.create(name='Foreign org')
    steward = DataPerson.objects.create(
        name='Marker steward',
        organization=org,
        is_steward=True,
    )
    foreign_steward = DataPerson.objects.create(
        name='Foreign steward',
        organization=foreign_org,
        is_steward=True,
    )
    marker = Category.objects.create(
        name='  To Be Deleted  ',
        organization=org,
    )
    existing_deleted_at = timezone.now() - timedelta(days=20)
    unverified, unverified_item = _group_with_item(
        org,
        'marker-unverified',
        marker,
        steward,
        'UNVERIFIED',
    )
    attention, attention_item = _group_with_item(
        org,
        'marker-attention',
        marker,
        steward,
        'ATTENTION',
        deleted_at=existing_deleted_at,
    )
    already_deleted, deleted_item = _group_with_item(
        org,
        'marker-deleted',
        marker,
        steward,
        'DELETED',
    )
    foreign, foreign_item = _group_with_item(
        foreign_org,
        'foreign-marker-link',
        marker,
        foreign_steward,
        'UNVERIFIED',
    )

    stale_unverified = GovernanceTask.objects.create(
        organization=org,
        item_group=unverified,
        reason=GovernanceTask.REASON_UNVERIFIED,
        title='Verify marker measure',
    )
    stale_category = GovernanceTask.objects.create(
        organization=org,
        item_group=unverified,
        reason=GovernanceTask.REASON_NO_CATEGORY,
        title='Stale category work',
    )
    stale_attention = GovernanceTask.objects.create(
        organization=org,
        item_group=attention,
        reason=GovernanceTask.REASON_ATTENTION,
        title='Review marker attention',
    )

    preview = StringIO()
    call_command(
        'remove_category',
        'To Be Deleted',
        org=org.id,
        stdout=preview,
    )
    assert 'Deletion-marker conversion: 2 non-DELETED' in preview.getvalue()
    assert '1 cross-tenant category link(s) are untrusted' in preview.getvalue()
    assert Category.objects.filter(pk=marker.pk).exists()
    assert StatusChangeLog.objects.count() == 0
    for group, status in (
        (unverified, 'UNVERIFIED'),
        (attention, 'ATTENTION'),
        (already_deleted, 'DELETED'),
        (foreign, 'UNVERIFIED'),
    ):
        group.refresh_from_db()
        assert group.status == status
        assert group.category_id == marker.id

    applied = StringIO()
    call_command(
        'remove_category',
        'To Be Deleted',
        org=org.id,
        apply=True,
        stdout=applied,
    )
    assert 'Converted 2 ItemGroup(s) to DELETED' in applied.getvalue()
    assert not Category.objects.filter(pk=marker.pk).exists()

    for group, item in (
        (unverified, unverified_item),
        (attention, attention_item),
        (already_deleted, deleted_item),
    ):
        group.refresh_from_db()
        item.refresh_from_db()
        assert group.category_id is None
        assert group.status == 'DELETED'
        assert group.deleted is False
        assert group.deleted_at is not None
        assert item.status == 'DELETED'
        assert item.deleted is False
        assert item.deleted_at is None

    attention.refresh_from_db()
    assert attention.deleted_at == existing_deleted_at
    transitions = {
        row.item_group_id: row
        for row in StatusChangeLog.objects.filter(new_status='DELETED')
    }
    assert set(transitions) == {unverified.id, attention.id}
    assert transitions[unverified.id].old_status == 'UNVERIFIED'
    assert transitions[attention.id].old_status == 'ATTENTION'
    assert all(row.changed_by_id is None for row in transitions.values())

    # The foreign marker link is detached but cannot carry another tenant's
    # deletion intent.
    foreign.refresh_from_db()
    foreign_item.refresh_from_db()
    assert foreign.category_id is None
    assert foreign.status == 'UNVERIFIED'
    assert foreign.deleted_at is None
    assert foreign_item.status == 'UNVERIFIED'
    assert not StatusChangeLog.objects.filter(item_group=foreign).exists()

    for task in (stale_unverified, stale_category, stale_attention):
        task.refresh_from_db()
        assert task.state == GovernanceTask.STATE_DONE
        assert task.closed_reason == GovernanceTask.CLOSED_RESOLVED
    assert not GovernanceTask.objects.filter(
        item_group_id__in=[
            unverified.id,
            attention.id,
            already_deleted.id,
        ],
        reason=GovernanceTask.REASON_NO_CATEGORY,
        state=GovernanceTask.STATE_OPEN,
    ).exists()
    assert GovernanceTask.objects.filter(
        item_group_id__in=[
            unverified.id,
            attention.id,
            already_deleted.id,
        ],
        reason=GovernanceTask.REASON_DELETED,
        state=GovernanceTask.STATE_OPEN,
        assignee=steward,
        assignee_role='steward',
    ).count() == 3


@pytest.mark.django_db
def test_non_marker_category_removal_keeps_status_and_opens_category_work():
    org = Organization.objects.create(name='Ordinary category org')
    owner = DataPerson.objects.create(
        name='Category owner',
        organization=org,
        is_owner=True,
    )
    category = Category.objects.create(name='Finance', organization=org)
    group, item = _group_with_item(
        org,
        'ordinary-category',
        category,
        owner,
        'UNVERIFIED',
    )
    group.ownership_person = owner
    group.save(update_fields=['ownership_person'])

    call_command(
        'remove_category',
        'Finance',
        org=org.id,
        apply=True,
        stdout=StringIO(),
    )

    group.refresh_from_db()
    item.refresh_from_db()
    assert group.category_id is None
    assert group.status == 'UNVERIFIED'
    assert group.deleted_at is None
    assert item.status == 'UNVERIFIED'
    assert not StatusChangeLog.objects.filter(item_group=group).exists()
    assert GovernanceTask.objects.filter(
        item_group=group,
        reason=GovernanceTask.REASON_NO_CATEGORY,
        state=GovernanceTask.STATE_OPEN,
        assignee=owner,
        assignee_role='owner',
    ).exists()
