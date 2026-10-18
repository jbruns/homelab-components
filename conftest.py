import sys
from pathlib import Path

# Each Component's package lives under components/<name>/src; package names are unique across Components.
for src in sorted((Path(__file__).parent / "components").glob("*/src")):
    sys.path.insert(0, str(src))

pytest_plugins = ["tools.contract"]
