# Project templates

New projects can be created from a template. A template describes the stages of a project (and optionally files like
Portage configuration), and can have options, like architecture or init system, that change what is created.

Official templates live in [`data/project_templates`](../data/project_templates) in the Catalyst Lab repository. The
app loads the current template list and definitions from the `main` branch when opening the template chooser. It does
not use local copies bundled with the app. Existing projects created from these templates can receive template
changes through **Update template**. The app checks the Git tree hash of only that template's directory. Changes
elsewhere in the application repository do not make the template appear out of date. When an update is opened, the app
downloads only the selected template directory and its files.

Templates do not have a separate Git repository, and new projects begin as normal Catalyst Lab project repositories.
Projects created from external template repositories keep their existing update source. The in-app template source
applies to projects created from these templates going forward; prior template project metadata is left unchanged.

Templates can't run code. Values are generated with small expressions (see [Expressions](#expressions)) that only
allow a fixed set of operations.

## Repository layout

Each official template is stored in its own directory:

```
data/project_templates/gentoo-release/
    template.toml               Required, definition of template.
    files/                      Optional, files copied to projects (see [[files]]).
    README.md                   Optional.
data/project_templates/raspberry-pi-5/
    template.toml
    ...
```

Template directories are checked when downloaded. They may contain `template.toml`, an optional `README`, and a
`files/` directory. The app rejects symbolic links, executable or special files, more than 2000 files, files larger
than 10 MB, or more than 50 MB in total.

## template.toml

```toml
format = 1                      # Optional, version of template format.
min_app_version = "2.0.0"       # Optional, oldest version of Catalyst Lab that supports the template.
name = "Gentoo release"         # Shown in list of templates and used as default project name.
description = "Stages built like official Gentoo releases."
architecture = "arm64"          # Optional, architecture of projects for templates made for one architecture.
```

The architecture of the project is set by the template when it has `architecture`, or a variable of `architecture`
type. Otherwise the app asks for it. A fixed architecture is available in expressions as `architecture`.

### Build identities

`stage_id_suffix = "{{ init }}"` at the top level gives each stage a separate saved identity for each init system
(requires Catalyst Lab 2.0.1). For example, logical stage `stage3` is saved as `stage3:openrc` or `stage3:systemd`
in `project-template.json`. Parents, software groups, stage names and directories still use the original layout.
Repeated updates with the same suffix preserve stage UUIDs; changing the suffix creates new UUIDs, so old build
artifacts cannot be reused as seeds for the new variant. Templates without this setting keep their existing IDs.
Adding this setting to an existing template changes its stage identities once, requiring fresh builds.

### Options: `[[variables]]`

Options selected when creating a project, shown in this order. All of them are optional, a template without
variables creates always the same project.

```toml
[[variables]]
id = "init"                     # Name used in expressions.
title = "Init system"           # Shown in the app.
description = "..."             # Optional, shown under the title.
type = "choice"                 # choice (default), boolean or architecture.
default = "openrc"              # Optional, the first available option is used otherwise.
when = "architecture != 'm68k'" # Optional, the option is shown only when this is true. Otherwise its value is none.
options = [
    { value = "openrc", title = "OpenRC" },
    { value = "systemd", title = "systemd", when = "architecture in ['amd64', 'arm64']" },
    "other",                    # Short form, value is also the title.
]
```

Types:

| Type | Values |
|---|---|
| `choice` | Texts from `options`. Options can have their own `when`, to be available only for some selections. |
| `boolean` | `true` or `false`, shown as a switch. Has no `options`. |
| `multiple` | Any number of `options`, shown as a list of switches. Its value is a list of selected values, `default` is a list too. |
| `architecture` | Architecture names (`amd64`, `arm64`, `arm`, `ppc`, `ppc64`, `ppc64le`, `riscv`, `s390x`, `sparc`, `x86`...). The selected one is also the architecture of the project, so the app doesn't ask for it separately. Only one per template. |

A `when` condition can use variables defined above it. When a selected value is no longer available (because of
another selection), the default or first available option is used.

### Groups: `[[groups]]`

Configuration that can be enabled when creating a project and applied to selected stages, for example sets of
packages. Every group is shown with a switch, and lists stages it can be applied to.

```toml
groups_title = "Additional software"    # Top level, title of groups in the app.

[[groups]]
id = "kernel"
title = "Kernel and firmware"
description = "Distribution kernel with initramfs."   # Optional.
default = true                          # Optional, enabled by default.
when = "architecture in ['amd64', 'arm64']"  # Optional, the group is offered only when this is true.
stages = ["stage4", "stage4-desktop"]   # Ids of stages the group can be applied to.
default_stages = ["stage4"]             # Optional, stages selected by default (all of stages otherwise).

[groups.arguments]                      # Added to arguments of selected stages.
stage4_packages = ["sys-kernel/gentoo-kernel-bin", "sys-kernel/linux-firmware"]

[[groups.files]]                        # Copied to directories of selected stages.
source = "files/kernel"
destination = "portage"                 # Relative to the stage directory.
```

- Lists in arguments are added to lists of the stage and of other groups (without duplicates), other values replace
  them.
- A group isn't applied to a stage built from a stage that already gets it (its parent, or further ancestor): the
  stage has its packages, services and files from its seed. Such stages are shown checked and disabled.
- Arguments that the target of a stage doesn't have are skipped, so a group can set for example both
  `stage4_packages` and `livecd_packages`.
- Enabled groups are available in expressions as list `groups`, for example `'kernel' in groups`. Groups can use
  variables in `when` and expressions, `[values]` can use `groups`.

### Overlays: `[[overlays]]`

Overlays (Gentoo ebuild repositories) used by stages, for example with packages or kernels for specific hardware.

```toml
[[overlays]]
id = "ps3"                              # Used in stage arguments.
url = "https://github.com/damiandudycz/ps3-gentoo-overlay.git"
name = "ps3-gentoo-overlay"             # Optional, name of overlay in Catalyst Lab when it's added.
when = "..."                            # Optional, overlay is used only when this is true.

[[stages]]
...
[stages.arguments]
repos = [{ type = "Overlay", value = "ps3" }]
```

When the project is created, an overlay already added to Catalyst Lab from the same URL is used. Otherwise it's
cloned to Overlays. The options page lists overlays and whether they will be downloaded. Children of the stage inherit
`repos` like other arguments.

### Computed values: `[values]`

Values computed from variables, available in expressions below (and in later values), in this order. Useful when the
same value is used in many places.

```toml
[values]
spec_directory = "{{ f'{variant}/' if variant else '' }}"
stage4_services = "{{ ['sshd|default'] if init == 'openrc' else [] }}"
```

### Stages: `[[stages]]`

Stages of the project. A parent has to be defined before its children.

```toml
[[stages]]
id = "stage3"                   # Used in parent of other stages.
parent = "stage1"               # Optional, id of the stage whose build is used as seed.
name = "stage3-{{ init }}"      # Name of stage in project.
target = "stage3"               # Catalyst target: stage1, stage3, stage4, livecd_stage1, livecd_stage2...
releng_template = "{{ spec_directory }}stage3-{{ init }}-23.spec"  # Optional, see below.
when = "include_stage3"         # Optional, the stage is created only when this is true.

[stages.arguments]              # Optional values of stage arguments.
stage4_packages = ["app-admin/sudo", "net-misc/chrony"]
stage4_rcadd = "{{ stage4_services }}"
```

- `releng_template` is a path of a spec in the Releng directory selected for the project, relative to
  `releases/specs/<architecture>` (for example `releases/specs/amd64` or `releases/specs/ppc/ppc64`). The stage
  inherits values from this spec. A missing spec is reported when the project is created, but the stage is still
  created.
- `arguments` are Catalyst spec options, with `/` replaced by `_` (`stage4/packages` → `stage4_packages`). Values can
  be texts, numbers, booleans or lists of texts. Empty values (`""`, `[]` or none) are skipped. Other kinds of values
  can be given in the format of `stage.json` files of projects, for example
  `profile = { type = "StageAutomaticOption", value = "INHERIT_FROM_PARENT" }`. To find names and formats, configure
  a stage in the app and look at its `stages/<name>/stage.json`.
- Arguments that are not set get the same defaults as stages added in the app (inherited from the parent or the Releng
  spec when possible). Values set by the template are shown as **Custom value** in stage settings.
- In lists, items that are none are skipped, so expressions can leave them out.
- Some values can join inherited values with own ones: put `"@releng"` (Releng spec) and/or `"@parent"` (parent
  stage) in a list with own entries, for example `livecd_packages = ["@releng", "net-misc/networkmanager"]` or
  `common_flags = ["@parent", "-flto"]`. Lists (packages, USE flags, services `rcadd`/`rcdel`, `unmerge`, `empty`,
  `rm`, `modblacklist`, `users`, `groups`, `repos`, `catalyst_use`, `hostuse`, `install_mask`) are joined without
  duplicates, texts (compiler flags, `gk_mainargs`, `livecd_bootargs`) are joined with spaces. Inherited entries can't
  be removed this way; to remove some, give the whole value without markers. Groups adding entries to such list keep
  the inherited entries too. Needs Catalyst Lab 0.3.1 or newer (`min_app_version = "0.3.1"`).
- Values that are paths to files of the project (for example fsscript or kernel configuration added with
  `[[files]]`) should use placeholders, replaced when building: `@STAGE_DIR@` (directory of the stage),
  `@PROJECT_DIR@` (directory of the project), and like in Releng specs `@REPO_DIR@` (Releng directory). For example
  `stage4_fsscript = "@STAGE_DIR@/fsscript.sh"`. Projects are moved and cloned, so they shouldn't contain
  absolute paths.

#### Kernels

Stages with kernels (`livecd_stage2`, `stage4`...) build kernels listed in `boot_kernel`. Settings of each kernel
(`boot/kernel/<name>/...` in Catalyst specs) are given in `boot_kernels`, a table of kernel names. Settings that are not
given come from the Releng spec of the stage. For example a kernel from an overlay:

```toml
[stages.arguments]
boot_kernel = ["ps3"]

[stages.arguments.boot_kernels.ps3]
sources = "sys-kernel/gentoo-kernel-ps3"
distkernel = true
dracut_args = "--xz --no-hostonly -a dmsquash-live -o btrfs"
extraversion = "ps3"
```

Use `boot_kernel = ["@releng"]` to use the Releng kernel list as-is. A template may optionally add its own kernels
alongside that list with `boot_kernel = ["@releng", "custom-name"]`; custom names need corresponding entries in
`boot_kernels`. Platform templates that replace the Releng kernel list should specify only their custom names.

Settings: `sources`, `distkernel`, `config`, `dracut_args`, `extraversion`, `packages`, `use`, `gk_kernargs`,
`gk_action`, `aliases`, `console`, `initramfs_overlay`, `softlevel`. `packages`, `use`, `aliases` and `console` are
lists, `distkernel` is a boolean (`true` writes `yes`, `false` leaves it out, as Catalyst only checks if it's set), others are
texts. Groups can add settings of kernels too
(`[groups.arguments.boot_kernels.<name>]`), they are combined with settings of the stage.
Kernel settings need Catalyst Lab 0.3.0 or newer (`min_app_version = "0.3.0"`).

### Files: `[[files]]`

Files copied to the project, for example Portage configuration or root overlays of stages.

```toml
[[files]]
source = "files/stage4"                 # File or directory in template.
destination = "stages/stage4-{{ init }}" # Path in project.
when = "init == 'openrc'"               # Optional.
```

- Paths can't lead outside of the template or the project.
- Files ending with `.template` are copied without this suffix, with `{{ expressions }}` in their contents replaced.
  Other files are copied without changes.
- Stage directories are `stages/<stage name>`. Their `portage` directory is Portage configuration of the stage,
  `root_overlay` its root overlay (files added to the root of stage4 or LiveCD), and `overlay` the ISO overlay of
  livecd-stage2 (files added to the ISO next to the live system). When a template adds files there, the stage uses
  them: **Stage overlay** is added to its Portage confdir, root overlay or ISO overlay sources (after Releng and
  parent sources). Groups are an easy way to add them only to selected stages.

## Updating projects

Projects remember the template they were created from: `project-template.json` in the project has its repository,
commit, selected options and stages created from template stages. Commits that generate the project ("Create project
from template", "Update from template") change this file. The project page shows the template, own commits of the
project (not in the template), not saved changes, and **Update template** button. It's blue when the template has
changes: when the project is opened, only listing of files of the latest template is downloaded to check it.

Changes of the project have to be saved before updating. Update starts with options of the latest version of the
template, with values selected for the project (options that aren't available anymore get defaults), so they can be
changed too. Update generates the project again from the latest version of
the template, with the same options, as a new commit on top of the last generation. Then it compares three versions:
the last generation, the project, and the new generation. Settings of stages are compared one by one (stages are
matched by ids, so renamed stages are compared too), other files as whole:

- changed only in the template: applied,
- changed only in the project: kept,
- changed in both differently: you choose to keep the project version or use the template version,
- stage removed by the template (eg. option was changed), but changed in the project (its settings, or files added
  to it): you choose to keep the whole stage, or remove it.

Own commits of the project are then replayed on top of the new generation, with these decisions, so the history of the
project has its own commits on top of the latest template. Commits keep their messages and authors. The template
repository isn't a remote of the project, projects get changes of templates only this way.

Projects cloned from Git repositories are updated the same way (**Update from repository**): the remote branch is
compared with the project since the last shared commit, and own commits are replayed on top of the remote branch, so
the history of the project stays the history of its repository with own commits on top. Projects without own commits
are moved to the latest version. As commits are replayed (rebased), commits already pushed elsewhere get new ids.

## Expressions

Texts in `template.toml` can contain expressions in `{{ }}`. A text that is a single expression gets its value (which
can be a list or boolean), otherwise values are inserted as text. `when` conditions are expressions without `{{ }}`.

Expressions can use:

- names of variables, computed values, `groups` and `project_name`,
- texts, numbers, `True`, `False`, `None` and lists,
- `and`, `or`, `not`, comparisons (`==`, `!=`, `<`, `<=`, `>`, `>=`), `in`, `not in`,
- `+` (texts, lists, numbers) and `-` (numbers),
- conditional values: `'a' if condition else 'b'`,
- f-strings: `f'{variant}/'`,
- functions `lower(text)`, `upper(text)`, `replace(text, old, new)`, `join(separator, list)`.

Everything else (calling other functions, attributes, indexing, loops, comprehensions, lambdas, imports...) is
rejected. Expressions are limited in length, complexity and size of their results.

## Testing a template

Put it in `data/project_templates/<template-id>/` and run Catalyst Lab from the checkout. Select **From template**;
errors in `template.toml` are shown in the list or options page. Check that each combination of options creates the
stages you expect. The options page lists the generated stages as you change options.

## Adding a template to Catalyst Lab

Templates are added to Catalyst Lab with a pull request:

1. Add `data/project_templates/<template-id>/` following the [layout](#repository-layout).
2. Open a pull request describing the template, its supported architectures, and the Releng specs it was checked with.

Use `min_app_version` when a template needs features of a newer Catalyst Lab. The app checks updates by comparing
the Git tree hash of each template directory with the matching path on `main`, so unrelated code commits do not
trigger template updates. The check downloads directory metadata only; template files are fetched only when the user
opens an update.
