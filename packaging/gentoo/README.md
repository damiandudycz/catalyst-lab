# Gentoo overlay

Ebuilds of Catalyst Lab (`dev-util/catalystlab`):

- `catalystlab-9999`: live ebuild, builds the newest commit of the `main` branch.
- `catalystlab-<version>`: release, downloads tag `v<version>` from GitHub.

## Using the overlay

The overlay is a directory of this repository. Clone it and add that directory as a repository, for example
`/etc/portage/repos.conf/catalystlab.conf`:

```ini
[catalystlab]
location = /var/db/repos/catalyst-lab-2/packaging/gentoo
auto-sync = no
```

```bash
git clone https://github.com/damiandudycz/catalyst-lab-2.git /var/db/repos/catalyst-lab-2
echo "=dev-util/catalystlab-9999 **" >> /etc/portage/package.accept_keywords/catalystlab   # live ebuild
echo "dev-util/catalystlab ~amd64" >> /etc/portage/package.accept_keywords/catalystlab     # releases (or ~arm64)
emerge --ask dev-util/catalystlab
```

Updating the overlay is `git -C /var/db/repos/catalyst-lab-2 pull`.

## USE flags

- `deploy` (enabled by default): tools for deploying builds to disks, disk images and live CD images (OpenSSH,
  dosfstools, e2fsprogs, mtools, libisoburn, xz-utils).

## Updating

New release: copy the newest versioned ebuild to `catalystlab-<version>.ebuild` and push tag `v<version>`. Manifest
of the release (`ebuild catalystlab-<version>.ebuild manifest`) is generated after the tag exists.
