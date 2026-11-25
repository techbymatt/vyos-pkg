#!/bin/bash
# Probe a validated TSV inventory. Do not install dependencies or fetch full archives.
set -euo pipefail

probe_target() (
  local method=$1 label=$2 url=$3 family trace metrics tried rc auth_file
  local -a args families=(4)
  if [[ ${PROBE_IPV6:-false} == true ]]; then families+=(6); fi
  trace=$(mktemp)
  auth_file=
  trap 'rm -f -- "$trace" ${auth_file:+"$auth_file"}' EXIT
  args=(--connect-timeout 20 --max-time 25 --output /dev/null --globoff
        --proto '=http,https' --proto-redir '=http,https')
  case "$method" in
    git)
      url="${url%/}/info/refs?service=git-upload-pack"
      args+=(--header 'Git-Protocol: version=2' --max-filesize 4194304) ;;
    head) args+=(--head) ;;
    get) args+=(--max-filesize 4194304) ;;
    *) return 2 ;;
  esac
  if [[ "$url" == 'https://github.com/techbymatt/tbm-vyos-patch.git/info/refs?service=git-upload-pack' && -n "${PATCH_TOKEN:-}" ]]; then
    # Only send the repository-scoped credential to this exact endpoint.
    # Do not print the trace or follow a redirect with this token.
    auth_file=$(mktemp)
    printf 'machine github.com login x-access-token password %s\n' "$PATCH_TOKEN" > "$auth_file"
    args+=(--netrc-file "$auth_file")
  elif [[ "$url" != https://kernel.org/.well-known/openpgpkey/* && "$url" != https://www.kernel.org/.well-known/openpgpkey/* ]]; then
    args+=(--location --max-redirs 4)
  fi
  if [[ -z "$auth_file" ]]; then
    # curl's verbose trace includes the Base64-encoded credential.
    args+=(--verbose)
  fi
  for family in "${families[@]}"; do
    rc=0
    metrics=$(curl -q "-$family" --noproxy '*' --silent --show-error \
      "${args[@]}" \
      --write-out '%{remote_ip} | %{http_code} | %{time_namelookup} | %{time_connect} | %{time_appconnect} | %{time_total} | %{num_redirects}\n' \
      "$url" 2>"$trace") || rc=$?
    # On failed connections remote_ip is empty; keep the IPs curl tried as well.
    tried=$(sed -nE 's/^\* +Trying (\[[0-9A-Fa-f:.]+\]|[0-9.]+):[0-9]+\.\.\.$/\1/p' "$trace" | paste -sd ',' -)
    printf '| %s | %s | IPv%s | %s | %s | %s |\n' "$label" "$url" "$family" "$rc" "${tried:--}" "${metrics:- | 000 | - | - | - | - | -}"
  done
)

export -f probe_target
curl --version
summary=${GITHUB_STEP_SUMMARY:-/dev/null}
if [[ ! -s "$1" ]]; then
  printf '%s\n' 'Build probe inventory is empty or unavailable.' >&2
  exit 1
fi
{
  printf '### Build network probes (%s)\n\n' "${PROBE_ARCH:-$(uname -m)}"
  printf '%s\n\n' 'Times are seconds from request start, including followed redirects. A zero milestone or empty IP can mean it did not complete. HTTP 000 means no response; HTTP errors still show reachability.'
  if [[ ${RECIPE_INVENTORY:-} != success ]]; then
    printf '%s\n\n' '**Partial inventory:** patched recipe remotes and versioned kernel URLs were unavailable. See the recipe inventory job.'
  fi
  if [[ -n ${PATCH_TOKEN:-} ]]; then
    printf '%s\n\n' 'Verbose tracing is disabled for the authenticated patch repository probe.'
  fi
  printf '%s\n' '| Resource | URL | Family | curl exit | Tried IPs | Connected IP | HTTP | DNS | TCP | TLS | Total | Redirects |' \
    '| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |'
} | tee -a "$summary"

# Keep concurrency low to avoid causing rate limits.
while IFS=$'\t' read -r method label url; do
  printf '%s\0' "$method" "$label" "$url"
done < "$1" | xargs -0 -n 3 -P 6 bash -e -o pipefail -c 'probe_target "$@"' _ \
  | LC_ALL=C sort | tee -a "$summary"
