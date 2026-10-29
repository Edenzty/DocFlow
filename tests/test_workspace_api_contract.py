"""Direct endpoint/schema contracts; complements HTTP tests, does not replace them."""
import shutil
import pytest

from docflow_lab.app import create_app, DocumentImport, PublishInput, SearchInput, SnapshotInput
from docflow_lab.config import Settings
from docflow_lab.storage import Conflict
from docflow_lab.workflow import Engine


def test_workspace_endpoint_contracts_without_event_loop(tmp_path):
    shutil.copytree(Settings().knowledge_dir, tmp_path / 'knowledge')
    service = Engine(Settings(root=tmp_path, data_dir=tmp_path / 'state'))
    try:
        app = create_app(engine=service)
        endpoints = {route.path: route.endpoint for route in app.routes if hasattr(route, 'endpoint')}
        initial = endpoints['/api/knowledge']()
        draft = endpoints['/api/knowledge/import'](DocumentImport(document_id='contract-doc', title='接口排查手册',
            filename='manual.md', source='原创接口合同测试', content='# DFX901\n设备接口DFX901的配置间隔为27秒。'))
        assert endpoints['/api/knowledge']()['snapshot_id'] == initial['snapshot_id']
        current = endpoints['/api/knowledge/publish'](PublishInput(version_id=draft['version_id'], expected_snapshot_id=initial['snapshot_id']))
        found = endpoints['/api/search'](SearchInput(query='设备接口DFX901配置间隔'))
        assert found['trace']['snapshot_id'] == current['snapshot_id']
        assert any(x['document_id'] == 'contract-doc' for x in found['results'])
        assert endpoints['/api/retrieval-traces/{trace_id}'](found['trace']['trace_id'])['results'][0]['rank'] == 1
        assert endpoints['/api/retrieval-traces'](None, 20)['traces']
        assert len(endpoints['/api/knowledge/snapshots']()['snapshots']) == 2
        assert endpoints['/api/knowledge/documents/{document_id}/versions']('contract-doc')['versions'][0]['version'] == 'r1'
        with pytest.raises(Conflict):
            endpoints['/api/knowledge/index'](SnapshotInput(expected_snapshot_id=initial['snapshot_id']))
        paths = app.openapi()['paths']
        assert '/api/knowledge/import' in paths and '/api/retrieval-traces' in paths
    finally:
        service.close()
