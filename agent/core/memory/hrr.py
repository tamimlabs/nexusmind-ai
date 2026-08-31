"""Holographic Reduced Representations (HRR) with phase encoding.

Adapted from Hermes Agent's holographic memory plugin (PR #2351 pattern):
a vector symbolic architecture that encodes compositional structure into
fixed-width distributed representations WITHOUT an embedding API.

Each concept is a vector of angles in [0, 2π). The algebraic operations are:

  bind   — circular convolution (phase addition)  — associates two concepts
  unbind — circular correlation (phase subtraction) — retrieves a bound value
  bundle — superposition (circular mean)           — merges multiple concepts

Atoms are generated deterministically from SHA-256 so representations are
identical across processes, machines, and language versions. numpy is
OPTIONAL: when unavailable, callers must fall back to keyword retrieval
(check ``HAS_NUMPY`` before using any function here).
"""

from __future__ import annotations

import hashlib
import logging
import math
import struct

logger = logging.getLogger(__name__)

try:
    import numpy as np

    HAS_NUMPY = True
except ImportError:  # pragma: no cover - exercised only without numpy
    np = None  # type: ignore[assignment]
    HAS_NUMPY = False

_TWO_PI = 2.0 * math.pi
_FLOAT32_BLOB_PREFIX = b"HRR1"
_DEFAULT_DIM = 512


def _require_numpy() -> None:
    if not HAS_NUMPY:
        raise RuntimeError("numpy is required for holographic memory operations")


def encode_atom(word: str, dim: int = _DEFAULT_DIM) -> np.ndarray:
    """Deterministic phase vector via SHA-256 counter blocks.

    Hashes f"{word}:{i}" for i=0,1,2,... interprets the digests as uint16
    values, and scales them to [0, 2π). Deterministic across processes.
    """
    _require_numpy()

    values_per_block = 16
    blocks_needed = math.ceil(dim / values_per_block)

    uint16_values: list[int] = []
    for i in range(blocks_needed):
        digest = hashlib.sha256(f"{word}:{i}".encode()).digest()
        uint16_values.extend(struct.unpack("<16H", digest))

    phases = np.array(uint16_values[:dim], dtype=np.float64) * (_TWO_PI / 65536.0)
    return phases


def bind(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Circular convolution = element-wise phase addition (associates concepts)."""
    _require_numpy()
    result: np.ndarray = (a + b) % _TWO_PI
    return result


def unbind(memory: np.ndarray, key: np.ndarray) -> np.ndarray:
    """Circular correlation = element-wise phase subtraction (retrieves bound value)."""
    _require_numpy()
    result: np.ndarray = (memory - key) % _TWO_PI
    return result


def bundle(*vectors: np.ndarray) -> np.ndarray:
    """Superposition via circular mean — merges vectors into one similar to each input."""
    _require_numpy()
    complex_sum = np.sum([np.exp(1j * v) for v in vectors], axis=0)
    result: np.ndarray = np.angle(complex_sum) % _TWO_PI
    return result


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Phase cosine similarity in [-1, 1]: 1.0 identical, ~0.0 unrelated."""
    _require_numpy()
    return float(np.mean(np.cos(a - b)))


def encode_text(text: str, dim: int = _DEFAULT_DIM) -> np.ndarray:
    """Bag-of-words encoding: bundle of atom vectors for each token."""
    _require_numpy()

    tokens = [token.strip(".,!?;:\"'()[]{}") for token in text.lower().split()]
    tokens = [t for t in tokens if t]

    if not tokens:
        return encode_atom("__hrr_empty__", dim)

    return bundle(*(encode_atom(token, dim) for token in tokens))


def encode_fact(content: str, entities: list[str], dim: int = _DEFAULT_DIM) -> np.ndarray:
    """Structured encoding: content bound to ROLE_CONTENT, each entity bound to
    ROLE_ENTITY, all bundled together.

    Enables algebraic extraction:
        unbind(fact_vector, bind(entity_atom, ROLE_ENTITY)) ≈ content signal
    """
    _require_numpy()

    role_content = encode_atom("__hrr_role_content__", dim)
    role_entity = encode_atom("__hrr_role_entity__", dim)

    components: list[np.ndarray] = [bind(encode_text(content, dim), role_content)]
    for entity in entities:
        components.append(bind(encode_atom(entity.lower(), dim), role_entity))

    return bundle(*components)


def phases_to_bytes(phases: np.ndarray, dim: int | None = None) -> bytes:
    """Serialize phase vectors as a prefixed float32 blob (compact SQLite storage)."""
    _require_numpy()
    if dim is None:
        dim = int(phases.shape[0])
    payload = np.asarray(phases, dtype=np.float32).tobytes()
    return _FLOAT32_BLOB_PREFIX + payload


def bytes_to_phases(data: bytes, dim: int | None = None) -> np.ndarray:
    """Deserialize a phase vector from the prefixed float32 blob format."""
    _require_numpy()
    if not data.startswith(_FLOAT32_BLOB_PREFIX):
        raise ValueError("HRR blob missing format prefix")
    payload = data[len(_FLOAT32_BLOB_PREFIX) :]
    expected = dim * np.dtype(np.float32).itemsize if dim is not None else None
    if expected is not None and len(payload) != expected:
        raise ValueError(f"HRR vector blob has {len(payload)} payload bytes; expected {expected}")
    if len(payload) % np.dtype(np.float32).itemsize != 0:
        raise ValueError(f"HRR float32 vector blob has invalid byte length: {len(payload)}")
    return np.frombuffer(payload, dtype=np.float32).astype(np.float64)


def snr_estimate(dim: int, n_items: int) -> float:
    """Signal-to-noise ratio estimate: SNR = sqrt(dim / n_items).

    Retrieval degrades below SNR 2.0 (n_items > dim / 4); logs a warning.
    """
    _require_numpy()

    if n_items <= 0:
        return float("inf")

    snr = math.sqrt(dim / n_items)
    if snr < 2.0:
        logger.warning(
            "HRR storage near capacity: SNR=%.2f (dim=%d, n_items=%d). "
            "Retrieval accuracy may degrade.",
            snr,
            dim,
            n_items,
        )
    return snr
