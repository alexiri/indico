# This file is part of Indico.
# Copyright (C) 2002 - 2025 CERN
#
# Indico is free software; you can redistribute it and/or
# modify it under the terms of the MIT License; see the
# LICENSE file for more details.

import datetime
import itertools
import os
import re
import sys
from contextlib import ExitStack
from functools import partial
from operator import attrgetter, itemgetter

import click
import sqlalchemy.orm
from flask import current_app
from sqlalchemy.orm.attributes import flag_modified

import indico
from indico.core import signals
from indico.core.celery import celery
from indico.core.config import config
from indico.core.db import db
from indico.core.db.sqlalchemy.util.models import get_all_models
from indico.core.plugins import plugin_engine
from indico.modules.events import Event
from indico.util.console import cformat
from indico.util.date_time import now_utc, server_to_utc
from indico.web.flask.stats import request_stats_request_started


def _add_to_context(namespace, info, element, name=None, doc=None, color='green'):
    if not name:
        name = element.__name__
    namespace[name] = element
    if doc:
        info.append(cformat('+ %%{%s}{}%%{white!} ({})' % color).format(name, doc))  # noqa: UP031
    else:
        info.append(cformat('+ %%{%s}{}' % color).format(name))  # noqa: UP031


def _add_to_context_multi(namespace, info, elements, names=None, doc=None, color='green'):
    if not elements:
        return
    if not names:
        names = [x.__name__ for x in elements]
    for name, element in zip(names, elements, strict=True):
        namespace[name] = element
    if doc:
        info.append(cformat('+ %%{white!}{}:%%{reset} %%{%s}{}' % color).format(doc, ', '.join(names)))  # noqa: UP031
    else:
        info.append(cformat('+ %%{%s}{}' % color).format(', '.join(names)))  # noqa: UP031


def _add_to_context_smart(namespace, info, objects, get_name=attrgetter('__name__'), color='cyan'):
    def _get_module(obj):
        segments = tuple(obj.__module__.split('.'))
        if segments[0].startswith('indico_'):  # plugin
            return f'plugin:{segments[0]}'
        elif segments[:2] == ('indico', 'modules'):
            return f'module:{segments[2]}'
        elif segments[:2] == ('indico', 'core'):
            return f'core:{segments[2]}'
        else:
            return '.'.join(segments[:-1] if len(segments) > 1 else segments)

    items = [(_get_module(obj), get_name(obj), obj) for obj in objects]
    for module, mod_items in itertools.groupby(sorted(items, key=itemgetter(0, 1)), key=itemgetter(0)):
        names, elements = list(zip(*((x[1], x[2]) for x in mod_items), strict=True))
        _add_to_context_multi(namespace, info, elements, names, doc=module, color=color)


def _make_shell_context():
    context = {}
    info = [cformat('%{white!}Available objects')]
    add_to_context = partial(_add_to_context, context, info)
    add_to_context_multi = partial(_add_to_context_multi, context, info)
    add_to_context_smart = partial(_add_to_context_smart, context, info)
    # Common stdlib modules
    info.append(cformat('*** %{magenta!}stdlib%{reset} ***'))
    datetime_attrs = ('date', 'time', 'datetime', 'timedelta')
    orm_attrs = ('joinedload', 'defaultload', 'contains_eager', 'lazyload', 'noload', 'subqueryload', 'undefer',
                 'undefer_group', 'load_only')
    add_to_context_multi([getattr(datetime, attr) for attr in datetime_attrs] +
                         [getattr(sqlalchemy.orm, attr) for attr in orm_attrs] +
                         [flag_modified] +
                         [itertools, re, sys, os],
                         color='yellow')
    # Models
    info.append(cformat('*** %{magenta!}Models%{reset} ***'))
    models = [cls for cls in sorted(get_all_models(), key=attrgetter('__name__')) if hasattr(cls, '__table__')]
    add_to_context_smart(models)
    # Tasks
    info.append(cformat('*** %{magenta!}Tasks%{reset} ***'))
    tasks = [task for task in sorted(celery.tasks.values(), key=attrgetter('name'))
             if not task.name.startswith('celery.')]
    add_to_context_smart(tasks, get_name=lambda x: x.name.replace('.', '_'), color='blue!')
    # Plugins
    plugins = [type(plugin) for plugin in sorted(plugin_engine.get_active_plugins().values(),
                                                 key=attrgetter('name'))]
    if plugins:
        info.append(cformat('*** %{magenta!}Plugins%{reset} ***'))
        add_to_context_multi(plugins, color='yellow!')
    # Utils
    info.append(cformat('*** %{magenta!}Misc%{reset} ***'))
    add_to_context(celery, 'celery', doc='celery app', color='blue!')
    add_to_context(db, 'db', doc='sqlalchemy db interface', color='cyan!')
    add_to_context(now_utc, 'now_utc', doc='get current utc time', color='cyan!')
    add_to_context(config, 'config', doc='indico config')
    add_to_context(current_app, 'app', doc='flask app')
    add_to_context(lambda *a, **kw: server_to_utc(datetime.datetime(*a, **kw)), 'dt',
                   doc='like datetime() but converted from localtime to utc')
    add_to_context(Event.get, 'E', doc='get event by id')
    # Stuff from plugins
    signals.plugin.shell_context.send(add_to_context=add_to_context, add_to_context_multi=add_to_context_multi)
    return context, info


def shell_cmd(verbose, with_req_context):
    try:
        from IPython.terminal.ipapp import TerminalIPythonApp
    except ImportError:
        click.echo(cformat('%{red!}You need to `pip install ipython` to use the Indico shell'))
        sys.exit(1)

    current_app.config['REPL'] = True  # disables e.g. memoize_request
    request_stats_request_started()
    context, info = _make_shell_context()
    banner = cformat('%{yellow!}Indico v{} is ready for your commands').format(indico.__version__)
    if verbose:
        banner = '\n'.join([*info, '', banner])
    ctx = current_app.make_shell_context()
    ctx.update(context)
    stack = ExitStack()
    if with_req_context:
        stack.enter_context(current_app.test_request_context(base_url=config.BASE_URL))
    with stack:
        ipython_app = TerminalIPythonApp.instance(user_ns=ctx, display_banner=False)
        ipython_app.initialize(argv=[])
        ipython_app.shell.show_banner(banner)
        ipython_app.start()
