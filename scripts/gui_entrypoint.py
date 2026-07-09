"""PyInstaller entry point for the windowed (no console) GUI build. Kept
outside the package for the same reason as entrypoint.py: relative imports
don't resolve when PyInstaller's Analysis treats a script as __main__."""

from agenticiam.gui import launch_gui

if __name__ == "__main__":
    launch_gui()
