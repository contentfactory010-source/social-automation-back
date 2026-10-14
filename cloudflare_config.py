from contextvars import ContextVar

from instagram import InstagramError


_current_env = ContextVar("cloudflare_worker_env", default=None)


def bind_env(env):
    return _current_env.set(env)


def reset_env(token) -> None:
    _current_env.reset(token)


def current_env():
    env = _current_env.get()
    if env is None:
        raise RuntimeError("Cloudflare Worker environment is unavailable")
    return env


def value(name: str, default: str = "") -> str:
    env = current_env()
    try:
        value = getattr(env, name)
    except AttributeError:
        return default
    if value is None:
        return default
    return str(value)


def setting(name: str) -> str:
    result = value(name).strip()
    if not result:
        raise InstagramError(f"{name} is not configured")
    return result
