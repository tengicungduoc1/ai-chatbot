"""Prepare and invoke the real PlatformIO ESP-IDF build; never synthesize binaries."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--platformio", help="Explicit PlatformIO executable")
    args = parser.parse_args()
    try:
        if args.prepare_only:
            prepare.prepare()
            print("Prepared upstream sources; not compiled. No firmware binary produced.")
            return 0
        # Respect an intentionally empty PATH (CI's missing-toolchain regression).
        pio = args.platformio or shutil.which("platformio") or shutil.which("pio")
        if not pio and os.environ.get("PATH"):
            candidate = Path.home() / ".platformio/penv" / ("Scripts/platformio.exe" if os.name == "nt" else "bin/platformio")
            if candidate.is_file():
                pio = str(candidate)
        if not pio:
            raise RuntimeError("PlatformIO ESP-IDF toolchain unavailable. Install PlatformIO or use --platformio PATH; no firmware produced.")
        result = subprocess.run([pio, "run", "-d", str(prepare.ROOT), "-e", "ai-chatbot-s3"])
        if result.returncode:
            return result.returncode
        binary = prepare.ROOT / ".pio/build/ai-chatbot-s3/firmware.bin"
        if not binary.is_file() or binary.stat().st_size == 0:
            raise RuntimeError("PlatformIO returned success without firmware.bin; build not verified")
        print(f"Built {binary} ({binary.stat().st_size} bytes). Hardware not tested.")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"build.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
