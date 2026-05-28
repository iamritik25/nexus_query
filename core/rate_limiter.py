"""
Token Bucket Rate Limiter — Zero-Dependency, Thread-Safe
Implements the Token Bucket algorithm to control request rates per client/user.
Highly optimized for high-performance concurrent environments.
"""

import time
import threading
from collections import defaultdict


class TokenBucketRateLimiter:
    """
    A thread-safe, in-memory Token Bucket rate limiter.
    
    Attributes:
        capacity (float): The maximum number of tokens in the bucket.
        refill_rate (float): The rate at which tokens are added to the bucket (tokens/second).
    """

    def __init__(self, capacity: float = 10.0, refill_rate: float = 1.0):
        self.capacity = capacity
        self.refill_rate = refill_rate
        
        # Maps client identifier -> [tokens, last_refill_timestamp]
        self._buckets = defaultdict(lambda: [self.capacity, time.time()])
        self._lock = threading.Lock()
        
        # Telemetry metrics
        self.total_requests = 0
        self.dropped_requests = 0

    def consume(self, client_id: str, tokens_to_consume: float = 1.0) -> bool:
        """
        Attempts to consume tokens from the bucket for a given client_id.
        Returns True if successful, False if rate limited.
        """
        with self._lock:
            self.total_requests += 1
            now = time.time()
            bucket = self._buckets[client_id]
            
            # 1. Refill bucket based on elapsed time
            elapsed = now - bucket[1]
            refilled_tokens = elapsed * self.refill_rate
            bucket[0] = min(self.capacity, bucket[0] + refilled_tokens)
            bucket[1] = now
            
            # 2. Check if we have enough tokens
            if bucket[0] >= tokens_to_consume:
                bucket[0] -= tokens_to_consume
                return True
            else:
                self.dropped_requests += 1
                return False

    def get_status(self, client_id: str) -> dict:
        """Returns the current token count and limits for a specific client."""
        with self._lock:
            now = time.time()
            bucket = self._buckets[client_id]
            elapsed = now - bucket[1]
            refilled_tokens = elapsed * self.refill_rate
            current_tokens = min(self.capacity, bucket[0] + refilled_tokens)
            
            return {
                "client_id": client_id,
                "current_tokens": round(current_tokens, 2),
                "capacity": self.capacity,
                "refill_rate": self.refill_rate
            }

    def get_telemetry(self) -> dict:
        """Returns aggregate rate limiting metrics."""
        with self._lock:
            return {
                "total_requests": self.total_requests,
                "dropped_requests": self.dropped_requests,
                "drop_rate": round(self.dropped_requests / max(1, self.total_requests), 4)
            }


# Singleton instance for application-wide rate limiting
global_rate_limiter = TokenBucketRateLimiter(capacity=30.0, refill_rate=2.0)
