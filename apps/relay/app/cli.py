"""Fixed relay migrate/serve entrypoint. Credentials are never CLI arguments."""
import argparse
import sys

class ArgumentFailure(Exception):pass
class Parser(argparse.ArgumentParser):
    def error(self,message):raise ArgumentFailure()


MAX_WS_FRAME=1024*1024  # Spec §08: one immutable device frame is at most 1 MiB.

def main(argv=None):
    parser=Parser(prog='wordheat-relay',description='Explicit relay private configuration root')
    commands=parser.add_subparsers(dest='command',required=True,parser_class=Parser)
    for name in ('migrate','serve'):
        command=commands.add_parser(name);command.add_argument('--config-root',required=True)
        if name=='serve':
            command.add_argument('--host',choices=('127.0.0.1','::1','0.0.0.0'),default='127.0.0.1')
            command.add_argument('--port',type=int,default=8766)
    try:args=parser.parse_args(argv)
    except ArgumentFailure:
        print('RELAY_ARGUMENTS_INVALID',file=sys.stderr);return 2
    try:
        from .server_config import load_runtime,load_migration
        from .composition import build_server,migrate_server
        if args.command=='migrate':
            migrate_server(load_migration(args.config_root));print('RELAY_MIGRATION_COMPLETE');return 0
        if not 1<=args.port<=65535:raise ValueError('CONFIG_PORT_INVALID')
        app=build_server(load_runtime(args.config_root))
        import uvicorn
        # No access headers, body, credentials or query strings in logs. A
        # single owner process runs the durable background workers.
        uvicorn.run(app,host=args.host,port=args.port,workers=1,reload=False,access_log=False,log_level='warning',ws_max_size=MAX_WS_FRAME)
        return 0
    except KeyboardInterrupt:return 0
    except Exception:
        # Driver/validation exceptions may include DSNs or key material.
        # Only a static error reaches an operator-facing stream.
        print('RELAY_COMMAND_FAILED',file=sys.stderr);return 1

if __name__=='__main__':raise SystemExit(main())
