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

"""Pluggable LLM, embedding and vector-store backends for the shopping assistant.

Every backend is selected by environment variable, so the same image can talk to a
self-hosted Ollama server (the default), any OpenAI-compatible endpoint, or the
Gemini API without a code change:

    LLM_BACKEND        ollama (default) | openai | google
    EMBEDDING_BACKEND  defaults to LLM_BACKEND

The vector store is always Postgres + pgvector over plain SQL, so it works the same
against an in-cluster pgvector container or a managed Postgres.
"""

import os
from urllib.parse import quote_plus

# The remote GPU box that serves inference for the local deployment.
DEFAULT_OLLAMA_BASE_URL = "http://192.168.86.62:11434"
DEFAULT_MODELS = {
    "ollama": {"chat": "gemma4:latest", "embedding": "embeddinggemma:latest"},
    "openai": {"chat": "gpt-4o-mini", "embedding": "text-embedding-3-small"},
    "google": {"chat": "gemini-1.5-flash", "embedding": "models/embedding-001"},
}


def _env(name, default=None):
    """Read an environment variable, treating empty/whitespace as unset."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _int_env(name, default):
    value = _env(name)
    return int(value) if value is not None else default


def _bool_env(name, default):
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def chat_backend():
    return _env("LLM_BACKEND", "ollama").lower()


def embedding_backend():
    return _env("EMBEDDING_BACKEND", chat_backend()).lower()


def _model_for(backend, kind, env_name):
    configured = _env(env_name)
    if configured:
        return configured
    try:
        return DEFAULT_MODELS[backend][kind]
    except KeyError:
        raise ValueError(
            f"unknown backend {backend!r}; expected one of {sorted(DEFAULT_MODELS)}"
        ) from None


def chat_model_name():
    return _model_for(chat_backend(), "chat", "LLM_MODEL")


def embedding_model_name():
    return _model_for(embedding_backend(), "embedding", "EMBEDDING_MODEL")


def get_chat_model():
    """Build the chat model selected by LLM_BACKEND."""
    backend = chat_backend()
    model = chat_model_name()

    if backend == "ollama":
        from langchain_ollama import ChatOllama

        kwargs = {
            "model": model,
            "base_url": _env("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
            "temperature": float(_env("LLM_TEMPERATURE", "0.2")),
            "num_predict": _int_env("LLM_NUM_PREDICT", 512),
            "num_ctx": _int_env("LLM_NUM_CTX", 8192),
        }
        # gemma4 advertises a "thinking" mode. It costs latency and the answers here are
        # short, so it is off by default; set OLLAMA_REASONING=unset to leave it to the
        # model (useful for backends that reject an explicit think flag).
        reasoning = _env("OLLAMA_REASONING", "false").lower()
        if reasoning != "unset":
            kwargs["reasoning"] = reasoning in ("1", "true", "yes", "on")
        timeout = _env("OLLAMA_TIMEOUT_SECONDS")
        if timeout:
            kwargs["client_kwargs"] = {"timeout": float(timeout)}
        return ChatOllama(**kwargs)

    if backend == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model,
            base_url=_env("OPENAI_BASE_URL"),
            api_key=_env("OPENAI_API_KEY", "not-needed"),
            temperature=float(_env("LLM_TEMPERATURE", "0.2")),
            max_tokens=_int_env("LLM_NUM_PREDICT", 512),
        )

    if backend == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model)

    raise ValueError(f"unsupported LLM_BACKEND {backend!r}")


def get_embeddings():
    """Build the embedding model selected by EMBEDDING_BACKEND."""
    backend = embedding_backend()
    model = embedding_model_name()

    if backend == "ollama":
        from langchain_ollama import OllamaEmbeddings

        return OllamaEmbeddings(
            model=model,
            base_url=_env("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
        )

    if backend == "openai":
        from langchain_openai import OpenAIEmbeddings

        return OpenAIEmbeddings(
            model=model,
            base_url=_env("OPENAI_BASE_URL"),
            api_key=_env("OPENAI_API_KEY", "not-needed"),
        )

    if backend == "google":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings

        return GoogleGenerativeAIEmbeddings(model=model)

    raise ValueError(f"unsupported EMBEDDING_BACKEND {backend!r}")


def collection_name():
    return _env("PGVECTOR_COLLECTION", "products")


def connection_url():
    """SQLAlchemy URL for the pgvector database.

    PGVECTOR_URL wins if set; otherwise it is assembled from the individual settings so
    the password can come from a Kubernetes Secret on its own.
    """
    url = _env("PGVECTOR_URL")
    if url:
        return url
    user = quote_plus(_env("PGVECTOR_USER", "postgres"))
    password = quote_plus(_env("PGVECTOR_PASSWORD", ""))
    host = _env("PGVECTOR_HOST", "pgvector")
    port = _int_env("PGVECTOR_PORT", 5432)
    database = _env("PGVECTOR_DATABASE", "products")
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{database}"


def get_vectorstore(embeddings=None, pre_delete_collection=False, collection_metadata=None):
    """Open (and create if needed) the pgvector collection.

    The embedding column is left unconstrained, so its dimension is whatever the
    configured embedding model produces -- nothing here assumes 768.
    """
    from langchain_postgres import PGVector

    return PGVector(
        embeddings=embeddings if embeddings is not None else get_embeddings(),
        collection_name=collection_name(),
        connection=connection_url(),
        use_jsonb=True,
        create_extension=_bool_env("PGVECTOR_CREATE_EXTENSION", True),
        pre_delete_collection=pre_delete_collection,
        collection_metadata=collection_metadata,
        engine_args={"pool_pre_ping": True, "pool_recycle": 300},
    )


def describe_config():
    """A log-safe summary of the active configuration (no passwords)."""
    return {
        "llm_backend": chat_backend(),
        "llm_model": chat_model_name(),
        "embedding_backend": embedding_backend(),
        "embedding_model": embedding_model_name(),
        "ollama_base_url": _env("OLLAMA_BASE_URL", DEFAULT_OLLAMA_BASE_URL),
        "pgvector_host": _env("PGVECTOR_HOST", "pgvector"),
        "pgvector_database": _env("PGVECTOR_DATABASE", "products"),
        "collection": collection_name(),
    }
