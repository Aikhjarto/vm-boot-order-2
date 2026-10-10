# vm-boot-order-2

Starts libvirt VMs one after another in a configured order at boot, and shuts
them down in parallel at system shutdown.

- **Start:** after each VM, wait up to a configured time before starting the
  next one. If a disk is configured, the wait ends early as soon as that
  disk's utilization (`iostat` `%util`) drops below a limit, so VMs don't all
  compete for I/O while they boot.
- **Stop:** send shutdown requests (ACPI) to several VMs at once, in a
  configured order. Requests are repeated for guests that miss the first one.
  A VM that hasn't stopped after a timeout is destroyed (powered off).

This is a rewrite of `vm-boot-order.py`, which was based on
[a gist by cyberang3l](https://gist.github.com/cyberang3l/f4c8b1ab6fc48374fbae9553d89e5eed).

## Requirements

- Python 3.7 or newer
- libvirt Python bindings (`import libvirt`)
- `iostat` from sysstat, only if `disk` is set in the config

## Installation

```sh
install -m 755 vm-boot-order-2.py /usr/local/sbin/
install -m 644 vm-boot-order-2.service /etc/systemd/system/
install -m 644 vm-boot-order-2.conf /etc/   # then edit it
systemctl daemon-reload
```

Check the config before enabling the service:

```sh
vm-boot-order-2.py status
```

The VMs started by this service should not also be started by libvirt
autostart (`virsh autostart --disable <vm>`).

## Usage

```
vm-boot-order-2.py [-c CONFIG] [-v] {start,stop,restart,status}
```

| Command | Action |
|---|---|
| `start` | Start the VMs in `[start_order]`. VMs that are already running are skipped. |
| `stop [--stop-all]` | Shut down the VMs in the stop order. With `--stop-all`, running VMs that aren't in the config are shut down too, before the configured ones. |
| `restart [--stop-all]` | `stop`, then `start`. |
| `status` | List the configured VMs in start order with their state, and the VMs that aren't in the config. |

`-c` selects a config file other than `/etc/vm-boot-order-2.conf`; `-v`
enables debug output.

The exit code is 0 on success, 1 if any VM failed to start or stop (the others
are still processed), and 2 for an invalid config.

## Configuration

`/etc/vm-boot-order-2.conf` uses INI syntax. See
[vm-boot-order-2.conf](vm-boot-order-2.conf) for a commented example.

```ini
[settings]
disk = nvme0n1
shutdown_timeout = 180
max_parallel_shutdowns = 5
shutdown_last = Debian_syslog

[start_order]
Debian_syslog = 90
HAOS = 90
Leap_build
```

`[start_order]` lists the VM names in start order. The value is the maximum
number of seconds to wait before starting the next VM (default:
`default_wait`). The optional `[stop_order]` section lists names only. Without
it, the VMs are stopped in reverse start order.

| Setting | Default | Meaning |
|---|---|---|
| `uri` | `qemu:///system` | libvirt connection |
| `disk` | *(empty)* | Device name as shown by `iostat -N` (e.g. `nvme0n1`, `system-root`). Empty: always wait the full time. |
| `util_limit` | `80` | Disk utilization in % below which the next VM is started |
| `min_wait` | `5` | Minimum wait after each start, since a fresh VM doesn't load the disk yet |
| `iostat_interval` | `5` | Measuring interval in seconds (whole seconds) |
| `default_wait` | `0` | Wait time for VMs without a value in `[start_order]` |
| `shutdown_timeout` | `180` | Seconds until a VM that hasn't shut down is destroyed |
| `shutdown_retry_interval` | `30` | Seconds between repeated shutdown requests to the same VM |
| `max_parallel_shutdowns` | `5` | Number of VMs shut down at the same time |
| `shutdown_last` | *(empty)* | VMs that are shut down only after all other VMs have stopped, separated by spaces or commas |

Unknown sections and settings are reported as errors, so typos don't go
unnoticed.

### Shutdown timing

Because several VMs are shut down at the same time, the last VMs in the stop
order overlap. Put VMs that others depend on (e.g. a syslog or DNS server) in
`shutdown_last`. The shutdown then runs in two phases: first all other VMs,
then, once every one of them has stopped or been destroyed, the
`shutdown_last` VMs in the order given. A `shutdown_last` VM is only stopped
if it's in the stop order, or running when `--stop-all` is used.

The shutdown takes at most

    (ceil(other VMs / max_parallel_shutdowns)
     + ceil(shutdown_last VMs / max_parallel_shutdowns)) * shutdown_timeout

seconds. `TimeoutStopSec=` in the service must be larger than that, otherwise
systemd kills the script while VMs are still running. The service sets 900 s,
which fits 15 VMs with one of them in `shutdown_last` and the defaults
((3 + 1) * 180 s = 720 s). Adjust it when you add VMs or change the settings;
`stop` logs the worst case when it starts.

## systemd

`vm-boot-order-2.service` runs `start` at boot and `stop --stop-all` at
shutdown. It conflicts with the old `vm-boot-order.service`, so only one of
them can be active.

```sh
systemctl enable vm-boot-order-2.service
```

The script's log messages carry syslog priorities when it runs under systemd,
so warnings (a VM had to be destroyed) and errors show up as such in the
journal.

## Migrating from vm-boot-order.py

Convert the old config (`name wait` per line):

```sh
{ printf '[settings]\ndisk = nvme0n1\n\n[start_order]\n'
  grep -v '^\s*#' /etc/vm-boot-order.conf | awk 'NF { print $1 " = " $2 }'
} > /etc/vm-boot-order-2.conf
vm-boot-order-2.py status
```

Then switch the services at the next reboot:

```sh
systemctl disable vm-boot-order.service
systemctl enable vm-boot-order-2.service
reboot
```

Don't `systemctl start vm-boot-order-2` while the old service is active.
Because the units conflict, systemd would first stop the old service, and its
stop command shuts down all VMs. At the reboot above, the old service still
shuts the VMs down one last time; from then on the new one handles it.

Changes compared to `vm-boot-order.py`:

- A VM that ignores the shutdown is destroyed after the timeout. The old script
  crashed at this point and left the VM running.
- `--stop-all` works (it was ignored before), and VMs that are missing or
  already stopped no longer abort the run.
- Shutdown requests are repeated, and the disk, timeouts and parallelism are
  set in the config instead of the code.
- The old default timeouts could exceed the unit's `TimeoutStopSec=360`, so
  systemd killed the script during long shutdowns.

## Bash completion

`completions/vm-boot-order-2.py` completes the commands, the options and the
config file. The packages install it; otherwise copy it to bash-completion's
directory:

```sh
install -D -m 0644 completions/vm-boot-order-2.py /usr/share/bash-completion/completions/vm-boot-order-2.py
```

## Tests

```sh
python3 -m unittest -v
```

The tests need neither libvirt nor its Python bindings.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
