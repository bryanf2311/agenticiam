"""PyInstaller entry point for the windowed (no console) GUI build. Kept
outside the package for the same reason as entrypoint.py: relative imports
don't resolve when PyInstaller's Analysis treats a script as __main__."""

from agenticiam.gui import main

if __name__ == "__main__":
    main()
