#!/usr/bin/python
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

"""Load the product catalog into the pgvector collection used by the shopping assistant.

Replaces the AlloyDB flow (generate_sql_from_products.py plus hand-written INSERTs and
an in-database embedding() call): PGVector owns its own schema, so the products go in
through add_texts() and the configured embedding model computes the vectors.

Re-running is safe. Rows are upserted by product ID, and if the collection was built
with a different embedding model -- which would leave vectors of the wrong dimension --
it is dropped and rebuilt instead of mixing them.

Usage:
    python seed_products.py [products.json | -]      # "-" reads stdin
"""

import json
import os
import sys
import time

from sqlalchemy import create_engine, text

import assistant_backends

DEFAULT_PRODUCTS_PATH = "/catalog/products.json"
DB_WAIT_SECONDS = int(os.environ.get("SEED_DB_WAIT_SECONDS", "120"))


def load_products(source):
    """Read products.json from a path, or from stdin when source is "-"."""
    if source == "-":
        data = json.load(sys.stdin)
    else:
        with open(source, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    products = data["products"]
    if not products:
        raise ValueError(f"no products found in {source}")
    return products


def to_documents(products):
    """Convert catalog entries into texts/metadatas/ids for the vector store.

    The text is the product description and the metadata keeps the id, name and
    comma-joined categories, matching what the AlloyDB table exposed, because the final
    RAG prompt is built from these fields and the frontend parses [productID] markers
    out of the answer.
    """
    texts, metadatas, ids = [], [], []
    for product in products:
        texts.append(product["description"])
        metadatas.append(
            {
                "id": product["id"],
                "name": product["name"],
                "categories": ",".join(product.get("categories", [])),
            }
        )
        ids.append(product["id"])
    return texts, metadatas, ids


def wait_for_database(url, timeout_seconds):
    """Block until Postgres accepts a connection, so the seeder can run right after deploy."""
    engine = create_engine(url, pool_pre_ping=True)
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return engine
        except Exception as error:  # noqa: BLE001 - any driver error means "not ready yet"
            last_error = error
            print(f"Waiting for Postgres at {engine.url.host}:{engine.url.port} ...")
            time.sleep(3)
    raise RuntimeError(f"database never became ready: {last_error}")


def existing_collection_metadata(engine, collection):
    """Return the stored cmetadata for the collection, or None if it does not exist yet."""
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT cmetadata FROM langchain_pg_collection WHERE name = :name"),
                {"name": collection},
            ).first()
    except Exception as error:  # noqa: BLE001 - table absent on a fresh database
        print(f"No existing collection metadata ({error.__class__.__name__})")
        return None
    if row is None:
        return None
    return row[0] or {}


def count_rows(engine, collection):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT count(*) FROM langchain_pg_embedding e "
                "JOIN langchain_pg_collection c ON c.uuid = e.collection_id "
                "WHERE c.name = :name"
            ),
            {"name": collection},
        ).scalar_one()


def main(argv):
    source = argv[1] if len(argv) > 1 else DEFAULT_PRODUCTS_PATH
    config = assistant_backends.describe_config()
    print(f"Seeding with configuration: {config}")

    products = load_products(source)
    texts, metadatas, ids = to_documents(products)
    print(f"Loaded {len(products)} products from {source}")

    # Embedding the first document also proves the embedding model is actually available
    # on the inference host, and tells us the dimension without hardcoding it.
    embeddings = assistant_backends.get_embeddings()
    probe_started = time.monotonic()
    dimension = len(embeddings.embed_query(texts[0]))
    print(
        f"Embedding model {config['embedding_model']} returned {dimension} dimensions "
        f"in {time.monotonic() - probe_started:.1f}s"
    )

    collection = assistant_backends.collection_name()
    url = assistant_backends.connection_url()
    engine = wait_for_database(url, DB_WAIT_SECONDS)

    stored = existing_collection_metadata(engine, collection)
    desired = {
        "embedding_backend": config["embedding_backend"],
        "embedding_model": config["embedding_model"],
        "embedding_dimension": dimension,
    }
    rebuild = False
    if stored:
        mismatched = {
            key: (stored.get(key), value)
            for key, value in desired.items()
            if stored.get(key) != value
        }
        if mismatched:
            print(
                f"Collection {collection!r} was built with different settings {mismatched}; "
                "dropping it so vectors of different dimensions are not mixed"
            )
            rebuild = True

    vectorstore = assistant_backends.get_vectorstore(
        embeddings=embeddings,
        pre_delete_collection=rebuild,
        collection_metadata=desired,
    )

    add_started = time.monotonic()
    vectorstore.add_texts(texts=texts, metadatas=metadatas, ids=ids)
    print(f"Upserted {len(ids)} products in {time.monotonic() - add_started:.1f}s")
    print(f"Collection {collection!r} now holds {count_rows(engine, collection)} rows")

    sample = vectorstore.similarity_search("something comfortable for a cozy living room", k=3)
    print("Sanity-check search returned:")
    for doc in sample:
        print(f"  {doc.metadata.get('id')} {doc.metadata.get('name')}: {doc.page_content}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
