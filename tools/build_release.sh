#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
output=$(realpath -m "${1:?usage: build_release.sh OUTPUT REGISTRY_PREFIX}")
registry=${2:?set candidate registry/repository prefix}
python=${PYTHON:-.venv/bin/python}
docker buildx version >/dev/null
"$python" tools/release.py prepare --output "$output"
version=$("$python" -c 'from shared.version import VERSION; print(VERSION)')
docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
    --tag "$registry/target-web-001:$version" \
    --output "type=oci,dest=$output/target-web-001-v$version.oci.tar" \
    --metadata-file "$output/target-build.json" "$output/target-context"
target_digest=$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$output/target-build.json")
"$python" -c 'import json,sys; from pathlib import Path; Path(sys.argv[1]).write_text(json.dumps({"web-001": sys.argv[2]}))' \
    "$output/arena-context/arena/case-images.json" "$registry/target-web-001@$target_digest"
for role in portal arena; do
    docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
        --tag "$registry/$role:$version" \
        --output "type=oci,dest=$output/$role-v$version.oci.tar" \
        --metadata-file "$output/$role-build.json" "$output/$role-context"
done
portal_digest=$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$output/portal-build.json")
arena_digest=$("$python" -c 'import json,sys; print(json.load(open(sys.argv[1]))["containerimage.digest"])' "$output/arena-build.json")
"$python" tools/release.py bundle --output "$output" \
    --portal-image "$registry/portal@$portal_digest" --arena-image "$registry/arena@$arena_digest"
echo "Candidate built in $output; not published or accepted."
