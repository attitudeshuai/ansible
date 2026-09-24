# Copyright (c) 2026 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from ansible.modules import async_governance as ag


def _write_job(directory: str, jid: str, data: object, mtime: float | None = None) -> str:
    path = os.path.join(directory, jid)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)

    if mtime is not None:
        os.utime(path, (mtime, mtime))

    return path


def _running_jid(pid: int) -> str:
    return f'j123.{pid}'


def _started(pid: int) -> dict:
    jid = _running_jid(pid)
    return {'started': True, 'finished': False, 'ansible_job_id': jid}


@pytest.fixture
def dead_pid() -> int:
    import subprocess

    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()

    return proc.pid


@pytest.fixture
def job_dir(tmp_path) -> str:
    path = str(tmp_path / 'async')
    os.makedirs(path)

    return path


def test_scan_classifies_jobs(job_dir: str, dead_pid: int) -> None:
    alive_jid = _running_jid(os.getpid())
    _write_job(job_dir, alive_jid, _started(os.getpid()))
    _write_job(job_dir, 'jf.5', {'rc': 0})
    dead_jid = f'jd.{dead_pid}'
    _write_job(job_dir, dead_jid, {'started': True, 'finished': False, 'ansible_job_id': dead_jid})

    # partial writes and unrelated files are ignored
    _write_job(job_dir, f'{alive_jid}.tmp', {'x': 1})
    _write_job(job_dir, '.marker', {})

    running, finished, orphaned, warnings = ag.scan_jobs(job_dir, set())

    assert [e['jid'] for e in running] == [alive_jid]
    assert [e['jid'] for e in finished] == ['jf.5']
    assert [e['jid'] for e in orphaned] == [dead_jid]


def test_scan_submitted_jids_protected(job_dir: str, dead_pid: int) -> None:
    jid = f'jd.{dead_pid}'
    _write_job(job_dir, jid, {'started': True, 'finished': False, 'ansible_job_id': jid})

    running, finished, orphaned, warnings = ag.scan_jobs(job_dir, {jid})

    assert [e['jid'] for e in running] == [jid]
    assert orphaned == []


def test_enforce_ttl_reclaims_old_finished(job_dir: str) -> None:
    now = time.time()
    old_path = _write_job(job_dir, 'jo.1', {'rc': 0}, mtime=now - 100)
    recent_path = _write_job(job_dir, 'jr.2', {'rc': 0}, mtime=now - 1)

    result = ag.run_governance(job_dir, 'enforce', job_ttl=60, orphan_policy='warn', submitted_jids=set(), check_mode=False)

    assert not os.path.exists(old_path)
    assert os.path.exists(recent_path)
    assert [e['jid'] for e in result['reclaimed_jobs']] == ['jo.1']
    assert result['reclaimed_jobs'][0]['reason'] == 'ttl_expired'
    assert result['changed']


def test_enforce_ttl_never_reclaims_running(job_dir: str) -> None:
    jid = _running_jid(os.getpid())
    path = _write_job(job_dir, jid, _started(os.getpid()), mtime=time.time() - 1000)

    result = ag.run_governance(job_dir, 'enforce', job_ttl=60, orphan_policy='warn', submitted_jids=set(), check_mode=False)

    assert os.path.exists(path)
    assert result['reclaimed_jobs'] == []
    assert result['running_jobs']


def test_enforce_orphan_warn_keeps_files(job_dir: str, dead_pid: int) -> None:
    jid = f'jd.{dead_pid}'
    path = _write_job(job_dir, jid, {'started': True, 'finished': False, 'ansible_job_id': jid})

    result = ag.run_governance(job_dir, 'enforce', job_ttl=0, orphan_policy='warn', submitted_jids=set(), check_mode=False)

    assert os.path.exists(path)
    assert result['orphaned_jobs']
    assert jid in result['warnings'][0]


def test_enforce_orphan_reclaim_removes_files(job_dir: str, dead_pid: int) -> None:
    jid = f'jd.{dead_pid}'
    path = _write_job(job_dir, jid, {'started': True, 'finished': False, 'ansible_job_id': jid})

    result = ag.run_governance(job_dir, 'enforce', job_ttl=0, orphan_policy='reclaim', submitted_jids=set(), check_mode=False)

    assert not os.path.exists(path)
    assert result['reclaimed_jobs'][0]['reason'] == 'orphan_reclaimed'


def test_enforce_orphan_fail_reports_failures(job_dir: str, dead_pid: int) -> None:
    jid = f'jd.{dead_pid}'
    path = _write_job(job_dir, jid, {'started': True, 'finished': False, 'ansible_job_id': jid})

    result = ag.run_governance(job_dir, 'enforce', job_ttl=0, orphan_policy='fail', submitted_jids=set(), check_mode=False)

    assert os.path.exists(path)
    assert result['failed_jobs'][0]['jid'] == jid


def test_enforce_check_mode_does_not_remove(job_dir: str) -> None:
    now = time.time()
    path = _write_job(job_dir, 'jo.1', {'rc': 0}, mtime=now - 100)

    result = ag.run_governance(job_dir, 'enforce', job_ttl=60, orphan_policy='warn', submitted_jids=set(), check_mode=True)

    assert os.path.exists(path)
    assert result['reclaimed_jobs'][0]['would_reclaim']


def test_enforce_creates_missing_dir(tmp_path) -> None:
    path = str(tmp_path / 'new_async')
    ag.run_governance(path, 'enforce', job_ttl=0, orphan_policy='warn', submitted_jids=set(), check_mode=False)

    assert os.path.isdir(path)


def test_enforce_on_non_directory_raises(tmp_path) -> None:
    path = str(tmp_path / 'afile')
    with open(path, 'w'):
        pass

    with pytest.raises(ag.AsyncJobDirError):
        ag.run_governance(path, 'enforce', job_ttl=0, orphan_policy='warn', submitted_jids=set(), check_mode=False)


def test_query_is_read_only(job_dir: str) -> None:
    now = time.time()
    old_path = _write_job(job_dir, 'jo.1', {'rc': 0}, mtime=now - 100)

    result = ag.run_governance(job_dir, 'query', job_ttl=60, orphan_policy='reclaim', submitted_jids=set(), check_mode=False)

    assert os.path.exists(old_path)
    assert result['reclaimed_jobs'] == []


def test_query_missing_dir_is_not_an_error(tmp_path) -> None:
    path = str(tmp_path / 'missing_async')
    result = ag.run_governance(path, 'query', job_ttl=0, orphan_policy='warn', submitted_jids=set(), check_mode=False)

    assert result['warnings']
    assert result['running_jobs'] == []


def test_remove_job_idempotent(job_dir: str) -> None:
    path = os.path.join(job_dir, 'jx.1')
    with open(path, 'w'):
        pass

    assert ag.remove_job(path, check_mode=False)
    assert not ag.remove_job(path, check_mode=False)


def test_pid_is_alive_current_process() -> None:
    assert ag.pid_is_alive(os.getpid())


def test_pid_is_alive_known_dead(dead_pid: int) -> None:
    assert not ag.pid_is_alive(dead_pid)
