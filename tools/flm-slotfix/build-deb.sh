#!/usr/bin/env bash
# Build a FastFlowLM .deb with the connection-slot leak fix (ROCm/FastFlowLM#680).
#
# Why: when a client drops a stream mid-generation, FLM's /v1/chat/completions
# cancels the generation and frees the NPU at once — exactly what a voice agent
# needs on barge-in — but the stock server leaks one connection slot per drop
# and, at the -s limit, refuses every connection until restarted. This builds
# the official release with connection-slot-release.patch applied.
#
# Everything is compiled inside a container from FLM's own build Dockerfile, so
# the host needs only docker + git. Output: dist/fastflowlm_<ver>+slotfix<N>_amd64.deb
#
# Usage:
#   tools/flm-slotfix/build-deb.sh [FLM_TAG] [BASE_IMAGE]
#     FLM_TAG     upstream release to rebuild   (default: v1.0.10)
#     BASE_IMAGE  must match the host OS         (default: ubuntu:26.04)
# Install:
#   sudo dpkg -i tools/flm-slotfix/dist/fastflowlm_*+slotfix*_amd64.deb
#   sudo apt-mark hold fastflowlm   # so an official package can't silently replace it
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLM_TAG="${1:-v1.0.10}"
BASE_IMAGE="${2:-ubuntu:26.04}"
FIX_REV="${FIX_REV:-1}"                      # bump to rebuild the same release
CODENAME="${CODENAME:-resolute}"
WORK="${WORK:-${XDG_CACHE_HOME:-$HOME/.cache}/flm-slotfix/$FLM_TAG}"
DEB_VERSION="${FLM_TAG#v}+slotfix${FIX_REV}"  # sorts above the stock ${FLM_TAG#v}
ENV_IMAGE="flm-build-env:${BASE_IMAGE//[:\/]/-}"

# Claude-spawned shells on this machine lack the docker group; fall back to sg.
docker_() {
    if docker info >/dev/null 2>&1; then docker "$@"
    else sg docker -c "$(printf '%q ' docker "$@")"; fi
}

echo "[flm-slotfix] $FLM_TAG -> $DEB_VERSION (base $BASE_IMAGE, workdir $WORK)"

# 1. Pristine source at the release tag, then the patch.
if [ ! -d "$WORK/src/.git" ]; then
    mkdir -p "$WORK"
    git clone -q --depth 1 --branch "$FLM_TAG" https://github.com/ROCm/FastFlowLM.git "$WORK/src"
    git -C "$WORK/src" submodule update -q --init --recursive --depth 1
fi
git -C "$WORK/src" checkout -q -- src/server
git -C "$WORK/src" apply "$HERE/connection-slot-release.patch"
grep -q "release_slot" "$WORK/src/src/server/server.cpp"

# 2. Changelog (upstream CI fills debian/changelog.in the same way).
cat > "$WORK/src/debian/changelog" <<EOF
fastflowlm ($DEB_VERSION) $CODENAME; urgency=medium

  * Rebuild of upstream $FLM_TAG with connection-slot-release.patch
    (ROCm/FastFlowLM#680): clients that drop a stream mid-generation no longer
    leak a connection slot.

 -- Personal Voice Assistant <noreply@localhost>  $(date -R)
EOF
chmod +x "$WORK/src/debian/rules"

# 3. Build environment from FLM's own Dockerfile, then the package.
docker_ build -q -t "$ENV_IMAGE" --build-arg BASE_IMAGE="$BASE_IMAGE" -f "$WORK/src/Dockerfile" "$WORK/src" >/dev/null
docker_ run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$WORK:/work" -w /work/src "$ENV_IMAGE" \
    bash -c "dpkg-buildpackage -b -us -uc > /work/dpkg-buildpackage.log 2>&1" \
    || { echo "[flm-slotfix] build failed — see $WORK/dpkg-buildpackage.log"; exit 1; }

mkdir -p "$HERE/dist"
DEB="$HERE/dist/fastflowlm_${DEB_VERSION}_amd64.deb"
mv "$WORK/fastflowlm_${DEB_VERSION}_amd64.deb" "$DEB"
echo "[flm-slotfix] built $DEB"
dpkg-deb -f "$DEB" Package Version Depends
