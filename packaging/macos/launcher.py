"""Entry point of Catalyst Lab in macOS application bundle. Points GTK and the app to files inside the bundle and
then starts the app like src/catalystlab.in does."""
import os
import sys
import signal
import gettext

BUNDLE_DIRECTORY = sys._MEIPASS # Contents/Frameworks, data files are linked there from Contents/Resources.
SHARE_DIRECTORY = os.path.join(BUNDLE_DIRECTORY, "share")
RESOURCES_DIRECTORY = os.path.normpath(os.path.join(BUNDLE_DIRECTORY, "..", "Resources"))
pkgdatadir = os.path.join(SHARE_DIRECTORY, "catalystlab")
with open(os.path.join(pkgdatadir, "VERSION"), encoding="utf-8") as file: # Written when building bundle.
    VERSION = file.read().strip()
localedir = os.path.join(SHARE_DIRECTORY, "locale")

# Icons (app, Adwaita, hicolor) and settings schemas of the bundle.
os.environ["XDG_DATA_DIRS"] = os.pathsep.join(filter(None, [SHARE_DIRECTORY, os.environ.get("XDG_DATA_DIRS")]))
os.environ["GSETTINGS_SCHEMA_DIR"] = os.path.join(SHARE_DIRECTORY, "catalystlab-schemas")
# Bundled tools: squashfs tools and Lima (virtual machines). Apps started from Finder don't get PATH of shell.
os.environ["PATH"] = os.pathsep.join([
    os.path.join(BUNDLE_DIRECTORY, "bin"),
    os.path.join(RESOURCES_DIRECTORY, "lima", "bin"),
    os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin",
])

signal.signal(signal.SIGINT, signal.SIG_DFL)
gettext.install("catalystlab", localedir)

if __name__ == "__main__":
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gio
    Gio.Resource.load(os.path.join(pkgdatadir, "catalystlab.gresource"))._register()

    from catalystlab import main
    sys.exit(main.main(VERSION))
