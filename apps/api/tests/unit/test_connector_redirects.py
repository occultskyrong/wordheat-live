import asyncio
from http import HTTPStatus
import pytest


def test_registered_websocket_never_follows_redirect_to_another_loopback_endpoint():
    from app.modules.connectors import transport
    assert hasattr(transport,'RegisteredConnect'),'Missing registered endpoint redirect guard'
    from websockets.asyncio.server import serve
    from websockets.exceptions import InvalidStatus
    async def run():
        reached=[]
        async def target(ws):reached.append(True)
        async with serve(target,'127.0.0.1',0) as destination:
            port=destination.sockets[0].getsockname()[1]
            def redirect(connection,request):
                response=connection.respond(HTTPStatus.FOUND,'Registered endpoint moved')
                response.headers['Location']=f'ws://127.0.0.1:{port}/unregistered'
                return response
            async with serve(target,'127.0.0.1',0,process_request=redirect) as origin:
                original=origin.sockets[0].getsockname()[1]
                with pytest.raises(InvalidStatus):
                    async with transport.RegisteredConnect(f'ws://127.0.0.1:{original}/relay/v1/streams',additional_headers={'Authorization':'Bearer synthetic-test-material'},proxy=None):pass
                assert reached==[]
    asyncio.run(run())
