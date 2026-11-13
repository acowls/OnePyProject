"""Entry point for the packaged GUI and its noninteractive build smoke test."""
import sys
import traceback
from pathlib import Path


if len(sys.argv) == 3 and sys.argv[1] == "--packaging-smoke-test":
    report = Path(sys.argv[2])
    try:
        from onepyproject._packaging_smoke import run

        run()
    except BaseException:
        report.write_text(traceback.format_exc(), encoding="utf-8")
        sys.exit(1)
    report.write_text("Packaging smoke test passed.\n", encoding="utf-8")
    sys.exit(0)

from onepyproject.app import main

sys.exit(main())
