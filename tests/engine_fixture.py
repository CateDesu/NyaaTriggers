from contextlib import contextmanager
import threading
from types import SimpleNamespace
from unittest.mock import patch


@contextmanager
def joined_bridge_workers(module, bridge):
    workers = []

    def thread(*args, **kwargs):
        worker = threading.Thread(*args, **kwargs)
        workers.append(worker)
        return worker

    with patch.object(module, "threading", SimpleNamespace(Thread=thread, Lock=threading.Lock)):
        try:
            yield workers
        finally:
            try:
                bridge.stop(wait=True)
            finally:
                for worker in workers:
                    if worker.ident is not None:
                        # Restoring paths while a worker can still log leaks fixture output.
                        worker.join()
