"""A new process reopens the real application after normal account file writes."""

import os
import subprocess
import sys
from pathlib import Path

from tests.test_service_runtime import installation_settings

_APPLICATION = """
import asyncio,json,sys
from pathlib import Path
import httpx
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items():
    setattr(settings,name,value)
from server import main
from server.large_prompts import spill_if_large
from server.users import UserManager
from server.workspace import paths_for

async def run():
    manifest=Path(sys.argv[2])
    async with main.app.router.lifespan_context(main.app):
        manager=main.session_manager
        users=UserManager(manager.db)
        if sys.argv[3]=='write':
            user=await users.create_user(username='restart-owner',password='owned-restart-password',is_admin=True)
            owner=user['id']
            agent=await manager.db.get_system_agent(owner)
            session=await manager.create_session(agent['id'],name='Original files')
            saved={'owner':owner,'session':session.id}
        else:
            saved=json.loads(manifest.read_text())
            owner=saved['owner']
        bearer=await users.issue_token(owner)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test',
                                     headers={'Authorization':'Bearer '+bearer}) as client:
            if sys.argv[3]=='write':
                uploaded=await client.post('/api/sessions/'+saved['session']+'/attachments',
                    files={'file':('original.txt',b'Complete original attachment','text/plain')})
                assert uploaded.status_code==201,uploaded.text
                saved['attachment']=uploaded.json()['id']
                prompt='[bg-task-result] '+ 'Complete original input. '*6000
                pointer=spill_if_large(saved['session'],prompt,user_id=owner)
                spill=list((paths_for(owner).large_prompts/saved['session']).glob('*.txt'))
                assert len(spill)==1 and str(spill[0]) in pointer
                assert spill[0].read_text()==prompt
                saved['spill']=spill[0].name
                manifest.write_text(json.dumps(saved))
            downloaded=await client.get('/api/sessions/'+saved['session']+'/attachments/'+saved['attachment'])
            assert downloaded.status_code==200 and downloaded.content==b'Complete original attachment'
            spill=paths_for(owner).large_prompts/saved['session']/saved['spill']
            assert spill.read_text()=='[bg-task-result] '+ 'Complete original input. '*6000
        assert not Path(settings.attachments_dir).exists()
        assert not Path(settings.large_prompts_dir).exists()
    assert not main.app.state.installation_owner._lock

asyncio.run(run())
"""


def test_account_upload_and_spill_survive_an_independent_application_restart(tmp_path):
    _config, config_file = installation_settings(tmp_path / "installation")
    manifest = tmp_path / "original-files.json"
    environment = dict(os.environ, OCTOPUS_ENV_FILE=str(tmp_path / "no-project.env"),
                       CODEX_HOME=str(tmp_path / "provider-codex"),
                       CLAUDE_CONFIG_DIR=str(tmp_path / "provider-claude"))
    for phase in ("write", "reopen"):
        result = subprocess.run(
            [sys.executable, "-c", _APPLICATION, str(config_file), str(manifest), phase],
            cwd=Path(__file__).resolve().parents[1], env=environment,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
