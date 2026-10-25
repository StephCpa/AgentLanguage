"""Invocation-local, shared interpreter fuel and cooperative time checks."""
from threading import Lock
from time import monotonic

from .errors import ExecutionLimitExceeded, InvalidData


MAX_ITERATIONS=1000000


class ExecutionBudget:
    def __init__(self, steps=100000, *, timeout=None, clock=monotonic):
        if type(steps) is not int or steps<0:
            raise InvalidData('execution steps must be a nonnegative integer')
        if timeout is not None and (type(timeout) not in (int,float) or not 0<=timeout<float('inf')):
            raise InvalidData('execution timeout must be finite and nonnegative')
        self.limit,self.used=steps,0
        self.clock=clock
        self.deadline=None if timeout is None else clock()+timeout
        self._lock=Lock()

    def step(self):
        with self._lock:
            self.check_deadline()
            if self.used>=self.limit:
                raise ExecutionLimitExceeded('shared interpreter step budget exhausted')
            self.used+=1

    def check_deadline(self):
        if self.deadline is not None and self.clock()>=self.deadline:
            raise ExecutionLimitExceeded('cooperative execution deadline exceeded')
