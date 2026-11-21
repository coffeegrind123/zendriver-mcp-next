"""Optional neural reranking for find_elements, with the Folate cross-encoder.

Folate (figranium/folate, Apache-2.0) is an ELECTRA-base model fine-tuned on
Mind2Web that scores (description, serialized element) pairs; its INT8 ONNX
build is about 107 MB and runs on CPU. Install with the `folate` extra. The
weights download from Hugging Face on first use, or load from
ZENDRIVER_MCP_FOLATE_MODEL_DIR.

Folate's own `rank()` takes page HTML, re-parses it with lxml and builds CSS or
XPath selectors from the copy. That loses shadow DOM, iframes and visibility,
and its selectors are often not unique (`a[href="..."]`). So this module uses
only the model: it serializes the walker's own candidates in Folate's training
format and scores them, and find_elements returns our element ids.

Its published speed is 2.3-4.3 s per step on 2 vCPUs for 50 candidates
(Folate's Mind2Web evaluation), not the 10-25 ms frowse's README states, so it
is opt-in per call.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from typing import Any

# Folate's ranker.py: the attributes it serializes, in its order, and its caps.
KEEP_ATTRS = (
    "id",
    "name",
    "type",
    "role",
    "aria-label",
    "placeholder",
    "title",
    "alt",
    "href",
    "data-testid",
)
ATTR_CHARS = 60
TEXT_CHARS = 80

# Folate's default candidate count and batch size.
MAX_CANDIDATES = 48
BATCH_SIZE = 16

MODEL_DIR_ENV = "ZENDRIVER_MCP_FOLATE_MODEL_DIR"

# Runs in the isolated world: what Folate's serializer reads from an element.
DESCRIBE_FN = """function(ids, keep, attrChars, textChars) {
  const S = globalThis.__zd;
  return ids.map((i) => {
    const el = S && S.get(i);
    if (!el) return null;
    const a = {};
    for (const k of keep) {
      const v = el.getAttribute(k);
      if (v && v.trim()) a[k] = v.trim().slice(0, attrChars);
    }
    const p = el.parentElement || (el.getRootNode() instanceof ShadowRoot ? el.getRootNode().host : null);
    return {
      tag: el.tagName.toLowerCase(),
      a,
      text: (el.textContent || "").replace(/\\s+/g, " ").trim().slice(0, textChars),
      parent: p ? p.tagName.toLowerCase() : "",
    };
  });
}"""

_finder: Any = None
_lock = threading.Lock()


def available() -> bool:
    return importlib.util.find_spec("folate") is not None


def serialize(desc: dict[str, Any]) -> str:
    """One element in Folate's format: '[TAG] a [HREF] /x [TEXT] Sign in [PARENT] li'."""
    parts = [f"[TAG] {desc.get('tag', '')}"]
    attrs = desc.get("a") or {}
    for k in KEEP_ATTRS:
        if attrs.get(k):
            parts.append(f"[{k.upper()}] {attrs[k]}")
    if desc.get("text"):
        parts.append(f"[TEXT] {desc['text']}")
    if desc.get("parent"):
        parts.append(f"[PARENT] {desc['parent']}")
    return " ".join(parts)


def _load() -> Any:
    global _finder
    with _lock:
        if _finder is None:
            from folate import FolateSelectorFinder

            _finder = FolateSelectorFinder(model_dir=os.environ.get(MODEL_DIR_ENV) or None)
        return _finder


def score(query: str, serialized: list[str]) -> list[float]:
    """Folate's relevance logit for each serialized element. Blocking: run in a thread.

    Mirrors FolateSelectorFinder.rank's batching, on our candidates instead of
    its lxml copy of the page.
    """
    import numpy as np

    finder = _load()
    out: list[float] = []
    for start in range(0, len(serialized), BATCH_SIZE):
        chunk = [(query, s) for s in serialized[start : start + BATCH_SIZE]]
        enc = finder.tokenizer.encode_batch(chunk)
        logits = finder.session.run(
            ["logits"],
            {
                "input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64),
                "token_type_ids": np.array([e.type_ids for e in enc], dtype=np.int64),
            },
        )[0].reshape(-1)
        out.extend(float(x) for x in logits)
    return out
