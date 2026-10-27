"""Validate source-bound current backend contracts, independently of app imports."""
import argparse,hashlib,json
from pathlib import Path,PurePosixPath
C1_SHA256='d69ae165a6d1169e00f20bb03b4cd66bdc9ed2fe7ef2a730c2a130934a88b4e8'
SPEC_SHA256='6c582420ea7d80d66cea1178e8629e7358a6a23a88c1d5ea65843a47f821f63b'
SPEC_PATH='docs/specs/20261025 - 直播语义猜词 - 后端开发Spec.md'
BE_VERSION='LW-BE/1.1.0'
METHODS={'get','post','put','patch','delete','head','options','trace'}

def verify_baseline(raw):return hashlib.sha256(raw).hexdigest()==C1_SHA256

def finite_shape(schema,document,visiting=None):
    """Whether a recursive shape has a finite structural witness.

    Re-entering a ref is not evidence of termination. Scalars, empty arrays and
    objects whose required children terminate provide actual finite bases.
    Annotation flags never participate in this decision.
    """
    if not isinstance(schema,dict) or not schema:return False
    visiting=set() if visiting is None else visiting;parts=[]
    if '$ref' in schema:
        ref=schema['$ref']
        if ref in visiting:return False
        target=document
        try:
            for key in ref[2:].split('/'):target=target[key.replace('~1','/').replace('~0','~')]
        except (KeyError,TypeError):return False
        parts.append(finite_shape(target,document,visiting|{ref}))
    for key in ('anyOf','oneOf'):
        if key in schema:parts.append(any(finite_shape(v,document,visiting) for v in schema[key]))
    if 'allOf' in schema:parts.append(all(finite_shape(v,document,visiting) for v in schema['allOf']))
    kind=schema.get('type')
    if isinstance(kind,list):parts.append(any(finite_shape(dict(schema,type=k),document,visiting) for k in kind))
    elif kind=='object':
        fields=schema.get('properties',{});additional=schema.get('additionalProperties')
        parts.append(all(finite_shape(fields.get(k,additional),document,visiting) for k in schema.get('required',[])))
    elif kind=='array':parts.append(schema.get('minItems',0)==0 or finite_shape(schema.get('items'),document,visiting))
    elif kind in ('string','number','integer','boolean','null'):parts.append(True)
    if 'const' in schema:parts.append(True)
    if 'enum' in schema:parts.append(bool(schema['enum']))
    return bool(parts) and all(parts)

def assert_typed(schema,document=None,seen=None,require_shape=True):
    if not isinstance(schema,dict) or not schema:raise ValueError('empty/boolean schema is not a typed wire contract')
    document=schema if document is None else document;seen=set() if seen is None else seen
    if isinstance(schema.get('type'),list):
        for kind in schema['type']:assert_typed(dict(schema,type=kind),document,seen,require_shape)
        return True
    shape=any(k in schema for k in ('type','const','enum'))
    if '$ref' in schema:
        ref=schema['$ref']
        if not isinstance(ref,str) or not ref.startswith('#/'):raise ValueError('only self-contained local refs are supported')
        target=document
        try:
            for part in ref[2:].split('/'):target=target[part.replace('~1','/').replace('~0','~')]
        except (KeyError,TypeError):raise ValueError('unresolved schema ref: '+ref) from None
        if ref in seen:
            if not finite_shape(target,document):raise ValueError('recursive schema has no finite structural witness: '+ref)
            return True
        shape=assert_typed(target,document,seen|{ref}) or shape
    if schema.get('type')=='object':
        extra=schema.get('additionalProperties')
        if extra is True or extra is None:raise ValueError('untyped additionalProperties is not a contract')
        if isinstance(extra,dict):assert_typed(extra,document,seen)
        elif extra is not False:raise ValueError('object fields lack a typed policy')
        if not schema.get('properties') and extra is False and schema.get('maxProperties')!=0:raise ValueError('empty object must explicitly be empty')
    for value in schema.get('properties',{}).values():assert_typed(value,document,seen)
    if schema.get('type')=='array' or 'items' in schema:assert_typed(schema.get('items'),document,seen)
    for keyword in ('oneOf','anyOf','allOf'):
        if keyword in schema:
            if not isinstance(schema[keyword],list) or not schema[keyword]:raise ValueError('empty schema combination')
            shape=any([assert_typed(v,document,seen,keyword!='allOf') for v in schema[keyword]]) or shape
    for keyword in ('if','then','else','not','contains','propertyNames'):
        if keyword in schema:assert_typed(schema[keyword],document,seen,False)
    for value in schema.get('$defs',{}).values():assert_typed(value,document,seen)
    if require_shape and not shape:raise ValueError('schema has no wire shape')
    return shape

def read(path):return json.loads(path.read_text())
def refs(value,document):
    if isinstance(value,dict):
        if '$ref' in value:
            target=document
            if not value['$ref'].startswith('#/'):raise ValueError('external ref is not frozen')
            try:
                for key in value['$ref'][2:].split('/'):target=target[key.replace('~1','/').replace('~0','~')]
            except (KeyError,TypeError):raise ValueError('unresolved ref: '+value['$ref']) from None
        for child in value.values():refs(child,document)
    elif isinstance(value,list):
        for child in value:refs(child,document)

def indexed_path(root,relative):
    value=PurePosixPath(relative)
    if not isinstance(relative,str) or value.is_absolute() or any(part in ('..','.git','.local') or part.startswith('.env') for part in value.parts):raise ValueError('unsafe indexed source path')
    target=root/relative
    if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):raise ValueError('indexed source path escapes root')
    return target

def check(root):
    root=Path(root);package=root/'packages/contracts';errors=[];http=set();ws=set();baseline=set();extra=set();status='unknown'
    try:
        from jsonschema import Draft202012Validator
        if not verify_baseline((package/'c1-baseline.json').read_bytes()):raise ValueError('historical C1 byte digest mismatch')
        spec=root/SPEC_PATH
        if not spec.is_file():raise ValueError('backend Spec source missing')
        if hashlib.sha256(spec.read_bytes()).hexdigest()!=SPEC_SHA256:raise ValueError('backend Spec digest changed')
        baseline={(m.lower(),p) for m,p,*_ in read(package/'c1-baseline.json')['api_routes']}
        document=read(package/'openapi.yaml');inventory=read(package/'source-inventory.json');status=inventory['source_status']
        if document['openapi']!='3.1.0' or document['x-spec-sha256']!=SPEC_SHA256:raise ValueError('OpenAPI version/source mismatch')
        if status not in {'DRAFT_INPUT','FROZEN_INPUT'}:raise ValueError('unknown source provenance status')
        if set(inventory['applications'])!={'api','scorer','relay'}:raise ValueError('three isolated app inventories required')
        for entry in inventory.get('generator_inputs',[]):
            p=indexed_path(root,entry['path'])
            if not p.is_file():raise ValueError('missing indexed generator/input file: '+entry['path'])
            if hashlib.sha256(p.read_bytes()).hexdigest()!=entry['sha256']:raise ValueError('generator/input hash drift: '+entry['path'])
        expected=set();expected_ws=set()
        for name,source in inventory['applications'].items():
            indexed=[e['path'] for e in source['source_files']]
            present={p.relative_to(root).as_posix() for p in root.glob('apps/'+name+'/app/**/*.py') if p.is_file()}
            present|={p.relative_to(root).as_posix() for p in (root/'apps'/name/'pyproject.toml',root/'apps'/name/'uv.lock') if p.is_file()}
            if len(indexed)!=len(set(indexed)):raise ValueError('duplicate indexed source file: '+name)
            if present-set(indexed):raise ValueError('unindexed source files: '+','.join(sorted(present-set(indexed))))
            for entry in source['source_files']:
                p=indexed_path(root,entry['path'])
                if not p.is_file():raise ValueError('missing indexed source file: '+entry['path'])
                if hashlib.sha256(p.read_bytes()).hexdigest()!=entry['sha256']:raise ValueError('source hash drift: '+entry['path'])
            expected|={(r['method'],r['path']) for r in source['http_operations']};expected_ws|=set(source['websocket_paths'])
        for schema in document['components']['schemas'].values():assert_typed(schema,document);Draft202012Validator.check_schema(schema)
        refs(document,document);ids=set()
        for path,ops in document['paths'].items():
            for method,op in ops.items():
                if method not in METHODS:raise ValueError('non-HTTP operation in paths')
                http.add((method,path))
                if (method,path) not in expected:
                    template=op.get('x-router-template')
                    if not template or (method,template) not in expected:raise ValueError('HTTP operation has no actual source route')
                if path.startswith('/relay/') and path!='/relay/v1/douyin/callback':
                    security=op.get('security',document.get('security',[]))
                    if not any(item.get('RelayBearer')==[] for item in security):raise ValueError('protected relay operation lacks machine Bearer security: '+path)
                    if not path.startswith('/relay/v1/device-tokens/') and any(not item for item in security):raise ValueError('protected relay operation permits anonymous security: '+path)
                if path=='/relay/v1/streams/bootstrap':
                    headers=[p for p in op.get('parameters',[]) if p.get('in')=='header' and p.get('name')=='Idempotency-Key' and p.get('required') is True]
                    if len(headers)!=1:raise ValueError('bootstrap requires explicit Idempotency-Key')
                if op['operationId'] in ids:raise ValueError('duplicate operationId')
                ids.add(op['operationId'])
                if not any(s.startswith('2') for s in op.get('responses',{})):raise ValueError('successful response missing')
                for code,response in op['responses'].items():
                    if code in {'204','304'}:
                        if response.get('content'):raise ValueError('HTTP '+code+' must not declare a response body')
                        continue
                    if not response.get('content'):raise ValueError('response lacks typed media schema')
                    for media in response['content'].values():assert_typed(media['schema'],document)
                if method in {'post','put','patch'} and path!='/relay/v1/leases':
                    if not op.get('requestBody',{}).get('content'):raise ValueError('write request shape missing')
                for media in op.get('requestBody',{}).get('content',{}).values():
                    assert_typed(media['schema'],document);validator=Draft202012Validator(dict(media['schema'],components=document['components']))
                    if 'example' in media and not validator.is_valid(media['example']):raise ValueError('positive request example rejected: '+path)
                    if 'x-invalid-request-example' in op and validator.is_valid(op['x-invalid-request-example']):raise ValueError('negative request example accepted: '+path)
        if not expected<=http:raise ValueError('actual app HTTP route missing from contracts')
        for path,protocol in document['x-websocket-protocols'].items():
            if protocol['http_operation'] is not False or path not in expected_ws:raise ValueError('WebSocket must be a real explicit non-HTTP route')
            ws.add(('wss' if path.startswith('/relay/') else 'ws',path))
        if {p for _,p in ws}!=expected_ws:raise ValueError('actual WS route missing')
        if not baseline<=http|ws:raise ValueError('historical C1 operation coverage missing')
        if document['x-undefined-operations']:raise ValueError('undefined current wire operation remains')
        for name in ('backend-extension.schema.json','relay.schema.json','runtime-manifest.schema.json','native-control.schema.json'):
            schema=read(package/name);Draft202012Validator.check_schema(schema);assert_typed(schema);refs(schema,schema)
        local=read(package/'local-websocket.schema.json')
        for protocol in local['protocols'].values():
            for key in ('client_initial_frame_schema','server_frame_schema'):
                schema=protocol[key];assert_typed(schema);Draft202012Validator.check_schema(schema);refs(schema,schema)
        extension=read(package/'backend-extension.schema.json')
        if extension['x-contract-version']!='LW-C1/1.0.0+'+BE_VERSION or extension['x-spec-sha256']!=SPEC_SHA256:raise ValueError('BE source/version mismatch')
        extra=http|ws-baseline
    except Exception as error:errors.append(str(error))
    return {'report_version':2,'spec_sha256':SPEC_SHA256,'c1_sha256':C1_SHA256,'valid':not errors,'schema_validated':not errors,'all_contracts_defined':not errors,'definition_scope':'backend HTTP/WS wire shapes; not implementation/platform/Windows acceptance','source_status':status,'partial_components':[],'defined_operations':len(http)+len(ws),'defined_http_operations':len(http),'actual_http_route_templates':len(expected) if 'expected' in locals() else 0,'dynamic_concrete_aliases':len(http-expected) if 'expected' in locals() else 0,'defined_websocket_protocols':len(ws),'defined_baseline_operations':len(baseline&(http|ws)),'baseline_operations':len(baseline),'defined_component_http_operations':len(http-{r for r in baseline if r[0] in METHODS}),'undefined_operations':[],'errors':errors}

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[2]);result=check(parser.parse_args().root);print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['valid'] else 1
if __name__=='__main__':raise SystemExit(main())
