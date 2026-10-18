from __future__ import annotations
import os, shlex, uuid
from .rootless import sessions_directory

# ------------------------------------------------------------------------------
# Building stage.

# Copies changes of synced cache (upper layer of its overlay) to saved cache: new and changed files, and deletions
# (whiteouts, character devices). Hidden files are skipped, they are unfinished files of interrupted builds.
_SAVE_CACHE_CHANGES = r'''save_cache_changes() {
    upper=$1; saved=$2
    mkdir -p "$saved"
    ( cd "$upper" && find . -type c ) | while read -r path; do rm -rf -- "$saved/$path"; done
    ( cd "$upper" && find . \( -type f -o -type l \) ! -name '.*' -print0 | xargs -0 -r cp -a --no-preserve=ownership --parents -t "$saved" ) \
        || echo "Failed to save cache $saved"
}'''

# Runs catalyst with mounts adjusted for user namespace. Catalyst itself is not modified.
CATALYST_WRAPPER = '''import os, shutil, subprocess, sys
import libmount

_Context = libmount.Context

class _SquashfuseMount:
    def __init__(self, source, target):
        self.source, self.target = source, target
    def mount(self):
        subprocess.run(["squashfuse", "-o", "ro", self.source, self.target], check=True)

class _DevptsMount:
    """Own devpts instance instead of bind of host /dev/pts. Ptys of host devpts have tty group of host, which isn't
    mapped in namespace, so portage can't chown them to portage user. /dev/ptmx is pointed to the new instance."""
    def __init__(self, target):
        self.target = target
    def mount(self):
        _Context(source="devpts", target=self.target, fstype="devpts",
                 options="newinstance,ptmxmode=0666,mode=0620,gid=5").mount()
        ptmx = os.path.join(os.path.dirname(self.target.rstrip("/")), "ptmx")
        _Context(source=os.path.join(self.target, "ptmx"), target=ptmx, options="bind").mount()

def Context(*args, **kwargs):
    source, options = str(kwargs.get("source")), kwargs.get("options")
    if options == "bind" and source == "/dev/pts":
        return _DevptsMount(kwargs["target"])
    # Mounts inherited from host are locked in user namespace, so non-recursive bind of /dev (which has locked
    # submounts) fails. Recursive bind also brings real device nodes into chroot.
    if options == "bind" and source == "/dev":
        kwargs["options"] = "rbind"
    if kwargs.get("fstype") == "squashfs":
        if os.path.isdir(source):
            kwargs["fstype"], kwargs["options"] = "", "bind" # Extracted snapshot.
        elif shutil.which("squashfuse"):
            return _SquashfuseMount(source, kwargs["target"])
    return _Context(*args, **kwargs)

libmount.Context = Context

# Catalyst passes Path of folder repository to portage RepoConfig, which expects str.
import catalyst.support as support
_get_repo_name_from_dir = support.get_repo_name_from_dir
support.get_repo_name_from_dir = lambda repo_path: _get_repo_name_from_dir(str(repo_path))

# Device nodes can't be created in user namespace, tar would fail on /dev/console and /dev/null of seed. Catalyst binds
# /dev into chroot anyway.
from DeComp.compress import CompressMap
_create_infodict = CompressMap.create_infodict
_DEVICE_EXCLUDES = ["--exclude=./dev/console", "--exclude=./dev/null"]
def create_infodict(self, *args, **kwargs):
    options = kwargs.get("other_options")
    if isinstance(options, str):
        kwargs["other_options"] = " ".join([options] + _DEVICE_EXCLUDES)
    elif isinstance(options, (list, tuple)):
        kwargs["other_options"] = type(options)(list(options) + _DEVICE_EXCLUDES)
    return _create_infodict(self, *args, **kwargs)
CompressMap.create_infodict = create_infodict

from catalyst.main import main
main(sys.argv[1:])
'''

def catalyst_snapshot_command() -> str:
    """Generate a Gentoo snapshot using Catalyst's normal command."""
    return "catalyst -s stable"

def stage_build_session_script(executor, toolset_path: str, bindings: list[tuple[str, str]], command: str,
                               diagnostics_command: str | None = None, synced_caches: list[tuple[str, str]] | None = None,
                               patch_catalyst_stage1: bool = False) -> str:
    """Script running command in toolset root (overlay over extracted toolset, changes are discarded), with host
    folders bound at given paths (host path, path in toolset). Diagnostics command runs after failure.
    Synced caches (saved path, path used by build) are mounted at used path as overlay over saved path, and changes
    are copied back after build (also when it fails, to keep downloaded files). Saved caches are plain files.
    When build is interrupted, changes stay in session and are saved by save_interrupted_caches."""
    session = os.path.join(sessions_directory(executor), f"build-{uuid.uuid4().hex}")
    q = shlex.quote
    caches = list(enumerate(synced_caches or []))
    # A synced cache is mounted at its build path as an overlay. Binding the raw machine cache there afterward would
    # hide the writable upper layer and make userpriv/userfetch writes fail.
    synced_cache_targets = {os.path.normpath(used) for _saved, used in (synced_caches or [])}
    bindings = [(host, target) for host, target in bindings if os.path.normpath(target) not in synced_cache_targets]
    binds = "\n".join(f"bind {q(host)} {q(target)}" for host, target in bindings)
    # Materialize bind mountpoints in the overlay upper layer before mounting the overlay. Creating a deeply nested
    # mountpoint directly in the merged view can fail with EOVERFLOW on the machine's SquashFS lower layer (notably
    # /var/tmp/catalyst/snapshots). Copy directory modes from existing lower-layer ancestors so this does not change
    # permissions such as /var/tmp's sticky bit.
    prepare_bind_targets = "\n".join(f"prepare_bind_target {q(target)} || exit 1" for _host, target in bindings)
    stage1_patch = """python3 - "$TOOLSET" "$UPPER" <<'PATCH_TOOLSET_FILE'
import os, shutil, sys
toolset, upper = sys.argv[1:]
relative = "/usr/share/catalyst/targets/stage1/build.py"
old, new = 'sys.stdout.write(b + " ")', 'sys.stdout.write(str(b) + " ")'
source = os.path.join(toolset, relative.lstrip("/"))
target = os.path.join(upper, relative.lstrip("/"))
if os.path.isfile(source):
    with open(source, encoding="utf-8") as file:
        contents = file.read()
    if old in contents:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as file:
            file.write(contents.replace(old, new))
        shutil.copystat(source, target)
        print(f"Patched toolset file in writable overlay: {relative}")
PATCH_TOOLSET_FILE""" if patch_catalyst_stage1 else ""
    # Saved caches are lower layer of overlay (read from shared folder without copying), changes go to upper layer in
    # session and are copied back after build: new and changed files, and deletions (whiteouts, character devices).
    sync_in = "\n".join(
        f'mkdir -p {q(saved)} {q(used)} "$SESSION/caches/{index}/upper" "$SESSION/caches/{index}/work" && '
        f'chown "$PORTAGE_UID:$PORTAGE_GID" "$SESSION/caches/{index}/upper" && '
        f'echo {q(saved)} > "$SESSION/caches/{index}/saved" && '
        f'mount -t overlay overlay -o lowerdir={q(saved)},upperdir="$SESSION/caches/{index}/upper",workdir="$SESSION/caches/{index}/work" {q(used)} '
        f'|| {{ echo "Failed to use cache {saved}"; exit 1; }}'
        for index, (saved, used) in caches)
    sync_out = "\n".join(
        f'save_cache "$SESSION/caches/{index}/upper" {q(used)} {q(saved)}'
        for index, (saved, used) in caches)
    portage_setup = """# Resolve Portage's ids from the toolset: the build machine itself may not have a portage account.
PORTAGE_UID=$(awk -F: '$1 == "portage" { print $3; exit }' "$TOOLSET/etc/passwd")
PORTAGE_GID=$(awk -F: '$1 == "portage" { print $4; exit }' "$TOOLSET/etc/passwd")
case "$PORTAGE_UID" in ''|*[!0-9]*) echo "Could not resolve portage uid from toolset"; exit 1 ;; esac
case "$PORTAGE_GID" in ''|*[!0-9]*) echo "Could not resolve portage gid from toolset"; exit 1 ;; esac""" if caches else ""
    has_gentoo_repo_binding = any(os.path.normpath(target) == "/var/db/repos/gentoo"
                                  for _host, target in bindings)
    portage_access_check = """# Check the same access that Portage's userpriv/userfetch workers need before launching Catalyst.
if [ -f "$ROOT/var/db/repos/gentoo/profiles/thirdpartymirrors" ]; then
    echo "Checking Gentoo snapshot and distfiles access as portage"
    if ! chroot "$ROOT" /usr/bin/python3 - <<'PY_PORTAGE_ACCESS'
import os, pwd, sys
account = pwd.getpwnam("portage")
os.setgid(account.pw_gid)
os.setuid(account.pw_uid)
try:
    with open("/var/db/repos/gentoo/profiles/thirdpartymirrors", "rb"):
        pass
except OSError as error:
    print(f"portage cannot read Gentoo snapshot: {error}", file=sys.stderr)
    sys.exit(1)
probe = f"/var/cache/distfiles/.catalystlab-portage-access-test-{os.getpid()}"
try:
    with open(probe, "xb"):
        pass
    os.unlink(probe)
except OSError as error:
    print(f"portage cannot write Gentoo distfiles cache: {error}", file=sys.stderr)
    sys.exit(1)
PY_PORTAGE_ACCESS
    then
        echo "Portage access check failed; effective identity and path permissions:"
        chroot "$ROOT" /usr/bin/python3 - <<'PY_PORTAGE_DIAGNOSTICS' || true
import os, pwd, stat
account = pwd.getpwnam("portage")
os.setgid(account.pw_gid)
os.setuid(account.pw_uid)
print(f"uid={os.getuid()} gid={os.getgid()}")
for path in ("/var", "/var/db", "/var/db/repos", "/var/db/repos/gentoo", "/var/db/repos/gentoo/profiles", "/var/db/repos/gentoo/profiles/thirdpartymirrors", "/var/cache", "/var/cache/distfiles"):
    try:
        info = os.stat(path)
        print(f"{stat.filemode(info.st_mode)} {info.st_uid}:{info.st_gid} {path}")
    except OSError as error:
        print(f"{path}: {error}")
PY_PORTAGE_DIAGNOSTICS
        exit 1
    fi
fi""" if has_gentoo_repo_binding and caches else ""
    diagnostics = f'[ $status != 0 ] && chroot "$ROOT" /usr/bin/env -i HOME=/tmp TERM=dumb PATH=/usr/sbin:/usr/bin:/sbin:/bin {diagnostics_command} < /dev/null' if diagnostics_command else ""
    return f"""set -u
SESSION={q(session)}
ROOT="$SESSION/root"; UPPER="$SESSION/upper"; WORK="$SESSION/work"
TOOLSET={q(toolset_path)}
cleanup() {{
    cd /
    umount -R -l "$ROOT" 2>/dev/null
    # Mounts are on merged root, upper and work contain only files of this session.
    rm -rf --one-file-system "$UPPER" "$WORK"
    # Changes of caches are kept until they are saved.
    [ -n "${{CACHES_SAVED:-}}" ] && rm -rf --one-file-system "$SESSION/caches"
    rmdir "$ROOT" "$SESSION" 2>/dev/null
}}
trap cleanup EXIT
{_SAVE_CACHE_CHANGES}
save_cache() {{
    umount "$2"
    save_cache_changes "$1" "$3"
}}
mkdir -p "$ROOT" "$UPPER" "$WORK" || exit 1
prepare_bind_target() {{
    local current="" component mode
    local -a components
    IFS=/ read -r -a components <<< "${{1#/}}"
    for component in "${{components[@]}}"; do
        current="$current/$component"
        mkdir -p "$UPPER$current" || return 1
        if [ -d "$TOOLSET$current" ]; then
            mode=$(stat -c %a "$TOOLSET$current") || return 1
            chmod "$mode" "$UPPER$current" || return 1
        fi
    done
}}
{prepare_bind_targets}
{stage1_patch}
{portage_setup}
mkdir -p "$UPPER/etc" || exit 1
if [ -d "$TOOLSET/etc" ]; then
    mode=$(stat -c %a "$TOOLSET/etc") || exit 1
    chmod "$mode" "$UPPER/etc" || exit 1
fi
# Put the VM's resolver file in the writable overlay so it overrides the toolset's possibly dangling systemd-resolved
# symlink. Writing it into the merged root would try to follow that symlink and leave the chroot without DNS.
cat /etc/resolv.conf > "$UPPER/etc/resolv.conf" || {{ echo "Failed to configure DNS in toolset"; exit 1; }}
mount -t overlay overlay -o lowerdir={q(toolset_path)},upperdir="$UPPER",workdir="$WORK" "$ROOT" || exit 1
mount --rbind /dev "$ROOT/dev" || exit 1
mount -t proc proc "$ROOT/proc" || exit 1
mount -t tmpfs tmpfs "$ROOT/tmp" && mount -t tmpfs tmpfs "$ROOT/run" || exit 1
bind() {{
    mkdir -p "$ROOT$2" && mount --rbind "$1" "$ROOT$2" || {{ echo "Failed to bind $1"; exit 1; }}
}}
{sync_in}
{binds}
{portage_access_check}
chroot "$ROOT" /usr/bin/env -i HOME=/tmp TERM=dumb PATH=/usr/sbin:/usr/bin:/sbin:/bin {command} < /dev/null
status=$?
{sync_out}
CACHES_SAVED=1
{diagnostics}
exit $status
"""

def save_interrupted_caches(executor, output_handler, process_holder: list | None = None) -> bool:
    """Saves changes of synced caches left by interrupted builds (eg. canceled), which didn't copy them back, and
    removes their sessions. Packages and sources they downloaded or built are kept this way."""
    q = shlex.quote
    sessions = q(sessions_directory(executor))
    script = f"""
{_SAVE_CACHE_CHANGES}
for manifest in {sessions}/build-*/caches/*/saved; do
    [ -f "$manifest" ] || continue
    directory=$(dirname "$manifest"); saved=$(cat "$manifest")
    echo "Saving cache of interrupted build: $saved"
    save_cache_changes "$directory/upper" "$saved"
done
rm -rf --one-file-system {sessions}/build-*
"""
    return executor.run_in_namespace(script, output_handler, process_holder)
