# (c) 2024 Ansible Project
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import annotations

import pytest

from ansible.errors import AnsibleParserError
from ansible.executor.handler_dependency import HandlerDependencyGraph


class FakeHandler:
    def __init__(self, name, uuid, depends_on=None):
        self.name = name
        self._uuid = uuid
        self.depends_on = depends_on or []
        self._ds = None

    def get_name(self, include_role_fqcn=True):
        return self.name


def resolver_from(handlers, listen=None):
    by_name = {handler.name: handler for handler in handlers}
    listen = listen or {}

    def resolve(token):
        matches = []
        if token in by_name:
            matches.append(by_name[token])
        for handler in handlers:
            if token in listen.get(handler.name, ()):
                matches.append(handler)
        # de-duplicate while preserving order
        unique = []
        seen = set()
        for match in matches:
            if match._uuid not in seen:
                seen.add(match._uuid)
                unique.append(match)
        return unique

    return resolve


def names(ordered_handlers):
    return [handler.name for handler in ordered_handlers]


def test_order_without_dependencies_keeps_definition_order():
    handlers = [FakeHandler('h1', 'u1'), FakeHandler('h2', 'u2'), FakeHandler('h3', 'u3')]
    graph = HandlerDependencyGraph.build(handlers, resolver_from(handlers))

    assert names(graph.ordered(handlers)) == ['h1', 'h2', 'h3']


def test_dependencies_run_first_with_stable_order_between_unrelated_handlers():
    # definition order: reload, independent, restart, stop_a, stop_b
    handlers = [
        FakeHandler('reload', 'u-reload', ['restart']),
        FakeHandler('independent', 'u-ind'),
        FakeHandler('restart', 'u-restart', ['stop_group']),
        FakeHandler('stop_a', 'u-stop-a'),
        FakeHandler('stop_b', 'u-stop-b'),
    ]
    listen = {'stop_a': ['stop_group'], 'stop_b': ['stop_group']}
    graph = HandlerDependencyGraph.build(handlers, resolver_from(handlers, listen))

    assert names(graph.ordered(handlers)) == ['independent', 'stop_a', 'stop_b', 'restart', 'reload']


def test_ordered_subset_matches_global_order():
    handlers = [
        FakeHandler('reload', 'u-reload', ['restart']),
        FakeHandler('restart', 'u-restart'),
        FakeHandler('other', 'u-other'),
    ]
    graph = HandlerDependencyGraph.build(handlers, resolver_from(handlers))

    subset = [handlers[0], handlers[1]]
    assert names(graph.ordered(subset)) == ['restart', 'reload']

    # a subset which is just a later node is unaffected
    assert names(graph.ordered([handlers[2]])) == ['other']


def test_missing_dependency_is_an_error_when_strict():
    handlers = [FakeHandler('h1', 'u1', ['nope'])]

    with pytest.raises(AnsibleParserError, match="depends on 'nope'"):
        HandlerDependencyGraph.build(handlers, resolver_from(handlers))


def test_missing_dependency_is_deferred_when_not_strict():
    handlers = [FakeHandler('h1', 'u1', ['nope'])]

    graph = HandlerDependencyGraph.build(handlers, resolver_from(handlers), strict_missing=False)
    assert names(graph.ordered(handlers)) == ['h1']


def test_duplicate_dependency_declaration_is_an_error():
    handlers = [
        FakeHandler('h1', 'u1'),
        FakeHandler('h2', 'u2', ['h1', 'h1']),
    ]

    with pytest.raises(AnsibleParserError, match="duplicate dependency on 'h1'"):
        HandlerDependencyGraph.build(handlers, resolver_from(handlers))


def test_distinct_declarations_resolving_to_the_same_handler_are_an_error():
    # name + listen topic on the very same handler
    handlers = [
        FakeHandler('h1', 'u1'),
        FakeHandler('h2', 'u2', ['h1', 'topic']),
    ]
    listen = {'h1': ['topic']}

    with pytest.raises(AnsibleParserError, match="both resolve to the same handler 'h1'"):
        HandlerDependencyGraph.build(handlers, resolver_from(handlers, listen))


def test_dependency_cycle_is_an_error():
    handlers = [
        FakeHandler('a', 'ua', ['b']),
        FakeHandler('b', 'ub', ['a']),
    ]

    with pytest.raises(AnsibleParserError, match='cycle detected: a -> b -> a'):
        HandlerDependencyGraph.build(handlers, resolver_from(handlers))


def test_self_dependency_is_a_cycle():
    handlers = [FakeHandler('a', 'ua', ['a'])]

    with pytest.raises(AnsibleParserError, match='cycle detected: a -> a'):
        HandlerDependencyGraph.build(handlers, resolver_from(handlers))


def test_listen_topic_dependency_orders_every_listener_first():
    handlers = [
        FakeHandler('after', 'u-after', ['group']),
        FakeHandler('listener1', 'u-l1'),
        FakeHandler('listener2', 'u-l2'),
    ]
    listen = {'listener1': ['group'], 'listener2': ['group']}
    graph = HandlerDependencyGraph.build(handlers, resolver_from(handlers, listen))

    assert names(graph.ordered(handlers)) == ['listener1', 'listener2', 'after']
