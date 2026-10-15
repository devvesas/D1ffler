# Third-party notices

D1ffler's original code is GPL-3.0-or-later; dependency licenses are listed separately below. Full, unmodified upstream texts and attribution records accompany the application in `licenses/`. `licensing/inventory.json` records dependency versions, origins, notice hashes, and source archives/checksums. The application does not purchase or rely on commercial PyQt/Qt licenses.

## Runtime components

| Component | Reviewed version | License and copyright | Origin / notice |
| --- | --- | --- | --- |
| PyQt6 bindings | 6.11.0 | GPL-3.0-only; Riverbank Computing Limited | [Riverbank licensing](https://www.riverbankcomputing.com/software/pyqt), `licenses/packages/PyQt6/LICENSE` |
| Qt libraries distributed as PyQt6-Qt6 | 6.11.0 | LGPL-3.0-only with upstream alternatives; The Qt Company Ltd. and contributors | [Qt source archive](https://download.qt.io/archive/qt/6.11/6.11.0/submodules/), `licenses/packages/PyQt6-Qt6/LICENSE` and `licenses/qtbase-6.11.0/LICENSES/` |
| PyQt6_sip runtime | 13.11.1 | BSD-2-Clause; Copyright (c) 2025 Phil Thompson | `licenses/packages/PyQt6_sip/LICENSE` |
| regex | 2026.4.4 | Apache-2.0 additions; historical CNRI/Python terms for the CPython-derived engine; Matthew Barnett and Secret Labs AB | `licenses/packages/regex/LICENSE.txt`, historical Python terms in `licenses/Python-3.10.7.txt`; package metadata records `Apache-2.0 AND CNRI-Python` |
| CPython interpreter and standard library | 3.10.7 | PSF-2.0 and historical Python licenses, plus bundled-library terms; Python Software Foundation and contributors | `licenses/Python-3.10.7.txt`; exact CPython source in the corresponding-source companion |
| libffi (used by ctypes) | 3.3.0 | MIT; Anthony Green, Red Hat, Inc. and contributors | `licenses/libffi-3.3.0/LICENSE`; source from Python's cpython-source-deps repository |
| bzip2 | 1.0.8 | bzip2 license; Julian Seward | `licenses/bzip2-1.0.8/LICENSE` and Python's bundled notices |
| xz / liblzma | 5.2.5 | Public-domain library code and upstream build/support licenses; Lasse Collin and contributors | `licenses/xz-5.2.5/COPYING` and adjacent upstream notices |
| zlib | 1.2.12 | Zlib license; Jean-loup Gailly and Mark Adler | `licenses/zlib-1.2.12/` and matching source archive |
| Expat / libmpdec | Bundled in CPython 3.10.7 | MIT / BSD-2-Clause; upstream authors | `licenses/CPython-3.10.7/Modules/expat/COPYING`, `licenses/CPython-3.10.7/Doc/license.rst`, and source headers |
| Microsoft Visual C/C++ runtime DLLs | Versions recorded per frozen build | Microsoft redistributable runtime terms; Microsoft Corporation | Supplied by the official CPython/PyQt6 Windows distributions. [Microsoft runtime terms](https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/), preserved as `licenses/Microsoft-VC-Runtime-2015-2022.docx` and an offline `.txt` rendering. These system/compiler runtime libraries are not licensed under D1ffler's GPL. |

PyQt is GPLv3 or commercially licensed, **not LGPL**. This project uses the GPL version with GPLv3-compatible application distribution. The first-party “or later” grant does not change PyQt6's GPL-3.0-only grant; use GPLv3 for the combined build with this PyQt6 release. Qt libraries use their own open-source terms. The build supplies application and library source together and documents how to rebuild with modified Qt, without signatures or keys that prevent replacement. See [SOURCE_ACCESS.md](SOURCE_ACCESS.md).

The frozen Qt profile includes Qt Core, GUI, Widgets, SVG, Windows/offscreen/minimal platform plugins, the Windows style, SVG icons, and the image plugins from Qt Base/SVG/Image Formats. Qt's embedded third-party components include compression, text/font shaping, rasterization, Unicode/locale data, image codecs, hashing, and platform helpers under their own licenses. Their exact upstream copyright/license records are preserved in `licenses/qtbase-6.11.0/**/qt_attribution.json`, `licenses/qtimageformats-6.11.0/**/qt_attribution.json`, and the adjacent copied license texts. These notice trees deliberately include an upstream superset, including platform-specific material that is not in the Windows executable; copying a notice does not imply that its code is bundled.

The frozen app excludes Qt PDF/Network, the unused TUIO plugin, the software OpenGL renderer, and Python's SSL/OpenSSL-backed hash modules. Hashing uses CPython's built-in algorithm implementations; no comparison or report requires network access. The ordinary Python interpreter used to build or run from source may contain additional libraries under the interpreter's licenses, including OpenSSL notices in its license file. Git is an optional external program, never bundled.

Microsoft's runtime terms are end-user terms, not a general grant to redistribute Microsoft's DLLs. Keep the unmodified runtime files and notices on Windows, preserve CPython's additional Windows distribution conditions in `licenses/CPython-3.10.7/PC/crtlicense.txt`, and use the applicable licensed Visual Studio redistribution rights for executable publication. Microsoft's [Visual Studio 2022 distributable-code list](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution) describes those rights. These restrictions apply to Microsoft's code; they do not change D1ffler's application license.

## Build and test tools

These tools are used unmodified to build or test D1ffler; their packages are not included wholesale in the executables. PyInstaller's bootloader/support code is bundled under its exception. Their notices are retained in `licenses/packages/` for reproducibility and review.

| Tool | Version | License |
| --- | --- | --- |
| PyInstaller | 6.1.0 | GPL-2.0-or-later with its bootloader exception, and Apache-2.0 for designated files; see `COPYING.txt` |
| pyinstaller-hooks-contrib | 2023.10 | GPL-2.0-or-later / Apache-2.0 as identified in upstream LICENSE files |
| altgraph | 0.17.4 | MIT |
| pefile | 2023.2.7 | MIT |
| pywin32-ctypes | 0.2.2 | BSD-3-Clause |
| packaging | 25.0 | Apache-2.0 OR BSD-2-Clause |
| setuptools | 80.9.0 | MIT; build-only vendored modules retain their own terms |
| pytest | 9.0.3 | MIT |
| iniconfig | 2.3.0 | MIT |
| pluggy | 1.6.0 | MIT |
| Pygments | 2.19.2 | BSD-2-Clause |
| colorama | 0.4.6 | BSD-3-Clause |
| exceptiongroup | 1.3.0 | MIT |
| tomli | 2.4.1 | MIT |

[PyInstaller's license exception](https://pyinstaller.org/en/v6.1.0/license.html) permits distribution of generated executables under the application's license while preserving dependency obligations. No proprietary-application license is assumed.

## Project artwork and data

Project artwork and sample comparison data use the application's license; [COPYRIGHT.md](COPYRIGHT.md) records the grouped artwork provenance and license grant. File-type badges are generic, not official vendor/product logos. System font names in SVGs reference locally installed fonts; fonts are not embedded.

Document externally sourced artwork separately when its license or attribution requirements differ from the project grant.
