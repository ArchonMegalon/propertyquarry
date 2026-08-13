#!/usr/bin/env bash
set -euo pipefail

: "${PROPERTYQUARRY_ANDROID_SIGNING_DIR:?Set PROPERTYQUARRY_ANDROID_SIGNING_DIR to an absolute directory outside the repository}"

case "${PROPERTYQUARRY_ANDROID_SIGNING_DIR}" in
  /*) ;;
  *)
    echo "PROPERTYQUARRY_ANDROID_SIGNING_DIR must be an absolute path" >&2
    exit 2
    ;;
esac

propertyquarry_mobile_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
propertyquarry_signing_dir="${PROPERTYQUARRY_ANDROID_SIGNING_DIR%/}"

case "${propertyquarry_signing_dir}/" in
  "${propertyquarry_mobile_root}/"*)
    echo "Signing material must be stored outside the PropertyQuarry mobile repository" >&2
    exit 2
    ;;
esac

propertyquarry_keystore_path="${propertyquarry_signing_dir}/propertyquarry-upload.p12"
propertyquarry_certificate_path="${propertyquarry_signing_dir}/propertyquarry-upload-cert.pem"
propertyquarry_environment_path="${propertyquarry_signing_dir}/android-release.env"
propertyquarry_key_alias="propertyquarry-upload"
propertyquarry_android_image="ghcr.io/cirruslabs/android-sdk@sha256:f9b3ea9ed2b5fc9522adae82c7b4622ab7aa54207ef532c8e615a347dca08f31"

for propertyquarry_target in \
  "${propertyquarry_keystore_path}" \
  "${propertyquarry_certificate_path}" \
  "${propertyquarry_environment_path}"
do
  if [[ -e "${propertyquarry_target}" || -L "${propertyquarry_target}" ]]; then
    echo "Refusing to replace existing signing material: ${propertyquarry_target}" >&2
    exit 3
  fi
done

command -v openssl >/dev/null

umask 077
install -d -m 0700 "${propertyquarry_signing_dir}"
propertyquarry_store_password="$(openssl rand -hex 32)"
export PROPERTYQUARRY_PROVISION_STORE_PASSWORD="${propertyquarry_store_password}"

propertyquarry_keytool() {
  if command -v keytool >/dev/null; then
    keytool "$@"
    return
  fi

  command -v docker >/dev/null
  docker run --rm \
    --user "$(id -u):$(id -g)" \
    -e PROPERTYQUARRY_PROVISION_STORE_PASSWORD \
    -v "${propertyquarry_signing_dir}:${propertyquarry_signing_dir}" \
    "${propertyquarry_android_image}" \
    keytool "$@"
}

propertyquarry_keytool -genkeypair \
  -keystore "${propertyquarry_keystore_path}" \
  -storetype PKCS12 \
  -storepass:env PROPERTYQUARRY_PROVISION_STORE_PASSWORD \
  -keypass:env PROPERTYQUARRY_PROVISION_STORE_PASSWORD \
  -alias "${propertyquarry_key_alias}" \
  -keyalg RSA \
  -keysize 4096 \
  -sigalg SHA256withRSA \
  -validity 9125 \
  -dname "CN=PropertyQuarry Upload, OU=Android Release, O=PropertyQuarry, L=Vienna, C=AT" \
  -noprompt

propertyquarry_keytool -exportcert \
  -rfc \
  -keystore "${propertyquarry_keystore_path}" \
  -storetype PKCS12 \
  -storepass:env PROPERTYQUARRY_PROVISION_STORE_PASSWORD \
  -alias "${propertyquarry_key_alias}" \
  -file "${propertyquarry_certificate_path}"

install -m 0600 /dev/null "${propertyquarry_environment_path}"
printf '%s\n' \
  "PROPERTYQUARRY_ANDROID_KEYSTORE_PATH=${propertyquarry_keystore_path}" \
  "PROPERTYQUARRY_ANDROID_KEYSTORE_PASSWORD=${propertyquarry_store_password}" \
  "PROPERTYQUARRY_ANDROID_KEY_ALIAS=${propertyquarry_key_alias}" \
  "PROPERTYQUARRY_ANDROID_KEY_PASSWORD=${propertyquarry_store_password}" \
  >"${propertyquarry_environment_path}"

chmod 0600 \
  "${propertyquarry_keystore_path}" \
  "${propertyquarry_certificate_path}" \
  "${propertyquarry_environment_path}"

unset PROPERTYQUARRY_PROVISION_STORE_PASSWORD
unset propertyquarry_store_password

printf 'upload_keystore=%s\n' "${propertyquarry_keystore_path}"
printf 'public_certificate=%s\n' "${propertyquarry_certificate_path}"
printf 'release_environment=%s\n' "${propertyquarry_environment_path}"
printf 'key_alias=%s\n' "${propertyquarry_key_alias}"
