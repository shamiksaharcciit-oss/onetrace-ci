"""The runtime observer of `onetrace-ci discover`.

It is copied, as `sitecustomize.py`, into a temporary directory that is put first on the
PYTHONPATH of the one command `discover` runs, so it loads in that command's interpreter and in
the Python processes it starts (which inherit the PYTHONPATH), and never in anyone else's. It
uses the standard library only: the observed interpreter may not have onetrace-ci installed.

WHAT IT RECORDS
---------------
In the user's own code (files under the repository, other than tests and any virtual
environment): file reads and writes (`open`, and the `Path.read_*`/`write_*` built on it),
environment reads, `subprocess.run`, HTTP calls made with `requests` or `httpx` (with `openai`
and `anthropic`, which use httpx, named when their frames are on the stack), exceptions (the
type, never the message), the calls the entry function makes (with fingerprints of their
arguments and return values), and which lines ran.

Every event names where it happened, the stage it belongs to (the function the entry function
called, on the stack at that moment), and whether it happened during a run of the entry: in the
entry's own calls, or in another thread or task while the entry was running.

WHAT IT NEVER RECORDS
---------------------
A value. Contents are fingerprints: HMAC-SHA256 under a random key made for this discovery run
and never written anywhere, so a fingerprint joins events within the run (the output of
`retrieve` is the argument of `answer`) but cannot be looked up or guessed afterwards.

Nor a name that could carry a value. A file is named only if git tracks it (outside a git work
tree: if it existed before the run); a file outside the repository only by the installed package
it belongs to, if any. An environment variable, a host or a program is named only if its name is
written in the code on the stack when it is used. Anything else is recorded as the kind of thing
it is, with a fingerprint.

THE PROGRAM IS NOT CHANGED
--------------------------
The observer's own failures never reach the observed program: each is caught, and counted as an
`observer-error` event so the report can say it may be incomplete.
"""
import atexit
import builtins
import dis
import functools
import hashlib
import hmac
import io
import json
import os
import re
import shlex
import sys
import threading
import time

_STARTED = time.time_ns()
_PREFIX = "ONETRACE_CI_DISCOVER_"
_REPO = os.path.normcase(os.path.abspath(os.environ[_PREFIX + "REPO"]))
_KEY = bytes.fromhex(os.environ[_PREFIX + "KEY"])
_EVENTS = os.environ[_PREFIX + "EVENTS"]
_KNOWN = os.environ.get(_PREFIX + "KNOWN", "")
_ENTRY_FILE = os.path.normcase(os.path.abspath(os.environ[_PREFIX + "ENTRY_FILE"]))
_ENTRY_NAME = os.environ[_PREFIX + "ENTRY_FUNC"]
_SELF = os.path.normcase(os.path.abspath(__file__))

_events = []
_lines = {}
_calls = {}
_active = [0]              # runs of the entry that have started and not finished
_errors = {}
_lock = threading.Lock()
_local = threading.local()
_real_open = io.open

#: Generator, coroutine, iterable-coroutine and async-generator code: a trace "call" event also
#: fires on every resume, and a "return" event on every suspension.
_GENERATOR_FLAGS = 0x20 | 0x80 | 0x100 | 0x200
_RESUME = dis.opmap.get("RESUME")          # Python 3.11 and later
_RETURNS = {dis.opmap[n] for n in ("RETURN_VALUE", "RETURN_CONST") if n in dis.opmap}
_YIELD_VALUE = dis.opmap["YIELD_VALUE"]
_YIELD_FROM = dis.opmap.get("YIELD_FROM")  # Python 3.10


class _Busy:
    """Re-entrancy guard: the observer's own work (reading a file to fingerprint it, encoding
    JSON) must not be observed."""

    def __enter__(self):
        self.was = getattr(_local, "busy", False)
        _local.busy = True
        return self.was

    def __exit__(self, *exc):
        _local.busy = self.was


def _busy():
    return getattr(_local, "busy", False)


def _failed(where, exc):
    """The observer could not record something; the program goes on unchanged."""
    key = f"{where}: {type(exc).__name__}"
    with _lock:
        _errors[key] = _errors.get(key, 0) + 1


def _fp(data):
    return "fp:" + hmac.new(_KEY, data, hashlib.sha256).hexdigest()[:16]


def _default(o):
    if isinstance(o, (bytes, bytearray)):
        return {"bytes": _fp(bytes(o))}
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=repr)
    if hasattr(o, "__fspath__"):
        return os.fspath(o)
    raise TypeError(type(o).__name__)


def _fp_value(value):
    """A fingerprint of a value; `none` or `bool` for those, which say nothing about where a
    value came from and are never joined; `unrecorded:<type>` for one JSON cannot hold (or too
    deep to encode). Never repr()."""
    if value is None:
        return "none"
    if value is True or value is False:
        return "bool"
    try:
        data = json.dumps(value, sort_keys=True, default=_default, ensure_ascii=False).encode("utf-8")
    except Exception:
        return "unrecorded:" + type(value).__name__
    return _fp(data)


def _rel(filename):
    """The path relative to the repository, spelled as it is (compared with case folded, where
    the filesystem folds case, but never returned folded), or None outside it."""
    absolute = os.path.abspath(filename)
    path = os.path.normcase(absolute)
    if path == _REPO or not path.startswith(_REPO + os.sep):
        return None
    return absolute[len(_REPO) + 1:].replace(os.sep, "/")


def _environment_dirs():
    dirs = set()
    for p in {sys.prefix, sys.exec_prefix, sys.base_prefix, getattr(sys, "base_exec_prefix", sys.base_prefix)}:
        d = os.path.normcase(os.path.abspath(p))
        if not (_REPO == d or _REPO.startswith(d + os.sep)):     # never the repository itself
            dirs.add(d)
    return dirs


_ENVIRONMENT_DIRS = _environment_dirs()


@functools.lru_cache(maxsize=None)
def _virtualenv(directory):
    return os.path.isfile(os.path.join(directory, "pyvenv.cfg"))


@functools.lru_cache(maxsize=None)
def _code_file(filename):
    """(path relative to the repository or None, whether it is the user's code), resolved once
    per code file: tracing calls this on every line."""
    if not filename or filename.startswith("<"):
        return None, False
    rel = _rel(filename)
    absolute = os.path.normcase(os.path.abspath(filename))
    if rel is None or absolute == _SELF:
        return rel, False
    #: A code file made during the run is not named: its name may carry a value, like any file's.
    shown = rel if _known("files", rel) or _known("code", rel) else _UNNAMED_CODE
    parts = rel.split("/")
    name = parts[-1]
    if "tests" in parts[:-1] or "test" in parts[:-1] or name.startswith("test_") \
            or name.endswith("_test.py") or name == "conftest.py":
        return shown, False
    if "site-packages" in parts or "dist-packages" in parts \
            or any(absolute.startswith(d + os.sep) for d in _ENVIRONMENT_DIRS):
        return shown, False
    directory = _REPO
    for part in parts[:-1]:                 # a virtual environment inside the repository
        directory = os.path.join(directory, part)
        if _virtualenv(directory):
            return shown, False
    return shown, True


def _is_user(filename):
    """Code under the repository, other than tests, virtual environments and this observer."""
    return _code_file(filename)[1]


_qualnames = {}


def _qualname(code, module_globals):
    """The function's qualified name. Python 3.10 has no `co_qualname`: there a module-level
    function (followed through `__wrapped__`, for a decorated one) is found in its module's
    globals, and anything else is taken as nested."""
    q = getattr(code, "co_qualname", None)
    if q is not None:
        return q
    q = _qualnames.get(code)
    if q is None:
        q, obj = "<locals>." + code.co_name, module_globals.get(code.co_name)
        for _ in range(10):
            if obj is None:
                break
            if getattr(obj, "__code__", None) is code:
                q = code.co_name
                break
            obj = getattr(obj, "__wrapped__", None)
        _qualnames[code] = q
    return q


def _transparent(frame):
    """A lambda, comprehension, generator expression, nested function (a decorator's wrapper,
    say) or `__call__` (a class-based decorator's): never a stage itself, and calls made from it
    count as calls from where it runs."""
    code = frame.f_code
    return (code.co_name.startswith("<") or code.co_name == "__call__"
            or "<locals>" in _qualname(code, frame.f_globals))


def _named_function(frame):
    """A transparent frame worth naming in the report: a nested function or `__call__`, not a
    lambda or comprehension, which are syntax in the calling function itself."""
    return not frame.f_code.co_name.startswith("<")


@functools.lru_cache(maxsize=None)
def _in_entry_file(filename):
    return os.path.normcase(os.path.abspath(filename)) == _ENTRY_FILE


def _is_entry(frame):
    code = frame.f_code
    return (code.co_name == _ENTRY_NAME and _in_entry_file(code.co_filename)
            and _qualname(code, frame.f_globals) == _ENTRY_NAME)


def _dotted(frame):
    rel = _code_file(frame.f_code.co_filename)[0] or frame.f_code.co_filename
    if rel == _UNNAMED_CODE:
        #: Nor are the names of its functions written; a keyed fingerprint keeps two of them apart.
        real = f"{frame.f_code.co_filename}:{_qualname(frame.f_code, frame.f_globals)}"
        return f"{_UNNAMED_CODE}#{_fp(real.encode('utf-8'))[3:11]}"
    module = rel[:-3] if rel.endswith(".py") else rel
    if module.endswith("/__init__"):
        module = module[: -len("/__init__")]
    if module.startswith("src/"):
        module = module[4:]
    return module.replace("/", ".") + ":" + _qualname(frame.f_code, frame.f_globals)


def _where(frame):
    """(site, stage, in_run, via, inside) for code running in `frame`, from the stack; None when
    no user frame is on it (the observer, the standard library or a test runner acting alone),
    or when it is the standard library reading source code to print a traceback. `inside` names
    the passed-through function it ran in, when that is all there is between it and the entry."""
    site, stage, in_run, child, via, inside = None, None, False, None, None, None
    while frame is not None:
        code = frame.f_code
        if _is_user(code.co_filename):
            if site is None:
                site = (_code_file(code.co_filename)[0], frame.f_lineno)
            if _is_entry(frame):
                in_run = True
                if child is not None:
                    stage = _dotted(child)
                elif inside is not None:
                    inside = _dotted(inside)
                break
            if not _transparent(frame):
                child = frame
            elif _named_function(frame):
                inside = frame
        elif site is None:
            if frame.f_globals.get("__name__") == "linecache":
                return None
            if via is None:
                via = _third_party(code.co_filename)
        frame = frame.f_back
    if site is None:
        return None
    if not in_run and _active[0] > 0:
        in_run = "elsewhere"            # another thread, or a task, while the entry was running
    return site, stage, in_run, via, (inside if isinstance(inside, str) else None)


def _third_party(filename):
    """The installed package a file belongs to, or None for the standard library and others."""
    parts = filename.replace("\\", "/").split("/")
    for marker in ("site-packages", "dist-packages"):
        if marker in parts:
            i = parts.index(marker)
            if i + 1 < len(parts):
                name = re.split(r"-\d", parts[i + 1], maxsplit=1)[0]
                for suffix in (".dist-info", ".egg-info", ".py", ".pth"):
                    if name.endswith(suffix):
                        name = name[: -len(suffix)]
                return name
    return None


def _library(frame, default):
    """openai or anthropic when their code made the call for the user's code, else `default`.
    Only the frames between the call and the first user frame count: a user function that such
    a library calls back makes its own calls."""
    while frame is not None:
        if _is_user(frame.f_code.co_filename):
            return default
        top = str(frame.f_globals.get("__name__", "")).partition(".")[0]
        if top in ("openai", "anthropic"):
            return top
        frame = frame.f_back
    return default


def _version(name):
    try:
        from importlib import metadata
        return metadata.version(name)
    except Exception:
        return getattr(sys.modules.get(name), "__version__", "unknown")


def _record(kind, frame, **fields):
    """Record an event for code running in `frame` (the caller of the hooked function)."""
    if _busy():
        return
    with _Busy():
        found = _where(frame)
        if found is None:
            return
        site, stage, in_run, via, inside = found
        event = {"kind": kind, **fields, "site": "%s:%d" % site, "stage": stage, "in_run": in_run}
        if via is not None:
            event["via"] = via            # an installed library's code made it, for the user's call
        if inside is not None:
            event["inside"] = inside
        with _lock:
            _events.append(event)


# ------------------------------------------------------------------ names

_sources = {}


def _source(filename):
    text = _sources.get(filename)
    if text is None:
        with _Busy():
            try:
                with _real_open(filename, "rb") as f:
                    text = f.read().decode("utf-8", "replace")
            except OSError:
                text = ""
        _sources[filename] = text
    return text


_string_literals = {}


def _strings(filename):
    """The string literals written in a source file, without their quotes, read once. Only a
    string literal counts as a name written in the code: an identifier that happens to match a
    value is not one."""
    found = _string_literals.get(filename)
    if found is None:
        found = []
        with _Busy():
            try:
                import tokenize
                middle = getattr(tokenize, "FSTRING_MIDDLE", None)      # Python 3.12's f-string parts
                with _real_open(filename, "rb") as f:
                    for token in tokenize.tokenize(f.readline):
                        if token.type == tokenize.STRING:
                            found.append(token.string.lstrip("rbuRBUfF").strip("\"'"))
                        elif token.type == middle:
                            found.append(token.string)
            except Exception:
                found = []
        _string_literals[filename] = found
    return found


def _written(frame, found):
    """Whether `found(string literals)` holds for the code that used a name: the library code
    between the hook and the first user frame (the standard library or a package doing the
    work), then the user's own frames only. Tests and the test runner, above the user's code,
    don't count: a value written there is not written in the pipeline."""
    reached_user = False
    while frame is not None:
        filename = frame.f_code.co_filename
        user = _is_user(filename)
        if (user or not reached_user) and filename and not filename.startswith("<") and found(_strings(filename)):
            return True
        reached_user = reached_user or user
        frame = frame.f_back
    return False


def _quoted(name):
    """Written as a string literal of its own (an environment variable's name)."""
    return lambda strings: name in strings


def _as_host(host):
    """Written as a host in a string literal: at its start, or after `//` or `@`, and followed by
    its end, a path, a port, a query or a fragment (`"localhost:8000"`, `"api.example.com/v1"`,
    `"https://user@mirror.example.com"`, `"//cdn.example.com/x"`). A value that is only part of
    a longer name (`example` in `llm.example.test`) is not."""
    pattern = re.compile(r"(?:^|//|@)" + re.escape(host) + r"(?=$|[/:?#])", re.IGNORECASE)
    return lambda strings: any(pattern.search(s) for s in strings)


def _in_a_string(name):
    """Written as a word inside a string literal (a program in a command)."""
    pattern = re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w-])")
    return lambda strings: any(pattern.search(s) for s in strings)


_package_sources = {}


def _in_package(library, text):
    """Whether `text` is written in the source of the installed package `library`."""
    module = sys.modules.get(library)
    root = os.path.dirname(getattr(module, "__file__", "") or "")
    if not root:
        return False
    if root not in _package_sources:
        chunks = []
        for dirpath, _, filenames in os.walk(root):
            chunks += [_source(os.path.join(dirpath, n)) for n in filenames if n.endswith(".py")]
        _package_sources[root] = "\n".join(chunks)
    return text in _package_sources[root]


_known_names = {}


def _known(kind, rel):
    """Whether a name may be written: `files` are those git tracks (outside a git work tree,
    those that existed before the run); `code` adds the .py files that existed before the run,
    so code a person is still writing keeps its name, and code made during the run does not."""
    if not _known_names:
        with _Busy():
            try:
                with _real_open(_KNOWN, encoding="utf-8") as f:
                    loaded = json.load(f)
            except Exception:
                loaded = {}
        for k in ("files", "code"):
            _known_names[k] = {os.path.normcase(p.replace("/", os.sep)) for p in loaded.get(k, ())}
    return os.path.normcase(rel.replace("/", os.sep)) in _known_names[kind]


_UNNAMED_CODE = "<a code file made during the run>"


def _file_name(path):
    """(name or None, where, package): a name only for a file git tracks."""
    rel = _rel(path)
    if rel is None:
        package = _third_party(os.path.abspath(path))
        return None, ("package" if package else "outside"), package
    if _known("files", rel):
        return rel, "repository", None
    return None, "untracked", None


# ------------------------------------------------------------------ files

def _content_fp(path):
    with _Busy():
        try:
            with _real_open(path, "rb") as f:
                return _fp(f.read())
        except OSError:
            return None


class _Open:
    """`open`, observed. A callable object rather than a function so that, like the builtin, it
    does not become a method when a class stores it (`class S: reader = open`). It pickles and
    copies as itself and reports the builtin's signature; only `inspect.isbuiltin` tells it
    apart, since no Python object can be a builtin function."""

    def __init__(self):
        self.__name__ = self.__qualname__ = "open"
        self.__doc__ = _real_open.__doc__
        self.__module__ = "io"
        self.__self__ = getattr(_real_open, "__self__", None)
        self.__text_signature__ = getattr(_real_open, "__text_signature__", None)

    def __reduce__(self):
        return "open"                   # pickled by reference, as `io.open`

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    @property
    def __signature__(self):
        import inspect                  # only when the program itself is inspecting
        return inspect.signature(_real_open)

    def __call__(self, file, mode="r", *args, **kwargs):
        handle = _real_open(file, mode, *args, **kwargs)
        if not _busy() and not isinstance(file, int):
            try:
                path = os.fspath(file)
                if isinstance(path, bytes):
                    path = os.fsdecode(path)
                name, where, package = _file_name(path)
                fields = dict(name=name, where=where, path=_fp(os.fsencode(os.path.abspath(path))))
                if package is not None:
                    fields["package"] = package
                if any(c in mode for c in "wax+"):
                    _record("file-write", sys._getframe(1), **fields)
                else:
                    _record("file-read", sys._getframe(1), **fields, content=_content_fp(path))
            except Exception as e:
                _failed("open", e)
        return handle


builtins.open = io.open = _Open()


# ------------------------------------------------------------------ environment

_Environ = type(os.environ)
_real_getitem = _Environ.__getitem__


def _getitem(self, key):
    if self is os.environ and isinstance(key, str) and not key.startswith(_PREFIX) and not _busy():
        try:
            frame = sys._getframe(1)
            with _Busy():
                named = _written(frame, _quoted(key))
            _record("env", frame, name=key if named else None)
        except Exception as e:
            _failed("environment", e)
    return _real_getitem(self, key)


_Environ.__getitem__ = _getitem


# ------------------------------------------------------------------ subprocess

import subprocess  # noqa: E402

_real_run = subprocess.run
_PROGRAM = re.compile(r"[A-Za-z0-9_.+-]+")
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


def _program(args, frame):
    """The program's name, if the code on the stack writes it; `python` for this interpreter;
    else None. A shell string's leading `NAME=value` assignments are never taken for it."""
    if isinstance(args, (list, tuple)):
        first = args[0] if args else ""
    else:
        text = os.fsdecode(args) if isinstance(args, bytes) else str(args)
        try:
            tokens = shlex.split(text, posix=os.name != "nt")
        except ValueError:
            tokens = text.split()
        tokens = [t.strip("\"'") for t in tokens]
        while tokens and _ASSIGNMENT.match(tokens[0]):
            tokens.pop(0)
        first = tokens[0] if tokens else ""
    if hasattr(first, "__fspath__"):
        first = os.fspath(first)
    first = os.fsdecode(first) if isinstance(first, bytes) else str(first)
    if os.sep in first or "/" in first:
        if os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(sys.executable)):
            return "python"
    base = os.path.basename(first)
    stem = base[:-4] if base.lower().endswith(".exe") else base
    with _Busy():
        return stem if _PROGRAM.fullmatch(stem) and _written(frame, _in_a_string(stem)) else None


def _run(args, *a, **kw):
    frame = sys._getframe(1)
    try:
        program = _program(args, frame)
    except Exception as e:
        _failed("subprocess", e)
        program = None
    try:
        result = _real_run(args, *a, **kw)
    except BaseException as e:
        _record("subprocess", frame, program=program, args=_fp_value(args), outcome=type(e).__name__)
        raise
    _record("subprocess", frame, program=program, args=_fp_value(args), outcome=str(result.returncode))
    return result


subprocess.run = _run


# ------------------------------------------------------------------ HTTP libraries

def _host(url):
    from urllib.parse import urlsplit
    try:
        return urlsplit(str(url)).hostname
    except Exception:
        return None


def _http_base(frame, default, method, host, url):
    with _Busy():                       # the metadata lookup for the version is the observer's own
        library = _library(frame, default)
        named = bool(host) and (_written(frame, _as_host(host))
                                or (library != default and _in_package(library, f"://{host}")))
        return dict(library=library, version=_version(library), method=str(method).upper(),
                    host=host if named else None, url=_fp_value(str(url)))


def _body(response):
    """A fingerprint of a response body the library has already read. A streamed body is
    never read here: it is the program's to read once."""
    try:
        attributes = vars(response)
    except TypeError:
        return "unrecorded:stream"
    content = attributes.get("_content", attributes.get("content"))
    return _fp(content) if isinstance(content, bytes) else "unrecorded:stream"


def _patch_requests(mod):
    session = getattr(mod, "Session", None)
    if session is None or getattr(session.request, "_onetrace_ci", False):
        return
    real = session.request

    def request(self, method, url, *a, **kw):
        frame = sys._getframe(1)
        try:
            body = kw.get("data") if kw.get("data") is not None else kw.get("json")
            base = dict(_http_base(frame, "requests", method, _host(url), url), request=_fp_value(body))
        except Exception as e:
            _failed("requests", e)
            base = {}
        try:
            response = real(self, method, url, *a, **kw)
        except BaseException as e:
            if base:
                _record("http", frame, **base, outcome=type(e).__name__)
            raise
        if base:
            _record("http", frame, **base, outcome=str(getattr(response, "status_code", "")),
                    response=_body(response))
        return response

    request._onetrace_ci = True
    session.request = request


def _patch_httpx(mod):
    def base_of(frame, request):
        try:
            url = getattr(request, "url", None)
            try:
                body = _fp(getattr(request, "content", b"") or b"")
            except Exception:
                body = "unrecorded:stream"      # a streamed body is not read before it is sent
            return dict(_http_base(frame, "httpx", getattr(request, "method", ""), getattr(url, "host", None), url),
                        request=body)
        except Exception as e:
            _failed("httpx", e)
            return {}

    for cls_name in ("Client", "AsyncClient"):
        cls = getattr(mod, cls_name, None)
        if cls is None or getattr(cls.send, "_onetrace_ci", False):
            continue
        real = cls.send

        if cls_name == "Client":
            def send(self, request, *a, _real=real, **kw):
                frame = sys._getframe(1)
                base = base_of(frame, request)
                try:
                    response = _real(self, request, *a, **kw)
                except BaseException as e:
                    if base:
                        _record("http", frame, **base, outcome=type(e).__name__)
                    raise
                if base:
                    _record("http", frame, **base, outcome=str(getattr(response, "status_code", "")))
                return response
        else:
            async def send(self, request, *a, _real=real, **kw):
                frame = sys._getframe(1)
                base = base_of(frame, request)
                try:
                    response = await _real(self, request, *a, **kw)
                except BaseException as e:
                    if base:
                        _record("http", frame, **base, outcome=type(e).__name__)
                    raise
                if base:
                    _record("http", frame, **base, outcome=str(getattr(response, "status_code", "")))
                return response
        send._onetrace_ci = True
        cls.send = send


_PATCHES = {"requests": _patch_requests, "httpx": _patch_httpx}


class _Finder:
    """Patches a library when it is imported, not before, so nothing is imported that the
    command would not have imported itself."""

    def find_spec(self, name, path=None, target=None):
        if name not in _PATCHES:
            return None
        import importlib.util
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(name)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None:
            return spec
        real_exec = spec.loader.exec_module

        def exec_module(module, _real=real_exec, _name=name):
            _real(module)
            try:
                _PATCHES[_name](module)
            except Exception as e:
                _failed("patch " + _name, e)

        spec.loader.exec_module = exec_module
        return spec


for _name, _patch in _PATCHES.items():
    if _name in sys.modules:
        _patch(sys.modules[_name])
sys.meta_path.insert(0, _Finder())


# ------------------------------------------------------------------ calls, exceptions, lines

def _params(frame):
    code = frame.f_code
    count = code.co_argcount + code.co_kwonlyargcount
    names = list(code.co_varnames[:count])
    flags = code.co_flags
    if flags & 0x04:        # *args
        names.append(code.co_varnames[count])
        count += 1
    if flags & 0x08:        # **kwargs
        names.append(code.co_varnames[count])
    return names


def _args(frame):
    #: A list of [name, fingerprint], in parameter order (a JSON object would be written with
    #: its keys sorted, and the order matters: it is the read order).
    return [[name, _fp_value(frame.f_locals.get(name))] for name in _params(frame)]


def _starts(frame):
    """Whether a "call" event starts the function, rather than resuming a generator or
    coroutine at a yield or an await."""
    code = frame.f_code
    if not code.co_flags & _GENERATOR_FLAGS:
        return True
    lasti = frame.f_lasti
    if _RESUME is None:
        return lasti < 0
    co = code.co_code
    return 0 <= lasti < len(co) - 1 and co[lasti] == _RESUME and co[lasti + 1] & 3 == 0


def _at_yield(frame):
    """Whether a generator or coroutine frame stands at a yield or an await."""
    code = frame.f_code
    if not code.co_flags & _GENERATOR_FLAGS or frame.f_lasti < 0:
        return False
    co, lasti = code.co_code, frame.f_lasti
    if co[lasti] == _YIELD_VALUE:
        return True
    return _YIELD_FROM is not None and (co[lasti] == _YIELD_FROM or
                                        (lasti + 2 < len(co) and co[lasti + 2] == _YIELD_FROM))


def _suspends(frame):
    """Whether a "return" event is a generator or coroutine suspending, not finishing."""
    return _at_yield(frame)


def _returns(frame):
    """Whether a "return" event returns a value, rather than an exception leaving the frame."""
    co, lasti = frame.f_code.co_code, frame.f_lasti
    return 0 <= lasti < len(co) and co[lasti] in _RETURNS


def _flavour(code):
    flags = code.co_flags
    if flags & 0x200:
        return "async generator"
    if flags & (0x80 | 0x100):
        return "async"
    if flags & 0x20:
        return "generator"
    return None


def _local_trace(frame, event, arg):
    if _busy():
        return _local_trace
    try:
        if event == "line":
            if _raising:
                _raising.pop(id(frame), None)
            rel = _code_file(frame.f_code.co_filename)[0]
            if rel != _UNNAMED_CODE:
                _lines.setdefault(rel, set()).add(frame.f_lineno)
        elif event == "exception":
            _on_exception(frame, arg)
        elif event == "return":
            _on_return(frame, arg)
    except Exception as e:
        _failed(event, e)
    return _local_trace


#: Frames an exception is being raised in, by id, with the instruction it was raised at: a
#: "return" at that same instruction is the frame leaving, even at a yield (a coroutine closed or
#: cancelled while suspended). A "line" event, where it carries on instead, clears it.
_raising = {}


def _descends(frame, ancestor):
    for _ in range(1000):
        frame = frame.f_back
        if frame is None:
            return False
        if frame is ancestor:
            return True
    return False


def _on_exception(frame, arg):
    exc_type, _, tb = arg
    if id(frame) in _calls or _is_entry(frame):
        _raising[id(frame)] = frame.f_lasti
    if frame.f_code.co_flags & _GENERATOR_FLAGS and issubclass(exc_type, (StopIteration, StopAsyncIteration, GeneratorExit)):
        return                          # how a generator or coroutine hands back its value, or ends
    if _at_yield(frame):
        return                          # thrown in at a yield (a context manager's __exit__): not raised here
    inner = tb.tb_next if tb is not None else None
    while inner is not None:
        if _is_user(inner.tb_frame.f_code.co_filename):
            if _descends(inner.tb_frame, frame):
                return                  # recorded already, in the user frame below that raised it
            break                       # an older traceback: the same object raised again here
        inner = inner.tb_next
    with _Busy():
        found = _where(frame)
    if found is not None:
        site, stage, in_run, _, _ = found
        with _lock:
            _events.append({"kind": "exception", "type": exc_type.__name__,
                            "site": "%s:%d" % site, "stage": stage, "in_run": in_run})


def _on_return(frame, arg):
    pending = _calls.get(id(frame))
    entry = pending is None and _is_entry(frame)
    leaving = _raising.pop(id(frame), None) == frame.f_lasti
    if (pending is None and not entry) or (not leaving and _suspends(frame)):
        return
    if entry:
        with _lock:
            _active[0] = max(0, _active[0] - 1)
        return
    del _calls[id(frame)]
    if _returns(frame):
        with _Busy():
            pending["returned"] = _fp_value(arg)
            pending["returned_none"] = arg is None


def _caller(frame):
    """The nearest user frame above `frame` that could call a stage: library frames and
    transparent ones (lambdas, comprehensions, decorator wrappers) are passed through."""
    frame = frame.f_back
    while frame is not None:
        if _is_user(frame.f_code.co_filename) and not _transparent(frame):
            return frame
        frame = frame.f_back
    return None


def _trace(frame, event, arg):
    if event != "call" or _busy():
        return None
    try:
        return _on_call(frame)
    except Exception as e:
        _failed("call", e)
        return None


def _on_call(frame):
    code = frame.f_code
    if not _is_user(code.co_filename):
        return None
    starting = _starts(frame)
    if _is_entry(frame):
        if starting:
            with _Busy():
                entry = {"kind": "entry", "args": _args(frame)}
            with _lock:
                _events.append(entry)
                _active[0] += 1
        return _local_trace
    if not starting:
        return _local_trace
    caller = _caller(frame)
    if _transparent(frame):
        if _named_function(frame) and caller is not None and _is_entry(caller):
            with _Busy():
                passed = {"kind": "passed-call", "function": _dotted(frame),
                          "site": "%s:%d" % (_code_file(caller.f_code.co_filename)[0], caller.f_lineno)}
            with _lock:
                _events.append(passed)
        return _local_trace
    if caller is not None and _is_entry(caller):
        flavour = _flavour(code)
        with _Busy():
            call = {"kind": "call", "function": _dotted(frame),
                    "site": "%s:%d" % (_code_file(caller.f_code.co_filename)[0], caller.f_lineno),
                    "args": _args(frame), "returned": None}
            if flavour:
                call["flavour"] = flavour
        if flavour in ("generator", "async generator"):
            #: What a generator yields is not one value, and it can stop at any yield: its
            #: output is never fingerprinted, nor taken to be nothing.
            call["returned"] = "unrecorded:generator"
        with _lock:
            _events.append(call)
        if call["returned"] is None:
            _calls[id(frame)] = call
    elif caller is None and _active[0] > 0:
        with _Busy():
            other = {"kind": "elsewhere-call", "function": _dotted(frame),
                     "site": "%s:%d" % (_code_file(code.co_filename)[0], code.co_firstlineno)}
        with _lock:
            _events.append(other)
    return _local_trace


def _packages():
    """Third-party distributions the command imported, with their installed versions, as the
    observed interpreter itself reports them."""
    try:
        from importlib import metadata
        by_module = metadata.packages_distributions()
    except Exception:
        return {}
    found = {}
    for name in list(sys.modules):
        top = name.partition(".")[0]
        for dist in by_module.get(top, ()):
            try:
                found[top] = [dist, metadata.version(dist)]
            except Exception:
                pass
    return found


def _flush():
    sys.settrace(None)
    threading.settrace(None)
    with _Busy():
        packages = _packages()
        with _lock:
            _events.append({"kind": "packages", "packages": packages})
            for where, count in sorted(_errors.items()):
                _events.append({"kind": "observer-error", "where": where, "count": count})
            for rel, lines in sorted(_lines.items()):
                _events.append({"kind": "lines", "file": rel, "lines": sorted(lines)})
            data = "".join(json.dumps(e, sort_keys=True) + "\n" for e in _events)
        #: Each Python process writes a file of its own, into a folder only this discovery uses,
        #: named so that sorting the names puts the processes in the order they started;
        #: `discover` joins them. Appending to one shared file loses events when processes end
        #: together.
        with _real_open(os.path.join(_EVENTS, "%020d-%d.part" % (_STARTED, os.getpid())), "w", encoding="utf-8") as f:
            f.write(data)


atexit.register(_flush)
sys.settrace(_trace)
threading.settrace(_trace)


# ------------------------------------------------------------------ the next sitecustomize

def _chain():
    """If the environment had a sitecustomize of its own, it still runs."""
    import importlib.machinery
    import importlib.util
    here = os.path.dirname(_SELF)
    rest = [p for p in sys.path if os.path.normcase(os.path.abspath(p or ".")) != os.path.normcase(here)]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is not None and spec.loader is not None:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)


try:
    _chain()
except Exception:
    pass
