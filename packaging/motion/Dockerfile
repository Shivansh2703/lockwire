# The base is the official ROS image digest approved for E5.9a.
FROM ros:jazzy-ros-core@sha256:17142ce4cb561d70d61f4fb485177caf0d6cceca4ccc418704168ff45f3713e5

# The direct stable-Noble arm64 pins are recorded in packages.lock.  The full
# dpkg closure remains deliberately unresolved until the sanctioned build
# records it; this guard prevents an accidentally floating install.
COPY packaging/motion/packages.lock /opt/lockwire/packages.lock
ARG LOCKWIRE_E5_9_CAPTURE_BUILD=0
ENV LOCKWIRE_E5_9_DIRECT_SELECTORS="ros-jazzy-gz-ros2-control=1.2.20-1noble.20260904.032933 ros-jazzy-joint-trajectory-controller=4.42.1-1noble.20260904.012550 ros-jazzy-ros-gz-sim=1.0.24-1noble.20260904.032840 ros-jazzy-gz-sim-vendor=0.0.13-1noble.20260904.022149"
RUN test "$LOCKWIRE_E5_9_CAPTURE_BUILD" = 1 \
 || grep -qx 'resolution_status=resolved-by-authorized-build' /opt/lockwire/packages.lock \
 || (echo 'E5.9a package closure is not yet authorized' >&2; exit 78)

# Only the explicitly authorized capture build may resolve the still-unlocked
# transitive closure. These deliberately separate layers are capture-only. Their
# BEGIN/DONE markers identify update, download, install and receipt work in a
# retained --progress=plain log. Every direct package version remains explicit.
RUN printf '%s\n' 'E5_9_UPDATE_BEGIN' >&2 \
 && apt-get update \
 && printf '%s\n' 'E5_9_UPDATE_DONE' >&2

RUN mkdir -p /opt/lockwire/archives/partial /opt/lockwire/receipt \
 && printf '%s\n' 'E5_9_DOWNLOAD_BEGIN' >&2 \
 && DEBIAN_FRONTEND=noninteractive apt-get \
    -o Dpkg::Use-Pty=0 \
    -o Dir::Cache::archives=/opt/lockwire/archives \
    -o APT::Keep-Downloaded-Packages=true \
    install --download-only --no-install-recommends -y \
    $LOCKWIRE_E5_9_DIRECT_SELECTORS \
 && deb_count="$(find /opt/lockwire/archives -maxdepth 1 -type f -name '*.deb' | wc -l)" \
 && test "$deb_count" -gt 0 \
 && find /opt/lockwire/archives -maxdepth 1 -type f -name '*.deb' -print0 \
      > /opt/lockwire/receipt/deb-files.nul \
 && LC_ALL=C sort -z -o /opt/lockwire/receipt/deb-files.nul \
      /opt/lockwire/receipt/deb-files.nul \
 && xargs -0 sha256sum < /opt/lockwire/receipt/deb-files.nul \
      > /opt/lockwire/receipt/deb-sha256.txt \
 && rm /opt/lockwire/receipt/deb-files.nul \
 && deb_bytes="$(find /opt/lockwire/archives -maxdepth 1 -type f -name '*.deb' -exec stat -c '%s' {} + \
      | awk '{ total += $1 } END { print total + 0 }')" \
 && printf 'E5_9_DOWNLOAD_DONE count=%s bytes=%s\n' "$deb_count" "$deb_bytes" >&2

# Bind the four requested direct artifacts to their lock records before any
# package is installed. Control metadata explicitly scopes all other archives
# to the transitive closure.
RUN LOCKWIRE_E5_9_LOCK_PATH=/opt/lockwire/packages.lock \
    LOCKWIRE_E5_9_ARCHIVES=/opt/lockwire/archives \
    python3 - <<'PY'
# E5_9_DIRECT_VERIFY_PY_BEGIN
import hashlib
import os
import subprocess
from pathlib import Path


def fail(message):
    raise SystemExit(f"E5.9 direct artifact verification failed: {message}")


lock_path = Path(os.environ["LOCKWIRE_E5_9_LOCK_PATH"])
archives = Path(os.environ["LOCKWIRE_E5_9_ARCHIVES"])
expected = {}
for line_number, raw_line in enumerate(lock_path.read_text(encoding="utf-8").splitlines(), 1):
    line = raw_line.strip()
    if not line or line.startswith("#") or line.startswith("resolution_status="):
        continue
    fields = line.split()
    try:
        package, version = fields[0].split("=", 1)
        pairs = [field.split("=", 1) for field in fields[1:]]
        attributes = dict(pairs)
        architecture = attributes["architecture"]
        size = int(attributes["size"])
        digest = attributes["sha256"]
    except (KeyError, ValueError):
        fail(f"invalid lock record on line {line_number}")
    if len(attributes) != len(pairs):
        fail(f"invalid lock record on line {line_number}")
    if package in expected:
        fail(f"duplicate lock record for {package}")
    if architecture != "arm64" or size < 0 or len(digest) != 64:
        fail(f"invalid lock record on line {line_number}")
    try:
        bytes.fromhex(digest)
    except ValueError:
        fail(f"invalid lock record on line {line_number}")
    expected[package] = (version, architecture, size, digest.lower())

if not expected:
    fail("lock has no direct package records")

declared = {}
for selector in os.environ["LOCKWIRE_E5_9_DIRECT_SELECTORS"].split():
    package, separator, version = selector.partition("=")
    if not separator or not package or not version:
        fail(f"invalid direct selector: {selector}")
    if package in declared:
        fail(f"duplicate direct selector for {package}")
    declared[package] = version

locked = {package: values[0] for package, values in expected.items()}
if declared != locked:
    fail("declared direct selectors do not exactly match lock pins")

expected_filenames = {
    f"{package}_{version}_{architecture}.deb": package
    for package, (version, architecture, _size, _digest) in expected.items()
}
seen = set()
for artifact in sorted(archives.glob("*.deb")):
    control_result = subprocess.run(
        ["dpkg-deb", "-f", str(artifact)],
        check=False,
        capture_output=True,
        text=True,
    )
    if control_result.returncode:
        fail(f"unreadable control metadata for {artifact.name}")
    metadata = {}
    for control_line in control_result.stdout.splitlines():
        name, separator, value = control_line.partition(":")
        if separator and name in {"Package", "Version", "Architecture"}:
            if name in metadata:
                fail(f"duplicate {name} control field for {artifact.name}")
            metadata[name] = value.strip()
    try:
        package = metadata["Package"]
        observed_version = metadata["Version"]
        observed_architecture = metadata["Architecture"]
    except KeyError:
        fail(f"incomplete control metadata for {artifact.name}")
    named_package = expected_filenames.get(artifact.name)
    if named_package is not None and package != named_package:
        fail(f"identity mismatch for {artifact.name}")
    if package not in expected:
        # Control metadata, never the filename, scopes closure-only transitives.
        continue
    if package in seen:
        fail(f"duplicate direct artifact for {package}")
    seen.add(package)
    version, architecture, size, digest = expected[package]
    if (observed_version, observed_architecture) != (version, architecture):
        fail(f"identity mismatch for {package}")
    if artifact.name != f"{package}_{version}_{architecture}.deb":
        fail(f"renamed direct artifact for {package}")
    if artifact.stat().st_size != size:
        fail(f"size mismatch for {package}")
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
        fail(f"sha256 mismatch for {package}")

missing = sorted(expected.keys() - seen)
if missing:
    fail(f"missing direct artifacts: {', '.join(missing)}")
# E5_9_DIRECT_VERIFY_PY_END
PY

RUN printf '%s\n' 'E5_9_INSTALL_BEGIN' >&2 \
 && DEBIAN_FRONTEND=noninteractive apt-get \
    -o Dpkg::Use-Pty=0 \
    -o Dir::Cache::archives=/opt/lockwire/archives \
    --no-download install --no-install-recommends -y \
    $LOCKWIRE_E5_9_DIRECT_SELECTORS \
 && printf '%s\n' 'E5_9_INSTALL_DONE' >&2

RUN printf '%s\n' 'E5_9_CLOSURE_DPKG_BEGIN' >&2 \
 && dpkg-query -W -f='${binary:Package}\t${Version}\t${Architecture}\t${source:Package}\n' \
      > /opt/lockwire/receipt/dpkg.unsorted.tsv \
 && LC_ALL=C sort /opt/lockwire/receipt/dpkg.unsorted.tsv \
      > /opt/lockwire/receipt/dpkg.tsv \
 && rm /opt/lockwire/receipt/dpkg.unsorted.tsv \
 && printf 'E5_9_CLOSURE_DPKG_DONE rows=%s\n' "$(wc -l < /opt/lockwire/receipt/dpkg.tsv)" >&2 \
 && printf '%s\n' 'E5_9_CLOSURE_APT_SHOW_BEGIN' >&2 \
 && awk -F '\t' 'NF != 4 || $1 == "" || $2 == "" || $3 == "" { exit 64 } { print $1 "=" $2 }' \
      /opt/lockwire/receipt/dpkg.tsv > /opt/lockwire/receipt/apt-show-selectors.txt \
 && test -s /opt/lockwire/receipt/apt-show-selectors.txt \
 && LC_ALL=C xargs -r -n 64 apt-cache show < /opt/lockwire/receipt/apt-show-selectors.txt \
      > /opt/lockwire/receipt/apt-show.txt \
 && awk -F '\t' 'NF != 4 { exit 64 } { package = $1; sub(/:[^:]+$/, "", package); if (package == "" || $2 == "" || $3 == "") exit 64; print package "\t" $2 "\t" $3 }' \
      /opt/lockwire/receipt/dpkg.tsv | LC_ALL=C sort -u \
      > /opt/lockwire/receipt/expected-package-version-architecture.tsv \
 && awk 'BEGIN { RS = ""; FS = "\n" } { package = version = architecture = ""; for (i = 1; i <= NF; i++) { if ($i ~ /^Package: /) package = substr($i, 10); else if ($i ~ /^Version: /) version = substr($i, 10); else if ($i ~ /^Architecture: /) architecture = substr($i, 15) } if (package == "" || version == "" || architecture == "") exit 64; print package "\t" version "\t" architecture }' \
      /opt/lockwire/receipt/apt-show.txt | LC_ALL=C sort -u \
      > /opt/lockwire/receipt/actual-package-version-architecture.tsv \
 && test -s /opt/lockwire/receipt/expected-package-version-architecture.tsv \
 && cmp -s /opt/lockwire/receipt/expected-package-version-architecture.tsv \
      /opt/lockwire/receipt/actual-package-version-architecture.tsv \
 && printf 'E5_9_CLOSURE_APT_SHOW_DONE bytes=%s\n' "$(wc -c < /opt/lockwire/receipt/apt-show.txt)" >&2 \
 && printf '%s\n' 'E5_9_CLOSURE_COPYRIGHT_BEGIN' >&2 \
 && find /usr/share/doc -type f -name copyright -print0 \
      > /opt/lockwire/receipt/copyright-files.nul \
 && LC_ALL=C sort -z -o /opt/lockwire/receipt/copyright-files.nul \
      /opt/lockwire/receipt/copyright-files.nul \
 && xargs -0 sha256sum < /opt/lockwire/receipt/copyright-files.nul \
      > /opt/lockwire/receipt/copyright-sha256.txt \
 && rm /opt/lockwire/receipt/copyright-files.nul \
 && printf 'E5_9_CLOSURE_COPYRIGHT_DONE rows=%s\n' "$(wc -l < /opt/lockwire/receipt/copyright-sha256.txt)" >&2 \
 && test -s /opt/lockwire/receipt/deb-sha256.txt \
 && printf 'E5_9_CLOSURE_DEB_HASH_DONE rows=%s\n' "$(wc -l < /opt/lockwire/receipt/deb-sha256.txt)" >&2 \
 && rm -rf /var/lib/apt/lists/* /opt/lockwire/archives \
 && printf '%s\n' 'E5_9_CLOSURE_RECEIPT_DONE' >&2

COPY packaging/motion/run-admission.sh /opt/lockwire/run-admission.sh
COPY tests/fixtures/motion_admission /workspace
RUN chmod 0555 /opt/lockwire/run-admission.sh
ENTRYPOINT ["/opt/lockwire/run-admission.sh"]
