# Source access and rebuilding

D1ffler is distributed as source code and build scripts only. The public repository contains the application, artwork, tests, sample data, dependency locks, notices, and build tools under GPL-3.0-or-later. Keep `LICENSE`, `COPYRIGHT.md`, and third-party notices with source copies.

## Run or build

Follow [README.md](README.md) to run the Python application or build Windows x64 executables. Building, testing, signing, and any executable distribution are the user's responsibility. The supplied executable build profile requires 64-bit Python 3.10.7; no signing key or published Git repository is needed. Exported snapshots can be built without Git metadata, and the build record identifies that absence.

The builder installs the exact dependency locks, verifies the reviewed license inventory, downloads missing upstream source archives from pinned origins, and checks their SHA-256 hashes. Its outputs include a portable executable ZIP and a matching `D1ffler-<version>-corresponding-source.zip`. The latter contains the exact application inputs under `D1ffler-source/` and dependency archives under `third-party-source/`. Dependency versions, origins, and hashes are recorded in `licensing/inventory.json`.

For an offline build, copy the matching dependency archives into `build/license-sources/` and provide a wheel directory using `PIP_NO_INDEX=1` and `PIP_FIND_LINKS`. The archive filenames and hashes must match the inventory.

## If you redistribute executables

Retain the application's license and third-party notices and provide the matching application and dependency sources with equivalent access. The build creates the source companion and `SOURCE_ARCHIVE.txt` to identify it. Signing and repackaging require updating hashes for the changed executable payload; retain a source companion matching the actual code and libraries used. Review the applicable terms in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and `licenses/` for your distribution.

## Modify or replace Qt and native libraries

Dependency source archives are fetched by the builder; they are not stored in the source repository. The inventory includes PyQt6, its SIP runtime, regex, PyInstaller, Qt Base/SVG/Image Formats, CPython, libffi, bzip2, xz/liblzma, and zlib. Microsoft runtime components retain their own recorded terms.

To replace Qt, build the relevant modules using the instructions in their upstream archives, build PyQt6 against those libraries, and install the replacements into your build environment before freezing D1ffler. Alternatively, replace matching Qt DLLs/plugins in the environment's `PyQt6/Qt6/` directory before freezing. Rebuild the one-file executables to include your replacement libraries. The supplied scripts do not require a signature, installation key, or hardware authorization to run modified builds.

The supplied dependency profile uses Qt's raster widgets and image interfaces and excludes unused PDF, network, and software OpenGL libraries. It uses CPython's built-in hashing implementations. Dependency modifications require updating the build profile, native-library audit, notices, and corresponding sources to match your changes.

## Update the dependency inventory

When changing a dependency or interpreter, update the locks, build profile, and upstream notices together. Install the exact `requirements/test.lock.txt` environment, then run `python scripts/update_licenses.py` to refresh supported inventory inputs. The updater currently targets Windows Python 3.10.7; changing the interpreter also requires adapting that profile and its native-library audit. Review generated versions, notice hashes, source origins, and source hashes before committing. The builder rejects dependency drift, missing or changed notices, corrupt source archives, and unreviewed native libraries.

Project artwork provenance and its license grant are recorded in [COPYRIGHT.md](COPYRIGHT.md). Ordinary artwork edits do not require an inventory refresh; document externally sourced artwork when its terms or attribution differ.
