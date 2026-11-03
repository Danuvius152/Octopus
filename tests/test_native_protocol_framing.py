"""The controlled peer and its wire observer must tolerate real partial writes."""

import asyncio
import json
import os
import sys

import pytest

from tests.native_app_server import PEER, wire_frames


def append(path, chunk):
    with path.open("ab") as stream:
        stream.write(chunk)


def test_wire_snapshot_waits_for_complete_byte_frames(tmp_path):
    path = tmp_path / "native-frames.jsonl"
    chunks = [b'{"text":', b'"\xe4', b'\xb8\xad"}', b'\n{"n":2}\n{"n":', b'3}\n']
    expected = [[], [], [], [{"text": "中"}, {"n": 2}], [{"text": "中"}, {"n": 2}, {"n": 3}]]
    for chunk, frames in zip(chunks, expected, strict=True):
        append(path, chunk)
        assert wire_frames(tmp_path) == frames
    assert wire_frames(tmp_path, finished=True) == expected[-1]


@pytest.fixture(params=["direct", "runpy"])
async def peer(tmp_path, request):
    launcher = [] if request.param == "direct" else [
        "-c", "import runpy,sys; runpy.run_path(sys.argv.pop(1), run_name='__main__')",
    ]
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", *launcher, str(PEER), str(tmp_path), "normal",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.terminate()
        await process.communicate()


async def barrier(peer, identifier):
    """An RPC receipt proves the peer passed its injection snapshot boundary."""
    peer.stdin.write(json.dumps({"id": identifier, "method": "fixture/barrier"}).encode() + b"\n")
    await peer.stdin.drain()
    received = []
    async with asyncio.timeout(3):
        while True:
            line = await peer.stdout.readline()
            assert line, (await peer.stderr.read()).decode()
            frame = json.loads(line)
            if frame.get("id") == identifier:
                return received
            received.append(frame)


async def test_peer_injections_wait_for_complete_byte_frames(peer, tmp_path):
    path = tmp_path / "native-injections.jsonl"
    chunks = [b'{"text":', b'"\xe4', b'\xb8\xad"}', b'\n{"n":2}\n{"n":', b'3}\n']
    expected = [[], [], [], [{"text": "中"}, {"n": 2}], [{"n": 3}]]
    for index, (chunk, frames) in enumerate(zip(chunks, expected, strict=True)):
        append(path, chunk)
        assert await barrier(peer, index) == frames
    assert await barrier(peer, "no-duplicates") == []
    peer.stdin.close()
    async with asyncio.timeout(3):
        assert await peer.wait() == 0
    assert await peer.stderr.read() == b""


@pytest.mark.parametrize("chunk,error", [(b'{bad}\n', "JSONDecodeError"), (b'{"s":"\xff"}\n', "UnicodeDecodeError")])
async def test_peer_rejects_complete_corrupt_frames(peer, tmp_path, chunk, error):
    append(tmp_path / "native-injections.jsonl", chunk)
    async with asyncio.timeout(3):
        assert await peer.wait() != 0
    assert error in (await peer.stderr.read()).decode()


@pytest.mark.parametrize("chunk", [b'{"half":', b'{"s":"\xe4'])
async def test_peer_eof_diagnoses_unfinished_injection(peer, tmp_path, chunk):
    append(tmp_path / "native-injections.jsonl", chunk)
    assert await barrier(peer, "active") == []
    peer.stdin.close()
    async with asyncio.timeout(3):
        assert await peer.wait() != 0
    assert "Incomplete JSONL frame" in (await peer.stderr.read()).decode()


@pytest.mark.parametrize("chunk,error", [
    (b'{bad}\n', json.JSONDecodeError), (b'{"s":"\xff"}\n', UnicodeDecodeError),
])
def test_wire_rejects_complete_corrupt_frames(tmp_path, chunk, error):
    append(tmp_path / "native-frames.jsonl", chunk)
    with pytest.raises(error):
        wire_frames(tmp_path)


@pytest.mark.parametrize("chunk", [b'{"half":', b'{"s":"\xe4'])
def test_wire_writer_end_diagnoses_unfinished_frame(tmp_path, chunk):
    append(tmp_path / "native-frames.jsonl", chunk)
    assert wire_frames(tmp_path) == []
    with pytest.raises(ValueError, match="Incomplete JSONL frame"):
        wire_frames(tmp_path, finished=True)


async def test_peer_final_snapshot_delivers_a_frame_completed_immediately_before_stdin_eof(tmp_path):
    path = tmp_path / "native-injections.jsonl"
    append(path, b'{"last":')
    ready_read, ready_write = os.pipe()
    release_read, release_write = os.pipe()
    wrapper = tmp_path / "gated_peer.py"
    wrapper.write_text(
        "import os,runpy\n"
        "read=os.read\n"
        "def gated(fd,size):\n"
        "    if fd == 0:\n"
        f"        os.write({ready_write},b'R')\n"
        f"        read({release_read},1)\n"
        "    return read(fd,size)\n"
        "os.read=gated\n"
        f"runpy.run_path({str(PEER)!r},run_name='__main__')\n"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-u", str(wrapper), str(tmp_path), "normal", pass_fds=(ready_write, release_read),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    os.close(ready_write)
    os.close(release_read)
    try:
        process.stdin.close()
        async with asyncio.timeout(3):
            assert await asyncio.to_thread(os.read, ready_read, 1) == b"R"
            append(path, b'true}\n')
            os.write(release_write, b"R")
            stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert [json.loads(line) for line in stdout.splitlines()] == [{"last": True}]
        assert (tmp_path / "native-injection-offset").read_text() == "1"
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
        os.close(ready_read)
        os.close(release_write)
