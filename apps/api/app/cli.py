"""Explicit local commands. Password is read from a hidden terminal prompt."""
# Direct -I execution must import this registered source tree, never an editable sibling app.
if __name__=='__main__' and __package__ in {None,''}:
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import argparse
import getpass
from pathlib import Path
from sqlalchemy.engine import make_url
from app.config import load_settings, load_migration_dsn
from app.infrastructure.db.database import Database, engine_for
from app.infrastructure.db.migrate import migrate
from app.modules.auth.service import bootstrap_operator, set_operator_state


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True,help='Explicit repository root containing private .env')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('serve'); sub.add_parser('migrate')
    native=sub.add_parser('native-control',help='Restricted local native confirmation and maintenance client')
    native.add_argument('action',choices=['status','request','prepare','confirm','cancel','recover','trust.prepare','trust.confirm','device.prepare','device.confirm','pair.prepare','pair.confirm'])
    native.add_argument('--username',required=True)
    native.add_argument('--source-root',type=Path)
    native.add_argument('--native-root',type=Path)
    native.add_argument('--password-fd',type=int)
    native.add_argument('--confirmation-fd',type=int)
    native.add_argument('--launch-token-fd',type=int)
    native.add_argument('--command-id');native.add_argument('--artifact-id');native.add_argument('--source-id')
    native.add_argument('--target-id');native.add_argument('--instance-id');native.add_argument('--fingerprint');native.add_argument('--nonce')
    native.add_argument('--operation',choices=['backup_local','verify_local_backup','restore_new_instance','upgrade_app','switch_model'])
    native.add_argument('--runtime-command',choices=['stop','request_maintenance'],default='request_maintenance')
    native.add_argument('--reason');native.add_argument('--allow-data-loss',action='store_true')
    init=sub.add_parser('bootstrap-operator'); init.add_argument('--username',required=True); init.add_argument('--display-name',required=True)
    for name,help in (('disable-operator','Controlled operator deactivation; revokes that actor sessions'),('enable-operator','Controlled operator reactivation')):
        state=sub.add_parser(name,help=help); state.add_argument('--username',required=True)
    args=parser.parse_args()
    try:
        if args.command=='native-control':
            from app.modules.operations.runtime_cli import run
            return run(args)
        source_root=Path(__file__).resolve().parents[3]
        settings=load_settings(args.root,source_root=source_root)
        if args.command=='serve':
            import uvicorn
            from app.application import create_app
            from app.modules.operations.runtime_router import bind_runtime,LocalServerStop
            stop_port=LocalServerStop()
            app=create_app(settings,runtime_stop_port=stop_port,source_root=source_root,program_entry=Path(__file__))
            bind_runtime(app,stop_port=stop_port)
            server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=8765,workers=1,loop='asyncio',reload=False,access_log=False))
            stop_port.bind_exit(lambda:setattr(server,'should_exit',True))
            server.run()
        elif args.command=='migrate':
            if getattr(settings,'test_namespace_file',None):
                from app.errors import DomainError
                raise DomainError('TEST_NAMESPACE_MIGRATION_FORBIDDEN','Owned test namespace is pre-provisioned; public migration forbidden',403)
            dsn=load_migration_dsn(args.root)
            if make_url(dsn).username==make_url(settings.database_url).username: raise ValueError('Migration role must differ')
            migrate(dsn,settings.instance_id,make_url(settings.database_url).username)
        elif args.command in {'disable-operator','enable-operator'}:
            database=Database(settings); database.verify_runtime()
            with database.engine.begin() as c: set_operator_state(c,args.username,args.command=='disable-operator')
            database.engine.dispose()
        else:
            database=Database(settings); database.verify_runtime()
            password=getpass.getpass('New operator password: ')
            if password!=getpass.getpass('Repeat password: '): raise ValueError('Passwords differ')
            with database.engine.begin() as c: bootstrap_operator(c,args.username,password,args.display_name)
            database.engine.dispose()
    except __import__('app.errors',fromlist=['DomainError']).DomainError as error:
        parser.exit(1,error.code+'\n')
    except Exception:
        parser.exit(1,'WordHeat operation failed; check private configuration and authorized operation.\n')

if __name__=='__main__': main()
