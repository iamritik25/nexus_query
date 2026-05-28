"""
Circuit Breaker Resiliency Pattern — Zero-Dependency, Thread-Safe
Prevents cascading failures by stopping calls to failing remote endpoints (LLMs, DB adapters).
Implements states: CLOSED, OPEN, HALF-OPEN with automatic reset timers.
"""

import time
import logging
import threading
from typing import Callable, Any

logger = logging.getLogger("CircuitBreaker")


class CircuitBreakerOpenException(Exception):
    """Raised when an execution is attempted on an open circuit breaker."""
    pass


class CircuitBreaker:
    """
    A thread-safe Circuit Breaker implementing the standard state transition model.
    """
    
    # States
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF-OPEN"

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        
        self.state = self.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0
        
        self._lock = threading.Lock()
        
        # Telemetry metrics
        self.total_calls = 0
        self.successful_calls = 0
        self.trips = 0

    def call(self, func: Callable[..., Any], *args, **kwargs) -> Any:
        """
        Executes a callable inside the circuit breaker wrapper.
        Raises CircuitBreakerOpenException if the circuit is open.
        """
        with self._lock:
            self.total_calls += 1
            now = time.time()
            
            # 1. State transition logic (OPEN -> HALF-OPEN)
            if self.state == self.OPEN:
                if now - self.last_failure_time >= self.recovery_timeout:
                    self.state = self.HALF_OPEN
                    logger.info(f"[CircuitBreaker] {self.name} moved to HALF-OPEN (recovery trial).")
                else:
                    raise CircuitBreakerOpenException(
                        f"Circuit breaker '{self.name}' is OPEN. Fast-failing downstream request."
                    )
        
        # 2. Execute the function (outside lock to prevent deadlocks and blockages)
        try:
            result = func(*args, **kwargs)
            
            # 3. Handle success
            with self._lock:
                self.successful_calls += 1
                if self.state == self.HALF_OPEN or self.failure_count > 0:
                    logger.info(f"[CircuitBreaker] {self.name} healed successfully. Returning to CLOSED.")
                    self.state = self.CLOSED
                    self.failure_count = 0
            return result
            
        except Exception as e:
            # 4. Handle failure
            with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.time()
                
                if self.state == self.CLOSED:
                    if self.failure_count >= self.failure_threshold:
                        self.state = self.OPEN
                        self.trips += 1
                        logger.warning(f"[CircuitBreaker] {self.name} TRIPPED! State is now OPEN.")
                elif self.state == self.HALF_OPEN:
                    # Half-open failure instantly sends it back to open
                    self.state = self.OPEN
                    logger.warning(f"[CircuitBreaker] {self.name} trial failed in HALF-OPEN. Retripped to OPEN.")
                    
            raise e

    def get_status(self) -> dict:
        """Returns the current state and health metrics of this circuit breaker."""
        with self._lock:
            return {
                "name": self.name,
                "state": self.state,
                "consecutive_failures": self.failure_count,
                "total_calls": self.total_calls,
                "successful_calls": self.successful_calls,
                "trips": self.trips,
                "recovery_time_remaining": max(0.0, round(self.recovery_timeout - (time.time() - self.last_failure_time), 2)) if self.state == self.OPEN else 0.0
            }


# Singleton registry of circuit breakers
class CircuitBreakerRegistry:
    def __init__(self):
        self._breakers = {}
        self._lock = threading.Lock()

    def get_breaker(self, name: str, failure_threshold: int = 5, recovery_timeout: float = 30.0) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(name, failure_threshold, recovery_timeout)
            return self._breakers[name]

    def list_breakers(self) -> list:
        with self._lock:
            return [breaker.get_status() for breaker in self._breakers.values()]


cb_registry = CircuitBreakerRegistry()
