"""Scorer POSIX development entry; no private API .env is read."""
import argparse
import importlib.util
from pathlib import Path
import sys
import re
from contextlib import asynccontextmanager


def emit_failure(stage,error):
    code=getattr(error,'code',None)
    if not isinstance(code,str) or not re.fullmatch('[A-Z0-9_]{1,64}',code):code=type(error).__name__.upper()
    if not re.fullmatch('[A-Z0-9_]{1,64}',code):code='TYPE'
    print('SCORER_ENTRY_FAILED:'+stage+':'+code,file=sys.stderr,flush=True)

def main(argv=None):
    stage='CONFIG'
    try:
        parser=argparse.ArgumentParser()
        parser.add_argument('--source-root',type=Path,required=True)
        parser.add_argument('--config-root',type=Path,required=True)
        parser.add_argument('--manifest',type=Path,required=True)
        parser.add_argument('--model-root',type=Path,required=True)
        parser.add_argument('--fds',type=int,nargs=2,required=True)
        args=parser.parse_args(argv)
        root=args.source_root.resolve(strict=True)
        if Path(__file__).resolve()!=root/'apps/scorer/app/runtime_entry.py':raise ValueError()
        helper=root/'scripts/backend/20261025-run-local.py'
        spec=importlib.util.spec_from_file_location('wordheat_dev_host',helper)
        host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
        host.require_posix()
        stage='BOOTSTRAP'
        boot=host.receive_bootstrap(args.fds[0],'scorer')
        stage='MANIFEST'
        _,sha=host.manifest_identity(args.manifest)
        if sha!=boot['identity']['manifest_sha256']:raise ValueError()
        sys.path.insert(0,str(root/'apps/scorer'))
        stage='COMPOSITION'
        from app.main import create_app
        app=create_app(boot['secret'],args.manifest,args.model_root)
        original=app.router.lifespan_context
        @asynccontextmanager
        async def diagnosed(value):
            try:
                async with original(value):yield
            except Exception as error:
                emit_failure('LIFESPAN',error);raise
        app.router.lifespan_context=diagnosed
        stage='BIND'
        host.serve(app,boot,args.fds[1],8124)
        return 0
    except Exception as error:
        emit_failure(stage,error)
        return 15

if __name__=='__main__':raise SystemExit(main())
