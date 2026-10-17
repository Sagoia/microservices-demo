# Shopping assistant, self-hosted

This component runs the optional [shopping assistant](../shopping-assistant/README.md)
feature with **no Google Cloud dependencies**: a self-hosted Ollama server does the
inference, and Postgres with the `pgvector` extension replaces AlloyDB as the vector
store. It is meant for a local cluster (`kind`, minikube, Docker Desktop).

The upstream `shopping-assistant` component is unchanged and still the right choice on
GKE; it needs a GCP project with owner access, a GKE Autopilot cluster, AlloyDB, Secret
Manager and billing.

| | upstream `shopping-assistant` | this component |
|---|---|---|
| Vector store | AlloyDB (`AlloyDBVectorStore`) | `pgvector/pgvector:pg17` in-cluster (`PGVector`) |
| Chat + vision model | Gemini 1.5 Flash | any Ollama model (default `gemma4:latest`) |
| Embeddings | `models/embedding-001` | any Ollama model (default `embeddinggemma:latest`) |
| DB password | Secret Manager | Kubernetes Secret |
| Identity | GKE Workload Identity service account | plain ServiceAccount |

Like upstream, the assistant stays **opt-in**: `kubernetes-manifests/kustomization.yaml`
is untouched, so the default deployment does not include it.

## Requirements

An [Ollama](https://ollama.com) server the cluster can reach, with a chat model that
supports vision and an embedding model:

```bash
ollama pull gemma4            # chat + vision
ollama pull embeddinggemma    # embeddings
```

Any host works — another machine on the LAN, or Ollama on the Docker host. Point
`OLLAMA_BASE_URL` at it in [shoppingassistantservice.yaml](shoppingassistantservice.yaml);
the default is `http://192.168.86.62:11434`. From a `kind` cluster, a LAN address
routes out of the node with no extra networking.

## Deploy

With skaffold (builds the image and renders the overlay in one step):

```bash
skaffold run -p arm64-assistant     # on an arm64 host; use -p shopping-assistant-local elsewhere
```

Or against images you already have:

```bash
kubectl apply -k kustomize/overlays/shopping-assistant-local
```

That overlay bases on `kustomize/overlays/dev-arm64`, so it also carries the
single-node fixes: `Recreate` rollouts (the default `RollingUpdate` surge rounds up to
one extra pod per service, which double-schedules the whole app on one node and
deadlocks) and slow-start probes for the Python services.

If the base app is already running and should not be restarted, deploy only the two new
workloads:

```bash
kubectl apply -k kustomize/overlays/shopping-assistant-only
kubectl set env deployment/frontend ENABLE_ASSISTANT=true
```

Then load the catalog into pgvector:

```bash
./kustomize/components/shopping-assistant-local/scripts/seed_pgvector.sh
```

The seeder runs [`seed_products.py`](../../../src/shoppingassistantservice/seed_products.py)
inside the service pod, so it uses exactly the same embedding model and database
settings as the service. It streams
[`products.json`](../../../src/productcatalogservice/products.json) in on stdin, upserts
by product ID — so re-running it is safe — and rebuilds the collection from scratch if
the embedding model changed, since vectors of two different dimensions cannot share a
collection. Nothing in the code or manifests assumes a particular dimension; it is
whatever the configured model returns.

## Configuration

Set on the `shoppingassistantservice` Deployment:

| Variable | Default | Notes |
|---|---|---|
| `LLM_BACKEND` | `ollama` | `ollama`, `openai` or `google` |
| `EMBEDDING_BACKEND` | same as `LLM_BACKEND` | so embeddings can live elsewhere |
| `OLLAMA_BASE_URL` | `http://192.168.86.62:11434` | |
| `LLM_MODEL` | `gemma4:latest` | must support vision |
| `EMBEDDING_MODEL` | `embeddinggemma:latest` | |
| `LLM_NUM_PREDICT` / `LLM_NUM_CTX` | `1024` / `8192` | Generation limits. Too low a value truncates the answer before the trailing `[productID]` list the frontend parses. |
| `OLLAMA_REASONING` | `false` | `true` enables thinking; `unset` leaves it to the model |
| `PGVECTOR_HOST` / `_PORT` / `_DATABASE` / `_USER` | `pgvector` / `5432` / `products` / `postgres` | |
| `PGVECTOR_PASSWORD` | from the `pgvector-credentials` Secret | |
| `PGVECTOR_URL` | — | full SQLAlchemy URL; overrides the four settings above |
| `PGVECTOR_COLLECTION` | `products` | |
| `RAG_TOP_K` | `4` | documents retrieved per request |

### Other backends

No rebuild is needed — all three client libraries are in the image:

```yaml
# Any OpenAI-compatible endpoint (vLLM, llama.cpp, LM Studio, OpenAI itself)
- name: LLM_BACKEND
  value: "openai"
- name: OPENAI_BASE_URL
  value: "http://vllm:8000/v1"
- name: OPENAI_API_KEY
  value: "..."
- name: LLM_MODEL
  value: "..."
```

```yaml
# Gemini, as upstream does it
- name: LLM_BACKEND
  value: "google"
- name: GOOGLE_API_KEY
  value: "..."
```

Re-run the seeder after changing the embedding model.

## Verify

```bash
kubectl get pods -l app=shoppingassistantservice
kubectl logs deployment/shoppingassistantservice

# Ask it something, with a room photo as a base64 data URI
kubectl port-forward deployment/shoppingassistantservice 8080:8080
curl -s localhost:8080 -H 'Content-Type: application/json' \
  -d "{\"message\":\"What should I add to this room?\",\"image\":\"data:image/jpeg;base64,$(base64 -w0 room.jpg)\"}"
```

`GET /healthz` reports that the process is serving; it deliberately does not touch the
database or the inference host, so the pod stays up while those are still starting.
The LLM client and the vector store are created on first request and cached.

The frontend needs no code change: it already shows the assistant when
`ENABLE_ASSISTANT` is `true` (this component patches that in) and calls the address in
`SHOPPING_ASSISTANT_SERVICE_ADDR`, which `kubernetes-manifests/frontend.yaml` already
sets.
