"""Adapter integration: spend guard ordering around paid Kling runs."""

from __future__ import annotations

from app.services.phygital.adapter import EnvPhygitalAdapter, reset_phygital_adapter
from app.services.phygital import spend as spend_module


class _FakePhygitalSession:

    def __init__(self, fail_start=False):
        self.calls = []
        self.fail_start = fail_start

    def sign_in(self):
        self.calls.append('sign_in')

    def list_projects(self):
        self.calls.append('list_projects')
        return [{'id': 129912, 'name': 'Property'}]

    def get_workspace(self, project_id):
        self.calls.append('get_workspace')
        return {'workspace': {'nodes': []}}

    def fetch_bytes(self, url):
        self.calls.append('fetch_bytes')
        return (b'\x89PNG', 'plan.png', 'image/png')

    def upload_fileobject(self, blob, *, filename, content_type, workspace_id=''):
        self.calls.append('upload_fileobject')
        return '23796125'

    def start_kling_task(self, payload):
        self.calls.append('start_kling_task')
        if self.fail_start:
            return {}
        return {'task_id': 10009999, 'http_status': 200}

    def poll_task(self, task_id, *, timeout_seconds=0):
        self.calls.append('poll_task')
        return {'status': 'done', 'position': -1, 'outputs': [{'name': 'out_video', 'id': [23594402], 'value': 's3://x/23594402.mp4'}]}

    def download_link(self, file_id):
        self.calls.append('download_link')
        return 'https://cdn.phygital.test/generated.mp4'


def _reset(monkeypatch, tmp_path, limit='3'):
    monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_ENABLED', '1')
    monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_MODE', 'live')
    monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_GENERATE', '1')
    monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_GENERATE_WAIT_SECONDS', '30')
    monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT', limit)
    monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_SPEND_WINDOW_SECONDS', '3600')
    monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_STATE_DIR', str(tmp_path / 'state'))
    reset_phygital_adapter()
    import app.services.phygital.adapter as adapter_module
    adapter_module._SPEND_LEDGER = None


def _adapter_with(monkeypatch, fake):
    adapter = EnvPhygitalAdapter()
    monkeypatch.setattr(adapter, '_ensure_session', lambda: fake)
    return adapter


def test_kill_switch_blocks_before_fetch(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path, limit='0')
    fake = _FakePhygitalSession()
    adapter = _adapter_with(monkeypatch, fake)
    artifact = adapter.generate_from_floorplan(floorplan_url='https://cdn.example.test/plan.png')
    assert artifact.status == 'error'
    assert artifact.reason == 'phygital_spend_disabled'
    assert 'fetch_bytes' not in fake.calls
    assert 'upload_fileobject' not in fake.calls
    assert 'start_kling_task' not in fake.calls


def test_cap_blocks_before_fetch(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path, limit='1')
    ledger = spend_module.SpendLedger(str(tmp_path / 'state'), limit=1, window_seconds=3600)
    assert ledger.reserve('other-artifact')[0] is True
    fake = _FakePhygitalSession()
    adapter = _adapter_with(monkeypatch, fake)
    artifact = adapter.generate_from_floorplan(floorplan_url='https://cdn.example.test/plan.png')
    assert artifact.status == 'error'
    assert artifact.reason == 'phygital_spend_limit_reached'
    assert 'fetch_bytes' not in fake.calls
    assert 'start_kling_task' not in fake.calls


def test_in_flight_blocks_before_fetch(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path, limit='5')
    ledger = spend_module.SpendLedger(str(tmp_path / 'state'), limit=5, window_seconds=3600)
    assert ledger.reserve('dup-key-1')[0] is True
    fake = _FakePhygitalSession()
    adapter = _adapter_with(monkeypatch, fake)
    artifact = adapter.generate_from_floorplan(floorplan_url='https://cdn.example.test/plan.png', artifact_key='dup-key-1')
    assert artifact.status == 'error'
    assert artifact.reason == 'phygital_task_in_flight'
    assert 'fetch_bytes' not in fake.calls
    assert 'start_kling_task' not in fake.calls


def test_happy_run_polls_completes_and_records(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path, limit='2')
    fake = _FakePhygitalSession()
    adapter = _adapter_with(monkeypatch, fake)
    artifact = adapter.generate_from_floorplan(floorplan_url='https://cdn.example.test/plan.png')
    assert artifact.status == 'ready'
    assert artifact.task_id == '10009999'
    assert fake.calls.count('start_kling_task') == 1
    assert fake.calls.index('upload_fileobject') < fake.calls.index('start_kling_task')
    ledger = spend_module.SpendLedger(str(tmp_path / 'state'), limit=2, window_seconds=3600)
    snap = ledger.snapshot()
    assert snap['count'] == 1
    record = list(snap['tasks'].values())[0]
    assert record['outcome'] == 'completed'
    assert record['task_id'] == '10009999'


def test_start_failure_refunds_slot(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path, limit='1')
    fake = _FakePhygitalSession(fail_start=True)
    adapter = _adapter_with(monkeypatch, fake)
    artifact = adapter.generate_from_floorplan(floorplan_url='https://cdn.example.test/plan.png')
    assert artifact.status == 'error'
    assert artifact.reason == 'phygital_task_start_failed'
    ledger = spend_module.SpendLedger(str(tmp_path / 'state'), limit=1, window_seconds=3600)
    assert ledger.snapshot()['count'] == 0
