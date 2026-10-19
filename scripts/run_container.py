"""Container entrypoint: start with authentication on unless explicitly configured."""

import os


def main() -> None:
    # Settings are built when the app is imported, so the default must be in place first.
    # Desktop installs keep their zero-config default; a container listens on 0.0.0.0.
    os.environ.setdefault("GALGAME2VOICE_AUTH_DISABLED", "0")
    from galgame2voice.main import run

    run()


if __name__ == "__main__":
    main()
