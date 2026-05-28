"""
Semantic Cache System — Zero-Dependency, Pure Python
Uses Bag-of-Words/N-Gram Term Frequency vectorization and Cosine Similarity math.
Bypasses LLM execution for semantically similar user prompts (latency < 1ms vs ~2.5s).
"""

import os
import json
import math
import string
import logging
import threading
from typing import Optional, Tuple

logger = logging.getLogger("SemanticCache")

STOP_WORDS = {
    "me", "show", "get", "list", "please", "all", "the", "a", "an", "of", "in", 
    "for", "to", "with", "from", "by", "on", "at", "what", "is", "are", "how", "many"
}

CACHE_FILE_PATH = os.path.join(os.path.dirname(__file__), "..", "db", "semantic_cache.json")


class PurePythonVectorizer:
    """Computes Term-Frequency vectors and Cosine Similarity between text sequences."""
    
    @staticmethod
    def tokenize(text: str) -> list:
        """Tokenizes, lowercases, and removes punctuation/stopwords from text."""
        text = text.lower()
        # Remove punctuation
        text = text.translate(str.maketrans("", "", string.punctuation))
        tokens = text.split()
        # Remove stopwords and keep semantic tokens
        return [t for t in tokens if t not in STOP_WORDS]

    @staticmethod
    def get_char_ngrams(text: str, n: int = 3) -> list:
        """Generates character-level n-grams to handle typos and spelling variances."""
        text = text.lower()
        return [text[i:i+n] for i in range(len(text) - n + 1)]

    def vectorize(self, text: str) -> dict:
        """
        Creates a composite Term-Frequency dictionary vector of
        both word tokens and character n-grams.
        """
        tokens = self.tokenize(text)
        ngrams = self.get_char_ngrams(text, 3)
        
        vector = {}
        # Word counts (high weight)
        for token in tokens:
            vector[token] = vector.get(token, 0.0) + 1.5
            
        # Character n-grams (low weight)
        for ngram in ngrams:
            vector[ngram] = vector.get(ngram, 0.0) + 0.3
            
        return vector

    @staticmethod
    def cosine_similarity(v1: dict, v2: dict) -> float:
        """Calculates the cosine similarity between two sparse frequency vectors."""
        # Find intersecting terms
        common_keys = set(v1.keys()).intersection(set(v2.keys()))
        if not common_keys:
            return 0.0

        dot_product = sum(v1[k] * v2[k] for k in common_keys)
        
        # Calculate magnitudes
        mag1 = math.sqrt(sum(val ** 2 for val in v1.values()))
        mag2 = math.sqrt(sum(val ** 2 for val in v2.values()))
        
        if mag1 == 0.0 or mag2 == 0.0:
            return 0.0
            
        return dot_product / (mag1 * mag2)


class SemanticCache:
    """
    Thread-safe database semantic query cache with JSON persistence.
    """
    
    def __init__(self, threshold: float = 0.85):
        self.threshold = threshold
        self.vectorizer = PurePythonVectorizer()
        self.cache = []
        self._lock = threading.Lock()
        
        # Telemetry metrics
        self.hits = 0
        self.misses = 0
        
        self._load_cache()

    def _load_cache(self):
        """Loads cached queries and pre-vectorizes their prompts."""
        with self._lock:
            if not os.path.exists(CACHE_FILE_PATH):
                self.cache = []
                return
            try:
                with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                    self.cache = []
                    for entry in raw_data:
                        # Rebuild vector in memory
                        entry["vector"] = self.vectorizer.vectorize(entry["prompt"])
                        self.cache.append(entry)
                logger.info(f"[SemanticCache] Loaded {len(self.cache)} cached queries.")
            except Exception as e:
                logger.error(f"[SemanticCache] Error loading cache: {e}")
                self.cache = []

    def _save_cache(self):
        """Saves current cache to file (excluding raw vector representations)."""
        try:
            os.makedirs(os.path.dirname(CACHE_FILE_PATH), exist_ok=True)
            with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
                # Store entry properties but drop computed vectors
                serializable = [
                    {"prompt": entry["prompt"], "query": entry["query"], "dialect": entry["dialect"]}
                    for entry in self.cache
                ]
                json.dump(serializable, f, indent=2)
        except Exception as e:
            logger.error(f"[SemanticCache] Error saving cache to disk: {e}")

    def lookup(self, prompt: str, dialect: str) -> Optional[str]:
        """
        Looks up a prompt inside the semantic cache.
        Returns the cached query string if similarity > threshold, else None.
        """
        with self._lock:
            if not self.cache:
                self.misses += 1
                return None
                
            input_vector = self.vectorizer.vectorize(prompt)
            best_match = None
            highest_score = 0.0
            
            for entry in self.cache:
                # Match database dialect context first
                if entry["dialect"] != dialect:
                    continue
                    
                similarity = self.vectorizer.cosine_similarity(input_vector, entry["vector"])
                if similarity > highest_score:
                    highest_score = similarity
                    best_match = entry
            
            if highest_score >= self.threshold and best_match:
                self.hits += 1
                logger.info(f"[SemanticCache] Hit! Score: {round(highest_score, 3)} for prompt '{prompt}' -> '{best_match['prompt']}'")
                return best_match["query"]
                
            self.misses += 1
            return None

    def store(self, prompt: str, query: str, dialect: str):
        """Stores a verified query translation in the semantic cache."""
        # Simple validation check: do not cache errors
        if "error" in query.lower() or not query.strip():
            return
            
        with self._lock:
            # Check if this exact prompt is already stored
            for entry in self.cache:
                if entry["prompt"].strip().lower() == prompt.strip().lower() and entry["dialect"] == dialect:
                    entry["query"] = query
                    self._save_cache()
                    return
            
            new_entry = {
                "prompt": prompt,
                "query": query,
                "dialect": dialect,
                "vector": self.vectorizer.vectorize(prompt)
            }
            self.cache.append(new_entry)
            
            # Protect system disk space (Evict oldest items if cache size > 50)
            if len(self.cache) > 50:
                self.cache.pop(0)
                
            self._save_cache()
            logger.info(f"[SemanticCache] Cached new entry: '{prompt}'")

    def get_telemetry(self) -> dict:
        """Returns semantic cache analytics."""
        with self._lock:
            total = self.hits + self.misses
            return {
                "total_lookups": total,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": round(self.hits / max(1, total), 4),
                "cache_size": len(self.cache)
            }


# Singleton instance
global_semantic_cache = SemanticCache(threshold=0.82)
