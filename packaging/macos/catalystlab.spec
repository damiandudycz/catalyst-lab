# PyInstaller spec of Catalyst Lab macOS application. Used by build-app.sh, which prepares paths below.
import os

STAGING = os.environ["CATALYSTLAB_STAGING"]   # Prefix where app was installed with meson.
SCHEMAS = os.environ["CATALYSTLAB_SCHEMAS"]   # Compiled settings schemas (GTK and app).
ICON = os.environ["CATALYSTLAB_ICON"]         # .icns file.
VERSION = os.environ["CATALYSTLAB_VERSION"]
TOOLS = os.environ["CATALYSTLAB_TOOLS"].split(os.pathsep) # Command line tools used by app (squashfs).
PKGDATADIR = os.path.join(STAGING, "share", "catalystlab")
HERE = os.path.dirname(os.path.abspath(SPEC))

# App modules are imported by scanning the package, so all of them are listed.
package_directory = os.path.join(PKGDATADIR, "catalystlab")
app_modules = ["catalystlab"] + sorted(
    f"catalystlab.{name[:-3]}" for name in os.listdir(package_directory) if name.endswith(".py") and name != "__init__.py"
)

a = Analysis(
    [os.path.join(HERE, "launcher.py")],
    pathex=[PKGDATADIR],
    binaries=[(tool, "bin") for tool in TOOLS],
    datas=[
        (os.path.join(PKGDATADIR, "catalystlab.gresource"), "share/catalystlab"),
        (os.path.join(PKGDATADIR, "VERSION"), "share/catalystlab"),
        (os.path.join(STAGING, "share", "icons", "hicolor"), "share/icons/hicolor"),
        (SCHEMAS, "share/catalystlab-schemas"),
    ],
    hiddenimports=app_modules + [
        "gi.repository.Gtk", "gi.repository.Gdk", "gi.repository.Adw", "gi.repository.Gio", "gi.repository.GLib",
        "gi.repository.GObject", "gi.repository.Pango", "gi.repository.GdkPixbuf", "gi.repository.cairo", "cairo",
        "requests",
    ],
    hooksconfig={
        "gi": {
            "icons": ["Adwaita"],
            "themes": [],
            "languages": [],
            "module-versions": {"Gtk": "4.0", "Gdk": "4.0"},
        },
    },
    excludes=["tkinter"],
    # Sources are kept next to compiled modules: app sends source of some functions to virtual machines and root
    # helper (inspect.getsource).
    module_collection_mode={"catalystlab": "pyz+py"},
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="catalystlab",
    console=False,
    argv_emulation=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="catalystlab")
app = BUNDLE(
    coll,
    name="Catalyst Lab.app",
    icon=ICON,
    bundle_identifier="com.damiandudycz.CatalystLab",
    version=VERSION,
    info_plist={
        "CFBundleName": "Catalyst Lab",
        "CFBundleDisplayName": "Catalyst Lab",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "13.0", # Virtualization.framework features used by Lima.
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.developer-tools",
    },
)
