# Gentoo overlay

Ebuilds of Catalyst Lab (`dev-util/catalyst-lab`):

- `catalyst-lab-9999`: live ebuild, builds the newest commit of the `main` branch.
- `catalyst-lab-<version>`: release, downloads tag `v<version>` from GitHub.

## Using the overlay

The overlay is a directory of this repository. Clone it and add that directory as a repository, for example
`/etc/portage/repos.conf/catalyst-lab.conf`:

```ini
[catalyst-lab]
location = /var/db/repos/catalyst-lab/packaging/gentoo
auto-sync = no
```

```bash
git clone https://github.com/damiandudycz/catalyst-lab.git /var/db/repos/catalyst-lab
echo "=dev-util/catalyst-lab-9999 **" >> /etc/portage/package.accept_keywords/catalyst-lab   # live ebuild
echo "dev-util/catalyst-lab ~amd64" >> /etc/portage/package.accept_keywords/catalyst-lab     # releases (or ~arm64)
emerge --ask dev-util/catalyst-lab
```

Updating the overlay is `git -C /var/db/repos/catalyst-lab pull`.

## USE flags

- `deploy` (enabled by default): tools for deploying builds to disks, disk images and live CD images (OpenSSH,
  dosfstools, e2fsprogs, mtools, libisoburn, xz-utils).
- `lima` (disabled by default): builds Lima from source into a private Catalyst Lab directory and installs QEMU and
  OpenSSH for Linux VM runners.

Enable it for Catalyst Lab with a package.use entry such as `dev-util/catalyst-lab lima`.

## Updating

New release: copy the newest versioned ebuild to `catalyst-lab-<version>.ebuild` and push tag
`catalyst-lab-v<version>`. The overlay directory is excluded from release source archives so its Manifest can hash
the archive without creating a checksum cycle.
