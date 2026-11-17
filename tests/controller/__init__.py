"""Synthetic controller regression tests. Never use an operator's workflow state."""
from pathlib import Path
import os
import sys
SOURCE = Path(__file__).resolve().parents[2] / 'workflow'
os.environ['AUTOWORKFLOW_WORKIQ'] = str(SOURCE / 'bin/teams-reader')
os.environ['AUTOWORKFLOW_TEAMS_READER'] = str(SOURCE / 'bin/teams-reader')
sys.path.insert(0, str(SOURCE))
