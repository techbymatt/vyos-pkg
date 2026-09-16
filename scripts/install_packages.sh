#!/bin/bash
# Verify published packages, batching only when APT's simulation succeeds.
# Usage: ./install_packages.sh path/to/package.deb [...]
set -euo pipefail

if (( $# == 0 )); then
	echo "ERROR: Supply at least one .deb path." >&2
	exit 1
fi

targets=()
target_count=0
for deb in "$@"; do
	package=$(dpkg-deb --field "$deb" Package)
	version=$(dpkg-deb --field "$deb" Version)
	case "$package" in
		strongswan-charon|strongswan-charon-dbgsym) continue ;;
	esac
	targets+=("${package}=${version}")
	target_count=$((target_count + 1))
done

if (( target_count == 0 )); then
	echo "ERROR: No install targets remain after exclusions." >&2
	exit 1
fi

apt_command=(sudo apt-get
	-o APT::Sandbox::User=root
	-o Dpkg::Options::=--force-confdef
	-o Dpkg::Options::=--force-confold
	--no-install-recommends --allow-downgrades --reinstall --yes)

# Explicitly preserve failures while reporting timing, including failed installs.
transaction() {
	local label="$1"
	shift
	local started=$SECONDS
	local status=0
	"${apt_command[@]}" "$@" || status=$?
	printf '%s: elapsed %s seconds (status %s)\n' "$label" "$((SECONDS - started))" "$status" >&2
	return "$status"
}

simulation_log=$(mktemp)
trap 'rm -f "$simulation_log"' EXIT
if transaction "APT simulation" --simulate install "${targets[@]}" >"$simulation_log" 2>&1; then
	cat "$simulation_log"
	# A real failure is fatal: never retry it through the sequential path.
	transaction "APT batch install" install "${targets[@]}"
else
	echo "WARNING: APT simulation failed; falling back to sequential installs." >&2
	cat "$simulation_log" >&2
	for target in "${targets[@]}"; do
		transaction "APT install $target" install "$target"
	done
fi
