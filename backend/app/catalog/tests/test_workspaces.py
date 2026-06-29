"""Tests for the per-source workspace resolver and the chatbot/lineage
integration points that consume it.
"""
import json
import pytest

from catalog.models import Item
from catalog.services.workspaces import (
    get_workspaces_for_source,
    resolve_default_workspace,
    resolve_default_workspaces_for_org,
)


def _mk_item(source, org, **kw):
    defaults = dict(
        item_id=kw.pop('item_id'),
        item_type='PB_COLUMN',
        item_name='item',
        deleted=False,
        service='powerbi',
        organization=org,
        integration_source=source,
    )
    defaults.update(kw)
    return Item.objects.create(**defaults)


@pytest.mark.django_db
class TestGetWorkspacesForSource:
    def test_returns_empty_when_source_has_no_items(self, source):
        assert get_workspaces_for_source(source) == []

    def test_collects_distinct_workspaces_sorted_by_name(self, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-z', workspace_name='Zebra')
        _mk_item(source, org, item_id='b', workspace_id='ws-a', workspace_name='Alpha')
        _mk_item(source, org, item_id='c', workspace_id='ws-a', workspace_name='Alpha')
        result = get_workspaces_for_source(source)
        assert [w['id'] for w in result] == ['ws-a', 'ws-z']
        assert [w['name'] for w in result] == ['Alpha', 'Zebra']

    def test_skips_blank_workspace_ids(self, source, org):
        _mk_item(source, org, item_id='a', workspace_id='', workspace_name='Skip')
        _mk_item(source, org, item_id='b', workspace_id='ws-1', workspace_name='Keep')
        result = get_workspaces_for_source(source)
        assert [w['id'] for w in result] == ['ws-1']

    def test_excludes_deleted_items(self, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-1', workspace_name='Live')
        _mk_item(source, org, item_id='b', workspace_id='ws-2', workspace_name='Gone', deleted=True)
        result = get_workspaces_for_source(source)
        assert [w['id'] for w in result] == ['ws-1']

    def test_uses_pb_workspace_items_when_present(self, source, org):
        # Many child items share the same workspace_id — should NOT cause dupes.
        _mk_item(source, org, item_id='ws-row', item_type='PB_WORKSPACE',
                 workspace_id='ws-1', workspace_name='Alpha')
        for i in range(5):
            _mk_item(source, org, item_id=f'col-{i}', item_type='PB_COLUMN',
                     workspace_id='ws-1', workspace_name='Alpha')
        result = get_workspaces_for_source(source)
        assert result == [{'id': 'ws-1', 'name': 'Alpha'}]

    def test_pb_workspace_path_ignores_inconsistent_child_names(self, source, org):
        # If child items disagree on workspace_name (e.g. legacy data) the
        # PB_WORKSPACE record is the source of truth.
        _mk_item(source, org, item_id='ws-row', item_type='PB_WORKSPACE',
                 workspace_id='ws-1', workspace_name='Canonical')
        _mk_item(source, org, item_id='c1', item_type='PB_COLUMN',
                 workspace_id='ws-1', workspace_name='legacy spelling')
        result = get_workspaces_for_source(source)
        assert result == [{'id': 'ws-1', 'name': 'Canonical'}]

    def test_falls_back_when_no_pb_workspace_rows(self, source, org):
        # No PB_WORKSPACE item: the fallback distinct query takes over.
        _mk_item(source, org, item_id='c1', item_type='PB_COLUMN',
                 workspace_id='ws-1', workspace_name='Alpha')
        _mk_item(source, org, item_id='c2', item_type='PB_COLUMN',
                 workspace_id='ws-1', workspace_name='Alpha')
        result = get_workspaces_for_source(source)
        assert result == [{'id': 'ws-1', 'name': 'Alpha'}]


@pytest.mark.django_db
class TestResolveDefaultWorkspace:
    def test_returns_none_when_no_workspaces(self, user, source):
        assert resolve_default_workspace(user, source) is None

    def test_auto_picks_when_only_one_workspace(self, user, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-only', workspace_name='Only')
        # Even when user has a saved default that doesn't match, the single
        # workspace wins (user can't pick a workspace that doesn't exist).
        user.default_workspaces = {str(source.id): 'something-else'}
        user.save()
        assert resolve_default_workspace(user, source) == 'ws-only'

    def test_uses_user_default_when_multiple_and_match(self, user, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-1', workspace_name='One')
        _mk_item(source, org, item_id='b', workspace_id='ws-2', workspace_name='Two')
        user.default_workspaces = {str(source.id): 'ws-2'}
        user.save()
        assert resolve_default_workspace(user, source) == 'ws-2'

    def test_falls_back_to_org_default_when_user_has_none(self, user, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-1', workspace_name='One')
        _mk_item(source, org, item_id='b', workspace_id='ws-2', workspace_name='Two')
        source.default_workspace_id = 'ws-2'
        source.save()
        assert resolve_default_workspace(user, source) == 'ws-2'

    def test_user_default_overrides_org_default(self, user, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-1', workspace_name='One')
        _mk_item(source, org, item_id='b', workspace_id='ws-2', workspace_name='Two')
        source.default_workspace_id = 'ws-1'
        source.save()
        user.default_workspaces = {str(source.id): 'ws-2'}
        user.save()
        assert resolve_default_workspace(user, source) == 'ws-2'

    def test_returns_none_when_multiple_and_no_default_set(self, user, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-1', workspace_name='One')
        _mk_item(source, org, item_id='b', workspace_id='ws-2', workspace_name='Two')
        assert resolve_default_workspace(user, source) is None

    def test_ignores_user_default_that_is_no_longer_valid(self, user, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-1', workspace_name='One')
        _mk_item(source, org, item_id='b', workspace_id='ws-2', workspace_name='Two')
        user.default_workspaces = {str(source.id): 'ws-deleted'}
        user.save()
        assert resolve_default_workspace(user, source) is None


@pytest.mark.django_db
class TestResolveForOrg:
    def test_skips_sources_with_no_workspaces(self, user, source, org):
        out = resolve_default_workspaces_for_org(user, org)
        assert out == []

    def test_returns_one_entry_per_active_source(self, user, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-1', workspace_name='One')
        _mk_item(source, org, item_id='b', workspace_id='ws-2', workspace_name='Two')
        user.default_workspaces = {str(source.id): 'ws-1'}
        user.save()
        out = resolve_default_workspaces_for_org(user, org)
        assert len(out) == 1
        entry = out[0]
        assert entry['source_id'] == source.id
        assert entry['workspace_id'] == 'ws-1'
        assert entry['workspace_name'] == 'One'
        assert entry['workspace_count'] == 2


@pytest.mark.django_db
class TestChatbotPromptInjection:
    """The system prompt must change shape based on workspace_scope so the
    LLM behaves differently in single-workspace vs ambiguous scenarios."""

    def _build_prompt(self, scope):
        # Avoid pulling in the LLM client by stubbing get_agent; we only need
        # to inspect the prompt string the function would build. Re-implement
        # the build logic by calling get_agent and capturing the system_prompt
        # via Agent constructor monkey-patch.
        from catalog import tools as tools_mod
        captured = {}
        original = tools_mod.Agent

        class FakeAgent:
            def __init__(self, *args, system_prompt=None, **kwargs):
                captured['system_prompt'] = system_prompt
            def tool_plain(self, *_a, **_k):
                pass

        tools_mod.Agent = FakeAgent
        try:
            tools_mod.get_agent(workspace_scope=scope)
        finally:
            tools_mod.Agent = original
        return captured['system_prompt']

    def test_no_scope_omits_workspace_block(self):
        prompt = self._build_prompt(None)
        assert 'Workspace scope' not in prompt

    def test_single_workspace_says_no_need_to_mention(self):
        scope = [{
            'source_name': 'PowerBI', 'workspace_id': 'ws-1',
            'workspace_name': 'Only', 'workspace_count': 1,
        }]
        prompt = self._build_prompt(scope)
        assert 'Workspace scope' in prompt
        assert 'workspace_id="ws-1"' in prompt
        assert 'No need to mention it' in prompt

    def test_multi_workspace_with_default_instructs_to_announce(self):
        scope = [{
            'source_name': 'PowerBI', 'workspace_id': 'ws-2',
            'workspace_name': 'Two', 'workspace_count': 3,
        }]
        prompt = self._build_prompt(scope)
        assert 'mention the workspace by name' in prompt
        assert '**Two**' in prompt
        assert 'workspace_id="ws-2"' in prompt

    def test_multi_workspace_no_default_instructs_to_ask(self):
        scope = [{
            'source_name': 'PowerBI', 'workspace_id': None,
            'workspace_name': None, 'workspace_count': 3,
        }]
        prompt = self._build_prompt(scope)
        assert 'ASK the user' in prompt

    def test_cross_workspace_rule_is_present(self):
        scope = [{
            'source_name': 'PowerBI', 'workspace_id': 'ws-1',
            'workspace_name': 'One', 'workspace_count': 2,
        }]
        prompt = self._build_prompt(scope)
        assert 'Cross-workspace queries' in prompt
        assert 'EXPLICITLY' in prompt


@pytest.mark.django_db
class TestUserSettingsWorkspacesAPI:
    """POSTing per-source defaults to /api/me/workspaces/ persists onto
    CustomUser.default_workspaces (ported from the removed User Settings page)."""

    def test_post_update_workspaces_saves_defaults(self, client, rw_user, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-1', workspace_name='One')
        _mk_item(source, org, item_id='b', workspace_id='ws-2', workspace_name='Two')
        client.force_login(rw_user)
        resp = client.post(
            '/api/me/workspaces/',
            data=json.dumps({'defaults': {str(source.id): 'ws-2'}}),
            content_type='application/json',
        )
        assert resp.status_code == 200
        rw_user.refresh_from_db()
        assert rw_user.default_workspaces == {str(source.id): 'ws-2'}

    def test_blank_value_clears_default(self, client, rw_user, source, org):
        _mk_item(source, org, item_id='a', workspace_id='ws-1', workspace_name='One')
        _mk_item(source, org, item_id='b', workspace_id='ws-2', workspace_name='Two')
        rw_user.default_workspaces = {str(source.id): 'ws-2'}
        rw_user.save()
        client.force_login(rw_user)
        client.post(
            '/api/me/workspaces/',
            data=json.dumps({'defaults': {str(source.id): ''}}),
            content_type='application/json',
        )
        rw_user.refresh_from_db()
        assert rw_user.default_workspaces == {}


@pytest.mark.django_db
class TestLineageNodeEnrichment:
    """get_network must surface workspace_id / workspace_name / parent for
    catalog-resident nodes so the frontend can filter and prefix labels."""

    def test_node_payload_includes_workspace_and_parent_for_columns(self, client, rw_user, source, org):
        # Create matching Item + NetworkNode rows. node_id is "TYPE::hash"
        # while item_id is just the hash.
        from catalog.models import NetworkNode
        Item.objects.create(
            item_id='hash-col-1', item_type='PB_COLUMN', item_name='is_valid',
            service='powerbi', organization=org, integration_source=source,
            workspace_id='ws-1', workspace_name='One', table_name='dim_user',
        )
        NetworkNode.objects.create(
            node_id='PB_COLUMN::hash-col-1', name='is_valid',
            group='PB_COLUMN', organization=org,
        )
        client.force_login(rw_user)
        resp = client.get('/api/network/?node_id=ALL')
        assert resp.status_code == 200
        nodes = resp.json()['nodes']
        col = next(n for n in nodes if n['id'] == 'PB_COLUMN::hash-col-1')
        assert col['workspace_id'] == 'ws-1'
        assert col['workspace_name'] == 'One'
        # Parent should be the table for column-type nodes (so the UI can show
        # "dim_user.is_valid" instead of bare "is_valid").
        assert col['parent'] == 'dim_user'

    # The classic /lineage/ page (which embedded window.initialWorkspaceId /
    # window.workspaceOptions into HTML) was removed — React reads the same data
    # from /api/network/ + /api/items/. Those page-render tests were dropped.


@pytest.mark.django_db
class TestPathWorkspaceConstraint:
    """find_shortest_path must block paths that cross between two PowerBI
    workspaces of the same source while still allowing PowerBI ↔ dbt hops."""

    def _make_pb_node(self, item_id, workspace_id, name='n', org=None):
        from catalog.models import NetworkNode
        Item.objects.create(
            item_id=item_id, item_type='PB_TABLE', item_name=name,
            service='powerbi', deleted=False, organization=org,
            workspace_id=workspace_id, workspace_name=workspace_id,
        )
        NetworkNode.objects.create(
            node_id=f'PB_TABLE::{item_id}', name=name, group='PB_TABLE',
            organization=org,
        )
        return f'PB_TABLE::{item_id}'

    def _make_dbt_node(self, item_id, name='n', org=None):
        from catalog.models import NetworkNode
        Item.objects.create(
            item_id=item_id, item_type='DBT_MODEL', item_name=name,
            service='dbt', deleted=False, organization=org,
        )
        NetworkNode.objects.create(
            node_id=f'DBT_MODEL::{item_id}', name=name, group='DBT_MODEL',
            organization=org,
        )
        return f'DBT_MODEL::{item_id}'

    def _edge(self, src, tgt, org):
        from catalog.models import NetworkEdge
        NetworkEdge.objects.create(source=src, target=tgt, organization=org)

    def test_path_blocks_node_in_other_workspace(self, org):
        from catalog.services.network_path import find_shortest_path
        a = self._make_pb_node('a', 'ws-1', org=org)
        b = self._make_pb_node('b', 'ws-2', org=org)  # bridge that violates constraint
        c = self._make_pb_node('c', 'ws-1', org=org)
        self._edge(a, b, org)
        self._edge(b, c, org)
        # Without the constraint, there's a 2-hop path a → b → c.
        result = find_shortest_path(a, c, max_depth=4)
        assert result.found is True
        # With workspace=ws-1, b is blocked → no path exists at all.
        result = find_shortest_path(a, c, max_depth=4, workspace_id='ws-1')
        assert result.found is False

    def test_path_allows_dbt_bridge_between_workspaces(self, org):
        from catalog.services.network_path import find_shortest_path
        # PowerBI in ws-1 → dbt model → PowerBI in ws-1 again. dbt is a different
        # service so it should never be blocked even when a workspace filter is
        # active.
        a = self._make_pb_node('a', 'ws-1', org=org)
        d = self._make_dbt_node('d', org=org)
        c = self._make_pb_node('c', 'ws-1', org=org)
        self._edge(a, d, org)
        self._edge(d, c, org)
        result = find_shortest_path(a, c, max_depth=4, workspace_id='ws-1')
        assert result.found is True
        # The dbt node MUST appear in the rendered path.
        assert any(n.id == d for n in result.nodes)

    def test_path_exempts_source_and_target_from_block(self, org):
        from catalog.services.network_path import find_shortest_path
        # When target itself is in another workspace, picking that workspace
        # shouldn't sever the path — the user explicitly chose the target.
        a = self._make_pb_node('a', 'ws-1', org=org)
        b = self._make_dbt_node('b', org=org)  # dbt bridge
        c = self._make_pb_node('c', 'ws-2', org=org)  # target in different ws
        self._edge(a, b, org)
        self._edge(b, c, org)
        result = find_shortest_path(a, c, max_depth=4, workspace_id='ws-1')
        assert result.found is True

    def test_no_workspace_filter_means_no_block(self, org):
        from catalog.services.network_path import find_shortest_path
        a = self._make_pb_node('a', 'ws-1', org=org)
        b = self._make_pb_node('b', 'ws-2', org=org)
        self._edge(a, b, org)
        result = find_shortest_path(a, b, max_depth=2)
        assert result.found is True


@pytest.mark.django_db
class TestGetFiltersDedup:
    """``/api/filters/`` returns distinct workspace/dataset/table names. The
    earlier implementation accidentally inflated counts because Django's
    default Meta.ordering on Item leaked ``item_name`` into the SELECT, which
    turned DISTINCT into "distinct (item_name, X)" pairs."""

    def test_workspace_names_are_truly_distinct(self, client, rw_user, org):
        # 100 PB_COLUMN items all point at the same workspace_name. The
        # response must include "Sales" exactly once.
        for i in range(100):
            Item.objects.create(
                item_id=f'col-{i}', item_type='PB_COLUMN',
                item_name=f'col_{i}', service='powerbi',
                organization=org, deleted=False,
                workspace_id='ws-1', workspace_name='Sales',
            )
        client.force_login(rw_user)
        resp = client.get('/api/filters/')
        assert resp.status_code == 200
        data = resp.json()
        assert data['workspaces'].count('Sales') == 1
        assert data['workspaces'] == ['Sales']

    def test_distinct_scales_across_dimensions(self, client, rw_user, org):
        # Two workspaces, three datasets, four tables — every duplicated 50x.
        for i in range(50):
            Item.objects.create(
                item_id=f'a-{i}', item_type='PB_COLUMN', item_name=f'n_{i}',
                service='powerbi', organization=org, deleted=False,
                workspace_id='w1', workspace_name='WS A',
                dataset_name='DS1', table_name='T1',
            )
            Item.objects.create(
                item_id=f'b-{i}', item_type='PB_COLUMN', item_name=f'n_{i}',
                service='powerbi', organization=org, deleted=False,
                workspace_id='w2', workspace_name='WS B',
                dataset_name='DS2', table_name='T2',
            )
        client.force_login(rw_user)
        resp = client.get('/api/filters/')
        data = resp.json()
        assert sorted(data['workspaces']) == ['WS A', 'WS B']
        assert sorted(data['datasets']) == ['DS1', 'DS2']
        assert sorted(data['tables']) == ['T1', 'T2']


@pytest.mark.django_db
class TestUserDefaultWorkspaceName:
    """``get_user_default_workspace_name`` resolves a single name string for
    the catalog list pages to pre-select."""

    def test_returns_empty_when_no_default_set(self, user, source, org):
        from catalog.services.workspaces import get_user_default_workspace_name
        # Multiple workspaces, no user/org default → no pre-selection
        Item.objects.create(item_id='w1', item_type='PB_WORKSPACE', service='powerbi',
                            deleted=False, organization=org, integration_source=source,
                            workspace_id='ws-1', workspace_name='Alpha')
        Item.objects.create(item_id='w2', item_type='PB_WORKSPACE', service='powerbi',
                            deleted=False, organization=org, integration_source=source,
                            workspace_id='ws-2', workspace_name='Beta')
        assert get_user_default_workspace_name(user, org) == ''

    def test_returns_user_default_name(self, user, source, org):
        from catalog.services.workspaces import get_user_default_workspace_name
        Item.objects.create(item_id='w1', item_type='PB_WORKSPACE', service='powerbi',
                            deleted=False, organization=org, integration_source=source,
                            workspace_id='ws-1', workspace_name='Alpha')
        Item.objects.create(item_id='w2', item_type='PB_WORKSPACE', service='powerbi',
                            deleted=False, organization=org, integration_source=source,
                            workspace_id='ws-2', workspace_name='Beta')
        user.default_workspaces = {str(source.id): 'ws-2'}
        user.save()
        assert get_user_default_workspace_name(user, org) == 'Beta'

    def test_returns_single_workspace_name_automatically(self, user, source, org):
        from catalog.services.workspaces import get_user_default_workspace_name
        Item.objects.create(item_id='w1', item_type='PB_WORKSPACE', service='powerbi',
                            deleted=False, organization=org, integration_source=source,
                            workspace_id='ws-only', workspace_name='Solo')
        assert get_user_default_workspace_name(user, org) == 'Solo'

    # test_context_processor_injects_value was removed: the workspace_defaults
    # template context processor + the classic pages it fed were deleted. The
    # underlying resolver (get_user_default_workspace_name) is still covered by
    # the tests above.
