# Catalyst Lab

**Build custom Gentoo Linux stages, live images and systems with Catalyst, from a friendly GNOME application.**

Catalyst Lab wraps [Catalyst](https://wiki.gentoo.org/wiki/Catalyst), the Gentoo
[Release Engineering](https://gitweb.gentoo.org/proj/releng.git/) templates, QEMU and other tools in a
GTK4 / libadwaita interface. You describe what to build as a tree of stages, and Catalyst Lab prepares the build
environment, generates spec files, runs the builds and can even install the result on another machine. No deep
knowledge of Catalyst internals is needed.

> **Status:** Catalyst Lab is in active development. Features and file formats can still change. Deploying is
> currently being tested and might not work correctly.

---

## Contents

- [Features](#features)
  - [Status indicators](#-status-indicators)
- [How it works](#how-it-works)
- [Supported systems](#supported-systems)
- [Installation](#installation)
- [Development](#development)
- [Roadmap](#roadmap)
- [License](#license)

---

## Features

### 📁 Projects and stages
- A project is a **tree of stages** (stage1 → stage3 → stage4, livecd, diskimage, netboot, embedded...), where each
  stage uses the build of its parent as a seed.
- Every Catalyst spec option can be edited, with values **inherited** from the parent stage or from a **Releng
  template**, or set by hand. Missing or unsupported values are marked with warnings.
- **Portage configuration** and **root overlays** are combined from Releng templates, parent stages and the stage
  itself.
- Profiles are listed from the snapshot and from the overlays used by the stage.
- Projects are **Git directories**, so their changes can be tracked and shared.
- New projects can be created from **templates** stored in the app repository, with options like architecture,
  init system and additional software. Projects can be updated when their template or Git repository changes: own
  commits are replayed on top of the new version, with conflicting changes decided one by one. See [Project templates](docs/project-templates.md) to create
  and publish one.

### 🔨 Builds
- Select the stages to build. Parent stages are **reused** from their latest builds, or built too when they have none.
- Choose the **snapshot** for each build: the project's own, any other one, or **get the latest** one, generated with
  the project toolset before building.
- Builds run **without root privileges** (unprivileged user namespaces) when the system allows it, otherwise through
  a root helper authorized with polkit.
- **Live progress**: build order, the last output line of every step, emerge progress as a percentage, colored output
  and a `build.log` saved for each stage.
- Package and kernel **caches** are kept between builds, and packages are built in parallel.
- When a stage fails, **diagnostics** are collected and the stages depending on it are skipped. Other branches of the
  tree are still built.
- The **Builds** section lists every build run of every stage, including the ones that were skipped or canceled.

### 🗂️ App data
- Everything the app stores is in its **library** (`~/CatalystLab` by default): toolsets, snapshots, projects,
  builds, machines, and its settings and lists of items (`Config` folder). Removing this folder resets the app. In
  **Settings** the library can be moved or copied to another place, or another library (new or existing) can be used;
  the app restarts afterwards. It can't be changed while something is loaded or running.

### 📦 Binary packages
- Binary packages built with stages are kept in **binary packages folders** (`~/CatalystLab/Packages`), separate from
  builds of projects, and reused by next builds.
- Projects can **share a folder** to reuse packages built by each other. Folders record architecture and CPU flags of
  projects they were made for: folders of other architecture can't be selected, other CPU flags show a warning (such
  packages might not run on machines of the project).
- New projects get their own folder or use an existing one. Changing architecture of a project offers to switch to
  another folder, deleting the last project using a folder asks whether to delete it too.

### 🧰 Environments
- **Toolsets** are isolated Gentoo environments with the tools needed for building (Catalyst, QEMU...). They are
  created from official stage3 images, kept as `.squashfs` files and can be updated from the app.
- **Virtual machines** (macOS) run toolsets where stages can't be built natively. Catalyst Lab manages them with
  [Lima](https://lima-vm.io):
  - they start when needed and stop when nothing uses them;
  - they emulate other architectures with QEMU;
  - their processors, memory and working space can be changed.

### 📚 Releng, snapshots and overlays
- **Releng** directories: clones of the Gentoo Release Engineering repository (or your fork), used as defaults for
  stages.
- **Snapshots**: compressed copies of the Gentoo ebuild repository, generated with Catalyst or imported from a file.
  You can browse the packages in each one.
- **Overlays**: additional ebuild repositories that stages can use, together with their profiles.

### 🚀 Deploy *(being tested)*
Install a built stage3 or stage4 on real hardware:
- **On another machine** booted from Gentoo LiveCD, through SSH.
- **On a disk connected to this computer** (SD card, external disk), on Linux.
- **To a disk image file** (`.img.xz`), written later to SD cards or disks with Raspberry Pi Imager or `dd`. The image
  is sized to the installed system and grows to fill the disk when the system starts first time. On macOS it's
  created in the virtual machine.

The deploy wizard handles:
- **Disk**: editable partition layout with sensible defaults, GPT or MBR.
- **System**: hostname, users, root password, network, timezone, locales, keyboard layout and SSH keys.
- **Boot**: kernel (from the stage or a distribution kernel), linux-firmware and bootloader (GRUB, systemd-boot,
  rEFInd, EFI stub, or kboot for PS3 petitboot), with warnings about anything that needs manual updates later.

### 🖥️ Test built images *(being tested)*

Builds with a generic kernel run in a virtual machine from their row in Builds:
- Choose an available backend before starting. Missing software and incompatible backends are shown with a reason.
- **Apple Virtualization** on macOS 13 or later boots builds matching the Mac's CPU architecture. It uses the signed
  native bridge included with Catalyst Lab, with an embedded display, serial console and power controls in the app, and
  NAT networking. Click the display to capture keyboard input; Ctrl+Alt+G releases it.
  QEMU is not required for this backend. ISO images must support EFI boot.
- **QEMU** boots native or other supported architectures. Its screen is shown in the app with keyboard, mouse,
  serial console and power controls; SSH is forwarded to a local port. Requires `brew install qemu` on macOS or
  `app-emulation/qemu` on Gentoo. Native guests use HVF on macOS or KVM on Linux when available; other guests are emulated.
- **Live CD ISO images** boot with EFI firmware.
- **stage3 and stage4 with kernel** boot their own kernel and initramfs, with system in disk image made from the stage
  (once, kept with the build). Changes are discarded when the machine stops, root logs in without password.
- Kernels of devices QEMU doesn't emulate (Raspberry Pi, PS3) can't run, their builds show why.
- Test VM CPU and memory defaults are under **Settings → Test built images**. Toolset VMs have separate resource settings.

### 🟢 Status indicators

Items in lists and sections of the side menu have a colored dot showing their state:

| Color | Meaning |
|---|---|
| ⚪ Gray | Not used or mounted, no changes |
| 🟢 Green | Completed successfully |
| 🔵 Blue | Mounted, loaded or running |
| 🟣 Purple | Has changes |
| 🟠 Orange | Has warnings |
| 🔴 Red | Has errors |

The dot **blinks** while the item is actively used (building, running an operation), in its current color (gray
becomes blue). An item can have several states at once (eg. running and has changes): the dot shows the most
important one (red, orange, purple, blue, green, gray), and hovering it lists all of them, each with its own color.

A section in the side menu shows the most important state of its items, and blinks while any of them is used or any of
its operations runs. Results, warnings and errors (eg. completed or failed build) are shown there until the section is
opened, and again when a new one appears. Changes and ongoing states (running machines, open environments, operations) stay visible.
Hovering the dot lists items and their states.

| Item | Green | Blue | Purple | Orange | Red | Blinking |
|---|---|---|---|---|---|---|
| Toolset | | Environment is open | | Catalyst is not installed | File or virtual machine is missing | Mounting, unmounting, used by operation or command |
| Virtual machine | | Running | | | Missing | Starting, stopping, used by operation |
| Project | Last build completed (until project is opened) | | Not saved changes | | Last build failed (until project is opened), toolset, Releng directory or snapshot is missing, Git error or conflicts | Building, updating, saving or discarding changes |
| Releng directory, overlay | | | Not saved changes | | Git error or conflicts | Updating, saving or discarding changes |
| Project in Builds (also Builds in side menu) | Last build completed (until project or its builds are opened) | | | Last build stopped (until opened) | Last build failed (until opened) | Building |
| Project in Deploy | | | | | | Deploying |
| Binary packages folder | | | | | | Used by build |

---

## How it works

1. **Prepare an environment**: create a toolset (on macOS, create a virtual machine first).
2. **Add sources**: clone a Releng directory and generate a snapshot.
3. **Create a project**: choose its toolset, Releng directory, snapshot and architecture, then add stages.
4. **Build**: select stages and start the build. Results appear in the **Builds** section.
5. **Deploy** *(optional)*: install a stage3/stage4 build on a machine or disk.

---

## Supported systems

| | Linux | macOS |
|---|---|---|
| Building stages | ✅ On the computer, without root when user namespaces are available | ✅ In virtual machines (Lima) |
| Other architectures | ✅ QEMU user emulation | ✅ QEMU user emulation in the virtual machine |
| Deploy over SSH | ✅ | ✅ |
| Deploy to a local disk | ✅ | — Virtual machines can't access disks of the Mac |

### Requirements

**Linux**
- `bwrap` (bubblewrap) 0.11 or newer
- `squashfs-tools`, `git`
- Kernel with overlayfs and squashfs support
- For building without root: `unshare`, `newuidmap` / `newgidmap`, entries for your user in `/etc/subuid` and
  `/etc/subgid`, and unprivileged user namespaces enabled. Otherwise `pkexec` (polkit) is used.
- For deploying: `ssh`, and QEMU user emulation (binfmt) when deploying another architecture to a local disk

**macOS**
- The application bundle includes everything except `git` (install the Xcode Command Line Tools).

---

## Installation

### Linux: Flatpak

```bash
packaging/flatpak/build.sh --install
```

Builds the Flatpak from this checkout with `flatpak-builder` (GNOME runtime 51 from Flathub) and installs it for your
user. Run it with `flatpak run com.damiandudycz.CatalystLab`.
- bubblewrap, squashfs-tools and Python packages are downloaded as pinned sources and built into the Flatpak.
- Options:
  - `--bundle` creates `dist/CatalystLab.flatpak`, which can be installed on other computers;
  - `--clean` starts from scratch.

### Gentoo: Portage

Ebuilds are in [packaging/gentoo](packaging/gentoo) (overlay with `dev-util/catalystlab`, live and release
versions). Add it as a repository and `emerge dev-util/catalystlab`, see its README.

### Linux: directly on the host

```bash
./install.sh
```

This builds the app with Meson and installs it system-wide (`sudo ninja install`).

### macOS: application bundle

```bash
packaging/macos/build-app.sh
```

Creates `dist/Catalyst Lab.app` and `dist/Catalyst Lab.dmg`. The bundle contains Python, GTK, libadwaita, squashfs
tools and Lima, so it runs on Macs without Homebrew.
- Build dependencies come from Homebrew and Lima from GitHub (checksum verified).
- Homebrew packages missing on your Mac are installed only for the build and removed when it ends.
- Options:
  - `--keep-packages` keeps those Homebrew packages installed;
  - `--no-dmg` skips the disk image;
  - `--clean` starts from scratch.

---

## Development

### Running from the checkout

- **Linux:** install Python 3 with PyGObject and requests, GTK 4, libadwaita 1.7 or newer, Meson, Ninja,
  gettext and GLib development tools using your distribution's package manager, then run:
  ```bash
  ./run.sh
  ```
  It rebuilds changes and launches the app using a local installation in `.build-linux`, without `sudo`.
  It uses your real Catalyst Lab folder and settings. Building toolsets also needs the Linux runtime requirements above.
  Alternatively, use GNOME Builder with `packaging/flatpak/com.damiandudycz.CatalystLab.json` or
  `packaging/flatpak/build.sh`.
- **macOS:**
  ```bash
  brew install gtk4 libadwaita pygobject3 meson ninja squashfs lima
  ./run.sh
  ```
  It uses your real Catalyst Lab folder and settings. Build files are kept in `.build-macos`.

### Updating Flatpak dependencies

- **Python packages:** `packaging/flatpak/update-python-dependencies.sh` regenerates
  `packaging/flatpak/dependencies/python3-requests.json` with the newest versions, using `flatpak-pip-generator.py`
  from [flatpak-builder-tools](https://github.com/flatpak/flatpak-builder-tools).
- **bubblewrap and squashfs-tools:** update the release URL and `sha256` in their files in
  `packaging/flatpak/dependencies/`.

### Project layout

| Path | Contents |
|---|---|
| `src/objects/` | Model and logic: projects, builds, toolsets, virtual machines, snapshots, deploy, root helper |
| `src/ui/` | Views (`.py` + `.ui` templates) and app sections |
| `data/` | Icons, desktop file, metainfo, GSettings schema and project template definitions (fetched from the app repository) |
| `docs/` | Documentation, including [project templates](docs/project-templates.md) and the [UI architecture handbook](docs/ui/README.md) |
| `packaging/flatpak/` | Flatpak manifest, its modules (bubblewrap, squashfs-tools, Python packages) and build scripts |
| `packaging/macos/` | macOS application bundle |

### Window structure

```
CatalystlabApplication
╰── CatalystlabWindow
    ╰── AdwNavigationView (navigation_view)
        ╰── AdwNavigationPage
            ╰── AdwOverlaySplitView (split_view)
                ├── AdwNavigationPage
                │   ╰── AdwToolbarView
                │       ╰── CatalystlabWindowSideMenu (side_menu)
                ╰── CatalystlabWindowContent (content_view)
```

### App sections

The main views of the application are **app sections**, registered with the `@app_section` decorator. It takes:
- **Display details:** title, label and icon.
- **Order:** position in the side menu.
- **Window behavior:** whether the section is shown in the side menu, and whether the side menu is shown with it.

Every section implements:

```python
def __init__(self, content_navigation_view: Adw.NavigationView, **kwargs):
```

`content_navigation_view` is created by `CatalystlabWindowContent`. A section can also be created elsewhere with
`AppSectionDetails.create_section(content_navigation_view)`.

### Navigation

A section shown in `CatalystlabWindowContent` is embedded in a `NavigationView` → `ToolbarView` structure, so it has
its own header and navigation. The main window controls the visibility of the side menu toggle that is added there.

To show a new view, push it on the section's `content_navigation_view`. To push it on the main navigation view, send
`AppEvents.PUSH_SECTION`.

### Root helper

Operations that need root privileges are Python functions marked with `@root_function`. They are sent to a helper
process started with `pkexec`, which streams their output back and can cancel them. Root access stays unlocked while
some operation holds an authorization keeper.

---

## Roadmap

### Planned features
- **Bugs**: collect bugs found in builds and link them with Gentoo Bugzilla.
- **Notes**: notes for projects.
- **Templates**: reusable templates derived from Releng specs or defined by the user, able to include other templates
  (with detection of circular dependencies).
- **Projects**:
  - export `.spec` and other Catalyst files instead of building in the app;
  - edit the final `.spec` before building, and show differences from the generated one.
- **Help**: text and video help for each section, opened from relevant places in the app.
- **Virtual machines on Linux**: for systems where toolsets can't run without root (unprivileged user namespaces
  blocked, eg. by AppArmor on Ubuntu or hardened kernels), as an alternative to the root helper.
  - Needs Lima configuration for Linux hosts: `vmType: qemu` with KVM, and a shared folder type that works there,
    like virtiofs or 9p.
  - See `virtual_machines_supported` in `lima.py`.

### Open tasks

<details>
<summary>General</summary>

- [ ] Store Catalyst logs and links to detected bugs in builds
- [ ] Link bugs to Bugzilla issues
- [ ] System checks for required host components (bwrap version and capabilities, pkexec...)
- [ ] Restore IDs when restoring items from repository, when they are free
- [ ] When using host environment, use host tools instead of Flatpak ones (bwrap, unsquashfs...)
</details>

<details>
<summary>Toolsets</summary>

- [ ] Save toolset commands with their bindings in files and load them by name, with escaping of passed commands
- [ ] View with combined output of all steps
- [ ] Install hidden dependencies together with the first emerge that needs them, without a separate step
- [ ] Show all installed applications from the world file
- [ ] Pass toolset to steps instead of reaching to the process (installer, updater...)
- [ ] Use bwrap matching the runtime environment (Flatpak or host)
- [ ] `emerge --sync` fails in Fedora when using native host install
- [ ] Flatpak on Gentoo doesn't see files created in `/var/tmp` by other processes, which breaks toolset installation
</details>

<details>
<summary>Root helper</summary>

- [ ] Display name of root functions in server calls, eg. `@root_function(name)`
- [ ] More reliable way of passing token to new server instance (initialization sometimes hangs)
- [ ] Better watchdog check (eg. ping based), the pipe can break while client still works
- [ ] Timeout for decoding, decoded messages sent as events
- [ ] Combine output handlers into one with pipe argument
</details>

<details>
<summary>Releng and Git directories</summary>

- [ ] Handle Git that isn't configured (user name, email, GitHub certificate, known hosts)
- [ ] View for commit message and changed files when committing
- [ ] Clone with `git@`, fork branches and push changes
- [ ] Check that created Git directory has the structure expected for its type (overlay, releng...)
</details>

<details>
<summary>UI</summary>

- [ ] Show/hide section help globally and per section, with links to help pages and videos
- [ ] "Continue" button in Environments when opened from Welcome
- [ ] Alternative layout with top tabs
- [ ] Welcome wizard with page dots at the bottom
</details>

---

## License

Catalyst Lab is free software, licensed under the [GNU General Public License v3.0](COPYING).

The app icon is the `computer` icon from the Adwaita icon theme (CC BY-SA 3.0, GNOME Project).
