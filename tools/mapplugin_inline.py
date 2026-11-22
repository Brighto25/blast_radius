import concurrent.futures as cf
import pickle


class InlineExecutor:
    """Same stand-in the selector uses for the tests' spawn process pools."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, *a, **kw):
        fut = cf.Future()
        try:
            fn, a, kw = pickle.loads(pickle.dumps((fn, a, kw)))
            fut.set_result(pickle.loads(pickle.dumps(fn(*a, **kw))))
        except BaseException as e:
            fut.set_exception(e)
        return fut

    def shutdown(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
