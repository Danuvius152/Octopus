"""Observe binary read() at real file handles, including bounded originals."""

from pathlib import Path


def observe_byte_reads(monkeypatch, *, before=None, after=None):
    opened = Path.open

    class ObservedFile:
        def __init__(self, path, stream):
            self.path, self.stream = path, stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def __iter__(self):
            return iter(self.stream)

        def read(self, size=-1):
            if before:
                before(self.path)
            raw = self.stream.read(size)
            if after:
                after(self.path, raw)
            return raw

    def open_file(path, mode="r", *args, **kwargs):
        stream = opened(path, mode, *args, **kwargs)
        return ObservedFile(path, stream) if mode == "rb" else stream

    monkeypatch.setattr(Path, "open", open_file)
