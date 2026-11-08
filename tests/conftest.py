import os
import sys
import tempfile
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="evetracker-test-")
os.environ["DISABLE_WORKER"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
