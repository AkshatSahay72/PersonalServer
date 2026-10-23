"""
Root pytest configuration and sys.path setup for PersonalServer test suite.
Ensures repository root is always present on sys.path across all test subdirectories.
"""

import sys
from pathlib import Path

# Add PersonalServer repository root to sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
