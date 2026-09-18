#!/bin/bash
# Assemble and sign the repository after Jekyll has generated _site.
set -euo pipefail

readonly SITE_DIR="_site"
readonly SOURCE_DIR="packages/rolling"
readonly REPO_DIR="${SITE_DIR}/deb"
readonly ARTIFACT_LIST="${REPO_DIR}/.artifacts"
readonly GPG_KEY_ID="${GPG_FINGERPRINT:-${GPG_KEY_ID:-}}"
readonly REPO_OWNER="${REPO_OWNER:-${ORIGIN:-}}"
readonly ARCHITECTURES=(all amd64 arm64)

error() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

check_inputs() {
	local cmd keys
	[[ -n "$REPO_OWNER" ]] || error "Set REPO_OWNER or ORIGIN"
	[[ -d "$SITE_DIR" ]] || error "Missing Jekyll output directory: $SITE_DIR"
	[[ -d "$SOURCE_DIR" ]] || error "Missing package input directory: $SOURCE_DIR"
	for cmd in dpkg-scanpackages dpkg-scansources gpg gzip bzip2 find sort \
		md5sum sha1sum sha256sum wc date mkdir mv rm; do
		command -v "$cmd" >/dev/null || error "Required command '$cmd' not found"
	done
	if [[ -n "$GPG_KEY_ID" ]]; then
		keys=$(gpg --with-colons --list-secret-keys "$GPG_KEY_ID") || error "Cannot list signing key"
	else
		keys=$(gpg --with-colons --list-secret-keys) || error "Cannot list signing key"
	fi
	[[ "$keys" == sec:* || "$keys" == *$'\nsec:'* ]] || error "No GPG secret signing key found"
}

collect_artifacts() {
	local file
	local artifacts=() deb_count=0
	mkdir -p "$REPO_DIR/pool/main"
	# A foreground find makes traversal failures fatal, unlike process substitution.
	find "$SOURCE_DIR" -type f \( -name '*.deb' -o -name '*.dsc' \
		-o -name '*.tar.gz' -o -name '*.tar.xz' -o -name '*.tar.bz2' \
		-o -name '*.changes' \) -print0 >"$ARTIFACT_LIST"
	while IFS= read -r -d '' file; do
		artifacts+=("$file")
		if [[ "$file" == *.deb ]]; then
			deb_count=$((deb_count + 1))
		fi
	done <"$ARTIFACT_LIST"
	((deb_count > 0)) || error "No .deb packages found in $SOURCE_DIR"
	for file in "${artifacts[@]}"; do
		mv "$file" "$REPO_DIR/pool/main/"
	done
}

compress_index() {
	local index="$1"
	gzip -9n <"$index" >"$index.gz"
	bzip2 -9 <"$index" >"$index.bz2"
	if command -v xz >/dev/null 2>&1; then
		xz -9 <"$index" >"$index.xz"
	fi
}

generate_indexes() {
	local arch index
	for arch in "${ARCHITECTURES[@]}"; do
		index="dists/rolling/main/binary-${arch}/Packages"
		mkdir -p "${index%/*}"
		dpkg-scanpackages -a "$arch" pool/main >"$index" || error "Package scanning failed for $arch"
		compress_index "$index"
	done
	index="dists/rolling/main/source/Sources"
	mkdir -p "${index%/*}"
	dpkg-scansources pool/main >"$index" || error "Source scanning failed"
	compress_index "$index"
}

generate_release() {
	local release_date hash_spec hash_name hash_cmd filepath file_hash file_size
	release_date=$(date -Ru)
	{
		printf 'Origin: VyOS\nLabel: %s\n' "$REPO_OWNER"
		printf 'Suite: rolling\nCodename: rolling\nVersion: 1.0\n'
		printf 'Architectures: all amd64 arm64\nComponents: main\n'
		printf 'Description: A repository for packages released by %s\n' "$REPO_OWNER"
		printf 'Date: %s\n' "$release_date"
	} >Release
	for hash_spec in MD5Sum:md5sum SHA1:sha1sum SHA256:sha256sum; do
		hash_name="${hash_spec%%:*}"
		hash_cmd="${hash_spec##*:}"
		printf '%s:\n' "$hash_name" >>Release
		find main -type f -not -path '*/\.*' | sort | while IFS= read -r filepath; do
			file_hash=$("$hash_cmd" "$filepath")
			file_size=$(wc -c <"$filepath")
			printf ' %s %s %s\n' "${file_hash%% *}" "${file_size//[[:space:]]/}" "$filepath" >>Release
		done
	done
}

sign_release() {
	local opts=(--batch --yes --pinentry-mode loopback)
	if [[ -n "$GPG_KEY_ID" ]]; then
		opts+=(--local-user "$GPG_KEY_ID")
	fi
	gpg "${opts[@]}" --detach-sign --armor --output Release.gpg <Release || error "GPG signing failed for Release.gpg"
	gpg "${opts[@]}" --clearsign --output InRelease <Release || error "GPG signing failed for InRelease"
}

cleanup() {
	rm -f "$ARTIFACT_LIST"
	rm -rf "$SITE_DIR/packages"
}

main() {
	check_inputs
	trap cleanup EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	collect_artifacts
	# Call functions directly: conditional calls disable errexit inside functions.
	(
		cd "$REPO_DIR"
		generate_indexes
		cd dists/rolling
		generate_release
		sign_release
	)
	printf 'Repository built successfully for rolling\n'
}

main "$@"
