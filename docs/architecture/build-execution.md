# Build execution and virtual machines

This guide describes where Catalyst Lab runs build tools and where it keeps their writable state. The key distinction is that a toolset image is a reusable, read-only base; each operation gets its own writable workspace.

## Lima VM runner path

When a VM runner is selected, builds run in a Lima virtual machine using Alpine Linux. Alpine supplies the Linux kernel features the build needs, including user namespaces, SquashFS, and OverlayFS. The application shares its Library folders with the VM. The macOS application bundles Lima and uses Virtualization.framework; Linux finds system Lima and uses QEMU with 9p shared folders.

When an operation needs the VM, Catalyst Lab starts it, creates a sparse ext4 workspace image under `Library/Temporary/BuildMachines/<machine>/workspace.img`, and mounts that image in the guest at `/var/lib/catalystlab`. The workspace stores Linux ownership metadata, mount points, and temporary operation data. The swap disk lives next to the workspace image and is recreated for machine starts.

For a stage build, a per-command OverlayFS session uses the toolset as its lower layer and puts its writable upper and work directories under the workspace in `Library/Temporary`. The command runs in a merged view. Changes to the base toolset are discarded when that command ends.

The original Gentoo repository SquashFS file is bound into the toolset at Catalyst's snapshot path, so Catalyst recognizes it as a SquashFS file and mounts it through its normal repository flow. Selecting Catalyst for a toolset automatically selects the hidden `sys-fs/squashfuse` dependency, which the rootless wrapper uses for Catalyst's SquashFS mount request. Stage inputs, generated specs, and build outputs are bound from Library folders. Package and distfile caches are shared with the host; writes are saved back after the operation. Canceled operations save completed cache changes before the workspace is released.

Each command has its own OverlayFS session. The VM and its read-only mounts can serve successive commands during one operation, but the writable merged view is not reused between commands. When the operation releases its workspace, mounts are removed and the temporary ext4 image is deleted. If the app exits unexpectedly, the next workspace setup unmounts and replaces the old image.

Toolset installation, updates, and interactive toolset use also run inside the VM, but use the toolset's normal spawn and bind-mount flow rather than the stage-build OverlayFS session. Their SquashFS handling is controlled by the source-level `MOUNT_SQFS_DIRECTLY` constant in `src/objects/global_objects/rootless.py`. This is intentionally a code flag, not a user preference. It is currently `False` to test the extracted flow: the application keeps an extracted toolset in `Library/Cache/Extracted`, then copies it into that operation's workspace under `Library/Temporary` before use. The copy is disposable; changes do not modify the reusable cache. Set the constant to `True` to use direct SquashFS or a mounted extracted cache instead.

Direct SquashFS use is **experimental**. It avoids extracting and copying the full toolset for each operation, which can save startup time, temporary disk space, and repeated filesystem work. However, direct mounts expose the image's filesystem ownership through the VM and user-namespace mappings. If those IDs are translated differently between the host, build machine, and `bwrap`, files may appear owned by unexpected IDs or become inaccessible to Portage. The extracted-cache-and-copy flow is the current test baseline while this ownership behavior is investigated. Emerging successfully from a snapshot only confirms that the snapshot is accessible; it does not verify toolset ownership or writable Portage paths. Stage-build OverlayFS setup is separate and continues to mount its lower layer directly.

## Linux Host build path

On Linux, builds can run on the Host or in a selected Lima VM runner. Host execution uses unprivileged user namespaces and the required tools when available; otherwise the application uses a privileged helper. Host execution extracts SquashFS images into `Library/Cache/Extracted` and runs tools in isolated namespaces. In the current `MOUNT_SQFS_DIRECTLY = False` mode, toolset execution copies the reusable extraction into `Library/Temporary` for each operation; the source SquashFS and extracted cache remain unchanged. The code flag also selects the direct-mount path when set to `True` where the executor supports it.

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
