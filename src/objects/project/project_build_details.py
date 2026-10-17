from __future__ import annotations
import gzip, os, re, shlex, subprocess

# ------------------------------------------------------------------------------
# Details of stage builds stored in build.json (StageBuild.details), for reproducing builds and reporting bugs:
# inputs (snapshot, toolset, catalyst, project commit, seed), environment (where build ran and its resources), failure
# (failed packages and reason, with logs collected for bug reports) and output (archive size, installed packages).

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# " * ERROR: net-libs/webkit-gtk-2.54.0-r411::gentoo failed (compile phase):"
_FAILED_PACKAGE = re.compile(r"\* ERROR: (\S+?)(?:::(\S+))? failed \((\w+) phase\)")

# Known reasons of failures, by text in build output (first match is used).
_FAILURE_REASONS = [
    (re.compile(r"Killed signal terminated program|fatal error: Killed|out of memory|Cannot allocate memory|virtual memory exhausted", re.I),
     "out of memory, compiler was killed"),
    (re.compile(r"No space left on device"), "no space left on device"),
    (re.compile(r"Couldn't download|Fetch failed|Could not resolve host"), "download failed"),
    (re.compile(r"there are no ebuilds to satisfy|there are no ebuilds built with USE flags"), "package not found in repository"),
    (re.compile(r"The following USE changes are necessary"), "USE changes are needed"),
    (re.compile(r"The following keyword changes are necessary|masked by: "), "packages are masked"),
    (re.compile(r"Multiple package instances within a single package slot"), "slot conflict"),
    (re.compile(r"is blocking|Error: The above package list contains packages which cannot be"), "blocked packages"),
    (re.compile(r"internal compiler error"), "internal compiler error"),
]

def run_details(process) -> dict:
    """Inputs and environment shared by all stages of build run."""
    details = {"inputs": {}, "environment": {}}
    inputs, environment = details["inputs"], details["environment"]
    if snapshot := process.snapshot:
        inputs["snapshot"] = {"file": snapshot.filename, "date": snapshot.date.isoformat() if snapshot.date else None}
    if toolset := process.toolset:
        from .toolset_application import ToolsetApplication
        catalyst = toolset.get_app_install(ToolsetApplication.CATALYST)
        inputs["toolset"] = {"name": toolset.name, "id": str(toolset.uuid),
                             "catalyst": str(catalyst.version) if catalyst else None}
    if project := _project_commit(process.project_directory):
        inputs["project"] = project
    machine = process.machine
    if machine:
        environment.update({"runs_on": machine.name, "virtual_machine": True, "cpus": machine.cpus,
                            "memory_gib": machine.memory_gib, "swap_gib": getattr(machine, "swap_gib", 0)})
    else:
        environment.update({"runs_on": os.uname().nodename, "virtual_machine": False, "cpus": os.cpu_count(),
                            "memory_gib": _memory_gib()})
    environment["rootless"] = process.rootless
    return details

def seed_details(process, stage) -> dict | None:
    """Build of parent stage used as seed, or downloaded seed file."""
    parent = process.plan.parent(stage)
    if parent is None:
        subpath = process.seed_subpaths.get(stage.id)
        return {"file": os.path.basename(subpath)} if subpath else None
    build = process.stage_builds.get(parent.id) or process.plan.entries[parent.id].reused_build
    if build is None:
        return None
    return {"stage": build.stage_name, "build_id": str(build.id), "timestamp": build.timestamp, "artifact": build.artifact}

def failure_details(lines: list[str], failure_directory: str | None) -> dict:
    """Failed packages and reason found in build output, and logs collected for bug reports."""
    packages, seen_packages, reason = [], set(), None
    for line in lines:
        line = _ANSI.sub("", line)
        if match := _FAILED_PACKAGE.search(line):
            package = match.group(1)
            if package not in seen_packages:
                seen_packages.add(package)
                packages.append({"package": package, "repository": match.group(2) or "gentoo", "phase": match.group(3)})
        if reason is None:
            reason = next((text for pattern, text in _FAILURE_REASONS if pattern.search(line)), None)
    failure = {"packages": packages, "reason": reason}
    if failure_directory and os.path.isdir(failure_directory) and os.listdir(failure_directory):
        failure["logs"] = os.path.basename(failure_directory)
    return failure

def output_details(build) -> dict:
    """Archive size and packages installed in built stage (from catalyst CONTENTS), saved to packages file."""
    from .project_build import StageBuild
    output = {}
    if build.artifact_path and os.path.isfile(build.artifact_path):
        output["size"] = os.path.getsize(build.artifact_path)
    contents = f"{build.artifact_path}.CONTENTS.gz" if build.artifact_path else None
    if contents and os.path.isfile(contents):
        packages = installed_packages(contents)
        if packages:
            with open(os.path.join(build.path, StageBuild.PACKAGES_FILE), "w", encoding="utf-8") as file:
                file.write("\n".join(packages) + "\n")
            output["packages"] = len(packages)
    return output

# CONTENTS is tar listing, path is last field: "drwxr-xr-x root/root 0 2026-10-09 18:13 ./var/db/pkg/sys-apps/portage-3.0.82/".
_PACKAGE_DIRECTORY = re.compile(r"^\./var/db/pkg/([^/]+/[^/]+)/$")

def installed_packages(contents_path: str) -> list[str]:
    packages = set()
    try:
        with gzip.open(contents_path, "rt", encoding="utf-8", errors="replace") as file:
            for line in file:
                fields = line.split()
                if fields and (match := _PACKAGE_DIRECTORY.match(fields[-1])):
                    packages.add(match.group(1))
    except OSError as e:
        print(f"Failed to read {contents_path}: {e}")
    return sorted(packages)

def failure_logs_script(chroot: str, destination: str) -> str:
    """Bash collecting data that helps to find cause of failed build (and is usually requested in Gentoo bug reports)
    from chroot of failed build. Runs after build failed, with repository and /proc mounted in chroot; /dev is mounted
    here too (catalyst unmounts it, emerge refuses to run without /dev/null), and unmounted when done.
    Collected: summary (failed packages with their errors), emerge --info, portage configuration of stage, installed
    packages, end of emerge.log, elog messages, toolchain, disk space and memory, and for every package left in
    /var/tmp/portage (failed ones, portage cleans finished): its temp folder (build.log, environment, phase logs),
    emerge --info and emerge -pqv --tree of package, and configure, build and test logs of its work folder."""
    q = shlex.quote
    return f"""
FAILURE={q(destination)}
C={q(chroot)}
mkdir -p "$FAILURE"
echo "--- collecting logs for bug reports in $FAILURE"
in_chroot() {{
    chroot "$C" /usr/bin/env -i HOME=/root TERM=dumb PATH=/usr/sbin:/usr/bin:/sbin:/bin "$@" < /dev/null
}}
DEV_MOUNTED=""
if [ ! -c "$C/dev/null" ]; then
    # Unmounted also when script ends early, chroot is removed later and must not contain /dev of system.
    mkdir -p "$C/dev" && mount --rbind /dev "$C/dev" && DEV_MOUNTED=1 && trap 'umount -R -l "$C/dev" 2>/dev/null' EXIT
fi
in_chroot emerge --info > "$FAILURE/emerge-info.txt" 2>&1
# Configuration of stage (make.conf, package.use, package.env...), make.profile link is kept as link.
cp "$C/etc/portage/make.conf" "$FAILURE/make.conf" 2>/dev/null
cp -RP "$C/etc/portage" "$FAILURE/portage-config" 2>/dev/null
ls -l "$C/etc/portage/make.profile" > "$FAILURE/profile.txt" 2>&1
# Installed packages with versions.
( cd "$C/var/db/pkg" 2>/dev/null && ls -d */* ) > "$FAILURE/installed-packages.txt" 2>/dev/null
tail -n 300 "$C/var/log/emerge.log" > "$FAILURE/emerge.log" 2>/dev/null
[ -d "$C/var/log/portage/elog" ] && cp -R "$C/var/log/portage/elog" "$FAILURE/elog" 2>/dev/null
{{
    echo "--- gcc"; in_chroot gcc -v 2>&1 | tail -n 3
    echo "--- gcc-config"; in_chroot gcc-config -l 2>&1
    echo "--- binutils"; in_chroot ld --version 2>&1 | head -n 1
    echo "--- binutils-config"; in_chroot binutils-config -l 2>&1
    echo "--- python"; in_chroot python3 --version 2>&1
}} > "$FAILURE/toolchain.txt"
{{
    echo "--- disk space"; df -h "$C" /var/tmp 2>&1
    echo "--- memory"; free -m 2>/dev/null || head -n 5 /proc/meminfo
    echo "--- kernel messages (out of memory kills, crashes)"; dmesg 2>/dev/null | tail -n 100 || echo "not available"
}} > "$FAILURE/system.txt" 2>&1
{{
    echo "Failed build of $(basename "$C")"
    echo
    grep -h ">>> Failed to emerge\\|!!! " "$FAILURE/emerge.log" 2>/dev/null | tail -n 20
}} > "$FAILURE/summary.txt"
for PACKAGE_DIR in "$C"/var/tmp/portage/*/*/; do
    [ -d "$PACKAGE_DIR/temp" ] || continue
    PACKAGE=${{PACKAGE_DIR#$C/var/tmp/portage/}}; PACKAGE=${{PACKAGE%/}}
    DEST="$FAILURE/$(echo "$PACKAGE" | tr / _)"
    mkdir -p "$DEST"
    # build.log, environment, die.env, phase logs (temp/logging)...
    find "$PACKAGE_DIR/temp" -maxdepth 2 -type f -size -50M 2>/dev/null | while read -r FILE; do
        RELATIVE=${{FILE#$PACKAGE_DIR/temp/}}
        mkdir -p "$DEST/$(dirname "$RELATIVE")" && cp "$FILE" "$DEST/$RELATIVE"
    done
    in_chroot emerge --info "=$PACKAGE" > "$DEST/emerge-info.txt" 2>&1
    in_chroot emerge -pqv --tree "=$PACKAGE" > "$DEST/emerge-pqv.txt" 2>&1
    if [ -d "$PACKAGE_DIR/work" ]; then
        find "$PACKAGE_DIR/work" -maxdepth 6 -type f -size -20M \\( -name config.log -o -name CMakeError.log \\
            -o -name CMakeOutput.log -o -name CMakeConfigureLog.yaml -o -name CMakeCache.txt -o -name meson-log.txt \\
            -o -name testlog.txt -o -name test-suite.log -o -name LastTest.log \\) 2>/dev/null | while read -r LOG; do
            RELATIVE=${{LOG#$PACKAGE_DIR/work/}}
            mkdir -p "$DEST/work-logs" && cp "$LOG" "$DEST/work-logs/$(echo "$RELATIVE" | tr / _)"
        done
    fi
    {{
        echo
        echo "===== $PACKAGE (logs in $(basename "$DEST"))"
        if [ -f "$DEST/build.log" ]; then
            echo "--- errors:"
            sed 's/\\x1b\\[[0-9;]*[A-Za-z]//g' "$DEST/build.log" | grep -n -i "error\\|failed\\|undefined reference\\|cannot\\|not found" | grep -v -i "werror\\|-Wno-error" | head -n 30
            echo "--- end of build.log:"
            sed 's/\\x1b\\[[0-9;]*[A-Za-z]//g' "$DEST/build.log" | tail -n 40
        fi
    }} >> "$FAILURE/summary.txt"
    echo "Collected logs of $PACKAGE"
done
[ -n "$DEV_MOUNTED" ] && umount -R -l "$C/dev" && trap - EXIT
chmod -R a+rwX "$FAILURE" 2>/dev/null
"""

def _project_commit(project_directory) -> dict | None:
    """Commit of project Git directory, and whether it had uncommitted changes."""
    try:
        result = subprocess.run(["git", "status", "--porcelain=v2", "--branch"],
                                cwd=project_directory.directory_path(), capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        commit = next((line.removeprefix("# branch.oid ") for line in result.stdout.splitlines()
                       if line.startswith("# branch.oid ")), None)
        if not commit or commit == "(initial)":
            return None
        return {"commit": commit, "modified": any(not line.startswith("#") for line in result.stdout.splitlines())}
    except (OSError, subprocess.SubprocessError):
        return None

def _memory_gib() -> int | None:
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 ** 3)
    except (ValueError, OSError):
        return None

def format_duration(duration) -> str:
    seconds = int(duration.total_seconds())
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {seconds:02d}s"
