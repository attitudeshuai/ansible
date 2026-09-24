# (c) 2012-2014, Michael DeHaan <michael.dehaan@gmail.com>
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

import typing as t

from ansible.errors import AnsibleAssertionError, AnsibleTemplateError
from ansible.module_utils.common.text.converters import to_text
from ansible.playbook.attribute import NonInheritableFieldAttribute
from ansible.playbook.task import Task
from ansible.utils.display import Display

if t.TYPE_CHECKING:
    from ansible._internal._templating._engine import TemplateEngine as _TemplateEngine


display = Display()


def resolve_handlers_by_notification(
    notification: str,
    handlers: list[Handler],
    templar_factory: t.Callable[[Handler], _TemplateEngine],
) -> list[Handler]:
    """Return the handlers matching a notification.

    ``handlers`` must be provided in reverse definition order (i.e. the last
    defined handler first), matching the semantics used when resolving
    ``notify`` entries: a name match resolves to the last defined handler with
    that name only, while every handler listening on the notification topic is
    returned.

    Handler names are templated lazily on first use and the templated value is
    cached on the handler itself. Handlers whose name cannot be templated are
    skipped, mirroring the behavior of notification resolution.
    """
    resolved: list[Handler] = []

    # iterate in reversed order since last handler loaded with the same name wins
    for handler in handlers:
        if not handler.name:
            continue

        if not handler.cached_name:
            templar = templar_factory(handler)

            try:
                handler.name = templar.template(handler.name)
            except AnsibleTemplateError as e:
                # We skip this handler due to the fact that it may be using
                # a variable in the name that was conditionally included via
                # set_fact or some other method, and we don't want to error
                # out unnecessarily
                if not handler.listen:
                    display.warning(
                        "Handler '%s' is unusable because it has no listen topics and "
                        "the name could not be templated (host-specific variables are "
                        "not supported in handler names). The error: %s" % (handler.name, to_text(e))
                    )
                continue

            handler.cached_name = True

        # first we check with the full result of get_name(), which may
        # include the role name (if the handler is from a role). If that
        # is not found, we resort to the simple name field, which doesn't
        # have anything extra added on it.
        if notification in {
            handler.name,
            handler.get_name(include_role_fqcn=False),
            handler.get_name(include_role_fqcn=True),
        }:
            resolved.append(handler)
            break

    seen = set()
    for handler in handlers:
        if notification in handler.listen:
            if handler.name and handler.name in seen:
                continue
            seen.add(handler.name)
            resolved.append(handler)

    return resolved


class Handler(Task):

    listen = NonInheritableFieldAttribute(isa='list', default=list, listof=(str,), static=True)
    depends_on = NonInheritableFieldAttribute(isa='list', default=list, listof=(str,), static=True)

    def __init__(self, block=None, role=None, task_include=None):
        self.notified_hosts = []

        self.cached_name = False

        super(Handler, self).__init__(block=block, role=role, task_include=task_include)

    def __repr__(self):
        """ returns a human-readable representation of the handler """
        return "HANDLER: %s" % self.get_name()

    def _validate_listen(self, attr, name, value):
        new_value = self.get_validated_value(name, attr, value, None)
        if self._role is not None:
            for listener in new_value.copy():
                new_value.extend([
                    f"{self._role.get_name(include_role_fqcn=True)} : {listener}",
                    f"{self._role.get_name(include_role_fqcn=False)} : {listener}",
                ])
        setattr(self, name, new_value)

    def _validate_depends_on(self, attr, name, value):
        # normalize a bare handler/listen name into a list at load time,
        # mirroring the handling of the static 'listen' attribute
        setattr(self, name, self.get_validated_value(name, attr, value, None))

    @staticmethod
    def load(data, block=None, role=None, task_include=None, variable_manager=None, loader=None):
        t = Handler(block=block, role=role, task_include=task_include)
        return t.load_data(data, variable_manager=variable_manager, loader=loader)

    def notify_host(self, host):
        if not self.is_host_notified(host):
            self.notified_hosts.append(host)
            return True
        return False

    def remove_host(self, host):
        try:
            self.notified_hosts.remove(host)
        except ValueError:
            raise AnsibleAssertionError(
                f"Attempting to remove a notification on handler '{self}' for host '{host}' but it has not been notified."
            )

    def clear_hosts(self):
        self.notified_hosts = []

    def is_host_notified(self, host):
        return host in self.notified_hosts
