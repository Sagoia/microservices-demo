#!/usr/bin/env bash
#
# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Load the product catalog into the pgvector collection used by the shopping assistant.
#
# The seeder runs inside the already-deployed shoppingassistantservice pod, so it picks
# up exactly the same embedding model and database settings as the service itself, and
# products.json is streamed in on stdin rather than baked into the image.
#
# Re-run it any time: rows are upserted by product ID.
#
#     ./seed_pgvector.sh [path/to/products.json]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
PRODUCTS="${1:-${REPO_ROOT}/src/productcatalogservice/products.json}"

if [[ ! -f "${PRODUCTS}" ]]; then
  echo "products.json not found at ${PRODUCTS}" >&2
  exit 1
fi

echo "Waiting for shoppingassistantservice to be ready..."
kubectl wait --for=condition=available --timeout=180s deployment/shoppingassistantservice
kubectl wait --for=condition=available --timeout=180s deployment/pgvector

echo "Seeding pgvector from ${PRODUCTS}"
kubectl exec -i deployment/shoppingassistantservice -- \
  python seed_products.py - < "${PRODUCTS}"
