"""PyInstaller entry point. Keeps the bundled frontend free of GPU dependencies."""

from voice_app.client_main import main


if __name__ == "__main__":
    raise SystemExit(main())
