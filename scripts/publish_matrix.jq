def runner_label:
  if . == "amd64" then "ubuntu-24.04" else "ubuntu-24.04-arm" end;
def entry:
  {
    package: .[2],
    arch: .[3],
    runner_label: (.[3] | runner_label),
    commit: .[4],
    group: .[0],
    cache_key: (
      "cache-v2-\(.[2])-\(.[3])-\(.[4])-\($namespace)-" as $prefix
      | if .[1] == "hit" then
          first($cached_keys | split("\n")[] | select(startswith($prefix)))
        else $prefix + $run end
    )
  }
  + (if .[5] == "" then {} else {deps: .[5]} end);
rtrimstr("\n")
| split("\n")
| map(select(length > 0) | split("\t"))
| if $changed == "false" then [] else . end
| {
    "build-matrix": {
      include: [.[] | select(.[0] == "build" and .[1] == "miss") | entry]
    },
    "build-extra-matrix": {
      include: [.[] | select(.[0] == "build-extra" and .[1] == "miss") | entry]
    },
    "restore-matrix": {
      include: ([.[] | select(.[1] == "hit") | entry]
                | group_by(.runner_label)
                | map(. as $entries | range(0; length; 8) as $start
                      | {runner_label: $entries[0].runner_label,
                         entries: $entries[$start:$start + 8]})
                | flatten)
    }
  }
| to_entries[]
| "\(.key)=\(.value)"
