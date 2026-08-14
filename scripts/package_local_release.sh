#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
OUT_DIR="${AUTODESIGN_RELEASE_DIR:-${DESIGN_ANYTHING_RELEASE_DIR:-${DESIGNANYTHING_RELEASE_DIR:-${REPO_ROOT}/dist}}}"
OUT_FILE="${1:-${OUT_DIR}/designanything-local.tar.gz}"
TEMP_ROOT=""
TEMP_ARCHIVE=""
TEMP_CHECKSUM=""

cleanup() {
  if [ -n "${TEMP_ROOT}" ] && [ -d "${TEMP_ROOT}" ]; then
    rm -rf "${TEMP_ROOT}"
  fi
  [ -z "${TEMP_ARCHIVE}" ] || rm -f "${TEMP_ARCHIVE}"
  [ -z "${TEMP_CHECKSUM}" ] || rm -f "${TEMP_CHECKSUM}"
}
trap cleanup EXIT

cd "${REPO_ROOT}"

TAR_METADATA_ARGS=(--no-xattrs)
TAR_OWNERSHIP_ARGS=()
TAR_VERSION="$(tar --version 2>&1 || true)"
if [[ "${TAR_VERSION}" == *"GNU tar"* ]]; then
  TAR_OWNERSHIP_ARGS=(--owner=0 --group=0 --numeric-owner)
else
  TAR_METADATA_ARGS+=(--no-mac-metadata)
  TAR_OWNERSHIP_ARGS=(--uid 0 --gid 0 --uname root --gname root)
fi

if [ ! -f "web/dist/index.html" ]; then
  cat >&2 <<'EOF'
web/dist/index.html is missing.

Build the frontend before packaging:

  cd web
  npm ci
  npm run build
EOF
  exit 1
fi

for required in install.sh scripts/autodesign scripts/designanything scripts/start_local_web.sh pyproject.toml uv.lock runtime/video/package.json runtime/video/package-lock.json; do
  if [ ! -f "${required}" ]; then
    echo "Missing required release file: ${required}" >&2
    exit 1
  fi
done

if [ -L web/dist ] || IFS= read -r -d '' _web_dist_symlink < <(
  find web/dist -mindepth 1 -type l -print0 -quit
); then
  echo "web/dist must not contain symlinks." >&2
  exit 1
fi

mkdir -p "$(dirname -- "${OUT_FILE}")"
TEMP_ROOT="$(mktemp -d)"
TEMP_ARCHIVE="$(mktemp "${OUT_FILE}.tmp.XXXXXX")"
TEMP_CHECKSUM="$(mktemp "${OUT_FILE}.sha256.tmp.XXXXXX")"
BUNDLE_DIR="${TEMP_ROOT}/AutoDesign"
FILE_LIST="${TEMP_ROOT}/files.list"
mkdir -p "${BUNDLE_DIR}"

{
  git ls-files -z
  find web/dist -mindepth 1 -type f -print0
} | while IFS= read -r -d '' release_path; do
  release_name="${release_path##*/}"
  case "${release_name}" in
    .env.example) ;;
    .env|.env.*) continue ;;
  esac
  printf '%s\0' "${release_path}"
done >"${FILE_LIST}"

COPYFILE_DISABLE=1 tar "${TAR_METADATA_ARGS[@]}" --null -T "${FILE_LIST}" -cf - | (
    cd "${BUNDLE_DIR}"
    COPYFILE_DISABLE=1 tar "${TAR_METADATA_ARGS[@]}" -xf -
  )

chmod +x "${BUNDLE_DIR}/install.sh" "${BUNDLE_DIR}/scripts/autodesign" "${BUNDLE_DIR}/scripts/designanything" "${BUNDLE_DIR}/scripts/start_local_web.sh"
COPYFILE_DISABLE=1 tar "${TAR_METADATA_ARGS[@]}" \
  "${TAR_OWNERSHIP_ARGS[@]}" \
  -czf "${TEMP_ARCHIVE}" -C "${TEMP_ROOT}" AutoDesign

if command -v sha256sum >/dev/null 2>&1; then
  ARCHIVE_SHA256="$(sha256sum "${TEMP_ARCHIVE}" | awk '{print $1}')"
else
  ARCHIVE_SHA256="$(shasum -a 256 "${TEMP_ARCHIVE}" | awk '{print $1}')"
fi
printf '%s  %s\n' "${ARCHIVE_SHA256}" "$(basename -- "${OUT_FILE}")" >"${TEMP_CHECKSUM}"
chmod 0644 "${TEMP_ARCHIVE}" "${TEMP_CHECKSUM}"
mv -f "${TEMP_ARCHIVE}" "${OUT_FILE}"
TEMP_ARCHIVE=""
mv -f "${TEMP_CHECKSUM}" "${OUT_FILE}.sha256"
TEMP_CHECKSUM=""

echo "Wrote ${OUT_FILE}"
echo "Wrote ${OUT_FILE}.sha256"
