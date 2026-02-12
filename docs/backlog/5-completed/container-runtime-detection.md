# Container Runtime Detection

Make container runtime detection robust for Linux vs macOS.

## Done

`src/pynchy/runtime.py` — frozen `ContainerRuntime` dataclass with lazy singleton. Detects via `CONTAINER_RUNTIME` env var, then platform, then `shutil.which()`. Apple Container uses `system status`/`start` + array JSON listing. Docker uses `docker info` + newline-delimited JSON. `container_runner.py` and `app.py` delegate to runtime. `build.sh` has matching shell detection.
