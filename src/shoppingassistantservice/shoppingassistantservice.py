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

import os
import threading
import time

from urllib.parse import unquote
from langchain_core.messages import HumanMessage
from flask import Flask, request

import assistant_backends

RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "4"))

# The LLM client and the vector store are built on first use rather than at import, so
# the pod comes up (and stays up) while pgvector or the inference host are still
# starting. Each is cached once built.
_lock = threading.Lock()
_chat_model = None
_vectorstore = None


def chat_model():
    global _chat_model
    with _lock:
        if _chat_model is None:
            _chat_model = assistant_backends.get_chat_model()
        return _chat_model


def vectorstore():
    global _vectorstore
    with _lock:
        if _vectorstore is None:
            _vectorstore = assistant_backends.get_vectorstore()
        return _vectorstore


def create_app():
    app = Flask(__name__)
    print(f"Shopping assistant configuration: {assistant_backends.describe_config()}")

    @app.route("/healthz", methods=["GET"])
    def healthz():
        # Deliberately does not touch the LLM or the database: this reports that the
        # process is serving, not that every dependency is reachable.
        return {"status": "ok"}

    @app.route("/", methods=['POST'])
    def talkToLLM():
        print("Beginning RAG call")
        started = time.monotonic()
        prompt = request.json['message']
        prompt = unquote(prompt)
        image = request.json.get('image')

        llm = chat_model()

        # Step 1 - Get a room description from the vision model. The frontend sends the
        # uploaded photo as a base64 data URI, which every supported backend accepts.
        if image:
            message = HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": "You are a professional interior designer, give me a detailed decsription of the style of the room in this image",
                    },
                    {"type": "image_url", "image_url": image},
                ]
            )
            vision_started = time.monotonic()
            response = llm.invoke([message])
            print(f"Description step took {time.monotonic() - vision_started:.1f}s:")
            print(response)
            description_response = response.content
        else:
            print("No image supplied; skipping the vision step")
            description_response = ""

        # Step 2 - Similarity search with the description & user prompt
        vector_search_prompt = f""" This is the user's request: {prompt} Find the most relevant items for that prompt, while matching style of the room described here: {description_response} """
        print(vector_search_prompt)

        docs = vectorstore().similarity_search(vector_search_prompt, k=RAG_TOP_K)
        print(f"Vector search: {description_response}")
        print(f"Retrieved documents: {len(docs)}")
        #Prepare relevant documents for inclusion in final prompt
        relevant_docs = ""
        for doc in docs:
            doc_details = doc.to_json()
            print(f"Adding relevant document to prompt context: {doc_details}")
            relevant_docs += str(doc_details) + ", "

        # Step 3 - Tie it all together by augmenting our call to the LLM
        design_prompt = (
            f" You are an interior designer that works for Online Boutique. You are tasked with providing recommendations to a customer on what they should add to a given room from our catalog. This is the description of the room: \n"
            f"{description_response} Here are a list of products that are relevant to it: {relevant_docs} Specifically, this is what the customer has asked for, see if you can accommodate it: {prompt} Start by repeating a brief description of the room's design to the customer, then provide your recommendations. Do your best to pick the most relevant item out of the list of products provided, but if none of them seem relevant, then say that instead of inventing a new product. At the end of the response, add a list of the IDs of the relevant products in the following format for the top 3 results: [<first product ID>], [<second product ID>], [<third product ID>] ")
        print("Final design prompt: ")
        print(design_prompt)
        design_started = time.monotonic()
        design_response = llm.invoke(
            design_prompt
        )
        print(f"Design step took {time.monotonic() - design_started:.1f}s")
        print(f"Total RAG call took {time.monotonic() - started:.1f}s")

        data = {'content': design_response.content}
        return data

    return app

if __name__ == "__main__":
    # Create an instance of flask server when called directly
    app = create_app()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", "8080")))
