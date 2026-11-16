"""Launch EasyTally.

    python run.py          desktop window (needs Tkinter)
    python run.py --web    opens in your browser; runs fully offline on this machine
"""
import os
import sys

if __name__ == "__main__":
    # A windowed (no-console) exe has no stdout/stderr; give them somewhere harmless to write.
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w"))
    if "--web" in sys.argv:
        from easytally.web.server import main
    else:
        from easytally.ui.app import main
    main()
