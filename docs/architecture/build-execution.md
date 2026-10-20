# Build execution and virtual machines

This guide describes where Catalyst Lab runs build tools and where it keeps their writable state. The key distinction is that a toolset image is a reusable, read-only base; each operation gets its own writable workspace.

## Lima VM runner path

When a VM runner is selected, commands run as root inside a Lima virtual machine using Alpine Linux. The guest kernel performs mounts, including Catalyst's SquashFS snapshot mount. Guest root is confined to the VM, while Library folders are shared with it. The macOS application bundles Lima and uses Virtualization.framework; Linux finds system Lima and uses QEMU with 9p shared folders.

When an operation needs the VM, Catalyst Lab starts it, creates a sparse ext4 workspace image under `Library/Temporary/BuildMachines/<machine>/workspace.img`, and mounts that image in the guest at `/var/lib/catalystlab`. The workspace stores Linux ownership metadata, mount points, and temporary operation data. The swap disk lives next to the workspace image and is recreated for machine starts.

Toolset commands run as guest root inside a `bwrap` mount and process sandbox. The VM workspace stores temporary writable toolset state, which is discarded when the toolset is unspawned.

The original Gentoo repository `.sqfs` file is passed to Catalyst as a file and bound into the toolset at Catalyst's snapshot path. Catalyst then handles the repository mount in its normal build flow; the app does not unpack snapshots into a directory and pass that directory to Catalyst.

Stage package caching defaults to **Automatic**. Catalyst writes completed binary packages to the registered Packages folder, in a subfolder named for the stage's `rel_type`, where compatible stages can reuse them. A stage can still select another cache folder or disable package caching.

The mount implementation depends on the runner:

- **VM runner:** Catalyst runs as guest root. Catalyst's normal SquashFS mount uses the guest kernel and loop devices. It does not need `squashfuse`.
- **Linux Host runner:** commands run in an unprivileged user namespace when available, so Catalyst cannot use the host kernel mount operation directly. A wrapper around Catalyst's mount interface recognizes its SquashFS mount request and runs `squashfuse` on the `.sqfs` file instead. `squashfuse` is a hidden, automatically selected toolset dependency of Catalyst so the executable is present in the toolset used by Host builds. The toolset also enables `user_allow_other` in `/etc/fuse.conf`, which permits the mounted snapshot to be accessed across the mapped users used by the rootless build.

The Host-only wrapper is in `src/objects/project/project_build_rootless.py`; it leaves Catalyst source unchanged and only substitutes the mount operation when Catalyst requests a SquashFS mount. The same Catalyst toolset can be used with either runner, so the hidden dependency remains part of Catalyst's toolset requirements even though VM builds do not invoke it. Stage inputs, generated specs, and build outputs are bound from Library folders. Package and distfile caches are shared with the host; writes are saved back after the operation. A custom package-cache folder outside the Library is added to the VM runner's shared folders before it starts, then bound into the toolset at its selected path. If the VM is already running without that share, the build asks the user to stop it and retry. Canceled operations save completed cache changes before the workspace is released.

The VM workspace is acquired for the build operation and may also be reserved by the loaded toolset. When the operation releases its workspace, mounts are removed and the temporary ext4 image is deleted. If the app exits unexpectedly, the next workspace setup unmounts and replaces the old image.

Toolset installation, updates, and interactive toolset use also run as guest root inside the VM, using the toolset's normal spawn and bind-mount flow. Toolset SquashFS extraction and caching remain separate from Catalyst's repository snapshot mount.

Host runner commands use user namespaces when available, or the privileged helper when they are unavailable. VM runner provisioning may configure subordinate IDs for its regular Alpine user, but normal toolset and build operations run as guest root. The user-namespace setup used to start or prepare a VM does not change the identity used for Catalyst operations inside the guest.

## Linux Host build path

On Linux, builds can run on the Host or in a selected Lima VM runner. Host execution uses unprivileged user namespaces and the required tools when available; otherwise the application uses a privileged helper. Host execution extracts reusable toolset SquashFS images into `Library/Cache/Extracted` and runs tools in isolated namespaces. In the current `MOUNT_SQFS_DIRECTLY = False` mode, toolset execution copies the reusable extraction into `Library/Temporary` for each operation; the source toolset image and extracted cache remain unchanged. This setting concerns the toolset image. Catalyst's Gentoo repository snapshot remains a `.sqfs` file and uses the runner-specific mount behavior described above.

Build preview files are generated under `Library/Temporary` while the user reviews them. When a build actually starts (including when a queued build is dequeued), the whole preview tree is moved into that build's workspace under `Library/Temporary/CatalystBuilds/<project>/<timestamp>/previews`. This removes the standalone preview directory immediately while keeping later stages' reviewed files available. Each stage copies its own files into its working directory when that stage starts. The build workspace, including any previews not consumed because a stage was skipped, is deleted during build cleanup.

## Applying Catalyst source patches

Toolset creation and updates can install user patches for Catalyst under `/etc/portage/patches/dev-util/catalyst`, where Gentoo's Catalyst ebuild applies them while building the package. This keeps a compatibility change in the reusable toolset instead of editing Catalyst files each time a stage build runs.

The Library patch `Toolsets/Patches/dev-util-catalyst/stage1-portage-atom-string.patch` fixes Catalyst 4.1.1-r1's stage1 build script, which concatenates a Portage Atom with a string. Select it as a Catalyst patch when creating or updating a toolset that uses this version. Newer Catalyst source already converts the Atom to a string, so this patch is only for versions whose `targets/stage1/build.py` contains `sys.stdout.write(b + " ")`; don't select it when that line is already fixed. The patch is applied when Catalyst is emerged into the toolset, and the resulting toolset records its patch filename.

## Other virtual machines

QEMU and Apple Virtualization are for testing built systems. They are separate from the Lima build machine: they boot a stage image, while Lima runs Catalyst and the build toolset. Test-VM base images and per-run writable images are stored under `Library/Cache` and `Library/Temporary`, respectively.

Stage startup is shown as separate preparation steps: create or reuse the cached base image, prepare the run's writable image, and build a custom kernel when requested. A custom kernel is emerged in a selected free Gentoo toolset using the chosen snapshot and stage overlays. Its read-only toolset image is mounted beneath one disposable writable root overlay; snapshot and configuration inputs are mounted read-only, while Portage state and outputs stay in temporary or cache folders. A write probe checks `/var/db/pkg`, `/var/lib/portage`, and `/var/log` before emerge starts. The toolset selection defaults to the project's toolset when it is free; other free external Gentoo toolsets can be selected. Stage Portage settings and the writable merged `/etc/portage` directory use separate mounts so the configuration copy cannot include its own destination. The build's binary-package cache is mounted read-only.

## Main implementation points

- `src/objects/build_machine/build_machine.py` owns machine lifecycle and the ext4 workspace.
- `src/objects/build_machine/lima.py` generates Lima configuration and Alpine setup.
- `src/objects/global_objects/rootless.py` selects mounted or extracted SquashFS and runs local or VM commands.
- `src/objects/project/project_build_rootless.py` builds the per-command overlay session and binds caches and inputs.
- `src/objects/project/project_build_process.py` coordinates preparation, stage builds, and cleanup.
- `src/objects/qemu/qemu_run.py` prepares test-VM images and builds optional custom kernels; `src/ui/qemu/qemu_run_view.py` owns the interactive VM screen.
