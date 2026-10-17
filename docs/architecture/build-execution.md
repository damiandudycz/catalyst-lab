# Build execution and virtual machines

This guide describes where Catalyst Lab runs build tools and where it keeps their writable state. The key distinction is that a toolset image is a reusable, read-only base; each operation gets its own writable workspace.

## macOS build path

Stage builds run in a Lima virtual machine using Alpine Linux. Alpine supplies the Linux kernel features the build needs, including user namespaces, SquashFS, and OverlayFS. The application shares its Library folders with the VM.

When an operation needs the VM, Catalyst Lab starts it, creates a sparse ext4 workspace image under `Library/Temporary/BuildMachines/<machine>/workspace.img`, and mounts that image in the guest at `/var/lib/catalystlab`. The workspace stores Linux ownership metadata, mount points, and temporary operation data. The swap disk lives next to the workspace image and is recreated for machine starts.

For a stage build, the VM mounts the toolset `.sqfs` directly as read-only SquashFS. It does not extract the image into Cache. A per-command OverlayFS session uses that mount as its lower layer and puts its writable upper and work directories under the workspace in `Library/Temporary`. The command runs in a merged view. Changes to the base toolset are discarded when that command ends.

The Gentoo repository snapshot is also mounted read-only and bound into the toolset at Catalyst's snapshot path. Stage inputs, generated specs, and build outputs are bound from Library folders. Package and distfile caches are shared with the host; writes are saved back after the operation. Canceled operations save completed cache changes before the workspace is released.

Each command has its own OverlayFS session. The VM and its read-only mounts can serve successive commands during one operation, but the writable merged view is not reused between commands. When the operation releases its workspace, mounts are removed and the temporary ext4 image is deleted. If the app exits unexpectedly, the next workspace setup unmounts and replaces the old image.

Toolset installation, updates, and interactive toolset use also run inside the VM, but use the toolset's normal spawn and bind-mount flow rather than the stage-build OverlayFS session. This keeps their writable environment isolated from the `.sqfs` image while preserving their own environment lifecycle.

## Linux build path

On Linux, builds run on the host. When unprivileged user namespaces and the required tools are available, the application extracts SquashFS images into `Library/Cache/Extracted` and runs tools in isolated namespaces. It uses a privileged helper when rootless toolset execution is unavailable. Extraction is used because this path cannot rely on the VM's real-root SquashFS mount; the extracted tree is reused while its source image remains unchanged.

## Other virtual machines

QEMU and Apple Virtualization are for testing built systems. They are separate from the Lima build machine: they boot a stage image, while Lima runs Catalyst and the build toolset. Test-VM base images and per-run writable images are stored under `Library/Cache` and `Library/Temporary`, respectively.

## Main implementation points

- `src/objects/build_machine/build_machine.py` owns machine lifecycle and the ext4 workspace.
- `src/objects/build_machine/lima.py` generates Lima configuration and Alpine setup.
- `src/objects/global_objects/rootless.py` selects mounted or extracted SquashFS and runs local or VM commands.
- `src/objects/project/project_build_rootless.py` builds the per-command overlay session and binds caches and inputs.
- `src/objects/project/project_build_process.py` coordinates preparation, stage builds, and cleanup.
- `src/objects/qemu/qemu_run.py` prepares test-VM images and builds optional custom kernels; `src/ui/qemu/qemu_run_view.py` owns the interactive VM screen.
