import logging


class StrictLogger:
    def debug(self, *args, **kwargs):
        pass

    def warning(self, message, *args):
        # Do not mark a partial or stale upstream snapshot as a successful sync.
        raise RuntimeError(message % args if args else message)


wxlog = StrictLogger()
