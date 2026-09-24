# (c) 2024 Ansible Project
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations

import heapq
import typing as t

from ansible.errors import AnsibleParserError
from ansible.playbook.handler import Handler


class HandlerDependencyGraph:
    """Handler ``depends_on`` graph for a play.

    Edges point from a handler to the handlers it depends on, i.e. an edge
    ``A -> B`` means B must run before A within a single handler flush.
    """

    def __init__(self, handlers: list[Handler], edges: dict[str, set[str]]):
        self._handlers = list(handlers)
        self._index = {handler._uuid: idx for idx, handler in enumerate(self._handlers)}
        self._by_uuid = {handler._uuid: handler for handler in self._handlers}
        self._edges: dict[str, set[str]] = {uuid: set(deps) for uuid, deps in edges.items()}

        self._dependents: dict[str, set[str]] = {handler._uuid: set() for handler in self._handlers}
        for uuid, deps in self._edges.items():
            for dependency in deps:
                self._dependents[dependency].add(uuid)

    @classmethod
    def build(
        cls,
        handlers: list[Handler],
        resolver: t.Callable[[str], list[Handler]],
        strict_missing: bool = True,
    ) -> HandlerDependencyGraph:
        """Build and validate a dependency graph.

        :param handlers: handlers in definition order; this is the universe
            against which dependencies are resolved.
        :param resolver: callable resolving a dependency name or listen topic
            to the matching handlers, using the same matching rules as
            notifications (last defined handler with the name wins, every
            handler listening on the topic matches).
        :param strict_missing: when True (the default), a dependency resolving
            to no handler is an error. The initial graph for a play is built
            non-strict because handlers pulled in later through dynamic
            ``include_role`` do not exist at play start; the graph is rebuilt
            strictly before every handler flush.
        """
        handler_list = list(handlers)
        uuids = {handler._uuid for handler in handler_list}
        edges: dict[str, set[str]] = {handler._uuid: set() for handler in handler_list}

        for handler in handler_list:
            declared = handler.depends_on or []
            if isinstance(declared, str):
                declared = [declared]
            declared = list(declared)

            seen_declarations: set[str] = set()
            resolved_by_token: dict[str, str] = {}

            for declaration in declared:
                if declaration in seen_declarations:
                    raise AnsibleParserError(
                        f"Handler '{handler.get_name()}' declares a duplicate dependency on '{declaration}'.",
                        obj=getattr(handler, '_ds', None),
                    )
                seen_declarations.add(declaration)

                targets = list(resolver(declaration))
                # a single declaration may match the same handler more than once
                # (a name match which also matches a listen topic)
                target_ids = {target._uuid for target in targets}

                if not target_ids:
                    if strict_missing:
                        raise AnsibleParserError(
                            f"Handler '{handler.get_name()}' depends on '{declaration}', but no handler with that name "
                            f"or handler listening on that topic was found.",
                            obj=getattr(handler, '_ds', None),
                        )
                    # the handler may be pulled in later by a dynamic include_role
                    continue

                for target_id in target_ids:
                    if target_id not in uuids:
                        if strict_missing:
                            raise AnsibleParserError(
                                f"Handler '{handler.get_name()}' depends on '{declaration}', but no handler with that name "
                                f"or handler listening on that topic was found.",
                                obj=getattr(handler, '_ds', None),
                            )
                        continue

                    previous = resolved_by_token.get(target_id)
                    if previous is not None and previous != declaration:
                        target_name = next(
                            target.get_name() for target in targets if target._uuid == target_id
                        )
                        raise AnsibleParserError(
                            f"Handler '{handler.get_name()}' declares dependencies '{previous}' and '{declaration}' "
                            f"that both resolve to the same handler '{target_name}'.",
                            obj=getattr(handler, '_ds', None),
                        )
                    resolved_by_token[target_id] = declaration

                edges[handler._uuid].update(target_ids)

        cls._detect_cycle(handler_list, edges)

        return cls(handler_list, edges)

    @staticmethod
    def _detect_cycle(handlers: list[Handler], edges: dict[str, set[str]]) -> None:
        names = {handler._uuid: handler.get_name() for handler in handlers}
        order = {handler._uuid: idx for idx, handler in enumerate(handlers)}

        unvisited = 0
        in_progress = 1
        done = 2
        state = {uuid: unvisited for uuid in names}

        def visit(start: str) -> None:
            # iterative DFS carrying the current path so cycle errors can
            # report the handlers involved
            stack: list[tuple[str, t.Iterator[str]]] = [(start, iter(sorted(edges[start], key=lambda x: order[x])))]
            path = [start]
            state[start] = in_progress

            while stack:
                uuid, iterator_entry = stack[-1]
                try:
                    neighbour = next(iterator_entry)
                except StopIteration:
                    state[uuid] = done
                    stack.pop()
                    path.pop()
                    continue

                if state[neighbour] == in_progress:
                    cycle_start = path.index(neighbour)
                    cycle = path[cycle_start:] + [neighbour]
                    raise AnsibleParserError(
                        "Handler dependency cycle detected: %s." % " -> ".join(names[node] for node in cycle)
                    )
                if state[neighbour] == unvisited:
                    state[neighbour] = in_progress
                    path.append(neighbour)
                    stack.append((neighbour, iter(sorted(edges[neighbour], key=lambda x: order[x]))))

        for handler in sorted(handlers, key=lambda h: order[h._uuid]):
            if state[handler._uuid] == unvisited:
                visit(handler._uuid)

    def ordered(self, handlers: list[Handler]) -> list[Handler]:
        """Return the given handlers in a stable dependency order.

        Dependencies always come first. Handlers without a dependency
        relationship keep the relative definition order they have in the
        graph, which in turn makes any per-host order a subsequence of the
        global definition-based order.
        """
        subset: list[Handler] = []
        seen: set[str] = set()
        for handler in handlers:
            if handler._uuid not in seen:
                seen.add(handler._uuid)
                subset.append(handler)

        subset_ids = [handler._uuid for handler in subset]
        id_set = seen

        indegree: dict[str, int] = {uuid: 0 for uuid in subset_ids}
        for uuid in subset_ids:
            for dependency in self._edges.get(uuid, ()):  # dependencies outside the subset are ignored
                if dependency in id_set:
                    indegree[uuid] += 1

        ready: list[tuple[int, str]] = [
            (self._index[uuid], uuid) for uuid in subset_ids if indegree[uuid] == 0
        ]
        heapq.heapify(ready)

        ordered_handlers: list[Handler] = []
        while ready:
            _, uuid = heapq.heappop(ready)
            ordered_handlers.append(self._by_uuid[uuid])

            for dependent in self._dependents.get(uuid, ()):
                if dependent in id_set:
                    indegree[dependent] -= 1
                    if indegree[dependent] == 0:
                        heapq.heappush(ready, (self._index[dependent], dependent))

        if len(ordered_handlers) != len(subset):
            unresolved = [handler.get_name() for handler in subset if handler not in ordered_handlers]
            raise AnsibleParserError(
                "Unable to order handlers due to a handler dependency cycle: %s."
                % ", ".join(unresolved)
            )

        return ordered_handlers

    def dependencies(self, uuid: str) -> set[str]:
        """Return the uuids of handlers the given handler directly depends on."""
        return set(self._edges.get(uuid, ()))

    def handler_for(self, uuid: str) -> Handler | None:
        return self._by_uuid.get(uuid)
