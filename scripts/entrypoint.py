"""PyInstaller entry point. Kept outside the package because relative
imports don't resolve when PyInstaller's Analysis treats a script as
__main__; this thin wrapper just calls into the real CLI."""

from agenticiam.cli import main

if __name__ == "__main__":
    main()
