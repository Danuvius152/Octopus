"""Database startup must establish trustworthy storage before public work."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("failure", ["corrupt", "hung", "cancelled"])
def test_unconfirmed_initialization_exits_before_admitting_operations(tmp_path, failure):
    path = tmp_path / "startup.db"
    if failure == "corrupt":
        path.write_bytes(b"retained invalid database")
    marker = tmp_path / "admitted"
    program = """
import asyncio,sys,time
from pathlib import Path
import aiosqlite
from server.database import Database
failure=sys.argv[2]
if failure!='corrupt':
    connect=aiosqlite.connect
    def blocked_connect(*args,**kwargs):
        connection=connect(*args,**kwargs)
        execute=connection.execute
        async def blocked_execute(sql,parameters=()):
            if failure=='cancelled':
                asyncio.get_running_loop().call_later(.03,asyncio.current_task().cancel)
            await connection._execute(time.sleep,60)
            return await execute(sql,parameters)
        connection.execute=blocked_execute
        return connection
    aiosqlite.connect=blocked_connect
async def main():
    database=Database(sys.argv[1])
    if failure=='hung':
        await database.initialize(timeout=.1)
    else:
        await database.initialize()
    Path(sys.argv[3]).touch()
    await database.close()
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), failure, str(marker)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 70, result.stderr
    assert not marker.exists()
    if failure == "corrupt":
        assert path.read_bytes() == b"retained invalid database"
