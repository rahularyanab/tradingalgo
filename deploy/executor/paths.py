"""Where the executor keeps its files (the Angel adapter asks for this). Override with EXECUTOR_HOME."""
import os
from pathlib import Path

HOME = Path(os.environ.get("EXECUTOR_HOME", "/opt/executor"))
