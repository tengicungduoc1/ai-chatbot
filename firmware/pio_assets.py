"""Run upstream custom CMake generators before SCons compiles consumers."""
import os
from pathlib import Path
import subprocess

Import("env")
# Component Manager may restore pristine dependencies during CMake configure.
# Reapply the verified overlays before assets and component sources are compiled.
import prepare
prepare.prepare()
platform = env.PioPlatform()
cmake = Path(platform.get_package_dir("tool-cmake")) / "bin/cmake"
# PlatformIO imports compile/link commands, not arbitrary upstream custom targets.
# Keep language/font/emoji generation in upstream's native CMake implementation.
build_env = dict(os.environ)
build_env["PATH"] = str(Path(env.subst("$ESPIDF_PYTHONEXE")).parent) + os.pathsep + build_env["PATH"]
subprocess.run([str(cmake), "--build", env.subst("$BUILD_DIR"), "--target",
                "lang_header", "generated_default_assets"], env=build_env, check=True)
# SCons resolves source nodes before any buildprog pre-action. Generate embedded
# assembly HERE, at post-script load time, using the actual ESP-IDF embed script.
# Read exact inputs/types from the generated CMake graph: Ogg, Wi-Fi HTML, etc.
# Never execute arbitrary COMMAND lines; invoke only ESP-IDF's known embed script.
import re
build = Path(env.subst("$BUILD_DIR"))
embed = Path(platform.get_package_dir("framework-espidf")) / "tools/cmake/scripts/data_file_embed_asm.cmake"
for line in (build / "build.ninja").read_text(encoding="utf-8").splitlines():
    if 'COMMAND =' not in line or 'data_file_embed_asm.cmake' not in line:
        continue
    match = re.search(r'-D\s*DATA_FILE=(.*?)\s+-D\s*SOURCE_FILE=(.*?)\s+-D\s*FILE_TYPE=(BINARY|TEXT)', line)
    if not match:
        raise RuntimeError('Unsupported ESP-IDF embedded-file command')
    data, output = (Path(value.replace('$ ', ' ').strip('"')) for value in match.groups()[:2])
    kind = match.group(3)
    if not output.resolve().is_relative_to(build.resolve()):
        raise RuntimeError('Embedded output escapes build directory')
    # The certificate bundle has its own PlatformIO generator and may already exist.
    if output.exists() and (not data.exists() or output.stat().st_mtime >= data.stat().st_mtime):
        continue
    if not data.is_file():
        raise RuntimeError(f'Missing embedded input: {data}')
    subprocess.run([str(cmake), f"-DDATA_FILE={data.as_posix()}",
                    f"-DSOURCE_FILE={output.as_posix()}", f"-DFILE_TYPE={kind}",
                    "-P", str(embed)], env=build_env, check=True)
assets = str(build / "generated_assets.bin")
env.Append(FLASH_EXTRA_IMAGES=[("0x800000", assets)])
if env.subst("$UPLOAD_PROTOCOL") == "esptool":
    env.Append(UPLOADERFLAGS=["0x800000", assets])
