# -*- mode: python ; coding: utf-8 -*-
# Single-file MediaRPC exe. Run from the repo root:
#   py -m PyInstaller --noconfirm mediarpc/MediaRPC.spec
# MEDIARPC_EXE_NAME overrides the output name (CI builds MediaRPC-<version>).
import os
import re

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
PKG = os.path.join(ROOT, "mediarpc")
with open(os.path.join(PKG, "version.py"), encoding="utf-8") as f:
    VERSION = re.search(r'VERSION = "([\d.]+)"', f.read()).group(1)
NAME = os.environ.get("MEDIARPC_EXE_NAME", "MediaRPC")

# Windows file properties (Details tab), generated from version.py.
nums = tuple(int(n) for n in VERSION.split(".")) + (0,)
version_file = os.path.join(SPECPATH, "build", "version_info.txt")
os.makedirs(os.path.dirname(version_file), exist_ok=True)
with open(version_file, "w", encoding="utf-8") as f:
    f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Kjerne'),
      StringStruct('FileDescription', 'MediaRPC - Discord Rich Presence for your media'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'MediaRPC'),
      StringStruct('OriginalFilename', 'MediaRPC.exe'),
      StringStruct('ProductName', 'MediaRPC'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")

a = Analysis(
    [os.path.join(PKG, "run_mediarpc.py")],
    pathex=[ROOT],
    datas=[(os.path.join(PKG, "Images", "MediaRPC_Active.ico"), "Images"),
           (os.path.join(PKG, "Images", "MediaRPC_Inactive.ico"), "Images")],
    excludes=["unittest", "pydoc"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    icon=os.path.join(PKG, "Images", "MediaRPC_Active.ico"),
    version=version_file,
    console=False,
    disable_windowed_traceback=False,
    upx=False,
    runtime_tmpdir=None,
)
