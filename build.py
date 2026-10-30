"""Run on each target OS: python build.py. Runtime is completely offline."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser()
parser.add_argument("--dist", default=str(root / "dist"))
parser.add_argument("--work", default=str(root / "build"))
parser.add_argument("--name", default="PixSort")
args = parser.parse_args()
command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
           "--name", args.name, "--distpath", args.dist, "--workpath", args.work,
           "--specpath", args.work, "--add-data", f"{root / 'assets'}{os.pathsep}assets",
           "--paths", str(root)]
if sys.platform == "win32":
    command += ["--icon", str(root / "assets/koma.ico")]
elif sys.platform == "darwin":
    command += ["--osx-bundle-identifier", "local.pixsort.photos"]
for excluded in ("numpy", "pandas", "matplotlib", "scipy", "tkinter", "IPython", "send2trash"):
    command += ["--exclude-module", excluded]
command.append(str(root / "app.py"))
environment = os.environ.copy()
if sys.platform == "win32":
    # Prevent unrelated tools on PATH (e.g. Poppler) from supplying an incompatible
    # icuuc.dll in place of Windows' own ICU API. Qt locates its own DLLs itself.
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    environment["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), str(windows / "System32"), str(windows)])
subprocess.run(command, check=True, cwd=root, env=environment)
print("Built in", Path(args.dist).resolve())
