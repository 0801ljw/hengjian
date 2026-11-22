import pathlib
import sys

base = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(base))
sys.path.insert(0, str(base / "scripts"))
from fusion.server import main

raise SystemExit(main())
