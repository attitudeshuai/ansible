# -*- coding: utf-8 -*-

# Copyright: (c) 2026, Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

DOCUMENTATION = r"""
---
module: async_governance
short_description: Inspect and govern asynchronous jobs on a host
description:
- Scans the async job directory of a host and classifies the jobs found there.
- In V(enforce) mode, reclaims finished jobs whose results exceeded the configured lifetime and applies
  the configured policy to jobs abandoned by a previous controller run.
- In V(query) mode, performs a read-only scan and never removes job files.
- This module is invoked automatically by the executor when async job governance is enabled for a play,
  but can also be called directly to query the async jobs on a host.
version_added: "2.23"
options:
  mode:
    description:
    - V(enforce) applies the configured lifetime reclamation and orphan policy.
    - V(query) performs a read-only scan.
    type: str
    choices: [ enforce, query ]
    default: enforce
  job_ttl:
    description:
    - Lifetime in seconds of finished async job status and result files.
    - Finished jobs older than the value are reclaimed.
    - Jobs that are still running are never reclaimed.
    - A value of V(0) disables lifetime reclamation.
    type: int
    default: 0
  orphan_policy:
    description:
    - How to treat jobs abandoned by a previous run, where the remote process referenced by the job id no longer
      exists while the status file still says the job is unfinished.
    - V(warn) leaves the files in place and reports a warning.
    - V(reclaim) removes the job files.
    - V(fail) leaves the files in place and reports the abandoned jobs as failed.
    type: str
    choices: [ warn, reclaim, fail ]
    default: warn
  submitted_jids:
    description:
    - Job ids submitted by the current controller run.
    - These jobs are never classified as abandoned, even if their status file is not fully written yet.
    type: list
    elements: str
    default: []
  _async_dir:
    description:
    - Expanded path of the async job directory.
    - Supplied by the async_governance action plugin.
    type: path
    required: true
attributes:
    action:
        support: full
    async:
        support: none
    check_mode:
        support: full
    diff_mode:
        support: none
    bypass_host_loop:
        support: none
    platform:
        support: full
        platforms: posix
seealso:
- module: ansible.builtin.async_status
- module: ansible.builtin.async_wrapper
author:
- Ansible Core Team
"""

EXAMPLES = r"""
---
- name: Query async jobs on the host
  ansible.builtin.async_governance:
    mode: query
  register: async_jobs

- name: Show running async job count
  ansible.builtin.debug:
    var: async_jobs.running_count
"""

RETURN = r"""
async_dir:
  description: The async job directory that was scanned.
  returned: always
  type: str
  sample: /root/.ansible_async
running_count:
  description: Number of async jobs classified as running.
  returned: always
  type: int
  sample: 2
running_jobs:
  description: Jobs classified as running.
  returned: always
  type: list
  elements: dict
  contains:
    jid:
      description: The async job id.
      type: str
      sample: j360874038559.4169
    pid:
      description: The remote process id encoded in the job id.
      type: int
      sample: 4169
    started_at:
      description: Epoch timestamp of the status file's last write.
      type: float
    pending:
      description: True when the job was submitted during the current run and its status file is not written yet.
      type: bool
finished_jobs:
  description: Jobs classified as finished.
  returned: always
  type: list
  elements: dict
  contains:
    jid:
      description: The async job id.
      type: str
    finished_at:
      description: Epoch timestamp of the status file's last write.
      type: float
orphaned_jobs:
  description: Jobs abandoned by a previous run.
  returned: always
  type: list
  elements: dict
  contains:
    jid:
      description: The async job id.
      type: str
    pid:
      description: The remote process id encoded in the job id.
      type: int
    reason:
      description: Why the job was classified as abandoned.
      type: str
    detected_at:
      description: Epoch timestamp when the job was detected.
      type: float
reclaimed_jobs:
  description: Jobs that were reclaimed.
  returned: always
  type: list
  elements: dict
  contains:
    jid:
      description: The async job id.
      type: str
    reason:
      description: Why the job was reclaimed.
      type: str
    reclaimed_at:
      description: Epoch timestamp when the job was reclaimed.
      type: float
    would_reclaim:
      description: Present and true in check mode, indicating the job would be reclaimed.
      type: bool
failed_jobs:
  description: Abandoned jobs reported as failed due to the fail orphan policy.
  returned: always
  type: list
  elements: dict
  contains:
    jid:
      description: The async job id.
      type: str
    msg:
      description: Failure description.
      type: str
warnings:
  description: Warnings reported during the scan.
  returned: always
  type: list
  elements: str
"""

import json
import os
import re
import time

from ansible.module_utils.basic import AnsibleModule

# job ids look like 'j360874038559.4169', with the remote process id after the last dot
_JOB_ID_RE = re.compile(r'^(?P<prefix>.+)\.(?P<pid>\d+)$')


def pid_is_alive(pid: int) -> bool:
    """Return True when a process with the given id exists on the remote host."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # the process exists but is owned by another user
        return True
    except OSError:
        return False

    return True


def read_job_file(path: str) -> tuple[dict | None, str]:
    """Read and parse a job status file.

    Returns the parsed data and one of the states ok, missing, unreadable, empty, invalid.
    """
    try:
        with open(path, encoding='utf-8') as f:
            raw = f.read()
    except FileNotFoundError:
        return None, 'missing'
    except OSError:
        return None, 'unreadable'

    if not raw.strip():
        return None, 'empty'

    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None, 'invalid'

    if not isinstance(data, dict):
        return None, 'invalid'

    return data, 'ok'


def job_is_finished(data: dict | None, state: str) -> bool:
    """Classify parsed job data as finished, mirroring the async_status status semantics."""
    if state != 'ok' or data is None:
        return False

    # async_wrapper writes {'started': True, 'finished': False} while running;
    # final module results do not contain 'started'
    return 'started' not in data or bool(data.get('finished'))


def scan_jobs(async_dir: str, submitted_jids: set[str]) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Scan the async job directory and classify the jobs found there."""
    now = time.time()
    running_jobs: list[dict] = []
    finished_jobs: list[dict] = []
    orphaned_jobs: list[dict] = []
    warnings: list[str] = []

    try:
        names = sorted(os.listdir(async_dir))
    except FileNotFoundError:
        return running_jobs, finished_jobs, orphaned_jobs, [f"async job directory {async_dir} does not exist"]
    except OSError as ex:
        raise AsyncJobDirError(f"could not access async job directory {async_dir}: {ex}") from ex

    for name in names:
        # ignore partial writes and non-job files
        if name.startswith('.') or name.endswith('.tmp'):
            continue

        match = _JOB_ID_RE.match(name)
        if not match:
            continue

        pid = int(match.group('pid'))
        path = os.path.join(async_dir, name)

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            # the file vanished while scanning the directory
            continue

        data, state = read_job_file(path)
        entry = {'jid': name, 'pid': pid, 'results_file': path}

        if job_is_finished(data, state):
            entry['finished_at'] = mtime
            finished_jobs.append(entry)
            continue

        alive = pid_is_alive(pid)

        if alive:
            entry['started_at'] = mtime
            running_jobs.append(entry)
        elif name in submitted_jids:
            # submitted during the current run; the status file may still be initializing
            entry['started_at'] = mtime
            entry['pending'] = True
            running_jobs.append(entry)
        else:
            entry['detected_at'] = now
            if state in ('invalid', 'unreadable'):
                entry['reason'] = 'remote process no longer exists and the status file could not be read'
            else:
                entry['reason'] = f'remote process {pid} no longer exists while the status file says the job is unfinished'
            orphaned_jobs.append(entry)

    return running_jobs, finished_jobs, orphaned_jobs, warnings


class AsyncJobDirError(Exception):
    """Raised when the async job directory cannot be used."""


def ensure_async_dir(async_dir: str) -> None:
    """Ensure the async job directory exists and is writable."""
    if os.path.isdir(async_dir):
        if not os.access(async_dir, os.W_OK):
            raise AsyncJobDirError(f"async job directory {async_dir} is not writable")
        return

    try:
        os.makedirs(async_dir, exist_ok=True)
    except OSError as ex:
        raise AsyncJobDirError(f"could not create async job directory {async_dir}: {ex}") from ex


def remove_job(path: str, check_mode: bool) -> bool:
    """Remove a job file, tolerating the file already being absent."""
    if check_mode:
        return True

    try:
        os.unlink(path)
    except FileNotFoundError:
        return False

    return True


def run_governance(
    async_dir: str,
    mode: str,
    job_ttl: int,
    orphan_policy: str,
    submitted_jids: set[str],
    check_mode: bool,
) -> dict:
    """Scan the async job directory and apply the configured governance actions."""
    warnings: list[str] = []

    if mode == 'enforce':
        ensure_async_dir(async_dir)

    running_jobs, finished_jobs, orphaned_jobs, scan_warnings = scan_jobs(async_dir, submitted_jids)
    warnings.extend(scan_warnings)

    reclaimed_jobs: list[dict] = []
    failed_jobs: list[dict] = []
    now = time.time()

    if mode == 'enforce':
        for entry in finished_jobs:
            if job_ttl > 0 and now - entry['finished_at'] >= job_ttl:
                reclaimed_entry = {
                    'jid': entry['jid'],
                    'reason': 'ttl_expired',
                    'reclaimed_at': now,
                }
                if check_mode:
                    reclaimed_entry['would_reclaim'] = True
                if remove_job(entry['results_file'], check_mode):
                    reclaimed_jobs.append(reclaimed_entry)

        for entry in orphaned_jobs:
            if orphan_policy == 'reclaim':
                reclaimed_entry = {
                    'jid': entry['jid'],
                    'reason': 'orphan_reclaimed',
                    'reclaimed_at': now,
                }
                if check_mode:
                    reclaimed_entry['would_reclaim'] = True
                if remove_job(entry['results_file'], check_mode):
                    reclaimed_jobs.append(reclaimed_entry)
            elif orphan_policy == 'warn':
                warnings.append(
                    f"async job {entry['jid']} was abandoned by a previous run: {entry['reason']}"
                )
            else:
                failed_jobs.append({
                    'jid': entry['jid'],
                    'msg': f"async job was abandoned by a previous run: {entry['reason']}",
                })

    return {
        'async_dir': async_dir,
        'running_count': len(running_jobs),
        'running_jobs': running_jobs,
        'finished_jobs': finished_jobs,
        'orphaned_jobs': orphaned_jobs,
        'reclaimed_jobs': reclaimed_jobs,
        'failed_jobs': failed_jobs,
        'warnings': warnings,
        'changed': bool(reclaimed_jobs),
    }


def main() -> None:
    module = AnsibleModule(
        argument_spec=dict(
            mode=dict(type='str', choices=['enforce', 'query'], default='enforce'),
            job_ttl=dict(type='int', default=0),
            orphan_policy=dict(type='str', choices=['warn', 'reclaim', 'fail'], default='warn'),
            submitted_jids=dict(type='list', elements='str', default=[]),
            _async_dir=dict(type='path', required=True),
        ),
        supports_check_mode=True,
    )

    params = module.params
    async_dir = params['_async_dir']

    try:
        result = run_governance(
            async_dir=async_dir,
            mode=params['mode'],
            job_ttl=params['job_ttl'],
            orphan_policy=params['orphan_policy'],
            submitted_jids=set(params['submitted_jids']),
            check_mode=module.check_mode,
        )
    except AsyncJobDirError as ex:
        module.fail_json(msg=str(ex))

    if result['failed_jobs']:
        result['failed'] = True
        result['msg'] = 'one or more async jobs abandoned by a previous run were reported as failed'

    module.exit_json(**result)


if __name__ == '__main__':
    main()
