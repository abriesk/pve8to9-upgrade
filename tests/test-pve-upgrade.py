#!/usr/bin/env python3
"""tests/test-pve-upgrade.py - the PVE 8 -> 9 script (pve-upgrade) against a fake root and PATH shims; nothing runs on a host.
Each case copies the script dir to a temp dir (so driver.conf can differ), points PVEU_TEST_ROOT at a fake / (honoured
only when not root) and runs without a controlling tty. Profiles come from tests/fixtures/. PVEU_SRC names the script dir
when it is not found relative to this file."""
import hashlib, os, pathlib, shutil, subprocess, tempfile

HERE = pathlib.Path(__file__).resolve().parent
# the script's dir: PVEU_SRC, else the repo layout (deploy/pve-upgrade), else the export layout (the repo root)
SRC = pathlib.Path(os.environ["PVEU_SRC"]) if os.environ.get("PVEU_SRC") else next(
    p for p in (HERE.parent / "deploy/pve-upgrade", HERE.parent) if (p / "pve-upgrade").is_file())
FIX = HERE / "fixtures"
T = pathlib.Path(os.path.realpath(tempfile.mkdtemp()))
NEWK, OLDK = "6.14.8-2-pve", "6.8.12-9-pve"
fails = 0
PHASES = ["precheck", "preclean", "fetch", "drain", "to-8.4", "prepare", "upgrade", "driver", "reboot", "recheck"]


def check(name, got, want):
    global fails
    if got == want:
        print(f"ok   {name}")
    else:
        fails += 1
        print(f"FAIL {name} (want {want!r}, got {got!r})")


SHIMS = {
    "qm": '[ -z "${FAKE_QM_ERR:-}" ] || { echo "ipcc_send_rec[1] failed: Connection refused"; exit ${FAKE_QM_RC:-255}; }; echo "      VMID NAME   STATUS"; printf "%b" "${FAKE_QM:-}"',
    "pct": 'echo "VMID       Status     Lock         Name"; printf "%b" "${FAKE_PCT:-}"',
    "pgrep": '[ -n "${FAKE_PROC:-}" ] && [[ "$*" == *"[${FAKE_PROC:0:1}]${FAKE_PROC:1}"* ]] && { echo 4242; exit 0; }; exit 1',
    "pveversion": '[ "${1:-}" = -v ] && { echo "proxmox-ve: 8.4.0"; exit 0; }; echo "pve-manager/${FAKE_PVEVER:-8.4.14}/abc (running kernel: 6.8.12-9-pve)"',
    "pve8to9": '[ -z "${FAKE_VMMON:-}" ] || echo "FAIL: custom role \'mon\' uses VM.Monitor"; n=${FAKE_P89_FAIL:-0}; [ "$n" = 0 ] || echo "FAIL: something is wrong"; [ -z "${FAKE_SDB_FAIL:-}" ] || { [ -n "${FAKE_SDB_GONE:-}" ] && [ -e "$FAKE_SDB_GONE" ]; } || { echo "FAIL: systemd-boot meta-package installed. This will cause problems on upgrades of other boot-related packages. Remove \'systemd-boot\' See https://pve.proxmox.com/wiki/Upgrade_from_8_to_9#sd-boot-warning for more information"; n=$((n+1)); }; [ -z "${FAKE_CEPH_FAIL:-}" ] || { echo "FAIL: failed to get \'noout\' flag status - got timeout"; echo "FAIL: unable to determine Ceph status!"; echo "FAIL: unable to determine Ceph daemon versions!"; n=$((n+3)); }; echo "TOTAL: 40"; [ -n "${FAKE_P89_NOLINE:-}" ] || printf "\033[1;31mFAILURES: %s\033[0m\n" "$n"',
    "pvecm": 'echo "Quorate:          ${FAKE_QUORATE:-Yes}"',
    "lspci": 'echo "00:02.0 VGA compatible controller [0300]: Intel Corporation [8086:0412]"; [ -z "${FAKE_CARD:-}" ] || echo "01:00.0 VGA compatible controller [0300]: NVIDIA Corporation GM204GL [Quadro M4000] [10de:13f1]"',
    "lsmod": 'echo "Module Size Used"; [ -z "${FAKE_LSMOD_NV:-}" ] || echo "nvidia 123 0"',
    "dpkg-query": 'case "$*" in *Depends*) echo "${FAKE_DEPS:-}" | tr ";" "\n" | sed -n "s/^${!#}=//p";; *systemd-boot*) printf "%b" "${FAKE_SDB:-}"; exit ${FAKE_SDB_RC:-0};; *intel-microcode*) printf "%s" "${FAKE_MC-install ok installed}";; *kernel*) echo "dpkg-query: no packages found matching pve-kernel-*" >&2; printf "%b" "${FAKE_DPKG_K:-}";; *nvidia*) echo "dpkg-query: no packages found matching libnvidia-*" >&2; printf "%b" "${FAKE_DPKG_NV:-}";; *) printf "%b" "${FAKE_DPKG_ALL:-}"; exit ${FAKE_DPKGQ_RC:-0};; esac',
    # FAKE_AUDIT = what `dpkg --audit` prints until `dpkg --configure -a` ran (FAKE_AUDIT_STUCK: never cleared)
    "dpkg": '[ "${1:-}" = --audit ] && { [ -f "$FAKE_LOG.fixed" ] && [ -z "${FAKE_AUDIT_STUCK:-}" ] || printf "%b" "${FAKE_AUDIT:-}"; exit 0; }; [ "${1:-}" = --configure ] && : > "$FAKE_LOG.fixed"; [ "${1:-}" = -S ] || exit 0; case " ${FAKE_OWNED:-} " in *" $2 "*) echo "pkg: $2";; *) exit 1;; esac',
    "fuser": 'case " ${FAKE_OPEN:-} " in *" ${!###*/} "*) exit 0;; esac; exit 1',
    "journalctl": "exit 0",
    "zpool": 'case "$1 ${2:-}" in "status -x") [ -n "${FAKE_ZDEG:-}" ] && echo "  pool: tank state: DEGRADED" || echo "pool \'tank\' is healthy";;'
             ' status*) echo " state: ONLINE"; [ -z "${FAKE_ZSCRUB:-}" ] || echo "  scan: scrub in progress since Sat";;'
             ' "get -H") echo "${FAKE_ZCF:--}";; set*) [ -n "${FAKE_ZSET_NOFILE:-}" ] || echo x > "$PVEU_TEST_ROOT/etc/zfs/zpool.cache";; get*) echo "tank size 1T -";; list*) echo ONLINE;; esac',
    "zfs": 'printf "%b" "${FAKE_ZVOLS:-tank/vm-100-disk-0\\ntank/vm-103-disk-0}"',
    "dkms": '[ -n "${FAKE_DKMS+x}" ] && { printf "%b\\n" "$FAKE_DKMS"; exit 0; }; cat "$PVEU_TEST_ROOT/../dkms" 2>/dev/null; true',
    "nvidia-smi": '[ "${1:-}" = -q ] && { for i in $(seq 1 ${FAKE_SMI_N:-2}); do echo "    Persistence Mode                      : ${FAKE_PM:-Enabled}"; done; exit 0; }; for i in $(seq 1 ${FAKE_SMI_N:-2}); do echo "GPU $i: Quadro M4000"; done',
    "systemctl": 'case "$1" in --failed) printf "%b" "${FAKE_FAILEDU:-}"; exit 0;; is-enabled) [ -z "${FAKE_NOTENABLED:-}" ] || { echo disabled; exit 1; }; echo enabled; exit 0;; list-units) [ -z "${FAKE_SYSCTL_RC:-}" ] || exit $FAKE_SYSCTL_RC; [ -z "${FAKE_CEPH_UNIT:-}" ] || echo "$FAKE_CEPH_UNIT loaded active running Ceph"; exit 0;; is-failed) case " ${FAKE_FAILED:-} " in *" ${!#} "*) echo failed; exit 0;; esac; echo active; exit 1;; cat) case " ${FAKE_TIMERS:-} " in *" ${!#} "*) exit 0;; esac; exit 1;; esac; case " ${FAKE_INACTIVE:-} " in *" ${!#} "*) echo inactive; exit 3;; esac; echo active',
    "ethtool": 'v=off; [ -z "${FAKE_OFFLOAD_ON:-}" ] || v=on; printf "tcp-segmentation-offload: %s\\ngeneric-segmentation-offload: %s\\ngeneric-receive-offload: %s\\n" $v $v $v',
    "findmnt": 'case "$*" in *nfs,nfs4*) printf "%b\\n" "${FAKE_NFSLINE:-/mnt/pve/ShareBk vers=4.2${FAKE_NFSOPT:-}}";; *FSTYPE*) echo "${FAKE_ROOTFS:-ext4}";; *SOURCE*) echo "${FAKE_ROOTSRC:-/dev/mapper/pve-root}";; */mnt/pve/*) [ -z "${FAKE_NOSHARED:-}" ];; esac',
    "df": 'f=$PVEU_TEST_ROOT/../dfn; v=${FAKE_FREE:-50}; [ -e $f ] && v=${FAKE_FREE_AFTER:-$v}; : > $f; printf " Avail\\n %sG\\n" "$v"',
    "hostname": 'echo "${FAKE_HOSTNAME:-pvetest}"',
    # after a reboot the host boots the kernel reboot_now expected, unless FAKE_UNAME says otherwise
    "uname": 'e="$PVEU_TEST_ROOT/root/pve-upgrade/expect-kernel"; if [ -n "${FAKE_UNAME:-}" ]; then echo "$FAKE_UNAME"; '
             'elif [ -f "$FAKE_REBOOTED" ] && [ -f "$e" ]; then cat "$e"; else echo 6.8.12-9-pve; fi',
    "id": '[ $# -ge 2 ] || { echo 1000; exit 0; }; [ -z "${FAKE_NOPUSER:-}" ] || { echo "id: ${!#}: no such user" >&2; exit 1; }; echo 999',
    "apt": 'exit ${FAKE_APT_RC:-0}',
    "apt-get": '[ "$*" = update ] && [ -n "${FAKE_UPD_OUT:-}" ] && { printf "%b" "$FAKE_UPD_OUT"; exit 0; }; [ -n "${FAKE_ENT_DU:-}" ] && [[ " $* " == *" dist-upgrade"* && " $* " != *" -d "* && " $* " != *" -s "* ]] && printf "Types: deb\\nURIs: https://enterprise.proxmox.com/debian/pve\\nSuites: trixie\\nComponents: pve-enterprise\\n" > $PVEU_TEST_ROOT/etc/apt/sources.list.d/pve-enterprise.sources; [[ "$*" == *" update" ]] && for a; do case "$a" in Dir::Etc::sourceparts=*) [ -n "${FAKE_PROBE_BAD:-}" ] && grep -rqs -- "$FAKE_PROBE_BAD" "${a#*=}" && { echo "Err:1 http://bad trixie InRelease"; exit 0; };; esac; done; [ -n "${FAKE_HDR_RC:-}" ] && [[ " $* " == *" proxmox-default-headers"* ]] && exit $FAKE_HDR_RC; [ -n "${FAKE_PURGE_RC:-}" ] && [[ " $* " == *" purge "* ]] && { echo "E: held packages"; exit $FAKE_PURGE_RC; }; '
               '[ "$*" = "${APT_RM:-x}" ] && [ -n "${FAKE_SDB_GONE:-}" ] && : > "$FAKE_SDB_GONE"; [ -n "${FAKE_FETCH:-}" ] && [[ " $* " == *" -d dist-upgrade"* ]] && { mkdir -p $PVEU_TEST_ROOT/var/cache/apt/archives; truncate -s $FAKE_FETCH $PVEU_TEST_ROOT/var/cache/apt/archives/f$RANDOM.deb; }; for a; do case "$a" in Dir::Etc::sourceparts=*) for x in "${a#*=}"/*.sources "${a#*=}"/*.list; do [ -f "$x" ] || continue; echo "== ${x##*/}"; cat "$x"; done >> "$FAKE_LOG.parts";; esac; done; case "$*" in "-s remove"*) printf "%b" "${FAKE_SDB_SIM:-Remv systemd-boot [252.39-1~deb12u2]\n}";; "-s purge"*) printf "%b" "${FAKE_APT_SIM:-}";; *" -s dist-upgrade"*) printf "%b" "${FAKE_DU_SIM:-}";; esac; exit ${FAKE_APT_RC:-0}',
    "apt-cache": 'printf "%b" "${FAKE_POLICY:-proxmox-ve:\\n  Installed: 9.0.3\\n  Candidate: 9.0.3\\n  Version table:\\n *** 9.0.3 500\\n        500 http://download.proxmox.com/debian/pve trixie/pve-no-subscription amd64 Packages\\n        100 /var/lib/dpkg/status\\n}"',
    "tar": '[ -n "${FAKE_TAR_ERR:-}" ] && [ "$1" = -czf ] && { : > "$2"; echo "$FAKE_TAR_ERR" >&2; exit ${FAKE_TAR_RC:-1}; }; PATH=/usr/bin:/bin exec tar "$@"',
    "update-initramfs": "exit 0",
    "proxmox-boot-tool": '[ "${1:-}" = status ] && { [ -n "${FAKE_GRUB:-}" ] && { echo "E: /etc/kernel/proxmox-boot-uuids does not exist." >&2; exit 2; }; echo "4A2B-1C3D is configured with: uefi"; exit 0; }; [ "${1:-}" = refresh ] && { echo "skipping ESP sync"; exit 0; }; n=$(( $(cat "$FAKE_LOG.pbt" 2>/dev/null || echo 0) + 1 )); echo $n > "$FAKE_LOG.pbt"; [ -n "${FAKE_PBT_RC_FROM:-}" ] && [ $n -ge $FAKE_PBT_RC_FROM ] && { echo "E: no ESP configured"; exit 2; }; [ -z "${FAKE_PBT_RC:-}" ] || { echo "E: no ESP configured"; exit $FAKE_PBT_RC; }; [ -z "${FAKE_PBT_JUNK:-}" ] || { echo "usage: kernel <add|remove|list>"; exit 0; }; echo "Manually selected kernels:"; echo "None."; echo; echo "Automatically selected kernels:"; echo "6.8.12-9-pve"; [ -z "${FAKE_PIN:-}" ] || printf "\\nPinned kernel:\\n%s\\n" "$FAKE_PIN"; [ -z "${FAKE_NEXTPIN:-}" ] || printf "\\nKernel pinned on next-boot:\\n%s\\n" "$FAKE_NEXTPIN"',
    "pveum": 'case "$*" in "role list"*) echo "[{\\"roleid\\":\\"PVEAdmin\\",\\"special\\":1,\\"privs\\":\\"${FAKE_BUILTIN:-VM.Monitor}\\"},{\\"roleid\\":\\"mon\\",\\"special\\":0,\\"privs\\":\\"VM.Audit${FAKE_ROLES:-}\\"}]";;'
             ' "acl list"*) echo \'[{"path":"/","type":"user","ugid":"snapper@pve","roleid":"PVEAuditor","propagate":1}]\';;'
             ' "user list"*) echo \'[{"userid":"snapper@pve"}]\';;'
             ' *token*) [ -z "${FAKE_NOUSER:-}" ] || { echo "no such user"; exit 2; }; [ -z "${FAKE_NOTOK:-}" ] || { echo "[]"; exit 0; }; t=\'{"tokenid":"snap","value":"secret-xyz"}\'; [ -z "${FAKE_TOK2:-}" ] || t="$t,{\\"tokenid\\":\\"$FAKE_TOK2\\",\\"value\\":\\"secret-abc\\"}"; echo "[$t]";; esac',
    "pvesh": 'case "$*" in *schedule-analyze*) [ -z "${FAKE_SA_BAD:-}" ] || { echo "400 Parameter verification failed."; exit 255; }; [ -z "${FAKE_SA_JUNK:-}" ] || { echo "ipcc_send_rec[1] failed: Unknown error -1"; exit 0; }; [ -z "${FAKE_SA_RC:-}" ] || { echo "ipcc_send_rec failed"; exit 2; }; echo "[{\\"timestamp\\":$(( $(date +%s) + ${FAKE_CAL_MIN:-600} * 60 )),\\"utc\\":\\"x\\"}]";;'
             ' *backup*) [ -z "${FAKE_BK_BAD:-}" ] || { echo "ipcc_send_rec[1] failed"; exit 2; }; echo "[{\\"id\\":\\"daily\\",\\"schedule\\":\\"21:00\\",\\"enabled\\":${FAKE_BK_EN:-1},\\"starttime\\":\\"x\\"}]";;'
             ' *tasks*) [ -z "${FAKE_PVESH_BAD:-}" ] || { echo "ipcc_send_rec failed"; exit 0; }; [ -n "${FAKE_VZ:-}" ] && echo \'[{"type":"vzdump","upid":"UPID:node1:vz"}]\' || echo \'[{"type":"vzdump","upid":"UPID:old","endtime":5,"status":"OK"}]\';; esac',
    "pvesm": '[ -z "${FAKE_PVESM_RC:-}" ] || exit $FAKE_PVESM_RC; echo "Name Type Status"; echo "${FAKE_SHN:-ShareBk} nfs ${FAKE_PVESM:-active}"; [ -z "${FAKE_RBD:-}" ] || echo "ceph-vm rbd ${FAKE_RBD_ST:-active}"',
    "exportfs": 'echo "/tank/backup 10.0.0.0/24(rw${FAKE_EXPORT:-})"',
    "nfsconf": 'echo "[nfsd]"',
    "rpcinfo": 'echo "   program vers proto   port  service"; echo "100003 4 tcp 2049 nfs"; echo "100005 3 udp ${FAKE_RPCPORT:-20048} mountd"',
    # the real mv; FAKE_MV_KILL=<name> kills the script right after it renames onto that name (a power cut)
    "mv": 'PATH=/usr/bin:/bin mv "$@" || exit; [ -n "${FAKE_MV_KILL:-}" ] && [[ "${!#}" == */"$FAKE_MV_KILL" ]] && kill -9 $PPID; exit 0',
    "sync": '[ -n "${FAKE_SYNC_DIR_RC:-}" ] && [ -d "${!#}" ] && exit $FAKE_SYNC_DIR_RC; exit 0',
    "reboot": '[ -z "${FAKE_REBOOT_RC:-}" ] || exit $FAKE_REBOOT_RC; : > "$FAKE_REBOOTED"',
}


def make(n, profile="pvetest"):
    """A fresh case dir: a copy of the script dir, a fake root with /boot and the NV .run, shims on PATH."""
    c = T / n
    d, root, b = c / "pu", c / "root", c / "bin"
    shutil.copytree(SRC, d)   # the cases below were written for a no-GPU pvetest; the real profile (GPUS=0, NVIDIA=yes) has its own case
    (d / "hosts").mkdir(exist_ok=True)
    for f in FIX.glob("*.conf"):
        shutil.copy(f, d / "hosts" / f.name)
    (d / "hosts/pvetest.conf").write_text('NAME=pvetest\nNVIDIA=no\nNICS=""\nCLUSTER=no\nSHARED=""\nNFS_SERVER=no\nGUARD_PROCS="workerd"\n')
    for p in (root / "boot", root / "etc/apt/sources.list.d", root / "etc/modprobe.d", root / "etc/sysctl.d", root / "root/pve-upgrade", b):
        p.mkdir(parents=True, exist_ok=True)
    (root / f"boot/vmlinuz-{OLDK}").write_text("")
    (root / f"boot/vmlinuz-{NEWK}").write_text("")
    (root / "etc/apt/sources.list").write_text("deb http://deb.debian.org/debian bookworm main\n")
    (root / "usr/local/sbin").mkdir(parents=True)
    (root / "usr/local/sbin/agentd").write_text("")
    (root / "sys/class/net/enp0s25/device").mkdir(parents=True)
    (root / "sys/class/net/enp0s25/address").write_text("aa:bb:cc:dd:ee:01\n")
    (root / "etc/network").mkdir(parents=True)
    (root / "etc/network/interfaces").write_text("auto lo\niface lo inet loopback\niface enp0s25 inet manual\n"
                                                 "  post-up ethtool -K enp0s25 tso off gso off gro off\nauto vmbr0\niface vmbr0 inet static\n")
    lk = root / "usr/local/lib/systemd/network"   # as if prepare had pinned it
    lk.mkdir(parents=True)
    (lk / "10-pve-upgrade-enp0s25.link").write_text("[Match]\nMACAddress=aa:bb:cc:dd:ee:01\n\n[Link]\nName=enp0s25\n")
    (root / "mnt/pve/ShareBk").mkdir(parents=True); (root / "etc/pve").mkdir(parents=True)
    (root / "proc/fs/nfsd").mkdir(parents=True); (root / "proc/fs/nfsd/versions").write_text("+3 +4 +4.1 +4.2\n")
    (root / "etc/sysctl.conf").write_text("# comment\nvm.swappiness = 1\n")
    run = root / "root/pve-upgrade/NVIDIA-Linux-x86_64-580.159.run"
    # the fake .run "builds" dkms only for the kernel it is given (--kernel-name=), else for the running one
    run.write_text('echo "nvrun $*" >> "$FAKE_LOG"; k=%s; for a; do case "$a" in --kernel-name=*) k="${a#*=}";; esac; done\n'
                   'echo "nvidia/580.159, $k, x86_64: installed" > "$PVEU_TEST_ROOT/../dkms"\n' % OLDK)
    sha = hashlib.sha256(run.read_bytes()).hexdigest()
    (d / "driver.conf").write_text(f"NV_RUN=NVIDIA-Linux-x86_64-580.159.run\nNV_SHA256={sha}\n")
    for name, body in SHIMS.items():
        f = b / name
        f.write_text(f'#!/bin/bash\necho "{name} $*" >> "$FAKE_LOG"\n{body}\n')
        f.chmod(0o755)
    host = dict(l.split("=", 1) for l in (d / f"hosts/{profile}.conf").read_text().splitlines()
                if "=" in l and not l.startswith("#"))["NAME"].strip('"')
    env = {"PATH": f"{b}:/usr/bin:/bin", "HOME": str(c), "PVEU_TEST_ROOT": str(root), "FAKE_LOG": str(c / "calls"),
           "FAKE_REBOOTED": str(c / "rebooted"), "FAKE_HOSTNAME": host}
    return c, d, root, env, host


def go(c, d, env, *args, **fake):
    keep = fake.pop("KEEP_BW", None)   # recheck runs after the upgrade: make()'s bookworm sources.list is trixie by then , unless a case keeps it
    if ("recheck" in args or (args[-2:] == ("all", "--apply") and (pathlib.Path(env["PVEU_TEST_ROOT"]) / "root/pve-upgrade/state").exists() and (pathlib.Path(env["PVEU_TEST_ROOT"]) / "root/pve-upgrade/state").read_text(errors="ignore").strip() == "reboot")) and "--apply" in args and not keep:
        sl = pathlib.Path(env["PVEU_TEST_ROOT"]) / "etc/apt/sources.list"
        sl.write_text(sl.read_text().replace("bookworm", "trixie"))
    e = dict(env, **{k: str(v) for k, v in fake.items()})
    r = subprocess.run(["bash", str(d / "pve-upgrade"), *args], env=e, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, start_new_session=True)
    return r


def calls(c):
    p = c / "calls"
    return p.read_text() if p.exists() else ""


def tree(root):
    return sorted((str(p.relative_to(root)), p.read_bytes() if p.is_file() else b"") for p in root.rglob("*"))


def state(root, v=None):
    p = root / "root/pve-upgrade/state"
    if v is not None:
        p.write_text(v + "\n")
    return p.read_text().strip() if p.exists() else ""


# (a) dry run: every phase printed, nothing changed, no mutating command called
c, d, root, env, host = make("a")
before = tree(root)
r = go(c, d, env, "pvetest", "all")
out = r.stdout + r.stderr
check("a: dry run exit 0", r.returncode, 0)
check("a: every phase printed", [p for p in PHASES if f"== {p}" not in out], [])
check("a: fake root unchanged", tree(root) == before, True)
mut = [l for l in calls(c).splitlines() if " -s " not in l and l.split()[0] in ("apt", "apt-get", "update-initramfs", "reboot", "pve-network-interface-pinning")]
check("a: no mutating command", mut, [])
check("a: says DRY RUN", "DRY RUN" in out, True)
check("a: prints the driver decision", "driver: skip" in out, True)

# (b) a running guest or a workerd process -> drain refuses, says what to do, state unchanged
for n, fake in (("b-qm", {"FAKE_QM": "  101 web running\\n"}), ("b-ct", {"FAKE_PCT": "200 running  ct1\\n"}),
                ("b-gp", {"FAKE_PROC": "workerd"})):
    c, d, root, env, host = make(n)
    state(root, "fetch")
    r = go(c, d, env, "pvetest", "drain", "--apply", **fake)
    check(f"{n}: drain refuses", r.returncode != 0, True)
    check(f"{n}: one STOP line naming drain", [l for l in r.stderr.splitlines() if l.startswith("STOP drain:")] != [], True)
    check(f"{n}: says what to do", "workerd running" in r.stderr or n != "b-gp", True)
    check(f"{n}: state still fetch", state(root), "fetch")
c, d, root, env, host = make("b-ok")
state(root, "fetch")
r = go(c, d, env, "pvetest", "drain", "--apply")
check("b-ok: empty host drains", (r.returncode, state(root)), (0, "drain"))

# (c) no-GPU profile + no card -> driver skipped with one log line, no driver command
c, d, root, env, host = make("c")
state(root, "upgrade")
r = go(c, d, env, "pvetest", "driver", "--apply")
check("c: driver phase ok", r.returncode, 0)
log = (root / "root/pve-upgrade/log").read_text()
check("c: one skip line in the log", len([l for l in log.splitlines() if "driver: skip" in l]), 1)
check("c: no driver command", [l for l in calls(c).splitlines() if l.split()[0] in ("update-initramfs", "dkms")], [])
check("c: state driver", state(root), "driver")

# (d) profile yes + no card -> a mismatch STOPs naming --gpu; --gpu no skips the driver, --gpu yes runs it
c, d, root, env, host = make("d", "host-a")
r = go(c, d, env, "host-a", "precheck", "--apply")
check("d: stops naming --gpu", (r.returncode != 0, "STOP driver: gpu decision:" in r.stderr, "--gpu" in r.stderr), (True, True, True))
check("d: no decision written", (root / "root/pve-upgrade/gpu").exists(), False)
r = go(c, d, env, "host-a", "precheck", "--apply", "--gpu", "no")
check("d: --gpu no accepted", (r.returncode, (root / "root/pve-upgrade/gpu").read_text().strip()), (0, "no"))
state(root, "upgrade")
r = go(c, d, env, "host-a", "driver", "--apply", "--gpu", "no")
check("d: --gpu no skips the driver", (r.returncode, "nvrun" in calls(c)), (0, False))
(root / "root/pve-upgrade/gpu").unlink()
state(root, "upgrade")
r = go(c, d, env, "host-a", "driver", "--apply", "--gpu", "yes")
check("d: --gpu yes runs the driver", "nvrun --dkms --silent" in calls(c), True)
check("d: the .run builds for the NEW kernel", f"--kernel-name={NEWK}" in calls(c), True)
check("d: persistenced unit installed", (root / "etc/systemd/system/nvidia-persistenced.service").exists(), True)
# (d3) the fake install.sh behaves like NVIDIA's: a failed start rolls back the user, the unit and the enable;
# before the reboot (running kernel != NEWK) install.sh is never called - the minimal unit is enabled, not started
c, d, root, env, host = make("d3", "host-a")
smp = root / "usr/share/doc/NVIDIA_GLX-1.0/samples"; smp.mkdir(parents=True)
src = c / "smp/nvidia-persistenced-init"; src.mkdir(parents=True)
unit = root / "etc/systemd/system/nvidia-persistenced.service"
(src / "install.sh").write_text('echo "install.sh $*" >> "$FAKE_LOG"; u="$PVEU_TEST_ROOT/etc/systemd/system/nvidia-persistenced.service"; '
                                'm="$PVEU_TEST_ROOT/.."; mkdir -p "$(dirname "$u")"; : > "$u"; : > "$m/puser"; : > "$m/penabled"; '
                                '[ "${FAKE_INST_RC:-0}" = 0 ] && exit 0; echo "Starting nvidia-persistenced.service... failed."; '
                                'echo "Aborting."; rm -f "$u" "$m/puser" "$m/penabled"; echo "Cleaning up... done."; exit $FAKE_INST_RC\n')
subprocess.run(["tar", "-cjf", str(smp / "nvidia-persistenced-init.tar.bz2"), "-C", str(c / "smp"), "nvidia-persistenced-init"], check=True)
(root / "root/pve-upgrade/gpu").write_text("yes\n")
def d3(**fake):
    (c / "calls").unlink(missing_ok=True); unit.unlink(missing_ok=True); state(root, "upgrade")
    for m in ("puser", "penabled"): (root / ".." / m).unlink(missing_ok=True)
    return go(c, d, env, "host-a", "driver", "--apply", **dict({"FAKE_CARD": 1, "FAKE_INST_RC": 1}, **fake))
def left(): return (unit.exists(), (root / "../puser").exists(), (root / "../penabled").exists())
r = d3(FAKE_UNAME=OLDK)
check("d3: running OLDK -> install.sh not called", "install.sh" in calls(c), False)
check("d3: running OLDK -> the minimal unit written", "ExecStart=/usr/bin/nvidia-persistenced\n" in unit.read_text(), True)
check("d3: daemon-reload + enable, no start", ("systemctl daemon-reload" in calls(c), "systemctl enable nvidia-persistenced" in calls(c),
                                              "systemctl start" in calls(c)), (True, True, False))
check("d3: the say line", f"persistenced: the minimal unit, enabled not started - the running {OLDK} has no nvidia module (built for {NEWK})" in r.stdout, True)
check("d3: the phase goes on", (r.returncode, state(root)), (0, "driver"))
r = d3(FAKE_UNAME=OLDK, FAKE_DKMS=f"nvidia/580.159, {OLDK}, x86_64: installed")
check("d3: the dkms check for NEWK still STOPs", (r.returncode != 0, "STOP driver: dkms:" in r.stderr, NEWK in r.stderr), (True, True, True))
r = d3(FAKE_UNAME=NEWK)
check("d3: running NEWK -> install.sh called", "install.sh systemd" in calls(c), True)
check("d3: install.sh failed -> rolled back, STOP persistenced", (r.returncode != 0, "STOP driver: persistenced:" in r.stderr, left(),
                                                                 "systemctl daemon-reload" in calls(c)), (True, True, (False, False, False), False))
r = d3(FAKE_UNAME=NEWK, FAKE_INST_RC=0)
check("d3: install.sh ok -> user, unit, enable left; the phase goes on", (r.returncode, left(), "minimal unit" in r.stdout,
                                                                        state(root)), (0, (True, True, True), False, "driver"))
c, d, root, env, host = make("d2")
r = go(c, d, env, "pvetest", "precheck", "--apply", FAKE_CARD=1)
check("d2: profile no + a card found -> stops", ("--gpu" in r.stderr, r.returncode != 0), (True, True))
r = go(c, d, env, "pvetest", "all", FAKE_CARD=1)
check("d2: dry run prints the disagreement", "disagree" in r.stdout, True)

# (e) dkms not built for the new kernel -> the driver phase fails and the reboot is refused
c, d, root, env, host = make("e", "host-a")
(root / "root/pve-upgrade/gpu").write_text("yes\n")
state(root, "upgrade")
r = go(c, d, env, "host-a", "driver", "--apply", FAKE_CARD=1, FAKE_DKMS=f"nvidia/580.159, {OLDK}, x86_64: installed")
check("e: driver phase stops on dkms", (r.returncode != 0, "dkms" in r.stderr), (True, True))
check("e: the .run ran with --dkms --silent", "nvrun --dkms --silent" in calls(c), True)
check("e: blacklist written", "blacklist nova_core" in (root / "etc/modprobe.d/pve-upgrade-blacklist.conf").read_text(), True)
bl = (root / "etc/modprobe.d/pve-upgrade-blacklist.conf").read_text()
check("q-d: blacklist ends in exactly one newline", (bl.endswith("\n"), bl.endswith("\n\n")), (True, False))
state(root, "driver")
r = go(c, d, env, "host-a", "reboot", "--apply", FAKE_CARD=1, FAKE_DKMS=f"nvidia/580.159, {OLDK}, x86_64: installed")
check("e: reboot refused", (r.returncode != 0, (c / "rebooted").exists()), (True, False))
check("e: names dkms and the new kernel", "dkms" in r.stderr and NEWK in r.stderr, True)
r = go(c, d, env, "host-a", "reboot", "--apply", FAKE_CARD=1)
check("e: built -> reboot with the typed name", ((c / "rebooted").exists(), state(root)), (True, "reboot"))

# (f) no tty is needed: --apply runs every point of no return (logged APPLY), a dry run changes nothing
c, d, root, env, host = make("f")
state(root, "prepare")
r = go(c, d, env, "pvetest", "upgrade")
check("f: dry run - no dist-upgrade", (r.returncode, "dist-upgrade" in calls(c)), (0, False))
r = go(c, d, env, "pvetest", "upgrade", "--apply")
check("f: --apply -> dist-upgrade runs, no tty", (r.returncode, "dist-upgrade" in calls(c)), (0, True))
check("f: APPLY logged", "APPLY dist-upgrade on pvetest" in (root / "root/pve-upgrade/log").read_text(), True)
src = (root / "etc/apt/sources.list.d/proxmox.sources").read_text()
check("f: deb822 trixie no-subscription", ("trixie" in src, "pve-no-subscription" in src), (True, True))
for n in ("debian", "proxmox"):
    s_ = (root / f"etc/apt/sources.list.d/{n}.sources").read_text()
    check(f"q-d: {n}.sources ends in exactly one newline", (s_.endswith("\n"), s_.endswith("\n\n")), (True, False))
check("f: old bookworm line out", "bookworm" in (root / "etc/apt/sources.list").read_text().replace("#", "X#").split("X#")[0], False)

# (g) resume: state reboot -> `all --apply` starts at recheck
c, d, root, env, host = make("g")
state(root, "reboot")
r = go(c, d, env, "pvetest", "all", "--apply")
ran = [p for p in PHASES if f"== {p}" in r.stdout]
check("g: only recheck runs", ran, ["recheck"])
check("g: ends RECHECK OK", (r.returncode, r.stdout.strip().splitlines()[-1]), (0, "RECHECK OK pvetest"))
r = go(c, d, env, "pvetest", "all", "--apply")
check("g: rerun after recheck does nothing", [p for p in PHASES if f"== {p}" in r.stdout], [])

# (h) a failed check stops with ONE line: phase, check, what was seen
c, d, root, env, host = make("h")
r = go(c, d, env, "pvetest", "all", "--apply", FAKE_P89_FAIL=2)
stops = [l for l in r.stderr.splitlines() if l.startswith("STOP")]
check("h: exit non-zero", r.returncode != 0, True)
check("h: one STOP line", len(stops), 1)
check("h: names phase and check", stops[0].startswith("STOP precheck: pve8to9") if stops else False, True)
check("h: nothing after it ran", "== drain" in r.stdout, False)
c, d, root, env, host = make("h2")
r = go(c, d, env, "pvetest", "precheck", "--apply", FAKE_HOSTNAME="other")
check("h2: wrong host stops at precheck", any(l.startswith("STOP precheck: hostname") for l in r.stderr.splitlines()), True)
c, d, root, env, host = make("h3")
state(root, "drain")
r = go(c, d, env, "pvetest", "to-8.4", "--apply", FAKE_PVEVER="8.3.2")
check("h3: pve-manager < 8.4.1 after -> stop", any(l.startswith("STOP to-8.4: pve-manager") for l in r.stderr.splitlines()), True)

# (i) the .run's sha256 does not match driver.conf -> refused before it runs
c, d, root, env, host = make("i", "host-a")
(root / "root/pve-upgrade/gpu").write_text("yes\n")
(d / "driver.conf").write_text("NV_RUN=NVIDIA-Linux-x86_64-580.159.run\nNV_SHA256=" + "0" * 64 + "\n")
state(root, "upgrade")
r = go(c, d, env, "host-a", "driver", "--apply", FAKE_CARD=1)
check("i: refused", (r.returncode != 0, "sha256" in r.stderr), (True, True))
check("i: .run never ran", "nvrun" in calls(c), False)
(root / "root/pve-upgrade/state").unlink()
r = go(c, d, env, "host-a", "precheck", "--apply", FAKE_CARD=1)
check("i: precheck refuses it too", any(l.startswith("STOP precheck: sha256") for l in r.stderr.splitlines()), True)

# (j) recheck: RECHECK OK only when every item passes; failures listed
c, d, root, env, host = make("j", "host-a")
(root / "root/pve-upgrade/gpu").write_text("yes\n")
state(root, "reboot")
r = go(c, d, env, "host-a", "recheck", "--apply", FAKE_CARD=1)
check("j: all pass -> RECHECK OK", (r.returncode, r.stdout.strip().splitlines()[-1]), (0, f"RECHECK OK host-a"))
for n, fake in (("smi", {"FAKE_SMI_N": 1}), ("persist", {"FAKE_INACTIVE": "nvidia-persistenced"}),
                ("offload", {"FAKE_OFFLOAD_ON": 1}), ("nfs", {"FAKE_NOSHARED": 1}), ("quorum", {"FAKE_QUORATE": "No"}),
                ("p89", {"FAKE_P89_FAIL": 1}), ("p89-noline", {"FAKE_P89_NOLINE": 1}), ("pmode", {"FAKE_PM": "Disabled"}), ("watchdog", {"FAKE_INACTIVE": "gpu-watchdog"})):
    state(root, "reboot")
    r = go(c, d, env, "host-a", "recheck", "--apply", FAKE_CARD=1, **fake)
    check(f"j-{n}: no RECHECK OK", ("RECHECK OK" in r.stdout, r.returncode != 0), (False, True))
    if n == "persist": check("j-persist: FAIL names it", "nvidia-persistenced not active" in r.stdout + r.stderr, True)
    check(f"j-{n}: state not recheck", state(root), "reboot")

# negatives (review round 12): every --apply checks the guards; the state moves forward only
for ph, prev in (("to-8.4", "drain"), ("upgrade", "prepare"), ("reboot", "driver")):
    for n, fake in (("guest", {"FAKE_QM": "  101 web running\\n"}), ("host", {"FAKE_HOSTNAME": "other"})):
        c, d, root, env, host = make(f"n-{ph}-{n}")
        state(root, prev)
        r = go(c, d, env, "pvetest", ph, "--apply", **fake)
        what = "guests" if n == "guest" else "hostname"
        check(f"n-{ph}-{n}: STOP {ph}: {what}", (r.returncode != 0, f"STOP {ph}: {what}" in r.stderr), (True, True))
        check(f"n-{ph}-{n}: nothing ran", ("apt-get" in calls(c), (c / "rebooted").exists(), state(root)), (False, False, prev))
c, d, root, env, host = make("n-dry-gpu", "host-a")
r = go(c, d, env, "host-a", "all", FAKE_CARD=1)
check("n-dry-gpu: dry run never runs the .run", ("nvrun" in calls(c), "would: sh" in r.stdout), (False, True))
for n, fake in (("rc", {"FAKE_QM_ERR": 1}), ("ipcc-rc0", {"FAKE_QM_ERR": 1, "FAKE_QM_RC": 0})):
    c, d, root, env, host = make(f"n-qm-{n}")
    state(root, "fetch")
    r = go(c, d, env, "pvetest", "drain", "--apply", **fake)
    check(f"n-qm-{n}: qm error -> STOP, never 'no guests'", ("STOP drain: guest-list" in r.stderr, state(root)), (True, "fetch"))
for n, st, ph, due in (("back", "upgrade", "prepare", "driver"), ("skip", "drain", "upgrade", "to-8.4"), ("again", "upgrade", "upgrade", "driver")):
    c, d, root, env, host = make(f"n-order-{n}")
    state(root, st)
    r = go(c, d, env, "pvetest", ph, "--apply")
    check(f"n-order-{n}: STOP naming {due}", (f"STOP {due}: order: the next due phase is {due}" in r.stderr, state(root), "apt-get" in calls(c)), (True, st, False))
c, d, root, env, host = make("n-nogpu", "host-a")
state(root, "driver")
r = go(c, d, env, "host-a", "reboot", "--apply", FAKE_CARD=1)
check("n-nogpu: GPU profile, no decision -> reboot refused", ("gpu decision" in r.stderr, (c / "rebooted").exists()), (True, False))
state(root, "reboot")
r = go(c, d, env, "host-a", "recheck", "--apply", FAKE_CARD=1)
check("n-nogpu: ... and recheck refused", ("gpu decision" in r.stderr, "RECHECK OK" in r.stdout), (True, False))
c, d, root, env, host = make("n-oldk", "host-a")
(root / "root/pve-upgrade/gpu").write_text("yes\n")
(c / "dkms").write_text(f"nvidia/580.159, {OLDK}, x86_64: installed\n")
state(root, "driver")
r = go(c, d, env, "host-a", "reboot", "--apply", FAKE_CARD=1)
check("n-oldk: built only for the old kernel -> reboot refused", ("STOP reboot: dkms" in r.stderr, (c / "rebooted").exists()), (True, False))
c, d, root, env, host = make("n-bak")
(root / "root/pve-upgrade/etc-before-pve9.tgz").write_text("old")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("n-bak: existing backup -> refused, kept", ("STOP precheck: etc-backup" in r.stderr, (root / "root/pve-upgrade/etc-before-pve9.tgz").read_text()), (True, "old"))
c, d, root, env, host = make("n-nic")
with open(root / "etc/network/interfaces", "a") as f:
    f.write("iface eno9 inet manual\n")
state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply")
check("n-nic: unpinned name in interfaces -> reboot refused", ("STOP reboot: nic-pinning" in r.stderr and "eno9" in r.stderr, (c / "rebooted").exists()), (True, False))
c, d, root, env, host = make("n-pin")
shutil.rmtree(root / "usr/local/lib/systemd/network")
state(root, "to-8.4")
r = go(c, d, env, "pvetest", "prepare", "--apply")
lk = root / "usr/local/lib/systemd/network/10-pve-upgrade-enp0s25.link"
check("n-pin: prepare pins enp0s25 to its own name by MAC", (r.returncode, lk.exists() and "Name=enp0s25" in lk.read_text() and "aa:bb:cc:dd:ee:01" in lk.read_text()), (0, True))
# (q) put's files end in a newline; pinned() reads each .link on its own (three NICs, the host-a shape)
NICS3 = (("enp0s25", "aa:bb:cc:dd:ee:01"), ("enp9s0", "aa:bb:cc:dd:ee:02"), ("enp10s0", "aa:bb:cc:dd:ee:03"))
def nics3(root):
    for n, mac in NICS3[1:]:
        (root / f"sys/class/net/{n}/device").mkdir(parents=True)
        (root / f"sys/class/net/{n}/address").write_text(mac + "\n")
    with open(root / "etc/network/interfaces", "a") as f:
        f.write("".join(f"iface {n} inet manual\n" for n, _ in NICS3[1:]))
    return root / "usr/local/lib/systemd/network"
c, d, root, env, host = make("q-a")
lkd = nics3(root); shutil.rmtree(lkd)
state(root, "to-8.4")
r = go(c, d, env, "pvetest", "prepare", "--apply")
links = sorted(lkd.glob("*.link"))
check("q-a: prepare writes three .link files, each ending in a newline", (len(links), all(p.read_text().endswith("\n") for p in links)), (3, True))
check("q-a: prepare's nic-pinning passes", (r.returncode, "nic-pinning" in r.stderr, state(root)), (0, False, "prepare"))
state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply")
check("q-a: reboot's nic-pinning passes", ("nic-pinning" in r.stderr, (c / "rebooted").exists()), (False, True))
c, d, root, env, host = make("q-b")
lkd = nics3(root)
for n, mac in NICS3:
    (lkd / f"10-pve-upgrade-{n}.link").write_text(f"[Match]\nMACAddress={mac}\nType=ether\n\n[Link]\nName={n}")
state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply")
check("q-b: three .link files without a final newline -> reboot's nic-pinning passes", ("nic-pinning" in r.stderr, (c / "rebooted").exists()), (False, True))
c, d, root, env, host = make("q-c")
lkd = nics3(root); shutil.rmtree(lkd)
(root / "sys/class/net/enp10s0/device").rmdir()   # no longer a physical NIC: prepare writes no .link, interfaces still names it
state(root, "to-8.4")
r = go(c, d, env, "pvetest", "prepare", "--apply")
check("q-c: one NIC without a .link -> prepare refuses, naming it", ("STOP prepare: nic-pinning" in r.stderr, "enp10s0" in r.stderr), (True, True))
state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply")
check("q-c: ... and reboot refuses, naming it", ("STOP reboot: nic-pinning" in r.stderr, "enp10s0" in r.stderr, (c / "rebooted").exists()), (True, True, False))
c, d, root, env, host = make("n-args")
for n, args in (("slash", ["../x"]), ("gpu-novalue", ["pvetest", "precheck", "--gpu"])):
    r = go(c, d, env, *args)
    check(f"n-args-{n}: refused", r.returncode, 2)
r = go(c, d, env, "pvetest", "all", FAKE_P89_FAIL=1)
check("n-args: dry run with a FAIL exits 1", r.returncode, 1)
check("n-args: dry run left no state", (root / "root/pve-upgrade/state").exists(), False)

# preclean: old kernels through apt only (running + newest + pinned kept); unowned dirs only by rm; lists backups; /tmp held open kept
c, d, root, env, host = make("pc")
state(root, "precheck")
K = "proxmox-kernel-{}-signed ii\\n"
pk = "".join(K.format(v) for v in (OLDK, NEWK, "6.8.12-4-pve", "6.5.13-1-pve")) + "proxmox-headers-6.5.13-1-pve ii\\npve-kernel-5.15.158-1-pve ii\\nproxmox-kernel-6.8 ii\\n"
for m in ("usr/lib/modules/5.4.0-1-pve", "usr/lib/modules/6.5.13-1-pve", f"usr/lib/modules/{OLDK}", "var/lib/vz/dump", "var/log", "tmp"):
    (root / m).mkdir(parents=True, exist_ok=True)
(root / "var/lib/vz/dump/vzdump-qemu-100.vma.zst").write_text("x")
(root / "var/log/syslog.2.gz").write_text("x"); (root / "var/log/syslog").write_text("x")
for n in ("held", "free", "fresh"):
    (root / f"tmp/{n}").write_text("x")
    if n != "fresh": os.utime(root / f"tmp/{n}", (0, 0))
r = go(c, d, env, "pvetest", "preclean", "--apply", FAKE_DPKG_K=pk, FAKE_PIN="6.8.12-4-pve", FAKE_OPEN="held",
       FAKE_OWNED="/usr/lib/modules/6.5.13-1-pve", FAKE_FREE=5, FAKE_FREE_AFTER=20)
purge = [l for l in calls(c).splitlines() if l.startswith("apt-get") and " purge " in l]
check("pc: runs, gate uses the after-value", (r.returncode, state(root)), (0, "preclean"))
check("pc: purges only the old kernels", sorted(purge[0].split(" purge ")[1].split()) if purge else [],
      ["proxmox-headers-6.5.13-1-pve", "proxmox-kernel-6.5.13-1-pve-signed", "pve-kernel-5.15.158-1-pve"])
check("pc: unowned dir removed, owned + running kept", [(root / m).exists() for m in ("usr/lib/modules/5.4.0-1-pve",
      "usr/lib/modules/6.5.13-1-pve", f"usr/lib/modules/{OLDK}")], [False, True, True])
check("pc: backup listed, kept", ("vzdump-qemu-100" in r.stdout, (root / "var/lib/vz/dump/vzdump-qemu-100.vma.zst").exists()), (True, True))
check("pc: rotated log gone, live log kept", ((root / "var/log/syslog.2.gz").exists(), (root / "var/log/syslog").exists()), (False, True))
check("pc: /tmp held + fresh kept, old free gone", [(root / f"tmp/{n}").exists() for n in ("held", "free", "fresh")], [True, False, True])
c, d, root, env, host = make("pc-low")
state(root, "precheck")
r = go(c, d, env, "pvetest", "preclean", "--apply", FAKE_FREE=20, FAKE_FREE_AFTER=5)
check("pc-low: < 10 GB after -> STOP", [l for l in r.stderr.splitlines() if l.startswith("STOP")][:1] and r.stderr.startswith("STOP preclean: free-root"), True)

# ZFS (profile ZFS_POOL): precheck stops on a scrub and a degraded pool; recheck on a missing zvol and a changed c_max; no zpool upgrade
def zmake(n):
    c, d, root, env, host = make(n)
    with open(d / "hosts/pvetest.conf", "a") as f: f.write('ZFS_POOL=tank\nSTAY="100"\n')
    (root / "etc/zfs").mkdir(parents=True); (root / "etc/zfs/zpool.cache").write_text("x")
    (root / "proc/spl/kstat/zfs").mkdir(parents=True); (root / "proc/spl/kstat/zfs/arcstats").write_text("c_max 4 17179869184\n")
    return c, d, root, env, host
for n, fake, chkn in (("z-scrub", {"FAKE_ZSCRUB": 1}, "zfs-scrub"), ("z-deg", {"FAKE_ZDEG": 1}, "zfs-health")):
    c, d, root, env, host = zmake(n)
    r = go(c, d, env, "pvetest", "precheck", "--apply", **fake)
    check(f"{n}: precheck STOP {chkn}", r.stderr.startswith(f"STOP precheck: {chkn}"), True)
for n, fake, mut in (("z-zvol", {"FAKE_ZVOLS": "tank/vm-100-disk-0"}, None), ("z-cmax", {}, "c_max 4 8589934592\n"), ("z-ok", {}, None)):
    c, d, root, env, host = zmake(n)
    r = go(c, d, env, "pvetest", "precheck", "--apply")
    state(root, "reboot")
    if mut: (root / "proc/spl/kstat/zfs/arcstats").write_text(mut)
    r = go(c, d, env, "pvetest", "recheck", "--apply", **fake)
    check(f"{n}: recheck", r.returncode == 0, n == "z-ok")
c, d, root, env, host = zmake("z-all")
for _ in range(4):   # each reboot ends a run; `all --apply` resumes
    r = go(c, d, env, "pvetest", "all", "--apply")
    if r.returncode or state(root) == "recheck": break
check("z-all: resumes to RECHECK OK", r.stdout.strip().splitlines()[-1], "RECHECK OK pvetest")
check("z: zpool upgrade never executed", "zpool upgrade" in calls(c) + (root / "root/pve-upgrade/log").read_text(), False)
c, d, root, env, host = zmake("z-stay")
state(root, "fetch")
r = go(c, d, env, "pvetest", "drain", "--apply", FAKE_QM="  100 win running\\n")
check("z-stay: stay guest running -> STOP stay", r.stderr.startswith("STOP drain: stay"), True)

# C4: to-8.4 builds Debian's 535 for the new kernel and gates on it
for n, k, ok in (("k84-ok", NEWK, True), ("k84-no", OLDK, False)):
    c, d, root, env, host = make(n, "host-a")
    (root / "root/pve-upgrade/gpu").write_text("yes\n"); state(root, "drain")
    r = go(c, d, env, "host-a", "to-8.4", "--apply", FAKE_CARD=1, FAKE_DKMS=f"nvidia-current/535.309.01, {k}, x86_64: installed")
    check(f"{n}: headers + autoinstall, gate", (f"dkms autoinstall -k {NEWK}" in calls(c), (c / "rebooted").exists(), r.stderr.startswith("STOP to-8.4: dkms")), (True, ok, not ok))
# C4: preclean ownership (dpkg-query, any state), half states, the simulated purge, guests checked before preclean
MODS = ("usr/lib/modules/6.2.16-3-pve", "usr/lib/modules/5.4.0-1-pve")
def pcmake(n):
    c, d, root, env, host = make(n); state(root, "precheck")
    for m in MODS: (root / m).mkdir(parents=True)
    return c, d, root, env, host
c, d, root, env, host = pcmake("pc-own")
r = go(c, d, env, "pvetest", "preclean", "--apply", FAKE_DPKG_ALL="proxmox-kernel-6.2.16-3-pve-signed deinstall ok config-files\\n")
check("pc-own: listed by dpkg-query (dpkg -S rc 1) kept, unowned removed", ([(root / m).exists() for m in MODS], r.returncode), ([True, False], 0))
c, d, root, env, host = pcmake("pc-dqerr")
r = go(c, d, env, "pvetest", "preclean", "--apply", FAKE_DPKGQ_RC=2)
check("pc-dqerr: dpkg-query error -> every dir kept", [(root / m).exists() for m in MODS], [True, True])
c, d, root, env, host = pcmake("pc-half")
r = go(c, d, env, "pvetest", "preclean", "--apply", FAKE_DPKG_K="proxmox-kernel-6.2.16-3-pve-signed iF\\n")
check("pc-half: half-installed kernel -> STOP naming it, dir kept", (r.stderr.startswith("STOP preclean: kernel-state") and "6.2.16-3" in r.stderr, (root / MODS[0]).exists()), (True, True))
c, d, root, env, host = pcmake("c3-rc")
r = go(c, d, env, "pvetest", "preclean", "--apply", FAKE_DPKG_K="proxmox-kernel-6.2.16-3-pve-signed rc\\n")
check("C3-rc: an rc kernel package is not a half state", "kernel-state" in r.stderr, False)
c, d, root, env, host = pcmake("pc-sim")
r = go(c, d, env, "pvetest", "preclean", "--apply", FAKE_DPKG_K="proxmox-kernel-6.2.16-3-pve-signed ii\\n",
       FAKE_APT_SIM="Purg proxmox-kernel-6.2.16-3-pve-signed [6.2.16-3]\\nRemv proxmox-ve [8.4.0]\\n")
real = [l for l in calls(c).splitlines() if l.startswith("apt-get") and " purge " in l and " -s " not in l]
check("pc-sim: a Remv outside the list -> STOP, nothing purged", (r.stderr.startswith("STOP preclean: kernel-purge-sim") and "proxmox-ve" in r.stderr, real), (True, []))
c, d, root, env, host = make("pc-guest")
r = go(c, d, env, "pvetest", "all", "--apply", FAKE_QM="  101 web running\\n")
check("pc-guest: all --apply with a guest up runs preclean and fetch, then drain stops ", (r.stderr.startswith("STOP drain"), state(root)), (True, "fetch"))
# C4: /etc backup via .part
for n, err, rc, ok in (("tar-changed", "tar: etc/pve/.version: file changed as we read it", 1, True),
                       ("tar-bad", "tar: etc/shadow: Cannot open: Permission denied", 2, False)):
    c, d, root, env, host = make(n)
    r = go(c, d, env, "pvetest", "precheck", "--apply", FAKE_TAR_ERR=err, FAKE_TAR_RC=rc)
    bk = root / "root/pve-upgrade/etc-before-pve9.tgz"
    check(f"{n}: backup kept={ok}", (bk.exists(), pathlib.Path(str(bk) + ".part").exists(), r.stderr.startswith("STOP precheck: etc-backup")), (ok, False, not ok))
# C4: ZFS health before the reboot; two NICs with one MAC
c, d, root, env, host = zmake("z-reboot"); state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_ZDEG=1)
check("z-reboot: degraded pool -> reboot refused", (r.stderr.startswith("STOP reboot: zfs-health"), (c / "rebooted").exists()), (True, False))
c, d, root, env, host = make("n-mac"); shutil.rmtree(root / "usr/local/lib/systemd/network")
(root / "sys/class/net/enp1s0/device").mkdir(parents=True); (root / "sys/class/net/enp1s0/address").write_text("aa:bb:cc:dd:ee:01\n")
state(root, "to-8.4")
r = go(c, d, env, "pvetest", "prepare", "--apply")
check("n-mac: shared MAC -> STOP, nothing pinned", (r.stderr.startswith("STOP prepare: nic-mac"), (root / "usr/local/lib/systemd/network").exists()), (True, False))

# C5 part B: backups, permissions, NFS
def nmake(n, server=False):
    c, d, root, env, host = make(n)
    with open(d / "hosts/pvetest.conf", "a") as f: f.write(f'SHARED=/mnt/pve/ShareBk\nNFS_SERVER={"yes" if server else "no"}\n')
    return c, d, root, env, host
c, d, root, env, host = make("b-max"); (root / "etc/pve/storage.cfg").write_text("nfs: ShareBk\n\tmaxfiles 3\n")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("b-max: maxfiles -> STOP", r.stderr.startswith("STOP precheck: maxfiles"), True)
for n, fake, chkn in (("b-vz", {"FAKE_VZ": 1}, "vzdump"), ("b-mon", {"FAKE_VMMON": 1}, "vm-monitor")):
    c, d, root, env, host = make(n)
    r = go(c, d, env, "pvetest", "precheck", "--apply", **fake)
    check(f"{n}: precheck STOP {chkn}", r.stderr.startswith(f"STOP precheck: {chkn}"), True)
# C7: an empty vzrun stays empty (a here-string once made it " " and every run FAILed vzdump)
c, d, root, env, host = make("vz-none")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("vz-none: no running vzdump -> check vzdump ok", (r.returncode, "STOP" in r.stderr, "vzdump: ok" in r.stdout), (0, False, True))
c, d, root, env, host = make("vz-one")
r = go(c, d, env, "pvetest", "precheck", "--apply", FAKE_VZ=1)
check("vz-one: one running -> STOP naming its upid", (r.stderr.startswith("STOP precheck: vzdump"), "UPID:node1:vz" in r.stderr), (True, True))
c, d, root, env, host = make("b-tok")
r = go(c, d, env, "pvetest", "precheck", "--apply")
s = root / "root/pve-upgrade"
blob = "".join(p.read_text(errors="replace") for p in s.rglob("*") if p.is_file() and p.suffix != ".tgz") + r.stdout + r.stderr
check("b-tok: token names recorded, never a secret", (r.returncode, (s / "pve-tokens").read_text(), "secret" in blob), (0, "snapper@pve!snap\n", False))
c, d, root, env, host = make("b-nouser")
r = go(c, d, env, "pvetest", "precheck", "--apply", FAKE_NOUSER=1)
check("b-nouser: an unlistable user logged, no STOP; no SHARED skipped", (r.returncode, "tokens: cannot list snapper@pve" in (s := (root / "root/pve-upgrade/log").read_text()), "skipped: no SHARED" in s), (0, True, True))
for n, server, fake, want in (("bn-ok", True, {}, None), ("bn-role", False, {"FAKE_ROLES": ",VM.Monitor"}, "pve-roles"),
                              ("bn-builtin", False, {"FAKE_BUILTIN": "VM.GuestAgent.Audit,VM.Replicate"}, None),
                              ("bn-port", True, {"FAKE_RPCPORT": "40001"}, None),
                              ("bn-pvesm", False, {"FAKE_PVESM": "inactive"}, "ShareBk not active"),
                              ("bn-mnt", False, {"FAKE_NFSOPT": ",soft"}, "nfs mounts differ"), ("bn-exp", True, {"FAKE_EXPORT": ",no_root_squash"}, "nfs-exports")):
    c, d, root, env, host = nmake(n, server)
    r = go(c, d, env, "pvetest", "precheck", "--apply", *(["--nfs-quiet"] if server else []))
    state(root, "reboot")
    r = go(c, d, env, "pvetest", "recheck", "--apply", **fake)
    check(f"{n}: recheck {want or 'OK'}", ("RECHECK OK" in r.stdout, want is not None and want in r.stderr), (want is None, want is not None))
BPN = "/mnt/pve/ShareBk rw,relatime,vers=4.2,rsize=1048576,wsize=1048576,namlen=255,hard,proto=tcp,timeo=600,retrans=2,sec=sys,clientaddr=10.0.0.170,local_lock=none,addr=10.0.0.150"
FNE = BPN.replace(",hard,", ",hard,fatal_neterrors=none,")
SHUF = BPN.split(" ")[0] + " " + ",".join(reversed(FNE.split(" ")[1].split(",")))
for n, live, want in (("bn-fne", FNE, None), ("bn-fne-order", SHUF, None),
                      ("bn-vers", FNE.replace("vers=4.2", "vers=4.1"), "nfs mounts differ"),
                      ("bn-soft", FNE.replace(",hard,", ",soft,"), "nfs mounts differ"),
                      ("bn-addr", FNE.replace("addr=10.0.0.150", "addr=10.0.0.151"), "nfs mounts differ"),
                      ("bn-target", FNE.replace("/mnt/pve/ShareBk", "/mnt/pve/Other"), "nfs mounts differ"),
                      ("bn-second", FNE + "\\n/mnt/pve/Other vers=4.2,hard", "nfs mounts differ"),
                      ("bn-unknown", FNE.replace(",hard,", ",hard,foo=1,"), "nfs mounts differ")):
    c, d, root, env, host = nmake(n, False)
    r = go(c, d, env, "pvetest", "precheck", "--apply", FAKE_NFSLINE=BPN)
    state(root, "reboot")
    r = go(c, d, env, "pvetest", "recheck", "--apply", FAKE_NFSLINE=live)
    check(f"{n}: normalised nfs record, recheck {want or 'OK'}", ("RECHECK OK" in r.stdout, want is not None and want in r.stderr), (want is None, want is not None))
c, d, root, env, host = nmake("b-quiet", True)
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("b-quiet: NFS server without --nfs-quiet -> STOP, command printed", (r.stderr.startswith("STOP precheck: nfs-quiet"), "confirm the share is idle" in r.stdout), (True, True))
r = go(c, d, env, "pvetest", "precheck", "--apply", "--nfs-quiet")
check("b-quiet: --nfs-quiet -> precheck done, logged", (state(root), "APPLY --nfs-quiet" in (root / "root/pve-upgrade/log").read_text()), ("precheck", True))

# C6: the gates hold for the run that cuts the service; pvesh that is not JSON STOPs; a hung share is a FAIL, never a wait
c, d, root, env, host = nmake("g-to84", True); state(root, "drain")
r = go(c, d, env, "pvetest", "to-8.4", "--apply", "--nfs-quiet")
check("g-to84: --nfs-quiet run passes the gates", ("check nfs-quiet: ok" in r.stdout, "STOP to-8.4: nfs-quiet" in r.stderr), (True, False))
state(root, "prepare"); r = go(c, d, env, "pvetest", "all", "--apply")
check("g-upg: a later run without --nfs-quiet STOPs before upgrade", r.stderr.startswith("STOP upgrade: nfs-quiet"), True)
r = go(c, d, env, "pvetest", "all", "--apply", "--nfs-quiet", FAKE_VZ=1)
check("g-upg: a vzdump started between runs STOPs the later run", r.stderr.startswith("STOP upgrade: vzdump"), True)
c, d, root, env, host = make("g-json")
r = go(c, d, env, "pvetest", "precheck", "--apply", FAKE_PVESH_BAD=1)
check("g-json: pvesh output not JSON -> STOP, never 'none'", r.stderr.startswith("STOP precheck: vzdump: pvesh get /cluster/tasks: not JSON"), True)
c, d, root, env, host = nmake("g-hang"); go(c, d, env, "pvetest", "precheck", "--apply"); state(root, "reboot")
(sh := root / "mnt/pve/ShareBk").mkdir(parents=True, exist_ok=True)
r = go(c, d, env, "pvetest", "recheck", "--apply")
check("g-probe: probe ok, removed, host in its name", ("RECHECK OK" in r.stdout, list(sh.iterdir())), (True, []))
sh.chmod(0o500); state(root, "reboot"); r = go(c, d, env, "pvetest", "recheck", "--apply"); sh.chmod(0o700)
check("g-probe: probe fails -> recheck FAIL", "probe failed" in r.stderr, True)
state(root, "reboot")
check("g-probe: probe only when pvesm active", "probe failed" in go(c, d, env, "pvetest", "recheck", "--apply", FAKE_PVESM="inactive").stderr, False)

# ANSI-coloured pve8to9, systemd-boot, header metas, the real pvetest profile, backup schedule
c, d, root, env, host = make("t1b")
check("1-ansi: coloured FAILURES: 0 passes", "check pve8to9: ok" in go(c, d, env, "pvetest", "precheck").stdout, True)
check("1-ansi: coloured FAILURES: 1 fails", "check pve8to9: FAIL" in go(c, d, env, "pvetest", "precheck", FAKE_P89_FAIL=1).stdout, True)
check("2-sdb: precheck: the systemd-boot FAIL alone is fixed by prepare", "check pve8to9: ok" in go(c, d, env, "pvetest", "precheck", FAKE_SDB_FAIL=1).stdout, True)
check("2-sdb: precheck: another FAIL beside it still fails", "check pve8to9: FAIL" in go(c, d, env, "pvetest", "precheck", FAKE_SDB_FAIL=1, FAKE_P89_FAIL=1).stdout, True)
SDB = dict(FAKE_SDB="ii ", FAKE_GRUB=1, FAKE_SDB_FAIL=1, FAKE_SDB_GONE=str(c / "sdbgone"), APT_RM="-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold remove systemd-boot")
state(root, "to-8.4"); r = go(c, d, env, "pvetest", "prepare", "--apply", **SDB)
check("2-sdb: no /boot/grub STOPs", ("no /boot/grub" in r.stderr, r.returncode), (True, 1))
(root / "boot/grub").mkdir()
state(root, "to-8.4"); r = go(c, d, env, "pvetest", "prepare", "--apply", **{k: v for k, v in SDB.items() if k != "FAKE_GRUB"})
check("2-sdb: boot-tool configured without the uuids file STOPs", ("an ESP is configured" in r.stderr, r.returncode), (True, 1))
state(root, "to-8.4"); r = go(c, d, env, "pvetest", "prepare", "--apply", **dict(SDB, FAKE_SDB="hi ", FAKE_SDB_RC=2))
check("2-sdb: a dpkg-query error STOPs, never 'not installed'", ("dpkg-query systemd-boot failed" in r.stderr, r.returncode), (True, 1))
state(root, "to-8.4"); r = go(c, d, env, "pvetest", "prepare", "--apply", **dict(SDB, FAKE_P89_FAIL=1))
check("2-sdb: pve8to9 still failing after the removal STOPs", ("STOP prepare: pve8to9" in r.stderr, r.returncode), (True, 1))
state(root, "to-8.4"); r = go(c, d, env, "pvetest", "prepare", "--apply", **dict(SDB, FAKE_SDB="rc ", FAKE_P89_FAIL=1))
check("2-sdb: ... and on the rerun (already removed)", ("STOP prepare: pve8to9" in r.stderr, "not installed" in r.stdout), (True, True))
(c / "sdbgone").unlink(missing_ok=True)
state(root, "to-8.4")
r = go(c, d, env, "pvetest", "prepare", "--apply", **SDB)
check("2-sdb: GRUB host removes it and passes", (r.returncode, "remove systemd-boot" in calls(c), state(root)), (0, True, "prepare"))
state(root, "to-8.4")
r = go(c, d, env, "pvetest", "prepare", "--apply", **dict(SDB, FAKE_SDB_SIM="Remv systemd-boot [1]\\nRemv systemd-boot-efi [1]\\n"))
check("2-sdb: a simulation removing a second package STOPs", "systemd-boot-efi" in r.stderr and r.returncode == 1, True)
(root / "etc/kernel").mkdir(); (root / "etc/kernel/proxmox-boot-uuids").write_text("4A2B\n")
r = go(c, d, env, "pvetest", "prepare", "--apply", **SDB)
check("2-sdb: a proxmox-boot-tool host STOPs", "systemd-boot in use" in r.stderr and r.returncode == 1, True)
c, d, root, env, host = make("t1b-meta"); state(root, "precheck")
ISO = dict(FAKE_DPKG_K="pve-headers-6.1 ii \\npve-headers-6.1.10-1-pve ii \\nproxmox-headers-6.8 ii \\nproxmox-headers-6.8.12-9-pve ii \\n",
           FAKE_DEPS="pve-headers-6.1=pve-headers-6.1.10-1-pve;proxmox-headers-6.8=proxmox-headers-6.8.12-9-pve",
           FAKE_APT_SIM="Purg pve-headers-6.1.10-1-pve [1]\\nPurg pve-headers-6.1 [1]\\n")
r = go(c, d, env, "pvetest", "preclean", "--apply", **ISO)
pl = [l for l in calls(c).splitlines() if l.startswith("apt-get -y") and " purge " in l]
check("3-meta: the ISO's meta 6.1 is purged with its kernel, meta 6.8 stays", (r.returncode, bool(pl) and "pve-headers-6.1 " in pl[0] + " ", bool(pl) and "proxmox-headers-6.8" in pl[0]), (0, True, False))
state(root, "precheck"); r = go(c, d, env, "pvetest", "preclean", "--apply", **dict(ISO, FAKE_APT_SIM="Purg pve-headers-6.1 [1]\\nPurg proxmox-headers-6.8 [1]\\n"))
state(root, "precheck"); r = go(c, d, env, "pvetest", "preclean", "--apply", **dict(ISO, FAKE_DEPS="pve-headers-6.1=pve-headers-6.1.10-1-pve-signed (= 1) | pve-headers-6.1.10-1-pve;proxmox-headers-6.8=proxmox-headers-6.8.12-9-pve"))
pl = [l for l in calls(c).splitlines() if l.startswith("apt-get -y") and " purge " in l]
check("3-meta: a meta on `X-signed | X` goes when X is purged", (r.returncode, bool(pl) and "pve-headers-6.1 " in pl[-1] + " "), (0, True))
state(root, "precheck"); r = go(c, d, env, "pvetest", "preclean", "--apply", **dict(ISO, FAKE_APT_SIM="Purg pve-headers-6.1 [1]\\nPurg proxmox-headers-6.8 [1]\\n"))
check("3-meta: anything else outside the list still STOPs", "proxmox-headers-6.8" in r.stderr and r.returncode == 1, True)
c, d, root, env, host = make("t1b-prof", "fake0")
check("4-prof: the real pvetest profile is pve-test, nvidia=yes", "pve-test" in go(c, d, env, "fake0", "precheck").stdout, True)
(root / "root/pve-upgrade/gpu").write_text("yes\n"); state(root, "reboot")
(root / f"lib/modules/{OLDK}/updates/dkms").mkdir(parents=True); (root / f"lib/modules/{OLDK}/updates/dkms/nvidia.ko").write_text("")
r = go(c, d, env, "fake0", "recheck", "--apply", "--gpu", "yes", FAKE_DKMS=f"nvidia/580.159, {NEWK}, x86_64: installed", FAKE_SMI_N=0)
check("4-prof: GPUS=0 recheck: module checks only, no nvidia-smi", ("module checks only" in r.stdout, "nvidia-smi" in calls(c)), (True, False))
(root / f"lib/modules/{OLDK}/updates/dkms/nvidia.ko").unlink(); state(root, "reboot")
r = go(c, d, env, "fake0", "recheck", "--apply", "--gpu", "yes", FAKE_DKMS=f"nvidia/580.159, {NEWK}, x86_64: installed", FAKE_SMI_N=0)
check("4-prof: GPUS=0 recheck with no nvidia.ko FAILs", ("no nvidia.ko" in r.stdout + r.stderr, r.returncode), (True, 1))
c, d, root, env, host = make("t1b-bk")
state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_CAL_MIN=10)
check("6-bk: a backup job due in 10 min STOPs the reboot", ("backup job daily runs at" in r.stderr, os.path.exists(c / "rebooted")), (True, False))
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_SA_BAD=1)
check("6-bk: schedule-analyze failing is a WARN, not a STOP", ("WARN backup job daily" in r.stdout, os.path.exists(c / "rebooted")), (True, True))
os.unlink(c / "rebooted"); (root / "root/pve-upgrade/expect-kernel").unlink(); state(root, "driver")   # no reboot came: no record either
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_CAL_MIN=10, FAKE_BK_EN=0)
check("6-bk: a disabled backup job is skipped", os.path.exists(c / "rebooted"), True)
os.unlink(c / "rebooted"); (root / "root/pve-upgrade/expect-kernel").unlink(); state(root, "driver")   # no reboot came: no record either
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_SA_JUNK=1)
check("C3-bk: schedule-analyze rc 0, not JSON -> STOP", (r.stderr.startswith("STOP reboot: backup-schedule:"), os.path.exists(c / "rebooted")), (True, False))
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_SA_RC=1)
check("C3-bk: schedule-analyze rc 2 (not a parse error) -> STOP", (r.stderr.startswith("STOP reboot: backup-schedule:"), os.path.exists(c / "rebooted")), (True, False))
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_CAL_MIN=10)
check("C3-bk: a due job's message offers disabling it", "disable job daily for the upgrade AND re-enable it before recheck" in r.stderr, True)
state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_BK_BAD=1)
check("6-bk: pvesh /cluster/backup failing STOPs", ("pvesh get /cluster/backup failed" in r.stderr, os.path.exists(c / "rebooted")), (True, False))
c, d, root, env, host = make("t1c-held", "fake0")
(root / "root/pve-upgrade/gpu").write_text("yes\n"); state(root, "to-8.4"); src = tree(root / "etc/apt")
NV = dict(FAKE_DPKG_NV="nvidia-kernel-dkms hi \nlibnvidia-ml1 ii \n", FAKE_PURGE_RC=100)
r = go(c, d, env, "fake0", "prepare", "--apply", "--gpu", "yes", **NV)
check("C1-held: a held nvidia package STOPs prepare at the purge, sources untouched",
      (r.stderr.startswith("STOP prepare: purge:"), tree(root / "etc/apt") == src, state(root)), (True, True, "to-8.4"))
check("C1-held: the held one is in the purge call", "nvidia-kernel-dkms" in "".join(l for l in calls(c).splitlines() if " purge " in l), True)
c, d, root, env, host = make("t1c-nonv", "fake0")
(root / "root/pve-upgrade/gpu").write_text("yes\n"); state(root, "to-8.4")
r = go(c, d, env, "fake0", "prepare", "--apply", "--gpu", "yes", FAKE_PURGE_RC=100)
check("C1-nonv: nvidia patterns match nothing -> no purge, no STOP at it",
      ("Debian nvidia packages to purge: none" in r.stdout, "STOP prepare: purge:" in r.stderr), (True, False))
c, d, root, env, host = make("t1c-rb")
state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_REBOOT_RC=1)
check("C2-rb: a failing reboot STOPs and puts the state back",
      (r.stderr.startswith("STOP reboot: reboot: the reboot command failed (rc 1)"), state(root)), (True, "driver"))
r = go(c, d, env, "pvetest", "reboot", "--apply")
check("C3-k: reboot records the kernel it expects", ((root / "root/pve-upgrade/expect-kernel").read_text().strip(), state(root)), (NEWK, "reboot"))
r = go(c, d, env, "pvetest", "recheck", "--apply")
check("C3-k: booted the expected kernel -> no kernel complaint", "expected" in r.stdout + r.stderr, False)
state(root, "reboot"); r = go(c, d, env, "pvetest", "recheck", "--apply", FAKE_UNAME=OLDK)
check("C3-k: booted another kernel -> recheck FAILs", (f"booted {OLDK}, expected {NEWK}" in r.stdout + r.stderr, r.returncode), (True, 1))
(root / "root/pve-upgrade/expect-kernel").unlink(); state(root, "reboot"); r = go(c, d, env, "pvetest", "recheck", "--apply")
check("C3-k: no record is said, not failed", ("no record expect-kernel" in r.stdout, "expected" in r.stdout + r.stderr), (True, False))

c, d, root, env, host = make("t1d-bk")
(root / "root/pve-upgrade/expect-kernel").write_text(NEWK + "\n"); state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply")
check("D1-bk: booted kernel differs from expect-kernel -> STOP at the gate",
      (r.stderr.startswith(f"STOP reboot: booted-kernel: booted {OLDK}, expected {NEWK} after the last reboot"), state(root)), (True, "driver"))
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_UNAME=NEWK)
check("D1-bk: a matching record passes the gate", ("booted-kernel" in r.stderr, state(root)), (False, "reboot"))
(root / "root/pve-upgrade/expect-kernel").unlink(); state(root, "driver"); r = go(c, d, env, "pvetest", "reboot", "--apply")
check("D1-bk: no record passes", ("booted-kernel" in r.stderr, state(root)), (False, "reboot"))
c, d, root, env, host = make("t1d-bkpre")
(root / "root/pve-upgrade/expect-kernel").write_text(NEWK + "\n")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("D1-bk: precheck STOPs on a record that differs", r.stderr.startswith("STOP precheck: booted-kernel:"), True)
for ph, st in (("precheck", None), ("prepare", "to-8.4")):
    for k, v in (("FAKE_PIN", "6.8.12-4-pve"), ("FAKE_NEXTPIN", "6.8.12-4-pve")):
        c, d, root, env, host = make(f"t1d-pin-{ph}-{k}")
        if st: state(root, st)
        r = go(c, d, env, "pvetest", ph, "--apply", **{k: v})
        check(f"D2-pin: {k} STOPs {ph}", (r.stderr.startswith(f"STOP {ph}: kernel-pin: 6.8.12-4-pve pinned - `proxmox-boot-tool kernel unpin` first"), state(root)), (True, st or ""))
    c, d, root, env, host = make(f"t1d-nopin-{ph}")
    if st: state(root, st)
    r = go(c, d, env, "pvetest", ph, "--apply")
    check(f"D2-pin: the None. list passes {ph}", "kernel-pin" in r.stderr, False)
c, d, root, env, host = make("t1d-w")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("D3-w: a state write leaves no .tmp", (state(root), sorted(p.name for p in (root / "root/pve-upgrade").glob("*.tmp"))), ("precheck", []))
(root / "root/pve-upgrade/state.tmp").mkdir(); r = go(c, d, env, "pvetest", "preclean", "--apply")
check("D3-w: a failed state write keeps the old state, never an empty one", ("STOP preclean: write: cannot write" in r.stderr, state(root)), (True, "precheck"))

K84 = "6.8.12-4-pve"
c, d, root, env, host = make("t1e-restore")
(root / "root/pve-upgrade/expect-kernel").write_text(K84 + "\n"); state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_REBOOT_RC=1, FAKE_UNAME=K84)
check("E1-order: a failed reboot restores an existing record and the previous state",
      ("STOP reboot: reboot:" in r.stderr, (root / "root/pve-upgrade/expect-kernel").read_text().strip(), state(root)), (True, K84, "driver"))
c, d, root, env, host = make("t1e-order")
state(root, "driver"); r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_MV_KILL="expect-kernel")
check("E1-order: a power cut after the first write -> the record new, the state old",
      ((root / "root/pve-upgrade/expect-kernel").read_text().strip(), state(root)), (NEWK, "driver"))
for k, v in (("FAKE_PBT_RC", "2"), ("FAKE_PBT_JUNK", "1")):
    c, d, root, env, host = make(f"t1e-pbt-{k}")
    r = go(c, d, env, "pvetest", "precheck", "--apply", **{k: v})
    first = "E: no ESP configured" if k == "FAKE_PBT_RC" else "usage: kernel <add|remove|list>"
    check(f"E3-pin: {k} STOPs precheck, never fails open",
          (r.stderr.startswith(f"STOP precheck: kernel-pin: cannot read the kernel list: {first}"), state(root)), (True, ""))

c, d, root, env, host = make("t1f-order")
r = go(c, d, env, "pvetest", "precheck", "--apply")
S_ = root / "root/pve-upgrade"; L = calls(c).splitlines()
i = next((n for n, l in enumerate(L) if l == f"sync {S_}/state.tmp"), None)
check("F1-sync: one wr of state syncs the .tmp, mvs it, then syncs the dir",
      L[i:i + 3] if i is not None else None, [f"sync {S_}/state.tmp", f"mv -f {S_}/state.tmp {S_}/state", f"sync {S_}"])
c, d, root, env, host = make("t1f-dirsync")
r = go(c, d, env, "pvetest", "precheck", "--apply", FAKE_SYNC_DIR_RC=1)
check("F3-wr: a failed dir sync after a good mv STOPs 'is in place', the file holds the new text",
      ("state is in place but its directory sync failed - rerun" in r.stderr, r.returncode, state(root)), (True, 1, "precheck"))
c, d, root, env, host = make("t1f-put"); state(root, "to-8.4")
r = go(c, d, env, "pvetest", "prepare", "--apply")
L = calls(c).splitlines(); lk = root / "usr/local/lib/systemd/network"
j = next((n for n, l in enumerate(L) if l == f"sync {lk}/10-pve-upgrade-enp0s25.link {lk}"), None)
check("F4-put: the .link is synced before the next state write",
      j is not None and any(l == f"sync {root}/root/pve-upgrade/state.tmp" for l in L[j:]), True)
for ph, st in (("to-8.4", "drain"), ("driver", "upgrade"), ("reboot", "driver")):
    c, d, root, env, host = make(f"t1f-newk-{ph}", "host-a")
    (root / "root/pve-upgrade/gpu").write_text("yes\n"); state(root, st)
    r = go(c, d, env, "host-a", ph, "--apply", FAKE_CARD=1, FAKE_PBT_RC_FROM=1, FAKE_DKMS=f"nvidia/580.159, {NEWK}, x86_64: installed")
    check(f"F2-newk: an unreadable list at a newk caller ({ph}) STOPs, no reboot, no record",
          ("kernel-pin: cannot read the kernel list" in r.stderr, (c / "rebooted").exists(), (root / "root/pve-upgrade/expect-kernel").exists()),
          (True, False, False))
c, d, root, env, host = make("t1f-driver-ok", "host-a"); state(root, "upgrade")
r = go(c, d, env, "host-a", "driver", "--apply", "--gpu", "yes", FAKE_CARD=1, FAKE_DKMS=f"nvidia/580.159, {NEWK}, x86_64: installed")
check("C2-driver: nvidia built for the new kernel -> driver passes", (r.returncode, state(root), r.stderr[-160:] if r.returncode else ""), (0, "driver", ""))
c, d, root, env, host = make("t1f-nextpin"); state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_NEXTPIN=K84)
check("F2-newk: the next-boot pin is the kernel reboot_now records",
      (root / "root/pve-upgrade/expect-kernel").read_text().strip() if (root / "root/pve-upgrade/expect-kernel").exists() else r.stderr[:120], K84)

# `$IFACE` in interfaces, oneshot units with a timer, CEPH=no excusing the three Ceph FAILs
c, d, root, env, host = make("g-iface")
(root / "etc/network/interfaces").write_text("auto lo\niface lo inet loopback\niface enp9s0 inet manual\n  post-up ethtool -K $IFACE tso off\n")
(root / "usr/local/lib/systemd/network/10-pve-upgrade-enp9s0.link").write_text("[Link]\nName=enp9s0\n")
state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply")
check("g-a: $IFACE is no NIC name, enp9s0 is - pinned -> nic-pinning passes", ("nic-pinning" in r.stderr, "$IFACE" in r.stdout + r.stderr), (False, False))
(root / "usr/local/lib/systemd/network/10-pve-upgrade-enp9s0.link").unlink(); state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply")
check("g-a: enp9s0 unpinned -> refused naming it, never $IFACE", ("STOP reboot: nic-pinning" in r.stderr and "enp9s0" in r.stderr, "$IFACE" in r.stderr), (True, False))
c, d, root, env, host = make("g-units")
with open(d / "hosts/pvetest.conf", "a") as f: f.write('UNITS="wd plain"\n')
state(root, "reboot")
def rc(**k): return go(c, d, env, "pvetest", "recheck", **k).stdout
o = rc(FAKE_INACTIVE="wd plain", FAKE_TIMERS="wd.timer")
check("g-b: oneshot inactive, timer active -> running; plain no timer -> named", ("FAIL wd" in o, "FAIL plain not active (no timer)" in o), (False, True))
check("g-b: timer inactive -> named", "FAIL wd.timer not active" in rc(FAKE_INACTIVE="wd wd.timer", FAKE_TIMERS="wd.timer"), True)
check("g-b: unit failed -> named", "FAIL wd failed" in rc(FAKE_INACTIVE="wd", FAKE_TIMERS="wd.timer", FAKE_FAILED="wd"), True)
c, d, root, env, host = make("g-ceph")
CF = dict(FAKE_CEPH_FAIL=1, FAKE_SDB_FAIL=1)
def pc(**k): return go(c, d, env, "pvetest", "precheck", **dict(CF, **k))
check("g-c: CEPH unset -> fails as today", "check pve8to9: FAIL" in pc().stdout, True)
with open(d / "hosts/pvetest.conf", "a") as f: f.write("CEPH=no\n")
r = pc()
check("g-c: CEPH=no, no Ceph, + sdb -> passes, each excuse said", ("check pve8to9: ok" in r.stdout, r.stdout.count("excused (CEPH=no")), (True, 3))
check("g-c: a fourth, other FAIL -> fails", "check pve8to9: FAIL" in pc(FAKE_P89_FAIL=1).stdout, True)
# ceph.conf is the cluster file (a symlink into /etc/pve on PVE) - Ceph is what runs or holds data, never the file
(root / "etc/ceph").mkdir(parents=True); (root / "etc/pve").mkdir(parents=True, exist_ok=True)
(root / "etc/pve/ceph.conf").write_text("[global]\nmon_host = 1 2 3 4\n"); (root / "etc/ceph/ceph.conf").symlink_to(root / "etc/pve/ceph.conf")
(root / "var/lib/ceph/osd").mkdir(parents=True); (root / "var/lib/ceph/mon").mkdir(parents=True)
r = pc()
check("h-a: CEPH=no + cluster ceph.conf, no daemon/data/storage -> passes, 'holds' said, 3 excuses",
      ("check pve8to9: ok" in r.stdout, "CEPH=no holds" in r.stdout + r.stderr, r.stdout.count("excused (CEPH=no")), (True, True, 3))
def stops(what, **k): r = pc(**k); return "STOP precheck: ceph" in r.stderr and what in r.stderr
check("h-b: an active ceph-osd@ unit -> STOP naming it", stops("ceph-osd@3", FAKE_CEPH_UNIT="ceph-osd@3.service"), True)
check("h-b: an active rbd storage -> STOP naming it", stops("ceph-vm", FAKE_RBD=1), True)
check("h-b: pvesm failing -> STOP", stops("pvesm status unreadable", FAKE_PVESM_RC=2), True)
check("i-b: an inactive rbd storage -> STOP naming it", stops("ceph-vm", FAKE_RBD=1, FAKE_RBD_ST="inactive"), True)
check("i-b: a disabled rbd storage -> no STOP", "check pve8to9: ok" in pc(FAKE_RBD=1, FAKE_RBD_ST="disabled").stdout, True)
check("i-c: pvesm timing out (rc 124) -> STOP 'timed out'", stops("pvesm status timed out", FAKE_PVESM_RC=124), True)
check("i-d: systemctl list-units failing -> STOP", stops("systemctl unreadable", FAKE_SYSCTL_RC=1), True)
# a mon store with no unit is a leftover (node1) - CEPH=no holds
(root / "var/lib/ceph/mon/ceph-node1").mkdir(); (root / "var/lib/ceph/mon/ceph-node1/store.db").write_text("")
r = pc()
check("i-a: mon store only -> passes, 'stale mon store' said, 3 excuses",
      ("check pve8to9: ok" in r.stdout, "stale mon store /var/lib/ceph/mon/ceph-node1, no unit - CEPH=no holds" in r.stdout + r.stderr, r.stdout.count("excused (CEPH=no")), (True, True, 3))
(root / "var/lib/ceph/osd/ceph-0").mkdir()
check("h-b: an entry in /var/lib/ceph/osd -> STOP naming it", stops("ceph-0"), True)
(root / "var/lib/ceph/osd/ceph-0").rmdir(); (root / "etc/systemd/system/ceph-mgr.target.wants").mkdir(parents=True)
(root / "etc/systemd/system/ceph-mgr.target.wants/ceph-mgr@node1.service").write_text("")
check("h-b: an enabled ceph-mgr@ unit -> STOP naming it", stops("ceph-mgr@node1"), True)
(root / "etc/systemd/system/ceph-mgr.target.wants/ceph-mgr@node1.service").unlink(); (root / "etc/systemd/system/ceph-mon.target.wants").mkdir(parents=True)
(root / "etc/systemd/system/ceph-mon.target.wants/ceph-mon@node1.service").write_text("")
check("g-c: CEPH=no with a ceph-mon@ unit -> STOP", "STOP precheck: ceph" in pc().stderr, True)
# a dry run defers the reboot's NIC check (prepare only "would write" the .link files); --apply still refuses
c, d, root, env, host = make("h-nic")
(root / "etc/network/interfaces").write_text("auto lo\niface lo inet loopback\niface enp9s0 inet manual\n")
state(root, "driver")
r = go(c, d, env, "pvetest", "reboot")
check("h-c: dry reboot, unpinned -> 'would check', no nic-pinning FAIL", ("would check: every NIC name" in r.stdout, "nic-pinning: FAIL" in r.stdout), (True, False))
state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply")
check("h-c: --apply reboot, unpinned -> refused", "STOP reboot: nic-pinning" in r.stderr, True)

# fetch pre-downloads bookworm and (with the keyring) trixie from upgrade's own .sources text, never touching /etc/apt
def fetchcase(n, key=True, pre=None, **fake):
    c, d, root, env, host = make(n)
    if pre:
        (root / "var/cache/apt/archives").mkdir(parents=True); (root / "var/cache/apt/archives/old.deb").touch(); os.truncate(root / "var/cache/apt/archives/old.deb", pre)
    if key:
        (root / "usr/share/keyrings").mkdir(parents=True); (root / "usr/share/keyrings/proxmox-archive-keyring.gpg").write_text("k")
    (root / "var/lib/apt/lists").mkdir(parents=True); (root / "var/lib/apt/lists/bookworm_Packages").write_text("x")
    apt0 = tree(root / "etc/apt"), tree(root / "var/lib/apt/lists")
    state(root, "preclean")
    r = go(c, d, env, "pvetest", "fetch", "--apply", FAKE_QM="  101 web running\\n", **fake)
    ac = [l for l in calls(c).splitlines() if l.startswith("apt-get ")]
    return c, root, r, ac, apt0 == (tree(root / "etc/apt"), tree(root / "var/lib/apt/lists"))
c, root, r, ac, same = fetchcase("fm-a")
want = "".join(f"== {n}.sources\n" + (T / "f/root/etc/apt/sources.list.d" / f"{n}.sources").read_text() for n in ("debian", "proxmox"))
check("fm-a: fetch --apply beside a running guest, rc 0, state fetch", (r.returncode, state(root)), (0, "fetch"))
check("fm-a: update + -d dist-upgrade twice, the second on temp sources",
      ([l.split()[-1] for l in ac if l.endswith(" update") or l.endswith("-d dist-upgrade")], sum("sourceparts=" in l for l in ac)),
      (["update", "dist-upgrade", "update", "dist-upgrade"], 3))   # + the -s dist-upgrade 
check("fm-a: temp sourceparts hold exactly upgrade's text", (c / "calls.parts").read_text(), want * 3)
check("fm-a: /etc/apt unchanged (the trixie half on temp lists)", same, True)
check("fm-a: the trixie half builds no pkgcache", sum("Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache=" in l for l in ac), 3)
check("fm-a: no apt-get clean", any(" clean" in l for l in ac), False)
c, root, r, ac, same = fetchcase("fm-b", key=False)
check("fm-b: no keyring -> bookworm only, one skipped line, rc 0",
      (r.returncode, len(ac), any("sourceparts" in l for l in ac), r.stdout.count("trixie pre-fetch skipped: keyring missing")), (0, 2, False, 1))
c, root, r, ac, same = fetchcase("fm-d", FAKE_FREE=6)
check("fm-d: free below the gate after fetch -> STOP fetch: free", (r.stderr.startswith("STOP fetch: free: 6 GB"), state(root)), (True, "preclean"))
c, root, r, ac, same = fetchcase("fm-f", FAKE_FREE=9, FAKE_FETCH="768M")
check("fm-f: free 9 GB + 1.5 GB fetched -> passes (the .debs are what the 10 GB is for)", (r.returncode, state(root), "fetched archive: 1536 MB" in r.stdout), (0, "fetch", True))
c, root, r, ac, same = fetchcase("fm-g", FAKE_FREE=8, FAKE_FETCH="768M")
check("fm-g: free 8 GB + 1.5 GB fetched -> STOP naming both", (r.returncode, r.stderr.startswith("STOP fetch: free: 8 GB on / after fetch + 1536 MB in the archive")), (1, True))
c, d, root, env, host = make("fm-c"); before = tree(root)
r = go(c, d, env, "pvetest", "fetch")
check("fm-c: dry fetch changes nothing, runs no apt-get", (tree(root) == before, [l for l in calls(c).splitlines() if l.startswith("apt-get")]), (True, []))
r = go(c, d, env, "pvetest", "all")
check("fm-c: all runs phases in the new order", [l[3:] for l in r.stdout.splitlines() if l.startswith("== ")], PHASES)
src = (SRC / "pve-upgrade").read_text()
check("fm-c: no apt-get clean after preclean", src.count('"${APT[@]}" clean'), 1)
check("fm-c: preclean's clean comes before ph_fetch", src.index('"${APT[@]}" clean') < src.index("ph_fetch()"), True)
c, d, root, env, host = make("fm-e"); state(root, "reboot")
r = go(c, d, env, "pvetest", "recheck", "--apply", FAKE_FAILEDU="pvestatd.service loaded failed failed PVE Status Daemon\\n")
check("fm-e: a failed unit -> STOP recheck naming it", (r.returncode, r.stderr.startswith("STOP recheck:"), "failed-units: pvestatd.service" in r.stderr), (1, True, True))
r = go(c, d, env, "pvetest", "recheck", "--apply")
check("fm-e: none failed -> RECHECK OK", r.stdout.strip().splitlines()[-1], "RECHECK OK pvetest")
c, d, root, env, host = make("fm-h"); FU = "\u25cf nut-monitor.service loaded failed failed UPS\\n"
r = go(c, d, env, "pvetest", "precheck", "--apply", FAKE_FAILEDU=FU)
rec = root / "root/pve-upgrade/failed-units"
check("fm-h: precheck records failed units atomically, prints them", (rec.read_text(), "failed units before (recheck ignores these): nut-monitor.service" in r.stdout,
      'wr "$S/failed-units" "$FU"' in (SRC / "pve-upgrade").read_text()), ("nut-monitor.service\n", True, True))
state(root, "reboot")
r = go(c, d, env, "pvetest", "recheck", "--apply", FAKE_FAILEDU=FU)
check("fm-h: a unit failed before is named, not a STOP", (r.stdout.strip().splitlines()[-1], "already failed before (precheck's record): nut-monitor.service" in r.stdout), ("RECHECK OK pvetest", True))
state(root, "reboot"); r = go(c, d, env, "pvetest", "recheck", "--apply", FAKE_FAILEDU=FU + "pvestatd.service loaded failed failed PVE\\n")
check("fm-h: a new failed unit STOPs naming only it", (r.returncode, "failed-units: pvestatd.service" in r.stderr, "nut-monitor" in r.stderr), (1, True, False))

c, root, r, ac, same = fetchcase("fm-i", FAKE_FREE=8, pre=2 << 30)
check("fm-i: a rerun, 2 GB already in the archive, nothing new, free 8 GB -> passes", (r.returncode, state(root), "fetched archive: 2048 MB" in r.stdout), (0, "fetch", True))
c, root, r, ac, same = fetchcase("fm-i2", FAKE_FREE=7, pre=2 << 30)
check("fm-i: free 7 GB + 2 GB in the archive -> STOP naming both", (r.returncode, r.stderr.startswith("STOP fetch: free: 7 GB on / after fetch + 2048 MB in the archive")), (1, True))
c, d, root, env, host = make("fm-j"); (root / "root/pve-upgrade").mkdir(parents=True, exist_ok=True)
(root / "root/pve-upgrade/failed-units").write_text("nut-monitor.service\n"); (root / "root/pve-upgrade/failed-units.tmp").mkdir()
r = go(c, d, env, "pvetest", "precheck", "--apply")   # the record's write is lost: .tmp is a directory
check("fm-j: precheck drops a stale record before it can be lost", (r.returncode, (root / "root/pve-upgrade/failed-units").exists()), (1, False))
state(root, "reboot"); r = go(c, d, env, "pvetest", "recheck", "--apply", FAKE_FAILEDU="nut-monitor.service loaded failed failed UPS\\n")
check("fm-j: recheck is then strict about the stale unit", (r.returncode, "failed-units: nut-monitor.service" in r.stderr), (1, True))

# recover: a half-upgraded dpkg is finished offline, first, in to-8.4 and in upgrade
AUD = "The following packages are unpacked but not yet configured:\\n  ifupdown2  network\\n  gcc-12  compiler\\n"
for ph, st in (("to-8.4", "drain"), ("upgrade", "prepare")):
    c, d, root, env, host = make(f"rc-{ph}"); state(root, st)
    r = go(c, d, env, "pvetest", ph, "--apply", FAKE_AUDIT=AUD)
    cl = [l for l in calls(c).splitlines() if l.startswith(("dpkg --configure", "apt-get"))]
    check(f"rc-{ph}: configure -a, then -f install --no-download, before any update", cl[0].startswith("dpkg --configure") and "-f install --no-download" in cl[1] and "update" in cl[2], True)
    check(f"rc-{ph}: recovered line, phase went on", (r.returncode, "recovered: 2 packages configured" in r.stdout, state(root) != st), (0, True, True))
    c, d, root, env, host = make(f"rc-bad-{ph}"); state(root, st)
    r = go(c, d, env, "pvetest", ph, "--apply", FAKE_AUDIT=AUD, FAKE_AUDIT_STUCK=1)
    check(f"rc-bad-{ph}: still broken -> STOP recover, no update", (r.returncode != 0, r.stderr.startswith(f"STOP {ph}: recover: ") , "update" in calls(c), state(root)), (True, True, False, st))
    c, d, root, env, host = make(f"rc-clean-{ph}"); state(root, st)
    r = go(c, d, env, "pvetest", ph, "--apply")
    check(f"rc-clean-{ph}: clean dpkg -> no recover command, no line", ("dpkg --configure" in calls(c), "-f install" in calls(c), "recover" in r.stdout), (False, False, False))
    c, d, root, env, host = make(f"rc-dry-{ph}"); state(root, st)
    r = go(c, d, env, "pvetest", ph, FAKE_AUDIT=AUD)
    check(f"rc-dry-{ph}: dry run says would recover, runs nothing", ("would recover" in r.stdout, "dpkg --configure" in calls(c), "-f install" in calls(c)), (True, False, False))

# fetch also downloads driver's proxmox-default-headers (GPU yes); precheck wants intel-microcode on an Intel CPU
def hdrgpu(n, gpu, **fake):
    c, d, root, env, host = make(n)
    (root / "usr/share/keyrings").mkdir(parents=True); (root / "usr/share/keyrings/proxmox-archive-keyring.gpg").write_text("k")
    state(root, "preclean")
    r = go(c, d, env, "pvetest", "fetch", "--apply", "--gpu", gpu, **fake)
    ac = [l for l in calls(c).splitlines() if l.startswith("apt-get ")]
    return r, ac, [l for l in ac if "proxmox-default-headers" in l]
r, ac, h = hdrgpu("t1o-a", "yes")
last_dist = max(i for i, l in enumerate(ac) if l.endswith("-d dist-upgrade"))
check("t1o-a: GPU yes -> one -d install headers with the temp options, after the trixie -d dist-upgrade",
      (r.returncode, len(h), bool(h) and "sourceparts=" in h[0] and " -d install proxmox-default-headers" in h[0], bool(h) and ac.index(h[0]) > last_dist),
      (0, 1, True, True))
r, ac, h = hdrgpu("t1o-b", "no")
check("t1o-b: GPU no -> no headers fetch", (r.returncode, h), (0, []))
r, ac, h = hdrgpu("t1o-c", "yes", FAKE_HDR_RC=1)
check("t1o-c: the headers fetch failing -> STOP fetch: apt-fetch", (r.returncode != 0, "STOP fetch: apt-fetch" in r.stderr), (True, True))
c, d, root, env, host = make("t1o-c2")
(root / "usr/share/keyrings").mkdir(parents=True); (root / "usr/share/keyrings/proxmox-archive-keyring.gpg").write_text("k")
state(root, "preclean")
r = go(c, d, env, "pvetest", "fetch", "--gpu", "yes")
check("t1o-c2: dry run says would -d install headers", "would: -d install proxmox-default-headers" in r.stdout, True)
def mcase(n, vendor, **fake):
    c, d, root, env, host = make(n)
    (root / "proc").mkdir(exist_ok=True); (root / "proc/cpuinfo").write_text(f"processor\t: 0\nvendor_id\t: {vendor}\n")
    return go(c, d, env, "pvetest", "precheck", "--apply", **fake), root
r, root = mcase("t1o-d1", "GenuineIntel", FAKE_MC="")
check("t1o-d1: Intel without intel-microcode -> STOP precheck: intel-microcode", (r.returncode, "STOP precheck: intel-microcode: not installed" in r.stderr), (1, True))
r, root = mcase("t1o-d2", "GenuineIntel")
check("t1o-d2: Intel with intel-microcode -> passes, recorded", (r.returncode, (root / "root/pve-upgrade/intel-microcode").read_text().strip()), (0, "install ok installed"))
r, root = mcase("t1o-d3", "AuthenticAMD", FAKE_MC="")
check("t1o-d3: AMD -> no check", (r.returncode, (root / "root/pve-upgrade/intel-microcode").exists()), (0, False))

# apt sources - enterprise, third party, Ceph, backups in the state dir, recheck's apt lines
ZS = "Types: deb\nURIs: https://repo.zabbix.com/zabbix/7.4/debian\nSuites: bookworm\nComponents: main\nSigned-By: /usr/share/keyrings/zabbix.gpg\n"
DL = "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable\n"
BL = "deb http://bad.example/repo bookworm main\n"
def pmake(n, third=True, **extra):
    c, d, root, env, host = make(n); sd = root / "etc/apt/sources.list.d"
    if third:
        (sd / "zabbix.sources").write_text(ZS); (sd / "docker.list").write_text(DL); (sd / "bad.list").write_text(BL)
    for k, v in extra.items(): (sd / k).write_text(v)
    return c, d, root, env, sd
def installs(c): return [l for l in calls(c).splitlines() if l.startswith(("apt-get", "dpkg")) and any(w in l.split() for w in ("install", "remove", "purge", "-r", "-P"))]
# (c) fetch probes, records the plan; upgrade applies it; (e) owner say line, nothing installed; (g) backups
c, d, root, env, sd = pmake("p-c"); state(root, "preclean"); rp = root / "root/pve-upgrade/repos"
apt0 = tree(root / "etc/apt")
r = go(c, d, env, "pvetest", "fetch", "--apply", FAKE_PROBE_BAD="bad.example")
plan = sorted(l.split("\t") for l in rp.read_text().splitlines()) if rp.exists() else None
check("p-c: fetch plan: zabbix + docker switch, the failing probe disables", plan, [["/etc/apt/sources.list.d/bad.list", "http://bad.example/repo", "disable"],
      ["/etc/apt/sources.list.d/docker.list", "https://download.docker.com/linux/debian", "switch"], ["/etc/apt/sources.list.d/zabbix.sources", "https://repo.zabbix.com/zabbix/7.4/debian", "switch"]])
parts = (c / "calls.parts").read_text() if (c / "calls.parts").exists() else ""
check("p-c: each probe in its own update, bookworm -> trixie, Signed-By kept, plan said", (r.returncode, sum("sourceparts=" in l and l.endswith(" update") for l in calls(c).splitlines() if l.startswith("apt-get")),
      "Suites: trixie" in parts and "signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie" in parts and "Signed-By: /usr/share/keyrings/zabbix.gpg" in parts, "repo plan: /etc/apt/sources.list.d/bad.list http://bad.example/repo -> disable" in r.stdout), (0, 3, True, True))
check("p-c: fetch wrote nothing under /etc/apt", tree(root / "etc/apt"), apt0)
state(root, "prepare"); before = tree(root)
r = go(c, d, env, "pvetest", "upgrade", FAKE_OWNED="/etc/apt/sources.list.d/zabbix.sources")
check("p-c: dry upgrade changes nothing, says would", (tree(root) == before, "would" in r.stdout and "zabbix.sources" in r.stdout), (True, True))
state(root, "prepare"); (c / "calls").unlink()
r = go(c, d, env, "pvetest", "upgrade", "--apply", FAKE_OWNED="/etc/apt/sources.list.d/zabbix.sources")
check("p-c: upgrade ok; zabbix stanza + docker line switched, bad.list commented", (r.returncode, "Suites: trixie" in (sd / "zabbix.sources").read_text(),
      " trixie stable" in (sd / "docker.list").read_text(), (sd / "bad.list").read_text().startswith("# pve-upgrade: ")), (0, True, True, True))
check("p-c: say lines name the repos", all(x in r.stdout for x in ("repo switchd: /etc/apt/sources.list.d/zabbix.sources", "repo disabled: /etc/apt/sources.list.d/bad.list")), True)
check("p-e: package-owned file -> one say line, nothing installed or removed", (r.stdout.count("pkg owns /etc/apt/sources.list.d/zabbix.sources: its next upgrade may write bookworm back"),
      [l for l in installs(c) if " install " in l or " remove " in l or " purge " in l or " -r" in l]), (1, []))
ab = root / "root/pve-upgrade/apt-before"
check("p-g: originals in apt-before, none beside the sources", (sorted(p.name for p in ab.iterdir()), (ab / "zabbix.sources").read_text(), list(sd.glob("*.pve-upgrade.bak")), (root / "etc/apt").glob("*.pve-upgrade.bak") is None),
      (["bad.list", "docker.list", "sources.list", "zabbix.sources"], ZS, [], False))
state(root, "prepare"); go(c, d, env, "pvetest", "upgrade", "--apply")
check("p-g: a second run does not overwrite a backup", ((ab / "zabbix.sources").read_text(), (ab / "sources.list").read_text()), (ZS, "deb http://deb.debian.org/debian bookworm main\n"))
# (d) no plan -> STOP upgrade: repos, nothing changed
c, d, root, env, sd = pmake("p-d"); state(root, "prepare")
r = go(c, d, env, "pvetest", "upgrade", "--apply")
check("p-d: no $S/repos -> STOP upgrade: repos, sources untouched, state kept", (r.returncode, r.stderr.startswith("STOP upgrade: repos:"), (sd / "zabbix.sources").read_text(), state(root), "apt-get" in calls(c)), (1, True, ZS, "prepare", False))
# (a) enterprise after the dist-upgrade; a trixie .list line; (b) SUBSCRIPTION
c, d, root, env, sd = pmake("p-a", third=False, **{"pve-enterprise.list": "deb https://enterprise.proxmox.com/debian/pve trixie pve-enterprise\n"}); state(root, "prepare")
r = go(c, d, env, "pvetest", "upgrade", "--apply", FAKE_ENT_DU=1)
check("p-a: the .sources the dist-upgrade installs -> Enabled: no; the .list line commented", (r.returncode, "Enabled: no" in (sd / "pve-enterprise.sources").read_text(),
      (sd / "pve-enterprise.list").read_text().startswith("# pve-upgrade: "), r.stdout.count("enterprise source disabled: /etc/apt/sources.list.d/pve-enterprise.sources")), (0, True, True, 1))
c, d, root, env, sd = pmake("p-b", third=False)
with open(d / "hosts/pvetest.conf", "a") as f: f.write("SUBSCRIPTION=yes\n")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("p-b: SUBSCRIPTION=yes -> STOP precheck", (r.returncode, "STOP precheck: subscription: subscription hosts are not supported" in r.stderr), (1, True))
state(root, "prepare"); r = go(c, d, env, "pvetest", "upgrade", "--apply")
check("p-b: and upgrade STOPs, nothing changed", (r.returncode, "STOP upgrade: subscription" in r.stderr, "apt-get" in calls(c)), (1, True, False))
# precheck lists third parties and an older .bak, runs no verdict
c, d, root, env, sd = pmake("p-pre", **{"old.list.pve-upgrade.bak": DL})
r = go(c, d, env, "pvetest", "precheck")
check("p-pre: third-party lines (file, URI, suite) and the old .bak said, never removed", ("third-party repo: /etc/apt/sources.list.d/zabbix.sources https://repo.zabbix.com/zabbix/7.4/debian bookworm" in r.stdout,
      "third-party repo: /etc/apt/sources.list.d/docker.list https://download.docker.com/linux/debian bookworm" in r.stdout, "old.list.pve-upgrade.bak" in r.stdout, (sd / "old.list.pve-upgrade.bak").exists()), (True, True, True, True))
# (f) Ceph
CL = "deb http://download.proxmox.com/debian/ceph-%s bookworm no-subscription\n"
c, d, root, env, sd = pmake("p-f1", third=False, **{"ceph.list": CL % "squid"}); state(root, "prepare")
r = go(c, d, env, "pvetest", "upgrade", "--apply")
want = "Types: deb\nURIs: http://download.proxmox.com/debian/ceph-squid\nSuites: trixie\nComponents: no-subscription\nSigned-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n"
check("p-f: ceph-squid bookworm line -> ceph.sources trixie, the old line commented", (r.returncode, (sd / "ceph.sources").read_text() if (sd / "ceph.sources").exists() else None, (sd / "ceph.list").read_text().startswith("# pve-upgrade: ")), (0, want, True))
c, d, root, env, sd = pmake("p-f2", third=False, **{"ceph.list": CL % "squid"}); state(root, "preclean")
(root / "usr/share/keyrings").mkdir(parents=True); (root / "usr/share/keyrings/proxmox-archive-keyring.gpg").write_text("k")
go(c, d, env, "pvetest", "fetch", "--apply")
check("p-f: the trixie pre-fetch holds the same ceph.sources text", "== ceph.sources\n" + want in (c / "calls.parts").read_text(), True)
c, d, root, env, sd = pmake("p-f3", third=False, **{"ceph.list": CL % "reef"})
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("p-f: ceph-reef -> STOP precheck", (r.returncode, "STOP precheck: ceph-repo: Ceph reef: Proxmox 9 needs squid - upgrade Ceph first" in r.stderr), (1, True))
c, d, root, env, sd = pmake("p-f4", third=False); state(root, "prepare")
r = go(c, d, env, "pvetest", "upgrade", "--apply")
check("p-f: no Ceph repo, no Ceph -> no ceph.sources", (r.returncode, (sd / "ceph.sources").exists()), (0, False))
# (h) recheck: each apt finding fails alone; clean passes
POL = "proxmox-ve:\\n  Candidate: 9.0.3\\n  Version table:\\n *** 9.0.3 500\\n        500 https://enterprise.proxmox.com/debian/pve trixie/pve-enterprise amd64 Packages\\n"
for n, fake, kw, want in (("clean", {}, {}, "RECHECK OK"), ("err", {"FAKE_UPD_OUT": "Err:3 http://x trixie InRelease\\n"}, {}, "apt-get update: Err:3"),
        ("warn", {"FAKE_UPD_OUT": "W: Failed to fetch http://x\\n"}, {}, "apt-get update: W:"),
        ("ignoring", {"FAKE_UPD_OUT": "N: Ignoring file 'x.pve-upgrade.bak' in directory '/etc/apt/sources.list.d/' as it has an invalid filename extension\\n"}, {}, "Ignoring file"),
        ("policy", {"FAKE_POLICY": POL}, {}, "candidate not from download.proxmox.com trixie/pve-no-subscription"),
        ("bw", {}, {"KEEP_BW": 1}, "enabled source names bookworm"),
        ("ent", {}, {"ent": 1}, "enabled source on enterprise.proxmox.com")):
    c, d, root, env, sd = pmake(f"p-h-{n}", third=False); state(root, "reboot")
    if "ent" in kw: (sd / "pve-enterprise.sources").write_text("Types: deb\nURIs: https://enterprise.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-enterprise\n"); kw = {}
    r = go(c, d, env, "pvetest", "recheck", "--apply", **fake, **kw)
    check(f"p-h-{n}: recheck {want}", ("RECHECK OK" in r.stdout, want in r.stdout + r.stderr), (n == "clean", True))

# continuation 1: stuck state, backups-read ceph, stanza edges
c, d, root, env, sd = pmake("p-s"); state(root, "prepare"); rp = root / "root/pve-upgrade/repos"
r = go(c, d, env, "pvetest", "upgrade", "--apply")
check("p-s1: upgrade STOP names the re-probe command", (r.returncode, "fetch --apply" in r.stderr and "state does not move" in r.stderr), (1, True))
r = go(c, d, env, "pvetest", "fetch")
check("p-s1: dry fetch past fetch says would re-probe", (r.returncode, "would: re-probe" in r.stdout), (0, True))
r = go(c, d, env, "pvetest", "fetch", "--apply"); a1 = tree(root / "etc/apt")
check("p-s1: fetch --apply past fetch re-records without moving state", (r.returncode, state(root), rp.exists() and len(rp.read_text().splitlines()), "apt-get" in "".join(l for l in calls(c).splitlines() if "-d" in l.split())), (0, "prepare", 3, False))
r = go(c, d, env, "pvetest", "upgrade", "--apply")
check("p-s1: upgrade then goes", (r.returncode, state(root)), (0, "upgrade"))
# 1b(b): a repo added after fetch -> STOP naming that entry; $S/repos rewritten (not appended) by the re-probe
c, d, root, env, sd = pmake("p-sb"); state(root, "prepare"); rp = root / "root/pve-upgrade/repos"; rp.parent.mkdir(parents=True, exist_ok=True)
rp.write_text("/etc/apt/sources.list.d/docker.list\thttps://download.docker.com/linux/debian\tswitch\n")
r = go(c, d, env, "pvetest", "upgrade", "--apply")
check("p-sb: entry without a plan -> STOP repos names it and the command", (r.returncode, "repos" in r.stderr, "bad.list" in r.stderr, "fetch --apply" in r.stderr), (1, True, True, True))
go(c, d, env, "pvetest", "fetch", "--apply")
check("p-sb: re-probe rewrites $S/repos (3 lines, no append)", (state(root), len(rp.read_text().splitlines()), (rp.parent / "repos.tmp").exists()), ("prepare", 3, False))
# 1b(a): past upgrade fetch --apply refuses as today
c, d, root, env, sd = pmake("p-sa"); state(root, "upgrade")
r = go(c, d, env, "pvetest", "fetch", "--apply")
check("p-sa: past upgrade fetch --apply refuses (order)", (r.returncode, "order" in r.stderr, state(root)), (1, True, "upgrade"))
# 2: rerun after the sed, ceph.sources not yet written
c, d, root, env, sd = pmake("p-s2", third=False); state(root, "prepare"); ab = root / "root/pve-upgrade/apt-before"; ab.mkdir(parents=True)
(ab / "ceph.list").write_text(CL % "squid"); (sd / "ceph.list").write_text("# pve-upgrade: " + CL % "squid")
r = go(c, d, env, "pvetest", "upgrade", "--apply")
check("p-s2: ceph read from apt-before -> ceph.sources written", (r.returncode, (sd / "ceph.sources").exists()), (0, True))
# stz_edit swaps atomically - no temp left in the state dir, the mode kept
c, d, root, env, sd = pmake("p-s5", third=False); state(root, "prepare"); (sd / "zabbix.sources").write_text(ZS); (sd / "zabbix.sources").chmod(0o640)
r = go(c, d, env, "pvetest", "fetch", "--apply"); r = go(c, d, env, "pvetest", "upgrade", "--apply")
check("p-s5: stanza swap keeps the mode, leaves no temp in the state dir", (oct((sd / "zabbix.sources").stat().st_mode & 0o777), list((root / "root/pve-upgrade").glob("stz.*"))), ("0o640", []))
# a DISABLED ceph-quincy stanza in the backup, no live Ceph repo -> no STOP, nothing written
c, d, root, env, sd = pmake("p-s6", third=False); state(root, "prepare"); ab = root / "root/pve-upgrade/apt-before"; ab.mkdir(parents=True)
(ab / "ceph.sources").write_text("Types: deb\nURIs: http://download.proxmox.com/debian/ceph-quincy\nSuites: bookworm\nComponents: no-subscription\nEnabled: no\n")
r = go(c, d, env, "pvetest", "upgrade", "--apply")
check("p-s6: disabled quincy stanza in apt-before -> no ceph STOP, nothing written", ("ceph" in r.stderr and "STOP" in r.stderr, (sd / "ceph.sources").exists()), (False, False))
# 3+4: two stanzas on one URI stay two; a URI that is a prefix of another is left alone
TS = "Types: deb\nURIs: https://x.example/repo\nSuites: bookworm\nComponents: main\n\nTypes: deb-src\nURIs: https://x.example/repo\nSuites: bookworm\nComponents: main\n"
OT = "Types: deb\nURIs: https://x.example/repo-extra\nSuites: bookworm\nComponents: main\n"
c, d, root, env, sd = pmake("p-s3", third=False, **{"two.sources": TS, "extra.sources": OT, "px.list": "deb https://x.example/repo-extra bookworm main\ndeb https://x.example/repo bookworm main\n"}); state(root, "preclean")
go(c, d, env, "pvetest", "fetch", "--apply", FAKE_PROBE_BAD="x.example"); state(root, "prepare")
(root / "root/pve-upgrade/repos").write_text("/etc/apt/sources.list.d/two.sources\thttps://x.example/repo\tswitch\n/etc/apt/sources.list.d/extra.sources\thttps://x.example/repo-extra\tdisable\n/etc/apt/sources.list.d/px.list\thttps://x.example/repo-extra\tdisable\n/etc/apt/sources.list.d/px.list\thttps://x.example/repo\tswitch\n")
r = go(c, d, env, "pvetest", "upgrade", "--apply"); tw = (sd / "two.sources").read_text(); pl = (sd / "px.list").read_text().splitlines()
check("p-s3: two stanzas stay two, both switched", (r.returncode, tw.count("URIs:"), tw.count("trixie"), "\n\n" in tw), (0, 2, 2, True))
check("p-s4: exact URI - repo-extra disabled, repo switched; other stanza intact", (pl[0].startswith("# pve-upgrade: ") and "bookworm" in pl[0], pl[1] == "deb https://x.example/repo trixie main", "Enabled: no" in (sd / "extra.sources").read_text()), (True, True, True))
# 5: no *.tmp left in sources.list.d; 6: Enabled values
check("p-s5: no leftover tmp beside the sources", [p.name for p in sd.iterdir() if p.name.endswith(".tmp")], [])
for v in ("false", "Off", "DISABLED"):
    c, d, root, env, sd = pmake(f"p-s6{v}", third=False, **{"off.sources": ZS + f"Enabled: {v}\n"}); state(root, "prepare")
    r = go(c, d, env, "pvetest", "upgrade", "--apply")
    check(f"p-s6: Enabled: {v} counts as disabled (no STOP, untouched)", (r.returncode, (sd / "off.sources").read_text()), (0, ZS + f"Enabled: {v}\n"))
# recheck: Warning:/Error: prefixes
c, d, root, env, sd = pmake("p-s7", third=False); state(root, "reboot")
r = go(c, d, env, "pvetest", "recheck", "--apply", FAKE_UPD_OUT="Warning: x\\n")
check("p-s7: recheck fails on Warning:", ("RECHECK OK" in r.stdout, "apt-get update: Warning:" in r.stdout + r.stderr), (False, True))

# (1) the pause before the reboot: all --apply stops after driver (the reboot is announced first); --through-reboot keeps the non-stop path
c, d, root, env, host = make("r1-pause"); state(root, "upgrade")
r = go(c, d, env, "pvetest", "all", "--apply")
check("r1: all --apply from upgrade stops after driver: rc 0, state driver, no reboot, NEXT line",
      (r.returncode, state(root), (c / "rebooted").exists(), "== reboot" in r.stdout, "NEXT reboot: announce it, then `./pve-upgrade pvetest reboot --apply` (or all --apply)" in r.stdout),
      (0, "driver", False, False, True))
r = go(c, d, env, "pvetest", "all", "--apply")
check("r1: a rerun of all --apply from driver reboots", ((c / "rebooted").exists(), state(root)), (True, "reboot"))
c, d, root, env, host = make("r1-thru"); state(root, "upgrade")
r = go(c, d, env, "pvetest", "all", "--apply", "--through-reboot")
check("r1: --through-reboot reboots in the same run", (r.returncode, (c / "rebooted").exists(), state(root), "NEXT reboot" in r.stdout), (0, True, "reboot", False))
c, d, root, env, host = make("r1-lone"); state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply")
check("r1: [pin] a lone reboot --apply reboots, no pause", (r.returncode, (c / "rebooted").exists(), state(root), "NEXT reboot" in r.stdout), (0, True, "reboot", False))

# (2) precheck: a NIC the config names with no device STOPs; a bridge-ports/bond-slaves name with none is said
c, d, root, env, host = make("r2-nic")
with open(root / "etc/network/interfaces", "a") as f: f.write("auto enp5s0\\niface enp5s0 inet manual\\niface enp8s0 inet manual\\n".replace("\\n", "\n"))
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("r2: iface stanzas with no device -> STOP precheck: nic-device naming both", (r.returncode, r.stderr.startswith("STOP precheck: nic-device: names a NIC with no device: enp5s0 enp8s0 - comment its stanzas out"), state(root)), (1, True, ""))
c, d, root, env, host = make("r2-port")
with open(root / "etc/network/interfaces", "a") as f: f.write("auto vmbr1\niface vmbr1 inet manual\n\tbridge-ports enp9s0 enp0s25\n\tbond-slaves none\n")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("r2: a bridge-ports name with no device is said, no STOP", (r.returncode, "no device: enp9s0 " in r.stdout, "enp0s25" in [l for l in r.stdout.splitlines() if "no device" in l][0]), (0, True, False))
c, d, root, env, host = make("r2-ok")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("r2: a clean config passes nic-device", (r.returncode, "check nic-device: ok" in r.stdout), (0, True))

# (3) ZFS after the reboot: either import unit will do; a lost cachefile is set again and checked
def zre(n, rm=True, **fake):
    c, d, root, env, host = zmake(n)
    go(c, d, env, "pvetest", "precheck", "--apply"); state(root, "reboot")
    if rm: (root / "etc/zfs/zpool.cache").write_text("")
    return c, d, root, env, host, go(c, d, env, "pvetest", "recheck", "--apply", **fake)
c, d, root, env, host, r = zre("r3-scan", FAKE_ZCF="none", FAKE_INACTIVE="zfs-import-cache")
check("r3: import-scan active, cachefile none, cache empty -> OK, zpool set cachefile ran, file non-empty",
      ("RECHECK OK" in r.stdout, "zpool set cachefile=/etc/zfs/zpool.cache tank" in calls(c), (root / "etc/zfs/zpool.cache").stat().st_size > 0), (True, True, True))
c, d, root, env, host, r = zre("r3-none", FAKE_INACTIVE="zfs-import-cache zfs-import-scan")
check("r3: [negative] both import units inactive -> FAIL", ("RECHECK OK" in r.stdout, "neither zfs-import-cache nor zfs-import-scan active" in r.stderr), (False, True))
c, d, root, env, host, r = zre("r3-nofile", FAKE_ZCF="none", FAKE_ZSET_NOFILE=1)
check("r3: [negative] the cache still empty after the set -> FAIL", ("RECHECK OK" in r.stdout, "zpool.cache still empty" in r.stderr), (False, True))
c, d, root, env, host = zmake("r3-dry"); go(c, d, env, "pvetest", "precheck", "--apply"); state(root, "reboot"); (root / "etc/zfs/zpool.cache").write_text("")
r = go(c, d, env, "pvetest", "recheck", FAKE_ZCF="none")
check("r3: a dry recheck says it would set the cachefile, runs nothing", ("would: zpool set cachefile=/etc/zfs/zpool.cache tank" in r.stdout, "zpool set" in calls(c)), (True, False))
c, d, root, env, host = zmake("r3-gate"); go(c, d, env, "pvetest", "precheck", "--apply"); state(root, "driver")
r = go(c, d, env, "pvetest", "reboot", "--apply", FAKE_ZCF="none")
check("r3: the reboot gate STOPs on a lost cachefile", (r.stderr.startswith("STOP reboot: zfs-cachefile"), (c / "rebooted").exists()), (True, False))

# (4) rediff: blanks only never differ, any record
def rd(n, rec_, text):
    c, d, root, env, host = nmake(n, True)
    go(c, d, env, "pvetest", "precheck", "--apply", "--nfs-quiet"); state(root, "reboot")
    (root / "root/pve-upgrade" / rec_).write_text(text)
    return go(c, d, env, "pvetest", "recheck", "--apply")
r = rd("r4-pad", "nfs-conf", "[nfsd]   \n")
check("r4: a trailing-blank difference passes", ("RECHECK OK" in r.stdout, "differs" in r.stderr), (True, False))
r = rd("r4-mid", "nfs-rpc", "100003  4\ttcp\n100005   3 udp\n")
check("r4: wider padding in another record passes", ("RECHECK OK" in r.stdout, "differs" in r.stderr), (True, False))
r = rd("r4-word", "nfs-rpc", "100003 4 udp\n100005 3 udp\n")
check("r4: [negative] a word differs -> FAIL", ("RECHECK OK" in r.stdout, "nfs-rpc differs from precheck's record" in r.stderr), (False, True))

# (6) removal guard: fetch records the non-rename Remv; upgrade STOPs on one REMOVE_OK does not name
SIM = "Remv libssl3 [3.0.11]\\nRemv libcurl3-gnutls [7.88]\\nRemv zabbix-agent [1:6.0]\\nInst libssl3t64 (3.5)\\nInst libcurl3t64-gnutls (8.14)\\nInst zabbix-agent2 (7)\\n"
c, root, r, ac, same = fetchcase("r6-fetch", FAKE_DU_SIM=SIM)
check("r6: fetch records only the non-rename Remv and says it", ((root / "root/pve-upgrade/removals").read_text().split(), "not t64 renames: zabbix-agent" in r.stdout, r.returncode),
      (["zabbix-agent"], True, 0))
check("r6: fetch simulates on the temp sources", any(" -s dist-upgrade" in l and "sourceparts=" in l for l in ac), True)
c, d, root, env, sd = pmake("r6-up", third=False); state(root, "prepare")
r = go(c, d, env, "pvetest", "upgrade", "--apply", FAKE_DU_SIM=SIM)
dist = [l for l in calls(c).splitlines() if l.startswith("apt-get") and "dist-upgrade" in l and " -s " not in l]
check("r6: upgrade STOPs on a Remv not in REMOVE_OK, before anything changes", (r.stderr.startswith("STOP upgrade: removals: zabbix-agent - name them in the profile's REMOVE_OK to accept"), state(root), dist, (root / "etc/apt/sources.list.d/debian.sources").exists()), (True, "prepare", [], False))
r = go(c, d, env, "pvetest", "upgrade", "--apply", FAKE_DU_SIM="Remv libssl3 [3]\\nInst libssl3t64 (3.5)\\n")
check("r6: renames alone pass", (r.returncode, state(root)), (0, "upgrade"))
c, d, root, env, sd = pmake("r6-ok", third=False); state(root, "prepare")
with open(d / "hosts/pvetest.conf", "a") as f: f.write('REMOVE_OK="zabbix-agent other"\n')
r = go(c, d, env, "pvetest", "upgrade", "--apply", FAKE_DU_SIM=SIM)
check("r6: REMOVE_OK naming it passes", (r.returncode, state(root)), (0, "upgrade"))
c, d, root, env, sd = pmake("r6-new", third=False); state(root, "prepare")
with open(d / "hosts/pvetest.conf", "a") as f: f.write('REMOVE_OK="zabbix-agent"\n')
r = go(c, d, env, "pvetest", "upgrade", "--apply", FAKE_DU_SIM=SIM + "Remv newpkg [1]\\n")
check("r6: [negative] a Remv new since fetch and not in REMOVE_OK STOPs", (r.stderr.startswith("STOP upgrade: removals: newpkg - name them"), "zabbix-agent" in r.stderr, state(root)), (True, False, "prepare"))
c, d, root, env, sd = pmake("r6-dry", third=False); state(root, "prepare"); (root / "root/pve-upgrade/removals").write_text("zabbix-agent\n")
r = go(c, d, env, "pvetest", "upgrade")
check("r6: a dry upgrade says the recorded list", "fetch recorded: zabbix-agent" in r.stdout, True)

# the guard sees switched third-party entries; commented/virtual NICs; Purg; to-8.4's reboot announced
c, d, root, env, sd = pmake("c1-sw", third=True); state(root, "prepare")
(root / "root/pve-upgrade/repos").write_text("/etc/apt/sources.list.d/zabbix.sources\thttps://repo.zabbix.com/zabbix/7.4/debian\tswitch\n/etc/apt/sources.list.d/docker.list\thttps://download.docker.com/linux/debian\tdisable\n/etc/apt/sources.list.d/bad.list\thttp://bad.example/repo\tdisable\n")
r = go(c, d, env, "pvetest", "upgrade", "--apply", FAKE_DU_SIM="Remv conflicted-pkg [1]\\nInst zabbix-agent2 (7)\\n")
parts = (c / "calls.parts").read_text()
check("c1: the guard's temp sources hold the switched entry (trixie), not the disabled ones; STOP names the Conflicts victim",
      (r.stderr.startswith("STOP upgrade: removals: conflicted-pkg"), "Suites: trixie" in parts.split("== sw1.sources")[-1] if "== sw1.sources" in parts else False, "docker.com" in parts, state(root)), (True, True, False, "prepare"))
c, d, root, env, sd = pmake("c1-ok", third=True); state(root, "prepare")
(root / "root/pve-upgrade/repos").write_text("/etc/apt/sources.list.d/zabbix.sources\thttps://repo.zabbix.com/zabbix/7.4/debian\tswitch\n/etc/apt/sources.list.d/docker.list\thttps://download.docker.com/linux/debian\tdisable\n/etc/apt/sources.list.d/bad.list\thttp://bad.example/repo\tdisable\n")
r = go(c, d, env, "pvetest", "upgrade", "--apply", FAKE_DU_SIM="Inst zabbix-agent2 (7)\\n")
check("c1: third=True and no new removal passes", (r.returncode, state(root)), (0, "upgrade"))

# A) rerun - the live entry is trixie already, apt-before holds bookworm; B) an unplanned non-bookworm entry
c, d, root, env, sd = pmake("c2-rerun", third=False, **{"zabbix.sources": ZS.replace("bookworm", "trixie")}); state(root, "prepare")
(root / "root/pve-upgrade/apt-before").mkdir(parents=True, exist_ok=True); (root / "root/pve-upgrade/apt-before/zabbix.sources").write_text(ZS)
(root / "root/pve-upgrade/repos").write_text("/etc/apt/sources.list.d/zabbix.sources\thttps://repo.zabbix.com/zabbix/7.4/debian\tswitch\n")
r = go(c, d, env, "pvetest", "upgrade", "--apply", FAKE_DU_SIM="Remv conflicted-pkg [1]\\nInst zabbix-agent2 (7)\\n")
parts = (c / "calls.parts").read_text() if (c / "calls.parts").exists() else ""
check("c2: rerun - the guard still holds the switched entry (trixie) and STOPs on its Conflicts victim",
      (r.stderr.startswith("STOP upgrade: removals: conflicted-pkg"), "zabbix" in parts.split("== sw1.sources")[-1] if "== sw1.sources" in parts else False), (True, True))
c, d, root, env, sd = pmake("c2-other", third=False, **{"grafana.list": "deb https://apt.grafana.com stable main\n"}); state(root, "prepare")
r = go(c, d, env, "pvetest", "upgrade", "--apply")
parts = (c / "calls.parts").read_text() if (c / "calls.parts").exists() else ""
check("c2: an enabled third-party entry with no plan row is in the guard's temp sources", (r.returncode, "apt.grafana.com" in parts), (0, True))

c, d, root, env, host = make("c2-comment")
with open(root / "etc/network/interfaces", "a") as f: f.write("# post-up ethtool -K enp5s0 tso off\n#iface enp6s0 inet manual\n")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("c2: a dead NIC only in commented lines -> no STOP", (r.returncode, "check nic-device: ok" in r.stdout), (0, True))
c, d, root, env, host = make("c2-unit")
(root / "etc/systemd/system").mkdir(parents=True, exist_ok=True); (root / "etc/systemd/system/nic-offload-enp5s0.service").write_text("")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("c2: a dead NIC from a service file says so", (r.returncode, "enp5s0(nic-offload-enp5s0.service)" in r.stderr), (1, True))
c, d, root, env, host = make("c3-virt")
with open(root / "etc/network/interfaces", "a") as f: f.write("iface wg0 inet manual\niface vlan20 inet manual\niface tap1 inet manual\n")
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("c3: virtual ifaces that are down do not trip nic-device", (r.returncode, "check nic-device: ok" in r.stdout), (0, True))

import subprocess
rl = subprocess.run(["bash", "-c", "source <(sed -n '/^remv_list()/,/sort -u; }/p' %s); printf 'Purg oldpkg [1]\\nRemv libssl3 [1]\\nInst libssl3t64 (2)\\n' | remv_list" % (SRC / "pve-upgrade")], capture_output=True, text=True)
check("c4: remv_list reads Purg lines, still skips renames", rl.stdout.split(), ["oldpkg"])

# generic profile keys - GUARD_PROCS, HOLD_HINT, the storage name from SHARED, token names, driver.conf only for NVIDIA, init
def setprof(d, *lines, drop=()):
    f = d / "hosts/pvetest.conf"
    f.write_text("".join(l + "\n" for l in f.read_text().splitlines() if not l.startswith(tuple(drop))) + "".join(l + "\n" for l in lines))

c, d, root, env, host = make("g9-a1"); state(root, "fetch"); setprof(d, drop=("GUARD_PROCS",))
r = go(c, d, env, "pvetest", "drain", "--apply", FAKE_PROC="workerd")
check("g9-a: GUARD_PROCS empty -> a running workerd blocks nothing", (r.returncode, state(root)), (0, "drain"))
c, d, root, env, host = make("g9-a2"); state(root, "fetch")
r = go(c, d, env, "pvetest", "drain", "--apply", FAKE_PROC="workerd")
check("g9-a: GUARD_PROCS=workerd -> STOP names it", (r.returncode, "workerd running" in r.stderr), (1, True))
c, d, root, env, host = make("g9-a3"); state(root, "fetch")
r = go(c, d, env, "pvetest", "drain", "--apply", FAKE_PROC="other")
check("g9-a: another process name blocks nothing", r.returncode, 0)
c, d, root, env, host = make("g9-b1"); state(root, "fetch"); setprof(d, 'HOLD_HINT="run the hold script"')
r = go(c, d, env, "pvetest", "drain", "--apply", FAKE_QM="  101 web running\\n")
check("g9-b: HOLD_HINT shown in the drain output", ("run the hold script" in r.stdout, "run the hold script" in r.stderr), (True, True))
c, d, root, env, host = make("g9-b2"); state(root, "fetch")
r = go(c, d, env, "pvetest", "drain", "--apply", FAKE_QM="  101 web running\\n")
check("g9-b: no HOLD_HINT -> the generic line", ("stop or migrate the guests and workloads listed" in r.stdout, "hold" in r.stdout + r.stderr), (True, False))
c, d, root, env, host = make("g9-c"); setprof(d, "SHARED=/mnt/pve/foo", "NFS_SERVER=no"); (root / "mnt/pve/foo").mkdir(parents=True)
r = go(c, d, env, "pvetest", "precheck", "--apply", FAKE_NFSLINE="/mnt/pve/foo vers=4.2", FAKE_SHN="foo")
state(root, "reboot")
r = go(c, d, env, "pvetest", "recheck", "--apply", FAKE_NFSLINE="/mnt/pve/foo vers=4.2", FAKE_SHN="foo")
check("g9-c: SHARED=/mnt/pve/foo checks pvesm 'foo'", "RECHECK OK" in r.stdout, True)
state(root, "reboot")
r = go(c, d, env, "pvetest", "recheck", "--apply", FAKE_NFSLINE="/mnt/pve/foo vers=4.2", FAKE_SHN="ShareBk")
check("g9-c: another storage name -> not active", "storage foo not active" in r.stderr, True)
c, d, root, env, host = make("g9-d")
go(c, d, env, "pvetest", "precheck", "--apply"); state(root, "reboot")
r = go(c, d, env, "pvetest", "recheck", "--apply", FAKE_TOK2="newtok")
blob = "".join(p.read_text(errors="replace") for p in (root / "root/pve-upgrade").rglob("*") if p.is_file() and p.suffix != ".tgz") + r.stdout + r.stderr
check("g9-d: a new token name -> recheck FAIL, no value anywhere", ("pve-tokens differs" in r.stderr, "secret" in blob), (True, False))
# an older record held only token ids: compared with OLD_TOKEN_USER's live ids (or, no user set, every id must still exist)
for nm, rec, user, want in (("old-match", "snap\n", "snapper@pve", False), ("old-differs", "other\n", "snapper@pve", True),
                            ("old-nouser-ok", "snap\n", None, False), ("old-nouser-gone", "gone\n", None, True),
                            ("new-format", "snapper@pve!snap\n", "snapper@pve", False)):
    c, d, root, env, host = make("g9-d-" + nm)
    if user: setprof(d, f'OLD_TOKEN_USER="{user}"')
    go(c, d, env, "pvetest", "precheck", "--apply"); state(root, "reboot"); (root / "root/pve-upgrade/pve-tokens").write_text(rec)
    r = go(c, d, env, "pvetest", "recheck", "--apply")
    check(f"g9-d: token record, {nm} -> " + ("FAIL" if want else "pass"), ("pve-tokens differs" in r.stderr, "RECHECK OK" in r.stdout), (want, not want))
# an empty NEW-format record (no tokens at precheck) is not the old format: a token added before recheck fails
c, d, root, env, host = make("g9-d-empty")
go(c, d, env, "pvetest", "precheck", "--apply", FAKE_NOTOK="1"); state(root, "reboot")
r = go(c, d, env, "pvetest", "recheck", "--apply")
check("g9-d: empty new-format record + a token added -> FAIL", "pve-tokens differs" in r.stderr, True)
# the old-format comparison matches the user exactly: a look-alike user's token is not OLD_TOKEN_USER's
c, d, root, env, host = make("g9-d-lookalike"); setprof(d, 'OLD_TOKEN_USER="apper@pve"')
go(c, d, env, "pvetest", "precheck", "--apply"); state(root, "reboot"); (root / "root/pve-upgrade/pve-tokens").write_text("snap\n")
r = go(c, d, env, "pvetest", "recheck", "--apply")
check("g9-d: look-alike user (snapper@pve vs apper@pve) -> FAIL", "pve-tokens differs" in r.stderr, True)
# --gpu yes on an NVIDIA=no node still reads driver.conf (NV_PATH names the .run, not a bare directory)
c, d, root, env, host = make("g9-gpu-no")
r = go(c, d, env, "pvetest", "all", "--gpu", "yes")
check("g9-d4: NVIDIA=no, --gpu yes: driver.conf is read", ("NVIDIA-Linux-x86_64-580.159.run" in r.stdout + r.stderr, "pve-upgrade/ " in r.stdout + r.stderr), (True, False))
c, d, root, env, host = make("g9-e"); (d / "driver.conf").unlink()
r = go(c, d, env, "pvetest", "precheck", "--apply")
check("g9-e: NVIDIA=no with no driver.conf -> no error", (r.returncode, "driver.conf" in r.stderr), (0, False))
c, d, root, env, host = make("g9-e2"); (d / "driver.conf").unlink()
r = go(c, d, env, "host-a", "precheck")
check("g9-e: NVIDIA=yes with no driver.conf and no NV_RUN -> refused", (r.returncode, "driver.conf" in r.stderr), (2, True))
c, d, root, env, host = make("g9-e3"); (d / "driver.conf").unlink(); setprof(d, "NVIDIA=yes", "GPUS=0", "NV_RUN=x.run", "NV_SHA256=" + "0" * 64, drop=("NVIDIA",))
r = go(c, d, env, "pvetest", "precheck")
check("g9-e: the profile may carry NV_RUN/NV_SHA256", "driver.conf" in r.stderr, False)
c, d, root, env, host = make("g9-f"); shutil.copy(d / "hosts/pvetest.conf", d / "hosts/my-node.conf")
r = go(c, d, env, "my-node", "precheck", "--apply")
check("g9-f: a hyphenated host name works", (r.returncode, state(root)), (0, "precheck"))
c, d, root, env, host = make("g9-g"); (root / "etc/exports").write_text("/srv/share 10.0.0.0/24(rw)\n")
before = tree(root); hosts = sorted(p.name for p in (d / "hosts").iterdir())
r = go(c, d, env, "newhost", "init")
keys = ["NAME", "NVIDIA", "GPUS", "ZFS_POOL", "NFS_SERVER", "CLUSTER", "SHARED", "CEPH", "STAY"]
lines = r.stdout.splitlines()
miss = [k for k in keys if not any(l.lstrip("#").startswith(k + "=") for l in lines)]
nocomment = [i for i, l in enumerate(lines) if "=" in l and not l.startswith("#") and not lines[i - 1].startswith("#")]
check("g9-g: init prints each key, each after a comment, writes no file",
      (r.returncode, miss, nocomment, "NFS_SERVER=yes" in lines, tree(root) == before, sorted(p.name for p in (d / "hosts").iterdir()) == hosts),
      (0, [], [], True, True, True))
r = go(c, d, env, "newhost", "precheck")
check("g9-g: a missing profile's STOP says to run init", (r.returncode, "newhost init" in r.stderr), (2, True))
r = go(c, d, env, "newhost", "init", "--apply")
check("g9-g: init takes no --apply", r.returncode, 2)


shutil.rmtree(T)
print(f"{'FAILED ' + str(fails) if fails else 'all ok'}")
raise SystemExit(1 if fails else 0)
