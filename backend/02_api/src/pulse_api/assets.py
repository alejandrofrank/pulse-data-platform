"""Frontend sources live outside Python packages and are copied into the runtime image."""

import os
from pathlib import Path

FRONTEND = Path(os.environ.get("PULSE_FRONTEND_DIR", "frontend")).resolve()
DASHBOARD_STATIC = FRONTEND / "01_dashboard" / "public"
LAB_STATIC = FRONTEND / "02_lab" / "public"
