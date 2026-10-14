"""Optional Linux Gunicorn adapter. Import only from the separate webhook process."""

from gunicorn.app.base import BaseApplication
from gunicorn.arbiter import Arbiter
from gunicorn.glogging import Logger

from .whatsapp_service import PrivateLogMixin, server_options


class PrivateLogger(PrivateLogMixin, Logger):
    pass


def make_server(app):
    class Server(BaseApplication):
        def do_load_config(self):
            # BaseApplication's default wrapper prints raw configuration exceptions.
            self.load_default_config()
            self.load_config()

        def load_config(self):
            for name, value in server_options().items():
                self.cfg.set(name, value)

        def load(self):
            return app

        def run(self):
            # The default run wrapper prints RuntimeError details; main owns failures.
            Arbiter(self).run()

    return Server()


def serve(app) -> None:
    make_server(app).run()
