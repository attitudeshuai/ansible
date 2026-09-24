# -*- coding: utf-8 -*-
# Copyright: Contributors to the Ansible project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import json
import os
import pathlib
import time

import pytest

from ansible.errors import AnsibleError
from ansible._internal._plugins._cache import PluginInterposer
from ansible.plugins.cache import BaseCacheModule
from ansible.plugins.cache.jsonfile import CacheModule as JsonFileCache
from ansible.plugins.cache.memory import CacheModule as MemoryCache


def _jsonfile_cache(tmp_path, timeout=86400):
    # bypass plugin option machinery (DOCUMENTATION config-def parsing) to instantiate directly
    cache = JsonFileCache.__new__(JsonFileCache)
    cache._cache_dir = str(tmp_path)
    cache._timeout = float(timeout)
    cache.plugin_name = 'jsonfile'
    cache._cache = {}
    cache._sanitized = {}
    cache._files = {}
    cache._options = {'_prefix': ''}
    return cache


def test_capability_flags():
    assert MemoryCache._supports_fact_subsets is True
    assert JsonFileCache._supports_fact_subsets is True
    assert BaseCacheModule._supports_fact_subsets is False
    base_default = BaseCacheModule.get_fact_record
    with pytest.raises(NotImplementedError):
        base_default(object(), 'key')


def test_memory_get_fact_record():
    cache = MemoryCache()
    with pytest.raises(KeyError):
        cache.get_fact_record('host')
    cache.set('host', {'a': 1})
    assert cache.get_fact_record('host') == {'a': 1}


def test_jsonfile_get_fact_record_bypasses_mtime_expiry(tmp_path):
    cache = _jsonfile_cache(tmp_path, timeout=1)
    cache.set('host', {'ansible_fact': 'value'})
    cachefile = pathlib.Path(cache._get_cache_file_name('host'))
    # age the file far beyond the whole-record TTL
    old = time.time() - 3600
    os.utime(cachefile, (old, old))

    # classical whole-record access expires
    cache._cache = {}
    with pytest.raises(KeyError):
        cache.get('host')

    # subset access ignores the whole-record TTL
    cache._cache = {}
    assert cache.get_fact_record('host') == {'ansible_fact': 'value'}


def test_jsonfile_corrupt_record_deleted_with_rerun_hint(tmp_path):
    cache = _jsonfile_cache(tmp_path)
    cachefile = pathlib.Path(cache._get_cache_file_name('host'))
    cachefile.write_text('this is not json {{{')

    cache._cache = {}
    with pytest.raises(AnsibleError, match='re-run'):
        cache.get_fact_record('host')
    assert not cachefile.exists()


class _Incapable:
    _persistent = True
    _supports_fact_subsets = False


def test_interposer_capability_is_proxied(tmp_path):
    capable = PluginInterposer(_jsonfile_cache(tmp_path))
    assert capable._supports_fact_subsets is True

    # ObjectProxy attribute access reaches the wrapped plugin
    assert PluginInterposer(_Incapable())._supports_fact_subsets is False


def test_interposer_get_fact_record_roundtrip(tmp_path):
    cache = _jsonfile_cache(tmp_path)
    interposer = PluginInterposer(cache)
    assert interposer._supports_fact_subsets is True

    with pytest.raises(KeyError):
        interposer.get_fact_record('host')

    record = {'ansible_fact': 'value'}
    interposer.set('host', record)
    assert interposer.get_fact_record('host') == record

    # the on-disk form keeps the __payload__ wrapper which old/new readers distinguish explicitly
    on_disk = json.loads(pathlib.Path(cache._get_cache_file_name(interposer._get_key('host'))).read_text())
    assert '__payload__' in on_disk
