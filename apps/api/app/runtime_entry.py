"""API POSIX development entry; private configuration is read only here."""
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
    print('API_ENTRY_FAILED:'+stage+':'+code,file=sys.stderr,flush=True)

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
        entry=root/'apps/api/app/runtime_entry.py'
        if Path(__file__).resolve()!=entry:raise ValueError()
        helper=root/'scripts/backend/20261025-run-local.py'
        spec=importlib.util.spec_from_file_location('wordheat_dev_host',helper)
        host=importlib.util.module_from_spec(spec);spec.loader.exec_module(host)
        host.require_posix()
        stage='BOOTSTRAP'
        boot=host.receive_bootstrap(args.fds[0],'api')
        stage='MANIFEST'
        manifest,sha=host.manifest_identity(args.manifest)
        if sha!=boot['identity']['manifest_sha256']:raise ValueError()
        # Each interpreter sees exactly its own app, with no shared app install.
        sys.path.insert(0,str(root/'apps/api'))
        stage='COMPOSITION'
        from app.config import load_settings
        from app.application import create_app
        from app.modules.scoring.runtime import ScorerRuntime
        settings=load_settings(args.config_root,source_root=root)
        proxy=ScorerRuntime(boot['secret'],manifest['manifest_id'],sha,manifest['dimension'],manifest_metadata=manifest)
        from app.modules.operations.runtime_controller import PrivateRuntimeBoot
        client=host.ApiControlClient.from_boot(boot)
        client.bind_manifest(manifest,settings.instance_id)
        context=PrivateRuntimeBoot.from_port(client)
        app=proxy.wrap_lifespan(create_app(settings,scorer=proxy,runtime_stop_port=client,runtime_boot_context=context,source_root=root,program_entry=Path(__file__)))
        original=app.router.lifespan_context
        @asynccontextmanager
        async def diagnosed(value):
            try:
                async with original(value):
                    registry=getattr(value.state,'maintenance_registry',None)
                    if registry is not None:registry.register_model_root('current',args.model_root)
                    yield
            except Exception as error:
                emit_failure('LIFESPAN',error);raise
        app.router.lifespan_context=diagnosed
        stage='BIND'
        host.serve(app,boot,args.fds[1],8765)
        return 0
    except Exception as error:
        emit_failure(stage,error)
        return 14

if __name__=='__main__':raise SystemExit(main())
