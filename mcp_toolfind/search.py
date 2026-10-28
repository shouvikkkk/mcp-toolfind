"""BM25 search over tool names + descriptions.

MODEL / APPROXIMATION: real clients do not rank tools with BM25 -- a
language model decides what to call. BM25 is used here as a transparent,
deterministic proxy for "would a keyword-based search surface this tool
for this intent?". A tool that BM25 cannot surface in the top 10 is very
likely also hard for a model to find when its name does not match the
user's vocabulary; a tool BM25 does surface is not guaranteed to be
chosen by a model. The report says this wherever rankings appear.

Tokenization: split on underscores, hyphens and camelCase boundaries,
lowercase, drop a small English stopword list, and lightly singularize
(``invoices`` -> ``invoice``) so plural forms still match intent words.
Name tokens are repeated NAME_BOOST times in the document: this models
the well-documented effect that a matching word in the *name* matters
far more than the same word buried in a long description.
"""

import math
import re

STOPWORDS = frozenset({
    "a", "an", "the", "to", "for", "of", "in", "on", "with", "and", "or",
    "me", "my", "please", "can", "you", "i", "is", "it", "do", "get",
    "by", "at", "from", "into", "over", "up", "out",
})

NAME_BOOST = 3


def _singularize(token):
    if len(token) > 3 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text):
    """Split text into search tokens (see module docstring)."""
    if not text:
        return []
    # camelCase / PascalCase boundaries, including acronym runs: XMLParser
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = re.sub(r"[^A-Za-z0-9]+", " ", text)
    tokens = []
    for raw in text.split():
        token = _singularize(raw.lower())
        if token and token not in STOPWORDS:
            tokens.append(token)
    return tokens


def document_tokens(tool):
    """Tokens for one tool: name tokens boosted, then description, then
    inputSchema property names (once)."""
    name = tokenize(tool.get("name", ""))
    doc = []
    for token in name:
        doc.extend([token] * NAME_BOOST)
    doc.extend(tokenize(tool.get("description", "")))
    schema = tool.get("inputSchema") or {}
    if isinstance(schema, dict):
        props = schema.get("properties") or {}
        if isinstance(props, dict):
            for prop_name in props:
                doc.extend(tokenize(str(prop_name)))
    return doc


class BM25:
    """Standard BM25 (k1=1.5, b=0.75) over pre-tokenized documents."""

    def __init__(self, documents, k1=1.5, b=0.75):
        self.documents = [list(d) for d in documents]
        self.k1 = k1
        self.b = b
        self.n_docs = len(self.documents)
        self.doc_lens = [len(d) for d in self.documents]
        self.avgdl = (sum(self.doc_lens) / self.n_docs) if self.n_docs else 0.0
        df = {}
        for doc in self.documents:
            for term in set(doc):
                df[term] = df.get(term, 0) + 1
        self.idf = {}
        for term, freq in df.items():
            # Robertson / Sparck-Jones IDF with +1 smoothing (never negative).
            self.idf[term] = math.log(1.0 + (self.n_docs - freq + 0.5) / (freq + 0.5))

    def score(self, query_tokens, doc_index):
        doc = self.documents[doc_index]
        if not doc or not query_tokens:
            return 0.0
        freq = {}
        for token in doc:
            freq[token] = freq.get(token, 0) + 1
        dl = self.doc_lens[doc_index]
        denom_base = 1.0 - self.b + self.b * (dl / self.avgdl) if self.avgdl else 1.0
        total = 0.0
        for term in query_tokens:
            f = freq.get(term, 0)
            if not f:
                continue
            idf = self.idf.get(term, 0.0)
            total += idf * (f * (self.k1 + 1.0)) / (f + self.k1 * denom_base)
        return total

    def rank(self, query_tokens):
        """All doc indices sorted by score desc, catalog order on ties.

        Returns a list of (doc_index, score). Zero-score docs are kept
        (at the tail) so callers can report a tool's exact rank.
        """
        scored = [(i, self.score(query_tokens, i)) for i in range(self.n_docs)]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored

    def rank_for_intent(self, intent):
        """Rank documents for a raw intent phrase."""
        return self.rank(tokenize(intent))
