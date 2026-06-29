"""
Tests for the governance CSV round-trip (export + import) on the Data
Dictionary. Governance lives on ItemGroup, so the CSV is one row per group,
matched back by group_pk (then group_id).
"""
import csv
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from catalog.models import (
    Category, DataPerson, Department, Item, ItemGroup,
    Organization, OrganizationMembership, CustomUser,
)

EXPORT_URL = '/api/governance/export-csv/'
IMPORT_URL = '/api/governance/import-csv/'


@pytest.fixture
def org(db):
    return Organization.objects.create(name='Gov Org')


@pytest.fixture
def member(db, org):
    u = CustomUser.objects.create_user(
        username='govuser', email='gov@example.com', password='pw')
    OrganizationMembership.objects.create(user=u, organization=org)
    return u


@pytest.fixture
def client(member):
    c = APIClient()
    c.force_authenticate(user=member)
    return c


@pytest.fixture
def people(db, org):
    alice = DataPerson.objects.create(name='Alice', is_owner=True, organization=org)
    bob = DataPerson.objects.create(name='Bob', is_steward=True, organization=org)
    return alice, bob


@pytest.fixture
def group(db, org):
    """A measure group with one item and no governance set yet."""
    g = ItemGroup.objects.create(
        group_key='gov::test', kind=ItemGroup.KIND_MEASURE_NAME, organization=org)
    Item.objects.create(
        item_id='m1', item_name='Revenue', item_type='PB_MEASURE',
        service='powerbi', organization=org, item_group=g)
    return g


def _read_csv(response):
    raw = b''.join(response.streaming_content)
    body = raw.decode('utf-8-sig')  # strips the leading BOM
    return list(csv.DictReader(io.StringIO(body))), raw


def _upload(client, rows, header=None):
    header = header or [
        'group_pk', 'group_id', 'kind', 'name', 'service', 'item_type',
        'status', 'owner', 'steward', 'department', 'category',
        'custom_description',
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, '') for k in header})
    f = SimpleUploadedFile('gov.csv', buf.getvalue().encode('utf-8'), 'text/csv')
    return client.post(IMPORT_URL, {'file': f})


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #

def test_export_requires_auth(db):
    assert APIClient().get(EXPORT_URL).status_code == 401


def test_export_headers_and_one_row_per_group(client, group, people):
    alice, bob = people
    group.ownership_person = alice
    group.steward = bob
    group.status = 'VERIFIED'
    group.custom_description = 'curated'
    group.save()

    resp = client.get(EXPORT_URL)
    assert resp.status_code == 200
    assert resp['Content-Type'].startswith('text/csv')
    assert 'attachment; filename="governance_' in resp['Content-Disposition']

    rows, raw = _read_csv(resp)
    assert raw.startswith(b'\xef\xbb\xbf')  # UTF-8 BOM so Excel opens it right
    assert len(rows) == 1
    r = rows[0]
    assert r['group_pk'] == str(group.id)
    assert r['group_id'] == 'gov::test'
    assert r['kind'] == 'measure_name'
    assert r['name'] == 'Revenue'
    assert r['service'] == 'powerbi'
    assert r['status'] == 'VERIFIED'
    assert r['owner'] == 'Alice'
    assert r['steward'] == 'Bob'
    assert r['custom_description'] == 'curated'


def test_export_is_org_scoped(client, group, db):
    other = Organization.objects.create(name='Other Org')
    ItemGroup.objects.create(group_key='other::x', organization=other)
    rows, _ = _read_csv(client.get(EXPORT_URL))
    keys = {r['group_id'] for r in rows}
    assert 'gov::test' in keys
    assert 'other::x' not in keys


# --------------------------------------------------------------------------- #
# Import — matching
# --------------------------------------------------------------------------- #

def test_import_requires_auth(db):
    assert APIClient().post(IMPORT_URL).status_code == 401


def test_import_rejects_missing_file(client):
    assert client.post(IMPORT_URL).status_code == 400


def test_import_rejects_csv_without_key_column(client):
    f = SimpleUploadedFile('x.csv', b'foo,bar\n1,2\n', 'text/csv')
    assert client.post(IMPORT_URL, {'file': f}).status_code == 400


def test_match_by_group_pk(client, group, people):
    alice, _ = people
    resp = _upload(client, [{'group_pk': str(group.id), 'owner': 'Alice'}])
    assert resp.status_code == 200
    assert resp.json()['updated'] == 1
    group.refresh_from_db()
    assert group.ownership_person_id == alice.id


def test_match_by_group_id_fallback(client, group, people):
    alice, _ = people
    # group_pk blank -> fall back to group_id (group_key)
    resp = _upload(client, [{'group_pk': '', 'group_id': 'gov::test', 'owner': 'Alice'}])
    assert resp.json()['updated'] == 1
    group.refresh_from_db()
    assert group.ownership_person_id == alice.id


def test_unmatched_group_is_reported_not_created(client, group):
    resp = _upload(client, [{'group_pk': '', 'group_id': 'does::not::exist', 'owner': 'X'}])
    data = resp.json()
    assert data['updated'] == 0
    assert data['skipped_no_match'] and data['skipped_no_match'][0]['group_id'] == 'does::not::exist'
    assert ItemGroup.objects.count() == 1


# --------------------------------------------------------------------------- #
# Import — field application & rules
# --------------------------------------------------------------------------- #

def test_full_happy_path(client, group, people, org):
    alice, bob = people
    dept = Department.objects.create(name='Finance', organization=org)
    cat = Category.objects.create(name='KPI', organization=org)
    resp = _upload(client, [{
        'group_pk': str(group.id), 'status': 'verified', 'owner': 'Alice',
        'steward': 'Bob', 'department': 'Finance', 'category': 'KPI',
        'custom_description': 'hello',
    }])
    assert resp.json()['updated'] == 1
    group.refresh_from_db()
    assert group.status == 'VERIFIED'  # case-insensitive
    assert group.ownership_person_id == alice.id
    assert group.steward_id == bob.id
    assert group.ownership_department_id == dept.id
    assert group.category_id == cat.id
    assert group.custom_description == 'hello'


def test_empty_cells_leave_values_unchanged(client, group, people):
    alice, _ = people
    group.ownership_person = alice
    group.status = 'VERIFIED'
    group.save()
    # All editable cells blank -> nothing changes, nothing saved.
    resp = _upload(client, [{'group_pk': str(group.id)}])
    assert resp.json()['updated'] == 0
    group.refresh_from_db()
    assert group.ownership_person_id == alice.id
    assert group.status == 'VERIFIED'


def test_unknown_name_skipped_and_reported(client, group):
    resp = _upload(client, [{'group_pk': str(group.id), 'owner': 'Ghost'}])
    data = resp.json()
    assert data['updated'] == 0
    assert data['unmatched_values'][0]['field'] == 'owner'
    assert data['unmatched_values'][0]['value'] == 'Ghost'
    group.refresh_from_db()
    assert group.ownership_person_id is None
    assert DataPerson.objects.filter(name='Ghost').count() == 0  # not auto-created


def test_invalid_status_reported(client, group):
    resp = _upload(client, [{'group_pk': str(group.id), 'status': 'BOGUS'}])
    data = resp.json()
    assert data['updated'] == 0
    assert data['invalid_status'][0]['value'] == 'BOGUS'
    group.refresh_from_db()
    assert group.status == 'UNVERIFIED'


def test_ambiguous_name_reported(client, group, org):
    DataPerson.objects.create(name='Twin', is_owner=True, organization=org)
    DataPerson.objects.create(name='Twin', is_owner=True, organization=org)
    resp = _upload(client, [{'group_pk': str(group.id), 'owner': 'Twin'}])
    data = resp.json()
    assert data['updated'] == 0
    assert data['ambiguous'][0]['field'] == 'owner'
    group.refresh_from_db()
    assert group.ownership_person_id is None


def test_context_columns_are_ignored(client, group, people):
    alice, _ = people
    resp = _upload(client, [{
        'group_pk': str(group.id), 'kind': 'singleton', 'name': 'HACKED',
        'service': 'evil', 'item_type': 'x', 'owner': 'Alice',
    }])
    assert resp.json()['updated'] == 1
    group.refresh_from_db()
    assert group.kind == ItemGroup.KIND_MEASURE_NAME  # unchanged
    assert group.ownership_person_id == alice.id


def test_round_trip(client, group, people, org):
    """Export -> edit one row -> import -> change applied, format preserved."""
    alice, _ = people
    Department.objects.create(name='Ops', organization=org)
    rows, _ = _read_csv(client.get(EXPORT_URL))
    assert len(rows) == 1
    rows[0]['owner'] = 'Alice'
    rows[0]['department'] = 'Ops'
    resp = _upload(client, rows)
    assert resp.json()['updated'] == 1
    group.refresh_from_db()
    assert group.ownership_person_id == alice.id
    assert group.ownership_department.name == 'Ops'
