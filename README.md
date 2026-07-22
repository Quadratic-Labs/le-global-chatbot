# L&E Global Legal Chatbot

Secure RAG-based legal chatbot for L&E Global, designed to answer employment-law questions using validated legal documents.

## Objective

The chatbot will provide answers based exclusively on approved L&E Global content, including:

- Employment law by country
- Country comparisons
- Legal topics and practical guidance
- Contact details of L&E Global member firms

Questions outside the available knowledge base will be redirected to the relevant local legal contact.

## Technology Stack

- Python
- FastAPI
- OpenSearch
- OpenSearch Dashboards
- Redis
- Docker
- Docker Compose

## Current Architecture

```text
User / WordPress
        |
        v
FastAPI Backend
        |
        +---- OpenSearch
        |
        +---- Redis


