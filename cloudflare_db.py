from contextlib import contextmanager

from pyodide.ffi import run_sync

from cloudflare_config import current_env


class IntegrityError(Exception):
    pass


def _to_python(value):
    converter = getattr(value, "to_py", None)
    return converter() if converter else value


class Cursor:
    def __init__(self, rows=None):
        self.rows = [_to_python(row) for row in (rows or [])]

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class Connection:
    def __init__(self, env):
        self.env = env

    def execute(self, sql: str, params=()):
        statement = self.env.DB.prepare(sql)
        try:
            if params:
                statement = statement.bind(*params)
            if sql.lstrip().upper().startswith(("SELECT", "PRAGMA", "WITH")):
                result = _to_python(run_sync(statement.all()))
                return Cursor(result.get("results", []) if isinstance(result, dict) else [])
            run_sync(statement.run())
            return Cursor()
        except Exception as exc:
            if "UNIQUE" in str(exc).upper() or "CONSTRAINT" in str(exc).upper():
                raise IntegrityError(str(exc)) from exc
            raise


@contextmanager
def connection():
    # D1 statements are committed individually; the context manager preserves
    # the existing backend API while the Worker uses D1 instead of SQLite.
    yield Connection(current_env())


def init_db() -> None:
    return None
