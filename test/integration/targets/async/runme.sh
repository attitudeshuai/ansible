#!/usr/bin/env bash

set -eux

export ASYNC_TEST_DIR="$(mktemp -d)"
_stub_roles="$(mktemp -d)"
mkdir -p "${_stub_roles}/prepare_tests"
trap 'rm -rf "${ASYNC_TEST_DIR}" "${_stub_roles}"' EXIT

# existing async keyword scenarios (role-style tasks/main.yml; uses the default async dir)
ANSIBLE_ROLES_PATH="${PWD}/..:${_stub_roles}" ansible-playbook legacy.yml -i legacy_inventory "$@"

# governance scenarios (isolated async dir)
ansible-playbook quota_reject.yml -i inventory "$@"

ansible-playbook quota_wait.yml -i inventory "$@"

ansible-playbook ttl_reclaim.yml -i inventory "$@"

ansible-playbook orphan_warn.yml -i inventory "$@"

ansible-playbook orphan_reclaim.yml -i inventory "$@"

ansible-playbook orphan_fail.yml -i inventory "$@"

ansible-playbook query.yml -i inventory "$@"

# invalid quota values fail before the run starts
set +e
invalid_output="$(ansible-playbook invalid_values.yml -i inventory "$@" 2>&1)"
invalid_rc=$?
set -e

[ "${invalid_rc}" -ne 0 ]
grep -F "async_max_jobs must be a non-negative integer" <<< "${invalid_output}"
