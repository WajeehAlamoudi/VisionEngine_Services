"""Stable entry point for the camera service."""

from .runtime.service import main

if __name__ == "__main__":
    raise SystemExit(main())
