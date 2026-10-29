from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil

import pytest

from docflow_lab.config import Settings
from docflow_lab.demo_v3 import run_demo
from docflow_lab.knowledge import KnowledgeStore
from docflow_lab.retrieval import RetrievalError, load_chunks
from docflow_lab.storage import Conflict
from docflow_lab.workflow import Engine


@pytest.fixture
def settings(tmp_path):
    shutil.copytree(Settings().knowledge_dir, tmp_path / 'knowledge')
    return Settings(root=tmp_path, data_dir=tmp_path / 'state')


@pytest.fixture
def engine(settings):
    service = Engine(settings)
    yield service
    service.close()


def stage(store, content='## DFX900\n设备接口DFX900重试间隔配置为15秒。', **kwargs):
    data = dict(document_id='custom-queue', title='队列维护手册', source='原创测试手册', filename='queue.md', content=content)
    return store.import_document(**{**data, **kwargs})


def test_real_lifecycle_demo_and_output_preservation(settings, tmp_path):
    output = tmp_path / 'demo'
    result = run_demo(settings, output)
    assert result['passed'] == 10 and result['model_requests'] == 0
    assert (output / 'demo.json').is_file()
    with pytest.raises(ValueError):
        run_demo(settings, output)


@pytest.mark.parametrize('change', [
    {'filename': '../secret.md'}, {'filename': 'D:\\file.md'}, {'filename': 'fake.pdf'},
    {'content': '  '}, {'content': 'binary\x00text'}, {'document_id': '../bad'},
    {'title': 'x\nmetadata'}, {'content': '字' * 200001},
])
def test_invalid_import_does_not_change_active_knowledge(engine, change):
    before = engine.knowledge.active()
    with pytest.raises(ValueError):
        stage(engine.knowledge, **change)
    assert engine.knowledge.active() == before


def test_baseline_is_immutable_and_import_is_idempotent(engine):
    base = engine.chunks[0].document_id
    with pytest.raises(Conflict):
        stage(engine.knowledge, document_id=base)
    first = stage(engine.knowledge)
    again = stage(engine.knowledge)
    assert first['version_id'] == again['version_id']
    assert first['chunks'] and all(c['version'] == 'r1' for c in first['chunks'])
    assert len(engine.knowledge.active()['documents']) == 12


def test_two_stale_publications_cannot_overwrite_each_other(engine):
    first, second = stage(engine.knowledge), stage(engine.knowledge, document_id='another-doc')
    expected = engine.knowledge.active()['snapshot_id']

    def publish(version):
        try:
            engine.knowledge.publish(version['version_id'], expected)
            return True
        except Conflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(publish, [first, second])) == [False, True]
    assert len(engine.knowledge.active()['documents']) == 13


def test_ticket_and_agent_tool_retrieval_pin_one_snapshot(engine):
    initial = engine.knowledge.active()['snapshot_id']
    one = stage(engine.knowledge)
    current = engine.publish_knowledge(one['version_id'], initial)
    ticket = engine.create('设备接口DFX900重试间隔配置多少秒？', strategy='agent')
    assert ticket['status'] == 'awaiting_review'
    records = engine.store.list_retrievals(ticket['id'])
    assert {r['stage'] for r in records} >= {'initial', 'agent_tool'}
    assert {r['snapshot_id'] for r in records} == {current['snapshot_id']}
    assert ticket['knowledge_snapshot_id'] == current['snapshot_id']
    assert engine.budget.summary()['used'] == 0


def test_edit_uses_original_evidence_and_followup_explicitly_uses_new_snapshot(engine):
    old = engine.publish_knowledge(stage(engine.knowledge)['version_id'], engine.knowledge.active()['snapshot_id'])
    ticket = engine.create('设备接口DFX900重试间隔配置多少秒？')
    newer = engine.publish_knowledge(stage(engine.knowledge, content='## DFX900\n设备接口DFX900重试间隔配置为45秒。')['version_id'], old['snapshot_id'])
    edited = engine.edit(ticket['id'], 1, ticket['draft']['answer'] + '\n人工核对。')
    assert edited['knowledge_snapshot_id'] == old['snapshot_id']
    assert edited['approval'] is None and edited['evidence'] == ticket['evidence']
    followed = engine.followup(ticket['id'], '请按当前资料重新核对')
    assert followed['knowledge_snapshot_id'] == newer['snapshot_id']
    assert any(v['knowledge_snapshot_id'] == old['snapshot_id'] for v in followed['versions'])


def test_missing_vector_index_logs_failure_against_current_snapshot(engine):
    current = engine.publish_knowledge(stage(engine.knowledge)['version_id'], engine.knowledge.active()['snapshot_id'])
    with pytest.raises(RetrievalError):
        engine.search('DFX900设备接口', 'vector')
    trace = engine.store.list_retrievals()[0]
    assert trace['snapshot_id'] == current['snapshot_id']
    assert trace['error'] and trace['results'] == [] and trace['method'] == 'vector'


def test_restarting_after_baseline_file_change_does_not_relabel_saved_data(settings):
    original = load_chunks(settings.knowledge_dir)
    store = KnowledgeStore(settings.data_dir / 'knowledge.sqlite3', original)
    snapshot = store.active()['snapshot_id']
    from dataclasses import replace
    changed = [replace(original[0], text='new content'), *original[1:]]
    with pytest.raises(Conflict):
        KnowledgeStore(store.path, changed)
    assert store.active()['snapshot_id'] == snapshot


def test_frozen_material_files_are_not_modified(engine, settings):
    before = {p.name: p.read_bytes() for p in settings.knowledge_dir.iterdir() if p.is_file()}
    initial = engine.knowledge.active()['snapshot_id']
    new = engine.publish_knowledge(stage(engine.knowledge)['version_id'], initial)
    engine.remove_knowledge('custom-queue', new['snapshot_id'])
    assert before == {p.name: p.read_bytes() for p in settings.knowledge_dir.iterdir() if p.is_file()}
