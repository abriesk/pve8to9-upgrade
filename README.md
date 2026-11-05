# pve8to9-upgrade — Proxmox VE 8 → 9, one node at a time


A single bash script that upgrades one Proxmox VE 8.x node to 9 in ten resumable phases. It is **dry-run by default**,
cluster-aware, checks before it changes anything, stops with one line when a check fails, and remembers how far it got.
Written for homelab and small clusters that upgrade node by node, with ZFS, NFS and NVIDIA cards in the mix.

> **No warranty.** This changes your hypervisor's packages, kernel and boot setup. Take full backups first, read the
> script, rehearse on a test VM. See the licence (MIT).

## Requirements and limits

- Run **on the node, as root**, in `tmux` or on the console (the ssh session drops during the dist-upgrade and the reboot).
- Perl (core `JSON::PP`), `pvesh`, `pveum`, `pvecm`, `pve8to9` (installed by the 8.4 update) and the usual Debian tools.
- **GRUB boot only.** A node booting with systemd-boot (`/etc/kernel/proxmox-boot-uuids`, an ESP) STOPs.
- **No-subscription repositories only**: `SUBSCRIPTION=yes` STOPs. The upgrade disables enterprise sources.
- **Ceph: squid only.** A Ceph repo for another release STOPs; upgrade Ceph first.
- Never touches corosync, `/etc/pve`, storage config, guests or `/etc/network/interfaces`. NIC names are pinned to their
  current names by systemd `.link` files.
- Guests are never started, stopped or migrated: the node must be drained (no running guest) before every phase that
  changes it, except `preclean` and `fetch`.

## Quick start

`mynode` is a profile name you choose (lowercase letters, digits, hyphens; `hosts/mynode.conf`), not necessarily the node name: `NAME=` in the profile holds the real `hostname -s`, which the guards compare against.

```
git clone https://github.com/abriesk/pve8to9-upgrade && cd pve8to9-upgrade   # or copy the files to the node
mkdir -p hosts && ./pve-upgrade mynode init > hosts/mynode.conf || rm -f hosts/mynode.conf   # a SUGGESTED profile - read and edit it
cp driver.conf.example driver.conf    # NVIDIA nodes only; put the .run it names in /root/pve-upgrade/
./pve-upgrade mynode          # dry run: every phase, what it would run, the checks that can run now
./pve-upgrade mynode precheck --apply
./pve-upgrade mynode preclean --apply
./pve-upgrade mynode fetch --apply    # the day before: downloads everything, changes no config; STOPs on removals you must review
# in the window, with the guests drained:
./pve-upgrade mynode all --apply      # stops before the reboot: "NEXT reboot: announce it ..."
./pve-upgrade mynode reboot --apply   # (or all --apply again); after the reboot:
./pve-upgrade mynode recheck --apply  # prints RECHECK OK <host>
```

`init` is read-only: it detects name, GPUs, ZFS pool, NFS server role, cluster, shared storage, Ceph and running guests and
prints a suggested profile with a comment per line. The profile stays required. `--through-reboot` makes `all --apply` run
driver → reboot without the pause. `--gpu yes|no` settles a disagreement between profile and detection (kept in
`/root/pve-upgrade/gpu`). Host names may hold `[a-z0-9-]`.

## Phases

`all` runs the due phases in order; a single phase runs only when it is the due one (state moves forward only).

| phase | checks | changes |
|---|---|---|
| `precheck` | hostname, quorum, free space, failed units, NIC with no device, pins, backups running or due, `maxfiles`, repos, ZFS, `pve8to9`, intel-microcode, driver sha256 | records roles, ACLs, API token names, backup jobs, NFS and ZFS state; backs up `/etc` |
| `preclean` | kernel packages in a half state; apt simulation | purges old kernels via apt, caches, old logs; lists (never removes) backups and ISOs; ≥ 10 GB free after |
| `fetch` | trixie dist-upgrade simulation, removals (STOPs on any not in REMOVE_OK) | downloads the 8.4 and 9 packages to `/var/cache/apt`; probes third-party repos; writes no apt config |
| `drain` | no running guest, no `GUARD_PROCS` process, `STAY` guests down | nothing |
| `to-8.4` | gates (backup due, vzdump running, NFS quiet), pin, 8.4.1+ , `pve8to9` | **point of no return (small):** bookworm dist-upgrade; reboots if the kernel changed |
| `prepare` | duplicate MACs, `pve8to9` FAILURES: 0 | pins NIC names, rebuilds initramfs, removes the systemd-boot meta-package (GRUB hosts), purges Debian `nvidia-*` |
| `upgrade` | gates, removal guard, repo plan | **point of no return:** trixie sources, `dist-upgrade`, disables enterprise sources |
| `driver` | .run sha256 | installs the NVIDIA .run with dkms for the new kernel, persistence daemon (skipped when no GPU) |
| `reboot` | NIC pinning, dkms built, ZFS healthy and cachefile, gates | **point of no return:** reboots |
| `recheck` | booted kernel, `pve8to9`, quorum, GPUs, offloads, units, files, ZFS, NFS and backups equal to precheck's record, apt sources | sets a lost ZFS cachefile again |

On an Intel host `precheck` stops until `intel-microcode` is installed; the package is in Debian's `non-free-firmware` component, so enable that on the host's Debian apt entries, `apt-get update`, then `apt-get install intel-microcode` (the script edits no sources for this).

## Profile keys (`hosts/<host>.conf`, shell assignments)

| key | default | meaning |
|---|---|---|
| `NAME` | required | `hostname -s`; every `--apply` checks it |
| `NVIDIA` | required | `yes`/`no`; detection must agree or `--gpu` settles it |
| `GPUS` | required if `NVIDIA=yes` | cards expected (0 = no card, module checks only) |
| `NICS` | empty | physical NICs whose TSO/GSO/GRO must be off after the reboot |
| `ZFS_POOL` | empty | a data pool to check, record and compare |
| `STAY` | empty | guest IDs that must be down (you stop them) |
| `UNITS` / `FILES` | empty | systemd units that must run, files that must exist after the reboot |
| `CLUSTER` | `yes` | `no` skips quorum checks |
| `SHARED` | empty | mount point `/mnt/pve/<name>` of the NFS storage; recheck probes it and checks pvesm `<name>` |
| `NFS_SERVER` | `no` | this node exports the share: records the exports; to-8.4, upgrade, reboot need `--nfs-quiet` (one run) |
| `CEPH` | empty | `no` excuses pve8to9's three Ceph-status FAILs (STOPs if Ceph is found) |
| `SUBSCRIPTION` | `no` | `yes` is not supported |
| `REMOVE_OK` | empty | packages the trixie simulation may remove (t64 renames always pass) |
| `GUARD_PROCS` | empty | process names (`pgrep -f`) that block every host-changing phase |
| `HOLD_HINT` | empty | text shown when the drain refuses (how you quiesce your workloads) |
| `NV_RUN`, `NV_SHA256` | `driver.conf` | the NVIDIA .run and its sha256; the profile wins over `driver.conf` |

`driver.conf` is read only when `NVIDIA=yes`. Examples: `examples/*.conf`.

## State, logs, recovery

`/root/pve-upgrade/`: `state` (last finished phase), `log` (every command, rc, timestamp), `expect-kernel`, `etc-before-pve9.tgz`,
and the records precheck makes. A failed check prints `STOP <phase>: <check>: <seen>` and the phase stays unfinished: fix, rerun.
A node that resets mid-upgrade boots half-upgraded, maybe without network: from the console run the next phase with
`--apply` — `recover` runs first, offline (`dpkg --configure -a`, `apt-get -f install --no-download` from the fetched cache).
An API token record lists token **names** only, never a value.

## Package removals

Moving to Debian 13 removes packages whose names no longer exist there. A plain `t64` rename passes by itself. Anything else
must be named in the profile's `REMOVE_OK`, or `fetch` and `upgrade` STOP, before either changes anything.

**`fetch --apply` STOPs on this the day before, while your guests still run.** `precheck` can't show the list, because it needs Debian 13's package lists. `fetch` prints the names and a ready `REMOVE_OK=` line, and saves the list to `/root/pve-upgrade/removals`. Review the names, add the line to your profile, and rerun `fetch`: what it downloaded stays.

How to read the list:

- **Usually fine:** old library names and Python modules Debian 13 dropped or renamed. For example, one real node listed
  `libgnutlsxx30 libqt5core5a libsmbclient libzfs4linux python3-distutils python3-future python3-lib2to3`. Yours may differ.
- **Stop and find out why:** `proxmox-ve`, anything starting `pve-`, `qemu-server`, `zfsutils-linux`, `ceph` packages, or
  something you installed on purpose. Don't add these to `REMOVE_OK`: on Proxmox, these on the list usually mean a wrong repository.
- **Your own scripts:** apt lists every package that depends on a removed one, but it can't see software it didn't install,
  such as a `pip` tool that uses `python3-future`. Check those yourself.

To accept the list, put the names you reviewed in `hosts/mynode.conf`:

```
REMOVE_OK="libgnutlsxx30 python3-distutils"
```

`upgrade` runs the simulation again, because `to-8.4` can change the list. A new name STOPs it again, still before any change.

## Refusals

| STOP | do |
|---|---|
| `drain` / `guests` / `guard-procs` | stop or migrate the guests and the listed workloads, rerun |
| `nfs-quiet` | confirm the share is idle (no backup job, no transfer running), rerun with `--nfs-quiet` |
| `backup-schedule` / `vzdump` | wait for the job, or disable it for the window and re-enable it before `recheck` |
| `nic-device` | comment out the `/etc/network/interfaces` stanzas of a NIC that is gone |
| `kernel-pin` | `proxmox-boot-tool kernel unpin` |
| `systemd-boot` | the node boots systemd-boot (or the removal would take more than the meta-package): decide by hand |
| `subscription`, `ceph-repo` | not supported / upgrade Ceph to squid first |
| `removals` | nothing has changed yet: review the names as in [Package removals](#package-removals), add them to `REMOVE_OK`, rerun |
| `gpu decision` | rerun with `--gpu yes|no` |
| `recheck` | each listed difference is a thing to look at before the node rejoins service |

## NVIDIA nodes

The driver phase installs NVIDIA's `.run` with `--dkms` for the **new** kernel, plus `nvidia-persistenced`; Debian's `nvidia-*`
packages are purged in `prepare` so their dkms never builds during the dist-upgrade. The reboot is refused unless dkms
built the module for the kernel it will boot. Tested: Quadro M4000 (Maxwell), RTX 3060, driver 580.

## Lessons from real runs

- **A slow mirror:** copy the missing `.deb`s from an already-upgraded node's `/var/cache/apt/archives`, then `fetch --apply` again; apt checks them against the index.
- **A NIC stanza with no device** stops `prepare` at NIC pinning; precheck now catches it a day early.
- **A disabled unit restarted by the upgrade** (the dist-upgrade restarts services): `systemctl mask --now <unit>` for the run, unmask after `recheck`; disable alone is not enough.
- **The ZFS cache file after the reboot:** a pool imported by scan leaves `zpool.cache` empty; recheck sets it again.
- Budget hours, not minutes, on a slow link; the window is the install time only if `fetch` ran the day before.

## Tested on

A four-node homelab cluster (ZFS, NFS server node, NVIDIA cards), PVE 8.4 → 9, and a single-node test VM. The test suite
(`python3 tests/test-pve-upgrade.py`) runs the script against a fake root with shimmed commands; nothing in it touches a host.

## Licence

MIT, see `LICENSE`. No warranty; full backups first.
