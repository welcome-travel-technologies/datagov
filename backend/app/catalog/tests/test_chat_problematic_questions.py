"""Regression guards around the problematic-chat-questions fixture.

The fixture (``fixtures/problematic_chat_questions.json``) freezes real
production questions that used to make the assistant time out / loop on tool
calls. These tests run in CI **without** an LLM:

  • ``test_fixture_is_well_formed`` — the fixture stays valid and self-consistent.
  • ``test_where_is_used_tool_registered`` — the one-shot usage tool that
    replaced the ``get_lineage`` graph-walk must stay wired into every agent;
    losing it reintroduces the loops these questions trigger.

To actually *replay* the questions against the live agent, use the harness
(it needs Gemini + a prod-like catalog, so it is not part of CI):

    python manage.py replay_chat \
        --questions-file catalog/tests/fixtures/problematic_chat_questions.json
"""
import json
from pathlib import Path

FIXTURE = Path(__file__).parent / 'fixtures' / 'problematic_chat_questions.json'

_VALID_PATTERNS = {
    'where_used_graph_walk',
    'multi_metric_fanout',
    'measure_disambiguation',
    'tool_error',
    'high_loop_other',
}


def _load():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def test_fixture_is_well_formed():
    data = _load()
    questions = data['questions']

    assert questions, 'fixture has no questions'
    assert data['counts']['total'] == len(questions)

    seen_ids = set()
    for q in questions:
        for field in ('id', 'question', 'pattern', 'note', 'original'):
            assert q.get(field) not in (None, ''), f"{q.get('id')}: missing {field}"
        assert q['id'] not in seen_ids, f"duplicate id {q['id']}"
        seen_ids.add(q['id'])
        assert q['pattern'] in _VALID_PATTERNS, f"{q['id']}: bad pattern {q['pattern']}"
        # Every entry is here because it was problematic: an error or a big loop.
        orig = q['original']
        assert (orig.get('errors') or 0) > 0 or (orig.get('tool_calls') or 0) >= 15

    # The advertised per-pattern counts match the actual entries.
    from collections import Counter
    actual = Counter(q['pattern'] for q in questions)
    assert dict(actual) == data['counts']['by_pattern']


def test_element_profiler_tools_registered():
    """Each integration must register its element profiler. These all-in-one
    profilers (definition + ownership + stats + used-by graph) replaced the
    chained schema/where-used lookups and the hop-by-hop ``get_lineage`` walking
    behind the loop/timeout failures, so their presence is load-bearing.
    """
    from catalog.tools.assistant.powerbi import build_tools as pb_build_tools
    from catalog.tools.assistant.dbt import build_tools as dbt_build_tools

    pb_names = {t.__name__ for t in pb_build_tools(None, client=None)}
    dbt_names = {t.__name__ for t in dbt_build_tools(None, client=None)}
    assert 'get_pb_item_details' in pb_names
    assert 'get_dbt_item_details' in dbt_names
