# Third-party licenses

ProMLens itself is released under the MIT License (see [LICENSE](LICENSE)).
The assets bundled in this repository keep their own licenses, listed below.

| Component | Version | License | Bundled license text |
|---|---|---|---|
| [vis-network](https://visjs.github.io/vis-network/) | 9.1.9 | Apache-2.0 OR MIT (dual) | [src/static/js/LICENSE-vis-network.txt](src/static/js/LICENSE-vis-network.txt) |
| [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) | -- | SIL OFL 1.1 | [src/static/fonts/LICENSE-JetBrainsMono.txt](src/static/fonts/LICENSE-JetBrainsMono.txt) |
| [Syne](https://gitlab.com/bonjour-monde/fonderie/syne-typeface) | -- | SIL OFL 1.1 | [src/static/fonts/LICENSE-Syne.txt](src/static/fonts/LICENSE-Syne.txt) |

## Runtime dependencies

The Python dependencies are not vendored. They are installed from Debian
packages (see `Containerfile` and `debian/control`) and each keeps its own
upstream license:

`python3-aiohttp`, `python3-fastapi`, `python3-uvicorn`, `python3-yaml`,
`python3-pydantic`, `python3-uvloop`, `python3-websockets`, `python3-httptools`,
`python3-passlib`, `python3-bcrypt`, `python3-itsdangerous`.
