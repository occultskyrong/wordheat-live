"""Role-specific WebSocket transport. SQL work runs outside the ASGI loop."""
import asyncio,time
from fastapi import APIRouter,WebSocket,WebSocketDisconnect
from app.errors import DomainError
from app.modules.realtime.protocol import parse_resume,strict_json
from app.modules.realtime.service import stamp,CONTRACT


def create_router(db,auth,service,realtime,settings):
    router=APIRouter()
    async def socket(ws,audience):
        display=None; nonce=None; receive_task=None; sid=None; cursor=0
        try:
            if ws.headers.get('host') not in settings.allowed_hosts or ws.headers.get('origin') not in settings.allowed_origins or ws.query_params:
                await ws.close(code=4403); return
            if audience=='DISPLAY':
                display=await asyncio.to_thread(auth.display,ws.cookies.get('lw_display')); actor=None
            else:
                actor=await asyncio.to_thread(auth.operator,ws.cookies.get('lw_operator'),'session.control')
            await ws.accept()
            raw=await asyncio.wait_for(ws.receive_text(),timeout=10)
            if len(raw.encode())>65536: raise DomainError('PAYLOAD_TOO_LARGE','WS frame too large',413)
            resume=parse_resume(strict_json(raw)); sid=resume['session_id']; cursor=int(resume['after_seq'])
            if audience=='STUDIO' and resume.get('be_extension_version') not in {None,'LW-BE/1.1.0'}: raise DomainError('BE_EXTENSION_VERSION_UNSUPPORTED','Unsupported backend extension',409)
            if audience=='DISPLAY':
                if 'be_extension_version' in resume: raise DomainError('BE_EXTENSION_VERSION_UNSUPPORTED','DISPLAY is C1 only',409)
                if display['session_id']!=sid: raise DomainError('DISPLAY_UNAUTHORIZED','Display scope mismatch',403)
                nonce,generation=await asyncio.to_thread(realtime.connect,display['id'],sid)
            # Replay never grants effects or waits for receipts.
            while True:
                events,upto=await asyncio.to_thread(realtime.replay,sid,audience,cursor,actor['actor_id'] if actor else None)
                for event in events: await ws.send_json(realtime.envelope(event,'replay',extended=resume.get('be_extension_version')=='LW-BE/1.1.0'))
                cursor=upto
                if display:
                    ready=await asyncio.to_thread(realtime.ready,display['id'],nonce,upto)
                    if ready is None: continue
                else:
                    if not await asyncio.to_thread(realtime.at_boundary,sid,audience,upto,actor['actor_id'] if actor else None): continue
                    ready={'kind':'resume_ready','schema_version':1,'contract_version':CONTRACT,'session_id':sid,'upto_seq':str(upto),'server_time':stamp(),'render_generation':None,'pending_effects':[]}
                await ws.send_json(ready); break
            receive_task=asyncio.create_task(ws.receive_text()); heartbeat=time.monotonic()
            while True:
                done,_=await asyncio.wait({receive_task},timeout=.05)
                if done:
                    value=receive_task.result()
                    # Only the initial resume is accepted; no browser command can grant effects.
                    raise DomainError('INPUT_INVALID','Unexpected WS control frame',422)
                if display:
                    current=await asyncio.to_thread(auth.display,ws.cookies.get('lw_display'))
                    await asyncio.to_thread(realtime.active,display['id'],nonce)
                else: await asyncio.to_thread(auth.operator,ws.cookies.get('lw_operator'),'session.control')
                events,upto=await asyncio.to_thread(realtime.replay,sid,audience,cursor,actor['actor_id'] if actor else None)
                for event in events:
                    await ws.send_json(realtime.envelope(event,'live',bool(display and display['purpose']=='primary'),extended=resume.get('be_extension_version')=='LW-BE/1.1.0'))
                    cursor=event['seq']
                if time.monotonic()-heartbeat>=2:
                    await ws.send_json({'kind':'heartbeat','schema_version':1,'contract_version':CONTRACT,'session_id':sid,'server_time':stamp(),'latest_seq':str(upto),'transport':await asyncio.to_thread(realtime.transport,sid,actor['actor_id'] if actor else None)}); heartbeat=time.monotonic()
        except WebSocketDisconnect: pass
        except Exception as error:
            if ws.application_state.name=='CONNECTING':
                await ws.close(code=4403)
            elif ws.application_state.name=='CONNECTED':
                code=error.code if isinstance(error,DomainError) else 'INPUT_INVALID'
                details=error.details if isinstance(error,DomainError) else {}
                if sid: await ws.send_json({'kind':'resync_required','schema_version':1,'contract_version':CONTRACT,'session_id':sid or '', 'reason_code':code,'min_seq':details.get('min_seq','0'),'latest_seq':details.get('latest_seq',str(cursor))})
                await ws.close(code=4403 if code in {'AUTH_REQUIRED','FORBIDDEN','DISPLAY_UNAUTHORIZED'} else 4409)
        finally:
            if receive_task:
                receive_task.cancel()
                try: await receive_task
                except BaseException: pass
            if display and nonce: await asyncio.to_thread(realtime.disconnect,display['id'],nonce)
    @router.websocket('/api/v1/display/ws')
    async def display_ws(ws:WebSocket): await socket(ws,'DISPLAY')
    @router.websocket('/api/v1/studio/ws')
    async def studio_ws(ws:WebSocket): await socket(ws,'STUDIO')
    return router
