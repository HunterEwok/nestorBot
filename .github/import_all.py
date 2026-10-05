"""CI, the build job: import every module of nestor but the __main__ ones (they would run) and those under the
prefixes given, e.g. nestor.importer, which needs Telethon (the import extra, the laptop's, not the Pi's)."""
import importlib
import pkgutil
import sys

import nestor

skip = tuple(sys.argv[1:])
names = [m.name for m in pkgutil.walk_packages(nestor.__path__, "nestor.")
         if not m.name.endswith("__main__") and not m.name.startswith(skip)]
for name in names:
    importlib.import_module(name)
print(f"{len(names)} modules import" + (f", all but {', '.join(skip)}" if skip else ""))
