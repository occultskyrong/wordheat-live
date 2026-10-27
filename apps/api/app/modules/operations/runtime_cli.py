"""Fixed local native client. Identity is checked before reading a password."""
import getpass
import json
import os
from pathlib import Path
from app.errors import DomainError
from app.modules.operations.runtime_socket import NativeControlClient

def line_from_fd(fd,limit):
    value=bytearray()
    while len(value)<=limit:
        block=os.read(fd,1)
        if not block or block==b'\n':break
        value.extend(block)
    if len(value)>limit:raise DomainError('INPUT_INVALID','Private input exceeds limit',422)
    return value.decode('utf-8')

def run(args):
    source=Path(args.source_root).resolve(strict=True) if args.source_root else Path(__file__).resolve().parents[5]
    native=args.native_root or args.root/'.local/runtime'
    client=NativeControlClient(native,source,configuration_root=args.root).connect()
    # No prompt or password FD read occurs before the kernel peer/registration/program checks.
    password=line_from_fd(args.password_fd,256) if args.password_fd is not None else getpass.getpass('Maintenance operator password: ')
    action=args.action;payload={}
    if action=='request':
        payload={'command':args.runtime_command,'reason':args.reason or 'Native operator request'}
        if args.operation:payload['operation']=args.operation
    elif action in {'prepare','confirm','cancel','recover','trust.confirm','device.confirm','pair.confirm'}:
        if not args.command_id:raise DomainError('INPUT_INVALID','Native command id required',422)
        payload={'command_id':args.command_id}
        if action=='prepare':
            if args.artifact_id:payload['artifact_id']=args.artifact_id
            if args.target_id:payload['target_id']=args.target_id
            if args.allow_data_loss:payload['allow_data_loss']=True
        elif action in {'confirm','trust.confirm','device.confirm','pair.confirm'}:
            if not all([args.fingerprint,args.nonce,args.instance_id]):raise DomainError('INPUT_INVALID','Bound native confirmation required',422)
            expected=args.instance_id+' '+args.fingerprint
            chosen=line_from_fd(args.confirmation_fd,300) if args.confirmation_fd is not None else input('Type '+expected+' to confirm: ')
            if chosen!=expected:raise DomainError('NATIVE_CONFIRMATION_CANCELLED','Native confirmation cancelled',409)
            payload.update(fingerprint=args.fingerprint,nonce=args.nonce,installation_id=args.instance_id)
    elif action in {'trust.prepare','pair.prepare'}:
        if not args.source_id:raise DomainError('INPUT_INVALID','Registered control source id required',422)
        payload={'source_id':args.source_id}
    if action=='pair.confirm':
        token=line_from_fd(args.launch_token_fd,8192) if args.launch_token_fd is not None else getpass.getpass('Platform launch token (memory only): ')
        payload['launch_token']=token
    result=client.call(args.username,password,action,payload)
    password=None
    print(json.dumps(result,ensure_ascii=False,sort_keys=True),flush=True)
    return 0
