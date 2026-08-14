#!/usr/bin/env bash
set -euo pipefail

DEFAULT_BUNDLE_URL="https://designanything.ai/downloads/designanything-local.tar.gz"
INSTALL_DIR="${AUTODESIGN_INSTALL_DIR:-${DESIGNANYTHING_INSTALL_DIR:-${HOME}/.local/share/autodesign}}"
BIN_DIR="${AUTODESIGN_BIN_DIR:-${DESIGNANYTHING_BIN_DIR:-${HOME}/.local/bin}}"
BUNDLE_URL="${AUTODESIGN_BUNDLE_URL:-${DESIGNANYTHING_BUNDLE_URL:-${DEFAULT_BUNDLE_URL}}}"
BUNDLE_SHA256="${AUTODESIGN_BUNDLE_SHA256:-${DESIGNANYTHING_BUNDLE_SHA256:-}}"
BUNDLE_SHA256_URL="${AUTODESIGN_BUNDLE_SHA256_URL:-${DESIGNANYTHING_BUNDLE_SHA256_URL:-${BUNDLE_URL}.sha256}}"
STATE_DIR="${AUTODESIGN_STATE_DIR:-${DESIGNANYTHING_STATE_DIR:-${HOME}/.autodesign}}"
case "${STATE_DIR}" in
  /*) ;;
  *) STATE_DIR="$(pwd -P)/${STATE_DIR}" ;;
esac
LEGACY_STATE_DIR="${HOME}/.designanything"
INSTALL_UV="${AUTODESIGN_INSTALL_UV:-${DESIGNANYTHING_INSTALL_UV:-1}}"
SKIP_SETUP="${AUTODESIGN_SKIP_SETUP:-${DESIGNANYTHING_SKIP_SETUP:-0}}"
TEMP_ROOT=""
DOWNLOADED_BUNDLE_ROOT=""
STAGED_INSTALL_DIR=""
ACTIVATED_PREVIOUS_DIR=""
ACTIVATED_NEW_INSTALL=0
LAUNCHER_BACKUP_DIR=""

die() {
  echo "error: $*" >&2
  exit 1
}

info() {
  echo "$*" >&2
}

have_cmd() {
  command -v "$1" >/dev/null 2>&1
}

cleanup() {
  if [ -n "${TEMP_ROOT}" ] && [ -d "${TEMP_ROOT}" ]; then
    rm -rf "${TEMP_ROOT}"
  fi
  if [ -n "${STAGED_INSTALL_DIR}" ] && [ -d "${STAGED_INSTALL_DIR}" ]; then
    rm -rf "${STAGED_INSTALL_DIR}"
  fi
}
trap cleanup EXIT

looks_like_bundle() {
  local dir="$1"
  [ -f "${dir}/pyproject.toml" ] \
    && [ -f "${dir}/uv.lock" ] \
    && [ -f "${dir}/scripts/autodesign" ] \
    && [ -f "${dir}/scripts/designanything" ] \
    && [ -f "${dir}/scripts/start_local_web.sh" ] \
    && [ -f "${dir}/web/dist/index.html" ]
}

migrate_legacy_state() {
  if [ "${STATE_DIR}" = "${LEGACY_STATE_DIR}" ]; then
    return 0
  fi
  if [ ! -e "${STATE_DIR}" ] && [ -d "${LEGACY_STATE_DIR}" ] && [ ! -L "${LEGACY_STATE_DIR}" ]; then
    mkdir -p "$(dirname -- "${STATE_DIR}")"
    mv "${LEGACY_STATE_DIR}" "${STATE_DIR}"
    if ! ln -s "${STATE_DIR}" "${LEGACY_STATE_DIR}"; then
      mv "${STATE_DIR}" "${LEGACY_STATE_DIR}"
      die "Could not create the legacy state compatibility symlink"
    fi
    info "Migrated AutoDesign state to ${STATE_DIR}."
  elif [ -e "${STATE_DIR}" ] && [ -d "${LEGACY_STATE_DIR}" ] && [ ! -L "${LEGACY_STATE_DIR}" ]; then
    info "warning: both ${STATE_DIR} and legacy ${LEGACY_STATE_DIR} exist; leaving both unchanged"
  fi
}

find_bundle_root() {
  local root="$1"
  local index_file
  local candidate

  if looks_like_bundle "${root}"; then
    printf '%s\n' "${root}"
    return 0
  fi

  while IFS= read -r index_file; do
    candidate="$(dirname -- "$(dirname -- "$(dirname -- "${index_file}")")")"
    if looks_like_bundle "${candidate}"; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done < <(find "${root}" -type f -path "*/web/dist/index.html" 2>/dev/null)

  return 1
}

ensure_uv() {
  if have_cmd uv; then
    return 0
  fi

  if [ "${INSTALL_UV}" = "0" ]; then
    die "Missing uv. Install uv first, or rerun without AUTODESIGN_INSTALL_UV=0."
  fi

  have_cmd curl || die "Missing curl; install curl and uv, then rerun."
  info "uv was not found; installing uv with Astral's official installer..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${HOME}/.cargo/bin:${PATH}"
  have_cmd uv || die "uv install finished, but uv is still not on PATH. Add ~/.local/bin to PATH and rerun."
}

download_with_retries() {
  local url="$1"
  local output="$2"
  local label="$3"
  local max_attempts="${AUTODESIGN_DOWNLOAD_ATTEMPTS:-5}"
  local retry_delay="${AUTODESIGN_DOWNLOAD_RETRY_DELAY:-2}"
  local attempt=1

  while [ "${attempt}" -le "${max_attempts}" ]; do
    rm -f "${output}"
    if curl -fL --connect-timeout 20 "${url}" -o "${output}"; then
      return 0
    fi
    if [ "${attempt}" -ge "${max_attempts}" ]; then
      break
    fi
    info "${label} download failed (attempt ${attempt}/${max_attempts}); retrying..."
    if [ "${retry_delay}" != "0" ]; then
      sleep "${retry_delay}"
    fi
    attempt=$((attempt + 1))
  done
  die "Could not download ${label} after ${max_attempts} attempts."
}

sha256_file() {
  local path="$1"
  if have_cmd sha256sum; then
    sha256sum "${path}" | awk '{print $1}'
  elif have_cmd shasum; then
    shasum -a 256 "${path}" | awk '{print $1}'
  else
    die "Missing sha256sum or shasum; cannot verify the release bundle."
  fi
}

resolve_bundle_sha256() {
  local checksum_file="$1"
  local expected="${BUNDLE_SHA256}"
  if [ -z "${expected}" ]; then
    info "Downloading AutoDesign release checksum..."
    download_with_retries "${BUNDLE_SHA256_URL}" "${checksum_file}" "release checksum"
    expected="$(awk 'match($0, /[0-9a-fA-F]{64}/) { print substr($0, RSTART, RLENGTH); exit }' "${checksum_file}")"
  fi
  case "${expected}" in
    ''|*[!0-9a-fA-F]*)
      die "Release checksum is missing or invalid."
      ;;
  esac
  if [ "${#expected}" -ne 64 ]; then
    die "Release checksum is missing or invalid."
  fi
  printf '%s\n' "$(printf '%s' "${expected}" | tr 'A-F' 'a-f')"
}

download_bundle() {
  have_cmd curl || die "Missing curl; install curl or run install.sh from an unpacked release bundle."
  have_cmd tar || die "Missing tar; install tar or run install.sh from an unpacked release bundle."

  TEMP_ROOT="$(mktemp -d)"
  local archive="${TEMP_ROOT}/autodesign-bundle"
  local archive_part="${archive}.part"
  local checksum_file="${TEMP_ROOT}/autodesign-bundle.sha256"
  local unpack_dir="${TEMP_ROOT}/unpack"
  local expected_sha256
  local actual_sha256
  mkdir -p "${unpack_dir}"

  info "Downloading AutoDesign release bundle..."
  download_with_retries "${BUNDLE_URL}" "${archive_part}" "release bundle"
  expected_sha256="$(resolve_bundle_sha256 "${checksum_file}")"
  actual_sha256="$(sha256_file "${archive_part}")"
  if [ "${actual_sha256}" != "${expected_sha256}" ]; then
    die "Release bundle checksum mismatch: expected ${expected_sha256}, got ${actual_sha256}."
  fi
  mv "${archive_part}" "${archive}"

  if tar -xzf "${archive}" -C "${unpack_dir}" >/dev/null 2>&1; then
    :
  elif have_cmd unzip && unzip -q "${archive}" -d "${unpack_dir}" >/dev/null 2>&1; then
    :
  else
    die "Could not unpack release bundle. Expected .tar.gz/.tgz or .zip."
  fi

  DOWNLOADED_BUNDLE_ROOT="$(find_bundle_root "${unpack_dir}")" || die "Release bundle is invalid: web/dist/index.html, scripts/autodesign, or project files are missing."
}

local_bundle_root() {
  local script_source="${BASH_SOURCE[0]:-}"
  local script_dir=""

  if [ -n "${script_source}" ] && [ -f "${script_source}" ]; then
    script_dir="$(cd -- "$(dirname -- "${script_source}")" && pwd)"
    if looks_like_bundle "${script_dir}"; then
      printf '%s\n' "${script_dir}"
      return 0
    fi
  fi

  return 1
}

stage_bundle() {
  local source_dir="$1"
  local source_real
  local install_real=""
  local parent_dir
  local staging_dir

  source_real="$(cd -- "${source_dir}" && pwd -P)"
  if [ -d "${INSTALL_DIR}" ]; then
    install_real="$(cd -- "${INSTALL_DIR}" && pwd -P)"
  fi

  if [ "${source_real}" = "${install_real}" ]; then
    info "AutoDesign is already installed at ${INSTALL_DIR}."
    STAGED_INSTALL_DIR="${INSTALL_DIR}"
    return 0
  fi

  parent_dir="$(dirname -- "${INSTALL_DIR}")"
  mkdir -p "${parent_dir}"
  staging_dir="$(mktemp -d "${parent_dir}/.autodesign-install.XXXXXX")"

  info "Installing AutoDesign to ${INSTALL_DIR}..."
  (
    cd "${source_dir}"
    tar \
      --exclude .git \
      --exclude .env \
      --exclude .env.local \
      --exclude ".env.*.local" \
      --exclude .venv \
      --exclude .uv-cache \
      --exclude .autodesign \
      --exclude .designanything \
      --exclude node_modules \
      --exclude "web/node_modules" \
      --exclude out \
      --exclude sessions \
      --exclude tmp \
      -cf - .
  ) | (
    cd "${staging_dir}"
    tar -xf -
  )

  chmod +x "${staging_dir}/scripts/autodesign" "${staging_dir}/scripts/designanything" "${staging_dir}/scripts/start_local_web.sh"

  STAGED_INSTALL_DIR="${staging_dir}"
}

setup_activated_bundle() {
  local setup_state="${TEMP_ROOT}/setup-state"
  mkdir -p "${setup_state}"
  AUTODESIGN_STATE_DIR="${setup_state}" "${INSTALL_DIR}/scripts/autodesign" setup
}

remove_previous_install() {
  local previous_dir="$1"
  if [ ! -e "${previous_dir}" ]; then
    return 0
  fi
  if ! find "${previous_dir}" -type d -exec chmod u+rwx {} +; then
    die "Could not prepare the previous AutoDesign installation for replacement."
  fi
  if ! rm -rf "${previous_dir}"; then
    die "Could not remove the previous AutoDesign installation."
  fi
}

activate_staged_bundle() {
  local previous_dir="${INSTALL_DIR}.previous"
  if [ "${STAGED_INSTALL_DIR}" = "${INSTALL_DIR}" ]; then
    return 0
  fi
  remove_previous_install "${previous_dir}"
  if [ -d "${INSTALL_DIR}" ]; then
    mv "${INSTALL_DIR}" "${previous_dir}"
  fi
  if ! mv "${STAGED_INSTALL_DIR}" "${INSTALL_DIR}"; then
    if [ -d "${previous_dir}" ]; then
      mv "${previous_dir}" "${INSTALL_DIR}"
    fi
    die "Could not activate the staged AutoDesign installation."
  fi
  STAGED_INSTALL_DIR=""
  ACTIVATED_PREVIOUS_DIR="${previous_dir}"
  ACTIVATED_NEW_INSTALL=1
}

rollback_activation() {
  if [ "${ACTIVATED_NEW_INSTALL}" != "1" ]; then
    return 0
  fi
  rm -rf "${INSTALL_DIR}"
  if [ -d "${ACTIVATED_PREVIOUS_DIR}" ]; then
    mv "${ACTIVATED_PREVIOUS_DIR}" "${INSTALL_DIR}"
  fi
  ACTIVATED_NEW_INSTALL=0
}

finalize_activation() {
  if [ "${ACTIVATED_NEW_INSTALL}" = "1" ] && [ -d "${ACTIVATED_PREVIOUS_DIR}" ]; then
    remove_previous_install "${ACTIVATED_PREVIOUS_DIR}"
  fi
  ACTIVATED_NEW_INSTALL=0
}

link_launcher() {
  mkdir -p "${BIN_DIR}" || return
  LAUNCHER_BACKUP_DIR="${TEMP_ROOT}/launcher-backup"
  mkdir -p "${LAUNCHER_BACKUP_DIR}" || return
  for launcher in autodesign designanything; do
    if [ -e "${BIN_DIR}/${launcher}" ] || [ -L "${BIN_DIR}/${launcher}" ]; then
      mv "${BIN_DIR}/${launcher}" "${LAUNCHER_BACKUP_DIR}/${launcher}" || return
    fi
  done
  chmod +x "${INSTALL_DIR}/scripts/autodesign" "${INSTALL_DIR}/scripts/designanything" || return
  ln -sfn "${INSTALL_DIR}/scripts/autodesign" "${BIN_DIR}/autodesign" || return
  ln -sfn "${INSTALL_DIR}/scripts/designanything" "${BIN_DIR}/designanything" || return
}

restore_launchers() {
  local launcher
  for launcher in autodesign designanything; do
    rm -f "${BIN_DIR}/${launcher}"
    if [ -n "${LAUNCHER_BACKUP_DIR}" ] && {
      [ -e "${LAUNCHER_BACKUP_DIR}/${launcher}" ] || [ -L "${LAUNCHER_BACKUP_DIR}/${launcher}" ]
    }; then
      mv "${LAUNCHER_BACKUP_DIR}/${launcher}" "${BIN_DIR}/${launcher}"
    fi
  done
}

install_setup_stamp() {
  local stamp_tmp
  mkdir -p "${STATE_DIR}" || return
  stamp_tmp="${STATE_DIR}/setup.stamp.tmp.$$"
  cp "${TEMP_ROOT}/setup-state/setup.stamp" "${stamp_tmp}" || return
  mv -f "${stamp_tmp}" "${STATE_DIR}/setup.stamp" || return
}

main() {
  local source_dir

  if source_dir="$(local_bundle_root)"; then
    :
  else
    download_bundle
    source_dir="${DOWNLOADED_BUNDLE_ROOT}"
  fi

  if [ -z "${TEMP_ROOT}" ]; then
    TEMP_ROOT="$(mktemp -d)"
  fi
  looks_like_bundle "${source_dir}" || die "Release bundle is invalid: web/dist/index.html and scripts/autodesign are required."
  migrate_legacy_state
  ensure_uv
  stage_bundle "${source_dir}"
  activate_staged_bundle
  if [ "${SKIP_SETUP}" != "1" ]; then
    local setup_status=0
    setup_activated_bundle || setup_status=$?
    if [ "${setup_status}" -ne 0 ]; then
      rollback_activation
      return "${setup_status}"
    fi
  else
    info "Skipping dependency setup because AUTODESIGN_SKIP_SETUP=1."
  fi
  if ! link_launcher; then
    restore_launchers
    rollback_activation
    die "Could not install the AutoDesign launcher; the previous installation was restored."
  fi
  if [ "${SKIP_SETUP}" != "1" ]; then
    if ! install_setup_stamp; then
      restore_launchers
      rollback_activation
      die "Could not record completed setup; the previous installation was restored."
    fi
  fi
  finalize_activation

  echo
  echo "AutoDesign installed."
  echo "Run: autodesign start"
  if [[ ":${PATH}:" != *":${BIN_DIR}:"* ]]; then
    echo
    echo "Note: ${BIN_DIR} is not on PATH. Add it, or run:"
    echo "  ${BIN_DIR}/autodesign start"
  fi
}

main "$@"
