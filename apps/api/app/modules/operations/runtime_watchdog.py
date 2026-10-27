"""Fixed inherited-FD watchdog for this API's direct CLI process; stdlib only."""
import json,os,select,signal,socket,sys,time,threading
from pathlib import Path

def write_reason(packet):
    try:
        path=Path(packet.get('reason_file') or '')
        if not path.is_absolute() or not path.parent.is_dir() or path.parent.is_symlink() or path.parent.stat().st_uid!=os.getuid() or path.parent.stat().st_mode&0o077 or path.is_symlink():return
        value={'protocol':'wordheat-stop-watchdog/1','code':'SHUTDOWN_DRAIN_TIMEOUT','command_id':packet['command_id'],'boot_id':packet['boot_id'],'api_pid':packet['parent_pid'],'deadline_ns':packet['deadline_ns'],'credentials_included':False}
        temporary=path.with_name(path.name+'.'+str(os.getpid())+'.part')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as stream:json.dump(value,stream)
        os.replace(temporary,path)
    except Exception:pass

def main(argv=None):
    args=sys.argv[1:] if argv is None else argv
    if os.name!='posix' or len(args)!=1:return 1
    channel=socket.socket(fileno=int(args[0]));channel.settimeout(3)
    raw=bytearray()
    while not raw.endswith(b'\n'):
        block=channel.recv(8193-len(raw))
        if not block or len(raw)+len(block)>8192:return 1
        raw.extend(block)
    packet=json.loads(raw)
    if packet.get('parent_pid')!=os.getppid() or not isinstance(packet.get('deadline_ns'),int):return 1
    deadline=min(packet['deadline_ns'],time.monotonic_ns()+15_000_000_000)
    channel.sendall(b'ARMED\n')
    while os.getppid()==packet['parent_pid']:
        left=(deadline-time.monotonic_ns())/1e9
        if left<=0:break
        if select.select([channel],[],[],min(left,.1))[0]:
            if not channel.recv(1):return 0  # Parent really exited and closed its private FD.
    if os.getppid()!=packet['parent_pid']:return 0
    # Kill first; best-effort diagnostics must never delay the process deadline.
    sig=getattr(signal,'SIGUSR1',signal.SIGTERM)
    os.kill(packet['parent_pid'],sig)
    logger=threading.Thread(target=write_reason,args=(packet,),daemon=True);logger.start();logger.join(.05)
    time.sleep(.1)
    if os.getppid()==packet['parent_pid']:os.kill(packet['parent_pid'],signal.SIGKILL)
    return 0

if __name__=='__main__':raise SystemExit(main())
