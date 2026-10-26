# Replacing Gemini AI & AlloyDB with a Local Ollama Server

This guide explains how the AI Shopping Assistant (`shoppingassistantservice`) works in Online Boutique and provides a step-by-step walkthrough to replace its cloud-dependent **Google Gemini API + AlloyDB + Secret Manager** stack with your **local host Ollama server** (`http://localhost:11434`).

---

## Table of Contents

1. [How the Current Gemini Integration Works](#1-how-the-current-gemini-integration-works)
2. [What Needs to Change for Local Ollama](#2-what-needs-to-change-for-local-ollama)
3. [Step 1: Prepare Your Local Host Ollama Server](#3-step-1-prepare-your-local-host-ollama-server)
4. [Step 2: Update `shoppingassistantservice` Dependencies](#4-step-2-update-shoppingassistantservice-dependencies)
5. [Step 3: Rewrite `shoppingassistantservice.py` for Ollama](#5-step-3-rewrite-shoppingassistantservicepy-for-ollama)
6. [Step 4: Bundle `products.json` or Fetch via Product Catalog](#6-step-4-bundle-productsjson-or-fetch-via-product-catalog)
7. [Step 5: Update Kubernetes & Kustomize Manifests](#7-step-5-update-kubernetes--kustomize-manifests)
8. [Step 6: Networking — Connecting Kubernetes Pods to Host Ollama](#8-step-6-networking--connecting-kubernetes-pods-to-host-ollama)
9. [Step 7: Build, Deploy, and Test](#9-step-7-build-deploy-and-test)
10. [Optional: Using Local PostgreSQL (`pgvector`) Instead of In-Memory Vector Store](#10-optional-using-local-postgresql-pgvector-instead-of-in-memory-vector-store)

---

## 1. How the Current Gemini Integration Works

In the default repository, the AI assistant is implemented across two services:

1. **Frontend (`src/frontend/handlers.go` & `src/frontend/templates/assistant.html`)**:
   - When `ENABLE_ASSISTANT=true` is set on the `frontend` deployment, the UI displays the **Cymbal Shops Assistant** page (`/assistant`).
   - Users type a prompt and optionally attach a photo of a room (encoded in the browser as a Base64 Data URL: `data:image/jpeg;base64,...`).
   - The browser sends `POST /bot` with `{ "message": "...", "image": "data:image/..." }`.
   - `frontend` forwards this JSON payload via `HTTP POST` to `http://shoppingassistantservice:80/` and expects a JSON response `{ "content": "..." }`.
   - The browser JavaScript (`extractIdsFromString` in `src/frontend/templates/assistant.html`) extracts bracketed product IDs (e.g., `[0PUK6V6EV0]`, `[6E92ZMYYFZ]`) from the AI response and fetches `/product-meta/<id>` to render clickable product cards below the chat reply.

2. **Shopping Assistant Service (`src/shoppingassistantservice/shoppingassistantservice.py`)**:
   - **Secret Manager & AlloyDB**: On startup, it connects to Google Cloud Secret Manager (`secretmanager_v1`) to fetch the Postgres password and initializes `AlloyDBEngine` and `AlloyDBVectorStore` using `GoogleGenerativeAIEmbeddings(model="models/embedding-001")`.
   - **Step 1 (Vision)**: Calls `ChatGoogleGenerativeAI(model="gemini-1.5-flash")` with the user's uploaded room image to generate a text description of the room's interior design style.
   - **Step 2 (Vector Retrieval)**: Queries `AlloyDBVectorStore.similarity_search()` using the user prompt + room description.
   - **Step 3 (Final Generation)**: Calls `ChatGoogleGenerativeAI(model="gemini-1.5-flash")` with the room description, retrieved catalog items, and user prompt, instructing it to end the response with `[<first product ID>], [<second product ID>], [<third product ID>]`.

---

## 2. What Needs to Change for Local Ollama

If you only swap `ChatGoogleGenerativeAI` for `ChatOllama`, the container will still crash at startup unless you also decouple **Google Cloud Secret Manager** and **AlloyDB** (which require GCP credentials, VPC peering, and `google_ml_integration` embeddings).

To make the assistant run **100% locally against your host Ollama server**:

```mermaid
graph LR
    Browser([Web Browser<br/>/assistant]) -->|POST /bot<br/>message + base64 image| FE[frontend<br/>Go]
    FE -->|HTTP POST :80| SA[shoppingassistantservice<br/>Python / Flask]
    SA -->|In-Memory Vector Search| VEC[(InMemoryVectorStore<br/>seeded from products.json)]
    SA -->|HTTP :11434<br/>Vision + Embeddings + Chat| OLLAMA[[Host Ollama Server<br/>llava / llama3.2-vision + nomic-embed-text]]
```

| Component | Original (Google Cloud) | Local Replacement (Ollama) |
| :--- | :--- | :--- |
| **Vision & Chat LLM** | `ChatGoogleGenerativeAI(model="gemini-1.5-flash")` | `ChatOllama(model="llama3.2-vision")` (or `llava`, `gemma3`, `qwen2.5vl`) |
| **Embeddings Model** | `GoogleGenerativeAIEmbeddings(model="models/embedding-001")` | `OllamaEmbeddings(model="nomic-embed-text")` |
| **Vector Store** | `AlloyDBVectorStore` + GCP Secret Manager | LangChain `InMemoryVectorStore` seeded from `products.json` (or local `pgvector`) |
| **Auth / Credentials** | `GOOGLE_API_KEY` + GCP Workload Identity | None required (`OLLAMA_BASE_URL` pointing to your host) |

---

## 3. Step 1: Prepare Your Local Host Ollama Server

### 1. Pull Required Ollama Models
On your host machine, pull a **multimodal vision/chat model** (to handle both room image analysis and text recommendations) and an **embedding model** (for RAG vector search):

```bash
# Vision + Chat model (pick one that fits your GPU/RAM):
ollama pull llama3.2-vision
# Or lighter alternatives:
# ollama pull llava:7b
# ollama pull gemma3:4b

# Embedding model for product catalog similarity search:
ollama pull nomic-embed-text
```

### 2. Expose Ollama to Containers / WSL / Kubernetes
By default, Ollama binds only to `127.0.0.1:11434`. Because `shoppingassistantservice` runs inside a Kubernetes pod (in `kind`, `minikube`, or Docker Desktop), Ollama must listen on all network interfaces (`0.0.0.0`) so the container can reach your host:

- **Linux / WSL (systemd)**:
  ```bash
  OLLAMA_HOST=0.0.0.0:11434 ollama serve
  ```
- **Windows / macOS Desktop App**:
  Set the user environment variable `OLLAMA_HOST=0.0.0.0:11434` and restart the Ollama application.

Verify Ollama is reachable from your terminal:
```bash
curl http://localhost:11434/api/tags
```

---

## 4. Step 2: Update `shoppingassistantservice` Dependencies

Update `src/shoppingassistantservice/requirements.in` to replace the Google Cloud packages with `langchain-ollama` and `langchain-core`:

```ini
flask==3.1.3
langchain==1.2.0
langchain-core>=0.3.0
langchain-ollama>=0.2.0
pillow==12.3.0
```

Then regenerate or update `src/shoppingassistantservice/requirements.txt`:

```bash
# If you use pip-compile:
cd src/shoppingassistantservice
pip-compile requirements.in -o requirements.txt
```

*(Alternatively, for rapid local development, you can edit `src/shoppingassistantservice/Dockerfile` to run `pip install -r requirements.in` directly instead of the pinned `requirements.txt`.)*

---

## 5. Step 3: Rewrite `shoppingassistantservice.py` for Ollama

Replace the contents of `src/shoppingassistantservice/shoppingassistantservice.py` with the following implementation. It handles:
- Loading products from a local `products.json` file into an `InMemoryVectorStore` using `OllamaEmbeddings`.
- Supporting **both** image+text queries (using the Vision step) and text-only queries (skipping the Vision step if the user didn't upload an image).
- Connecting to your local Ollama server via `OLLAMA_BASE_URL`.

```python
#!/usr/bin/python
import json
import os
from urllib.parse import unquote
from flask import Flask, request, jsonify

from langchain_core.documents import Document
from langchain_core.messages import HumanMessage
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_ollama import ChatOllama, OllamaEmbeddings

# Configuration via Environment Variables with sensible local defaults
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_CHAT_MODEL = os.environ.get("OLLAMA_CHAT_MODEL", "llama3.2-vision")
OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", OLLAMA_CHAT_MODEL)
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "nomic-embed-text")
PRODUCTS_JSON_PATH = os.environ.get("PRODUCTS_JSON_PATH", "products.json")


def init_vectorstore() -> InMemoryVectorStore:
    """Loads products.json and indexes product descriptions in an in-memory vector store."""
    print(f"Initializing OllamaEmbeddings ({OLLAMA_EMBED_MODEL}) at {OLLAMA_BASE_URL}...")
    embeddings = OllamaEmbeddings(
        base_url=OLLAMA_BASE_URL,
        model=OLLAMA_EMBED_MODEL,
    )

    with open(PRODUCTS_JSON_PATH, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    docs = []
    for p in catalog.get("products", []):
        categories = ", ".join(p.get("categories", []))
        page_content = f"{p['name']}: {p['description']} (Categories: {categories})"
        docs.append(
            Document(
                page_content=page_content,
                metadata={
                    "id": p["id"],
                    "name": p["name"],
                    "description": p["description"],
                    "categories": categories,
                },
            )
        )

    print(f"Indexing {len(docs)} catalog products...")
    return InMemoryVectorStore.from_documents(docs, embedding=embeddings)


def create_app():
    app = Flask(__name__)
    vectorstore = None

    def get_vectorstore():
        nonlocal vectorstore
        if vectorstore is None:
            vectorstore = init_vectorstore()
        return vectorstore

    @app.route("/", methods=["POST"])
    def talkToOllama():
        print("Beginning local Ollama RAG call")
        payload = request.get_json(force=True)
        prompt = unquote(payload.get("message", ""))
        image_data_url = payload.get("image")

        # Step 1 – Optional room description from Ollama Vision model (if user uploaded an image)
        description_response = "No room image was provided by the customer."
        if image_data_url and isinstance(image_data_url, str) and image_data_url.strip():
            print(f"Invoking Ollama vision model ({OLLAMA_VISION_MODEL})...")
            llm_vision = ChatOllama(
                base_url=OLLAMA_BASE_URL,
                model=OLLAMA_VISION_MODEL,
                temperature=0.2,
            )
            vision_message = HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": "You are a professional interior designer, give me a detailed description of the style of the room in this image",
                    },
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ]
            )
            vision_result = llm_vision.invoke([vision_message])
            description_response = vision_result.content
            print(f"Vision description: {description_response}")

        # Step 2 – Similarity search with the description & user prompt
        vector_search_prompt = (
            f"This is the user's request: {prompt} "
            f"Find the most relevant items for that prompt, while matching style of the room described here: {description_response}"
        )
        vs = get_vectorstore()
        docs = vs.similarity_search(vector_search_prompt, k=4)
        print(f"Retrieved {len(docs)} relevant catalog documents")

        relevant_docs = "\n".join(
            [
                f"- ID: [{d.metadata['id']}], Name: {d.metadata['name']}, Description: {d.metadata['description']}, Categories: {d.metadata['categories']}"
                for d in docs
            ]
        )

        # Step 3 – Tie it all together by augmenting our call to Ollama Chat model
        llm = ChatOllama(
            base_url=OLLAMA_BASE_URL,
            model=OLLAMA_CHAT_MODEL,
            temperature=0.4,
        )
        design_prompt = (
            f"You are an interior designer that works for Online Boutique. "
            f"You are tasked with providing recommendations to a customer on what they should add to a given room from our catalog.\n"
            f"This is the description of the room:\n{description_response}\n\n"
            f"Here are a list of products that are relevant to it:\n{relevant_docs}\n\n"
            f"Specifically, this is what the customer has asked for, see if you can accommodate it: {prompt}\n"
            f"Start by repeating a brief description of the room's design to the customer, then provide your recommendations. "
            f"Do your best to pick the most relevant item out of the list of products provided, but if none of them seem relevant, then say that instead of inventing a new product. "
            f"IMPORTANT: At the very end of your response, on a new line, output ONLY the IDs of the recommended products in exact square brackets like this: [<first product ID>], [<second product ID>], [<third product ID>]"
        )

        design_response = llm.invoke(design_prompt)
        print("Final Ollama response:", design_response.content)

        return jsonify({"content": design_response.content})

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
```

> **Why is the bracket format `[<product ID>]` critical?**
> In `src/frontend/templates/assistant.html`, the function `extractIdsFromString(message)` uses the regex `/\[([a-zA-Z0-9-]+)\]/g` to find product IDs in the response and automatically fetch `/product-meta/<id>` to render product cards. Ensuring the prompt clearly formats IDs as `[0PUK6V6EV0]` guarantees the UI renders the product cards identically to Gemini.

---

## 6. Step 4: Bundle `products.json` or Fetch via Product Catalog

Because `shoppingassistantservice` is built with `context: src/shoppingassistantservice` in `skaffold.yaml`, copy `src/productcatalogservice/products.json` into `src/shoppingassistantservice/`:

```bash
cp src/productcatalogservice/products.json src/shoppingassistantservice/products.json
```

Since `src/shoppingassistantservice/Dockerfile` runs `COPY . .`, `products.json` will automatically be included inside `/shoppingassistantservice/products.json` in the container image.

---

## 7. Step 5: Update Kubernetes & Kustomize Manifests

### 1. Update `kustomize/components/shopping-assistant/shoppingassistantservice.yaml`
Remove the Google Cloud Secret Manager / AlloyDB / API Key environment variables and Workload Identity annotations, and replace them with the Ollama configuration:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: shoppingassistantservice
  labels:
    app: shoppingassistantservice
spec:
  selector:
    matchLabels:
      app: shoppingassistantservice
  template:
    metadata:
      labels:
        app: shoppingassistantservice
    spec:
      serviceAccountName: shoppingassistantservice
      terminationGracePeriodSeconds: 5
      securityContext:
        fsGroup: 1000
        runAsGroup: 1000
        runAsNonRoot: true
        runAsUser: 1000
      containers:
      - name: server
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop:
              - ALL
          privileged: false
          readOnlyRootFilesystem: false
        image: shoppingassistantservice
        ports:
        - name: http
          containerPort: 8080
        env:
        - name: OLLAMA_BASE_URL
          value: "http://host.docker.internal:11434" # Or http://host.minikube.internal:11434 on Minikube
        - name: OLLAMA_CHAT_MODEL
          value: "llama3.2-vision"
        - name: OLLAMA_VISION_MODEL
          value: "llama3.2-vision"
        - name: OLLAMA_EMBED_MODEL
          value: "nomic-embed-text"
        resources:
          requests:
            cpu: 100m
            memory: 256Mi
          limits:
            cpu: 500m
            memory: 512Mi
---
apiVersion: v1
kind: Service
metadata:
  name: shoppingassistantservice
  labels:
    app: shoppingassistantservice
spec:
  type: ClusterIP
  selector:
    app: shoppingassistantservice
  ports:
  - name: http
    port: 80
    targetPort: 8080
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: shoppingassistantservice
```

*(Note: Memory limits are increased from `128Mi` to `512Mi` to comfortably hold the Python runtime and LangChain in-memory vector store.)*

### 2. Enable the `shopping-assistant` Component in `kubernetes-manifests/kustomization.yaml`
In `kubernetes-manifests/kustomization.yaml` (or `kustomize/kustomization.yaml`), uncomment **only** the `shopping-assistant` component (you no longer need the `alloydb` component!):

```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
- adservice.yaml
- cartservice.yaml
- checkoutservice.yaml
- currencyservice.yaml
- emailservice.yaml
- frontend.yaml
- loadgenerator.yaml
- paymentservice.yaml
- productcatalogservice.yaml
- recommendationservice.yaml
- shippingservice.yaml
components:
- ../kustomize/components/shopping-assistant
```

---

## 8. Step 6: Networking — Connecting Kubernetes Pods to Host Ollama

Depending on how you run your local Kubernetes cluster, use the matching `OLLAMA_BASE_URL` in `shoppingassistantservice.yaml`:

| Local Kubernetes Environment | `OLLAMA_BASE_URL` to Use | Notes |
| :--- | :--- | :--- |
| **Docker Desktop Kubernetes** | `http://host.docker.internal:11434` | Works out of the box on Windows, macOS, and WSL2. |
| **Kind (Kubernetes in Docker)** | `http://host.docker.internal:11434` | Docker 20.10+ resolves `host.docker.internal` inside Kind nodes automatically, or use the `kind` Docker bridge gateway IP (`docker network inspect kind`). |
| **Minikube** | `http://host.minikube.internal:11434` | Built-in Minikube DNS name that resolves to your host machine IP. |
| **WSL2 (Native Linux Cluster)** | `http://<WSL_OR_HOST_IP>:11434` | Run `ip route show | awk '/default/ {print $3}'` inside WSL2 to get the Windows host IP if Ollama runs on Windows, or `hostname -I` if Ollama runs inside WSL2. |

> **Tip: Verify Connectivity from Inside the Cluster**
> Once deployed, you can verify that the pod can reach your host Ollama server:
> ```bash
> kubectl exec -it deployment/shoppingassistantservice -- python -c "import urllib.request; print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags').read().decode())"
> ```

---

## 9. Step 7: Build, Deploy, and Test

1. **Run Skaffold to build and deploy**:
   ```bash
   skaffold run
   ```
   Or for live-reload development:
   ```bash
   skaffold dev --port-forward
   ```

2. **Forward the Frontend Port** (if using `skaffold run`):
   ```bash
   kubectl port-forward deployment/frontend 8080:8080
   ```

3. **Test the Assistant UI**:
   - Navigate to `http://localhost:8080` in your browser.
   - Click the **"Cymbal Shops Assistant"** button in the top navigation bar (or go directly to `http://localhost:8080/assistant`).
   - Upload any room photo (or leave the file input blank) and type a prompt such as:
     > *"I want something for my kitchen counter or table decor"*
   - Click **Send** and watch the logs of `shoppingassistantservice`:
     ```bash
     kubectl logs -f deployment/shoppingassistantservice
     ```
   - Your local Ollama server will analyze the room, search the embedded product catalog (`Candle Holder [0PUK6V6EV0]`, `Bamboo Glass Jar [9SIQT8TOJO]`, `Mug [6E92ZMYYFZ]`, `Salt & Pepper Shakers [LS4PSXUNUM]`), and return recommendations with clickable product cards.

---

## 10. Optional: Using Local PostgreSQL (`pgvector`) Instead of In-Memory Vector Store

If you also want to test a real PostgreSQL `pgvector` database locally instead of `InMemoryVectorStore` (replicating the AlloyDB architecture without GCP):

1. Deploy a `pgvector/pgvector:pg16` container in your Kubernetes cluster.
2. Add `langchain-postgres` and `psycopg[binary]` to `src/shoppingassistantservice/requirements.in`.
3. Replace `InMemoryVectorStore` with `PGVector`:
   ```python
   from langchain_postgres import PGVector

   connection = os.environ.get(
       "PGVECTOR_CONNECTION_STRING",
       "postgresql+psycopg://postgres:postgres@pgvector-service:5432/products",
   )
   vectorstore = PGVector.from_documents(
       documents=docs,
       embedding=OllamaEmbeddings(base_url=OLLAMA_BASE_URL, model=OLLAMA_EMBED_MODEL),
       collection_name="catalog_items",
       connection=connection,
   )
   ```
   *(Important: When creating a custom `vector(N)` column in PostgreSQL, note that `nomic-embed-text` produces **768-dimensional** vectors—the same dimension `VECTOR(768)` used in `kustomize/components/shopping-assistant/scripts/2_create_populate_alloydb_tables.sh`—whereas `mxbai-embed-large` produces **1024-dimensional** vectors.)*
