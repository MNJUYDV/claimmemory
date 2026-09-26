"""Voyage embeddings (1024 dims). Call as embeddings.embed(...) so tests can patch it."""
from functools import lru_cache

import voyageai

import config

DIMENSIONS = 1024


@lru_cache(maxsize=1)
def _client() -> voyageai.Client:
    return voyageai.Client(api_key=config.VOYAGE_API_KEY)


def embed(texts: list, input_type: str) -> list:
    """input_type is 'document' for stored text and 'query' for search queries."""
    assert input_type in ("document", "query")
    out = _client().embed(texts, model=config.VOYAGE_MODEL, input_type=input_type,
                          output_dimension=DIMENSIONS).embeddings
    assert all(len(v) == DIMENSIONS for v in out)
    return out
