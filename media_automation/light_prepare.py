"""Opt-in isolated preparation, not discovery/import. No decoding/content hashes.
A structural screen cannot establish full AV integrity; preserve original for repair.
"""
import json,os,subprocess,time,resource,fcntl
from pathlib import Path
from .importer import identity_seal
from .core import ReviewRequired

def require(ok,msg):
 if not ok:raise ReviewRequired(msg)
def probe(path):
 p=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-show_chapters','-of','json',str(path)],capture_output=True,text=True,timeout=60,check=True)
 return json.loads(p.stdout)
def language(s):return s.get('tags',{}).get('language','').lower()
def streams(p):return p.get('streams',[])
def flags(s,default=None,forced=None):
 d=dict(s.get('disposition',{}))
 if default is not None:d['default']=default
 if forced is not None:d['forced']=forced
 return '+'.join(k for k,v in d.items() if v) or '0'
def command(source,target,before,english_index):
 # Keep every track; specify dispositions so the muxer does not choose new defaults.
 args=['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-n','-i',str(source),'-map','0','-map_metadata','0','-map_chapters','0','-c','copy']
 for pos,s in enumerate(streams(before)):
  if s['codec_type'] not in ('audio','subtitle'):args += ['-disposition:'+str(pos),flags(s)]
  if s['codec_type']=='subtitle' and language(s) in ('eng','en'):args += ['-disposition:'+str(pos),flags(s)]
  if s['codec_type']=='audio':args += ['-disposition:'+str(pos),flags(s,default=int(s['index']==english_index))]
  if s['codec_type']=='subtitle' and language(s) not in ('eng','en'):args += ['-disposition:'+str(pos),flags(s,default=0,forced=0)]
 return args+[str(target)]
def compare(before,after,english_index,tolerance):
 a,b=streams(before),streams(after);require(len(a)==len(b),'Track count changed')
 fields=('codec_type','codec_name','width','height','sample_rate','channels','channel_layout')
 for old,new in zip(a,b):
  require(all(old.get(k)==new.get(k) for k in fields),'Track structure changed')
  require(language(old)==language(new),'Track language changed')
  for k in ('title','filename','mimetype'):require(old.get('tags',{}).get(k)==new.get('tags',{}).get(k),'Track metadata changed')
  expected=dict(old.get('disposition',{}))
  if old['codec_type']=='audio':expected['default']=int(old['index']==english_index)
  if old['codec_type']=='subtitle' and language(old) not in ('eng','en'):expected.update(default=0,forced=0)
  require(all(bool(expected.get(k,0))==bool(new.get('disposition',{}).get(k,0)) for k in set(expected)|set(new.get('disposition',{}))),'Unexpected track disposition change')
 require(abs(float(before['format']['duration'])-float(after['format']['duration']))<=tolerance,'Duration changed beyond explicit tolerance')
 oldch,newch=before.get('chapters',[]),after.get('chapters',[]);require(len(oldch)==len(newch),'Chapter count changed')
 for x,y in zip(oldch,newch):
  require(x.get('tags',{})==y.get('tags',{}),'Chapter tags changed')
  require(all(abs(float(x[k])-float(y[k]))<=tolerance for k in ('start_time','end_time')),'Chapter timing changed')
def original_language_selection(review, source, seal, streams, index, max_bytes, max_height):
    raise ReviewRequired("Installation-specific original-language remux approval not supported")


def prepare(source,destination,record,*,expected_seal,max_bytes,max_height,english_index,english_evidence=False,original_language_review=None,duration_tolerance=0.1,probe_fn=probe,run_fn=None):
 source,destination,record=map(Path,(source,destination,record));start=time.monotonic();cpu=resource.getrusage(resource.RUSAGE_CHILDREN)
 require(type(max_bytes)==int and max_bytes>0 and type(max_height)==int and max_height>0,'Explicit size/resolution limits required')
 require(type(duration_tolerance) in (int,float) and 0<=duration_tolerance<=0.25,'Explicit bounded timing tolerance required')
 for p in (source,destination,record):require(p.is_absolute() and not any(x.is_symlink() for x in [p,*p.parents]),'Absolute nonsymlink paths required')
 require(source!=destination and source!=record and destination!=record,'Distinct paths required')
 require(identity_seal(source)==expected_seal,'Source changed')
 require(not destination.exists() and not record.exists(),'Existing output/intent requires reconciliation, never retry blindly')
 require(source.stat().st_size<=max_bytes,'Source exceeds explicit size limit')
 before=probe_fn(source);require(identity_seal(source)==expected_seal,'Source changed during probe');ss=streams(before)
 require(any(s.get('codec_type')=='video' for s in ss),'Video stream missing')
 require(all(s.get('height',0)<=max_height for s in ss if s.get('codec_type')=='video'),'Resolution exceeds limit')
 aud=[s for s in ss if s.get('codec_type')=='audio'];selected=[s for s in aud if s.get('index')==english_index]
 selection_record={}
 if original_language_review is not None:
  selection_record=original_language_selection(original_language_review,source,expected_seal,ss,english_index,max_bytes,max_height)
 else:
  require(len(selected)==1 and (language(selected[0]) in ('eng','en') or (not language(selected[0]) and english_evidence is True)),'English audio identity unproved')
 needs=any(bool(s.get('disposition',{}).get('default')) != (s['index']==english_index) for s in aud) or any((s.get('disposition',{}).get('default') or s.get('disposition',{}).get('forced')) and language(s) not in ('eng','en') for s in ss if s.get('codec_type')=='subtitle')
 if not needs:return {**selection_record,'mode':'ORIGINAL','path':str(source),'identity_seal':expected_seal,'probe':before,'probe_calls':1,'full_hashes':0,'decode_passes':0,'elapsed_seconds':time.monotonic()-start}
 require(destination.suffix.lower()=='.mkv','All-track remux requires explicit MKV destination')
 # Keep interrupted output away from the admitted folder; the intent names the partial.
 partial=record.parent/('.'+record.name+'.partial.mkv')
 require(not partial.is_relative_to(destination.parent), 'Partial staging must be outside admitted media folder')
 require(record.parent.stat().st_dev==destination.parent.stat().st_dev, 'Staging and prepared output must share filesystem')
 require(not partial.exists() and not partial.is_symlink(),'Partial output requires reconciliation')
 result_path=record.with_name(record.name+'.result');require(not result_path.exists() and not result_path.is_symlink(),'Existing result requires reconciliation')
 require(destination.parent.is_dir() and record.parent.is_dir(),'Review output folders must preexist')
 statv=os.statvfs(destination.parent);require(statv.f_bavail*statv.f_frsize>=source.stat().st_size+21474836480,'Insufficient space plus20GiB reserve')
 fd=os.open(record,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 try:
  fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
  intent={**selection_record,'status':'INTENT','source':str(source),'source_identity_seal':expected_seal,'destination':str(destination),'partial':str(partial),'english_index':english_index,'max_bytes':max_bytes,'max_height':max_height,'duration_tolerance':duration_tolerance}
  os.write(fd,json.dumps(intent).encode());os.fsync(fd)
  argv=command(source,partial,before,english_index)
  (run_fn or (lambda c:subprocess.run(c,check=True,timeout=3600)))(argv)
  require(identity_seal(source)==expected_seal,'Original changed during remux')
  require(partial.is_file() and not partial.is_symlink(),'Remux output missing/unsafe');partial_seal=identity_seal(partial);after=probe_fn(partial);compare(before,after,english_index,duration_tolerance)
  require(identity_seal(partial)==partial_seal and identity_seal(source)==expected_seal,'Source/output changed during output probe')
  require(partial.stat().st_size<=max_bytes,'Prepared output exceeds explicit byte limit')
  with partial.open('rb') as output:os.fsync(output.fileno())
  os.utime(partial,ns=(partial.stat().st_atime_ns,source.stat().st_mtime_ns));os.link(partial,destination)
  require(identity_seal(source)==expected_seal,'Original changed before publication')
  result={**selection_record,'status':'PREPARED','mode':'STREAM_COPY','path':str(destination),'source_identity_seal':expected_seal,'prepared_identity_seal':identity_seal(destination),'probe':after,'probe_calls':2,'full_hashes':0,'decode_passes':0,'all_tracks_preserved_structurally':True,'full_av_integrity_proved':False,'elapsed_seconds':time.monotonic()-start,'child_cpu_seconds':(resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime+resource.getrusage(resource.RUSAGE_CHILDREN).ru_stime)-(cpu.ru_utime+cpu.ru_stime),'maximum_bytes':max_bytes,'maximum_height':max_height,'bytes_written':destination.stat().st_size,'partial_retained_same_inode':True}
  with record.with_name(record.name+'.result').open('x') as f:json.dump(result,f,indent=2);f.flush();os.fsync(f.fileno())
  return result
 finally:os.close(fd)
