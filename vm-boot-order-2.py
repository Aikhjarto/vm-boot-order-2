#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
"""Start libvirt VMs in a configured order and shut them down in parallel.

Rewrite of vm-boot-order.py, which was based on
https://gist.github.com/cyberang3l/f4c8b1ab6fc48374fbae9553d89e5eed
"""

import argparse
import configparser
import json
import logging
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import libvirt

DEFAULT_CONFIG = '/etc/vm-boot-order-2.conf'

log = logging.getLogger('vm-boot-order-2')


@dataclass
class Config:
    uri: str = 'qemu:///system'
    # Block device (iostat -N name) whose utilization ends the wait after a
    # VM start early. Empty: always wait the full time.
    disk: str = ''
    util_limit: float = 80.0
    # VMs don't load the disk right after start, so always wait this long.
    min_wait: float = 5.0
    iostat_interval: int = 5
    default_wait: float = 0.0
    shutdown_timeout: float = 180.0
    shutdown_retry_interval: float = 30.0
    max_parallel_shutdowns: int = 5
    # VMs that are shut down only after all others have stopped
    shutdown_last: list = field(default_factory=list)
    start_order: list = field(default_factory=list)  # [(name, max_wait)]
    stop_order: list = field(default_factory=list)   # [name]


def load_config(path):
    parser = configparser.ConfigParser(allow_no_value=True,
                                       delimiters=('=',),
                                       inline_comment_prefixes=('#',))
    parser.optionxform = str  # VM names are case sensitive
    with open(path) as f:
        parser.read_file(f)

    cfg = Config()
    unknown = set(parser.sections()) - {'settings', 'start_order', 'stop_order'}
    if unknown:
        raise ValueError('unknown section(s): {}'.format(', '.join(sorted(unknown))))

    if parser.has_section('settings'):
        settings = parser['settings']
        for key in settings:
            if key in ('start_order', 'stop_order') or not hasattr(cfg, key):
                raise ValueError('unknown setting: {}'.format(key))
            default = getattr(cfg, key)
            if isinstance(default, list):
                # VM names separated by whitespace or commas
                setattr(cfg, key, settings.get(key, '').replace(',', ' ').split())
            elif isinstance(default, int):
                setattr(cfg, key, settings.getint(key))
            elif isinstance(default, float):
                setattr(cfg, key, settings.getfloat(key))
            else:
                setattr(cfg, key, settings.get(key) or '')

    if cfg.max_parallel_shutdowns < 1:
        raise ValueError('max_parallel_shutdowns must be at least 1')
    if cfg.iostat_interval < 1:
        raise ValueError('iostat_interval must be at least 1')

    if parser.has_section('start_order'):
        for name, wait in parser['start_order'].items():
            cfg.start_order.append((name, float(wait) if wait else cfg.default_wait))
    if not cfg.start_order:
        raise ValueError('no VMs in [start_order]')

    if parser.has_section('stop_order'):
        cfg.stop_order = list(parser['stop_order'])
    else:
        cfg.stop_order = [name for name, _ in reversed(cfg.start_order)]
    return cfg


class JournalFormatter(logging.Formatter):
    """Prefix messages with <priority> so journald records the log level."""
    PRIORITY = {logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4,
                logging.ERROR: 3, logging.CRITICAL: 2}

    def format(self, record):
        return '<{}>{}'.format(self.PRIORITY.get(record.levelno, 6),
                               super().format(record))


def setup_logging(verbose):
    handler = logging.StreamHandler(sys.stdout)
    if os.environ.get('JOURNAL_STREAM'):
        handler.setFormatter(JournalFormatter('%(message)s'))
    else:
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s: %(message)s'))
    log.addHandler(handler)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)


def sleep_until(deadline):
    time.sleep(max(0.0, deadline - time.monotonic()))


# ----------------------------------------------------------------------
def disk_utilization(disk, interval):
    """Return %util of disk measured over interval seconds, None if unknown disk."""
    out = subprocess.run(['iostat', '-ymxN', '-o', 'JSON', str(interval), '1'],
                         capture_output=True, text=True, check=True).stdout
    for dev in json.loads(out)['sysstat']['hosts'][0]['statistics'][0]['disk']:
        if dev['disk_device'] == disk:
            return dev['util']
    return None


def wait_after_start(cfg, max_wait):
    """Wait max_wait seconds, or less once cfg.disk is below cfg.util_limit."""
    deadline = time.monotonic() + max_wait
    if not cfg.disk or max_wait <= cfg.min_wait:
        sleep_until(deadline)
        return

    start = time.monotonic()
    # the first iostat sample takes iostat_interval seconds itself
    time.sleep(max(0.0, cfg.min_wait - cfg.iostat_interval))
    while time.monotonic() + cfg.iostat_interval <= deadline:
        try:
            util = disk_utilization(cfg.disk, cfg.iostat_interval)
        except (OSError, subprocess.CalledProcessError, ValueError, KeyError, IndexError) as e:
            log.warning('Cannot measure disk utilization (%s), waiting the full %gs', e, max_wait)
            break
        if util is None:
            log.warning("Disk '%s' not found by iostat, waiting the full %gs", cfg.disk, max_wait)
            break
        if util < cfg.util_limit:
            log.info('Disk utilization of %s is %.1f%% (< %g%%), continuing after %.0fs',
                     cfg.disk, util, cfg.util_limit, time.monotonic() - start)
            return
        log.debug('Disk utilization of %s is %.1f%%, still waiting', cfg.disk, util)
    sleep_until(deadline)


def start_vms(conn, cfg):
    ok = True
    for i, (name, max_wait) in enumerate(cfg.start_order):
        try:
            dom = conn.lookupByName(name)
            if dom.isActive():
                log.info("VM '%s' is already running", name)
                continue
            dom.create()
        except libvirt.libvirtError as e:
            log.error("Cannot start VM '%s': %s", name, e)
            ok = False
            continue

        if i == len(cfg.start_order) - 1:
            log.info("Started '%s'", name)
        else:
            log.info("Started '%s'. Waiting up to %gs before starting '%s'",
                     name, max_wait, cfg.start_order[i + 1][0])
            wait_after_start(cfg, max_wait)

    log.info('All VMs have been started' if ok else 'Finished starting VMs, some failed')
    return ok


# ----------------------------------------------------------------------
def is_active(dom):
    try:
        return bool(dom.isActive())
    except libvirt.libvirtError as e:
        # transient domains vanish when they stop
        if e.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            return False
        raise


def stop_vm(uri, name, cfg):
    """Shut down one VM, destroy it after cfg.shutdown_timeout. Return True on success."""
    # libvirt connections must not be shared between threads
    with libvirt.open(uri) as conn:
        try:
            dom = conn.lookupByName(name)
        except libvirt.libvirtError as e:
            log.error("Cannot find VM '%s': %s", name, e)
            return False
        if not is_active(dom):
            log.info("VM '%s' is already stopped", name)
            return True

        log.info("Stopping VM '%s'", name)
        start = time.monotonic()
        next_request = start
        while is_active(dom):
            now = time.monotonic()
            if now >= start + cfg.shutdown_timeout:
                log.warning("VM '%s' did not shut down within %gs, destroying it",
                            name, cfg.shutdown_timeout)
                try:
                    dom.destroy()
                except libvirt.libvirtError as e:
                    if is_active(dom):
                        log.error("Cannot destroy VM '%s': %s", name, e)
                        return False
                break
            # guests that are still booting or busy may miss the first ACPI request
            if now >= next_request:
                try:
                    dom.shutdown()
                except libvirt.libvirtError as e:
                    if e.get_error_code() != libvirt.VIR_ERR_OPERATION_INVALID:
                        log.warning("Shutdown request for VM '%s' failed: %s", name, e)
                next_request = now + cfg.shutdown_retry_interval
            time.sleep(1)

        log.info("VM '%s' stopped after %.0fs", name, time.monotonic() - start)
        return True


def stop_vms(conn, cfg, stop_all=False):
    names = list(cfg.stop_order)
    if stop_all:
        running = sorted(dom.name() for dom in
                         conn.listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE))
        # not configured means no dependencies are known, so stop them first
        extra = [name for name in running if name not in names]
        if extra:
            log.info('Also stopping VMs that are not in the config: %s', ', '.join(extra))
        names = extra + names

    last = [name for name in cfg.shutdown_last if name in names]
    first = [name for name in names if name not in last]
    workers = cfg.max_parallel_shutdowns
    log.info('Stopping %d VMs, up to %d in parallel (worst case %.0fs)', len(names), workers,
             worst_case_stop_time(cfg, len(first), len(last)))
    uri = conn.getURI()
    ok = stop_parallel(uri, first, cfg)
    if last:
        log.info('All other VMs are stopped, now stopping: %s', ', '.join(last))
        ok = stop_parallel(uri, last, cfg) and ok

    log.info('All VMs have been stopped' if ok else 'Finished stopping VMs, some failed')
    return ok


def stop_parallel(uri, names, cfg):
    """Stop the VMs in names, up to cfg.max_parallel_shutdowns at a time."""
    ok = True
    # the executor starts submitted jobs in order, so the stop order is kept
    with ThreadPoolExecutor(max_workers=cfg.max_parallel_shutdowns) as pool:
        futures = {pool.submit(stop_vm, uri, name, cfg): name for name in names}
        for future in as_completed(futures):
            try:
                ok = future.result() and ok
            except libvirt.libvirtError as e:
                log.error("Stopping VM '%s' failed: %s", futures[future], e)
                ok = False
    return ok


def worst_case_stop_time(cfg, n_first, n_last):
    workers = cfg.max_parallel_shutdowns
    return (math.ceil(n_first / workers) + math.ceil(n_last / workers)) * cfg.shutdown_timeout


# ----------------------------------------------------------------------
def show_status(conn, cfg):
    domains = {dom.name(): dom for dom in conn.listAllDomains()}
    configured = [name for name, _ in cfg.start_order]
    ok = True

    print('Configured start order:')
    for i, (name, max_wait) in enumerate(cfg.start_order, 1):
        dom = domains.get(name)
        if dom is None:
            state = 'NOT DEFINED'
            ok = False
        else:
            state = 'running' if dom.isActive() else 'shut off'
        if name in cfg.shutdown_last:
            state += ' (shutdown last)'
        print('  {:2}. {:30} {:>5g}s  {}'.format(i, name, max_wait, state))

    others = sorted(set(domains) - set(configured))
    if others:
        print('Not in config:')
        for name in others:
            print('      {:30}         {}'.format(
                name, 'running' if domains[name].isActive() else 'shut off'))

    undefined = [name for name in cfg.shutdown_last if name not in domains]
    if undefined:
        print('shutdown_last lists undefined VMs: {}'.format(', '.join(undefined)))
        ok = False
    return ok


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('-c', '--config', default=DEFAULT_CONFIG,
                        help='config file (default: %(default)s)')
    parser.add_argument('-v', '--verbose', action='store_true', help='enable debug output')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('start', help='start the VMs in the configured order')
    for command, text in (('stop', 'shut down the VMs in the stop order'),
                          ('restart', 'stop, then start the VMs')):
        sub = commands.add_parser(command, help=text)
        sub.add_argument('--stop-all', action='store_true',
                         help='also shut down running VMs that are not in the config')
    commands.add_parser('status', help='show the configured VMs and their state')
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    try:
        cfg = load_config(args.config)
    except (OSError, configparser.Error, ValueError) as e:
        log.error('Invalid config %s: %s', args.config, e)
        return 2

    try:
        with libvirt.open(cfg.uri) as conn:
            if args.command == 'start':
                ok = start_vms(conn, cfg)
            elif args.command == 'stop':
                ok = stop_vms(conn, cfg, args.stop_all)
            elif args.command == 'restart':
                ok = stop_vms(conn, cfg, args.stop_all)
                ok = start_vms(conn, cfg) and ok
            else:
                ok = show_status(conn, cfg)
    except libvirt.libvirtError as e:
        log.error('libvirt error: %s', e)
        return 1
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
