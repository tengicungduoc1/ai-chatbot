"""Select the real ESP-IDF project and pre-generate embedded audio assets."""
import os
from pathlib import Path
import subprocess
import sys

Import("env")
root = Path(env.subst("$PROJECT_DIR"))
sys.path.insert(0, str(root))
import prepare
from build_guard import acquire

try:
    import atexit
    _build_lock = acquire(root / 'generated/build.lock')
    atexit.register(_build_lock.close)
except RuntimeError as exc:
    print(str(exc), file=sys.stderr)
    env.Exit(2)

try:
    prepare.prepare(os.getenv("CHATBOT_SERVER_URL"))
    if not (prepare.UPSTREAM / "main/chatbot_local_config.h").is_file():
        raise RuntimeError("Missing generated firmware configuration; run python prepare.py.")
except (OSError, RuntimeError, ValueError) as exc:
    print(f"Firmware preparation failed: {exc}", file=sys.stderr)
    env.Exit(2)


# Embedded audio is generated in pio_assets.py before SCons resolves sources.
env.Replace(PROJECT_DIR=str(prepare.UPSTREAM), PROJECT_SRC_DIR=str(prepare.UPSTREAM / "main"))
board = env.BoardConfig()
board.update("build.partitions", str(prepare.UPSTREAM / "partitions/v2/16m.csv"))
board.update("build.esp-idf.sdkconfig_path", str(prepare.UPSTREAM / "sdkconfig.platformio"))
defaults = f"{prepare.UPSTREAM.as_posix()}/sdkconfig.defaults;{root.as_posix()}/sdkconfig.defaults"
board.update("build.cmake_extra_args", f'"-DSDKCONFIG_DEFAULTS={defaults}"')
