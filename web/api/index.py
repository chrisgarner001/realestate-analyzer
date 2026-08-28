import sys
import os
from pathlib import Path

# Add the parent directory (web/) to Python path so server.py can import database, auth, etc.
sys.path.insert(0, str(Path(__file__).parent.parent))

from server import app
