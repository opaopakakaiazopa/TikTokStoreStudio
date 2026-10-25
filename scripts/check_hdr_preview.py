"""Verify actual PQ levels, 10-bit HDR output, text layers and browser copies."""
from pathlib import Path
from array import array
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

sys.stdout.reconfigure(encoding='utf-8')
ROOT=Path(__file__).resolve().parents[1]
WORK=ROOT/'output/temp'/('check_hdr_'+uuid.uuid4().hex)
WORK.mkdir(parents=True)
server=None
passed=[]
BASE='http://127.0.0.1:8766/api'

def call(path,data=None,raw=False):
    req=urllib.request.Request(BASE+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=180) as response:
        value=response.read()
    return value if raw else json.loads(value)

def wait_for(fn,timeout=90):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        result=fn()
        if result:return result
        time.sleep(.2)
    raise AssertionError('Timed out')

def ready():
    try:return call('/state')
    except OSError:return None

def ok(name):
    passed.append(name);print('PASS: '+name,flush=True)

def pq_y(nits):
    value=(nits/10000)**(2610/16384)
    return 64+876*((3424/4096+2413/128*value)/(1+2392/128*value))**(2523/32)

def main():
    global server
    for name in ('core.py','app.py','worker.py','publishing.py','publishing_routes.py','tiktok_api.py','typography.py','progress_overlay.py'):shutil.copyfile(ROOT/name,WORK/name)
    for name in ('static','templates'):shutil.copytree(ROOT/name,WORK/name)
    (WORK/'tools/bin').mkdir(parents=True)
    for name in ('ffmpeg.exe','ffprobe.exe'):os.link(ROOT/'tools/bin'/name,WORK/'tools/bin'/name)
    sys.path.insert(0,str(WORK))
    import core
    from PIL import Image
    core.initialize();f=core.executable('ffmpeg')
    sdr=WORK/'white_sdr.mp4';hdr=WORK/'white_1000nits.mp4';music=WORK/'music.wav'
    core.run([f,'-v','error','-y','-f','lavfi','-i','color=white:s=320x180:r=30:d=3','-c:v','libx264','-crf','0','-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709',str(sdr)])
    conversion='zscale=pin=bt709:tin=bt709:min=bt709:rin=limited:p=bt2020:t=linear:m=gbr:r=full,format=gbrpf32le,zscale=pin=bt2020:tin=linear:min=gbr:rin=full:p=bt2020:t=smpte2084:m=bt2020nc:r=limited:npl=1000,format=yuv420p10le'
    core.run([f,'-v','error','-y','-i',str(sdr),'-vf',conversion,*core.hdr_encoder_options({'hdr':True}),str(hdr)])
    core.run([f,'-v','error','-y','-f','lavfi','-i','sine=d=3',str(music)])
    logs=(WORK/'server.log').open('w',encoding='utf-8')
    server=subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port','8766'],cwd=WORK,stdout=logs,stderr=logs,creationflags=core.CREATE_FLAGS)
    wait_for(ready,30)
    config=call('/state')['settings'];config.update(encoder_mode='cpu',parallel_jobs=1,count=1,duration_min=1,duration_max=1,resolution='720',font_size=48,line_spacing=1.4,letter_spacing=2,text_align='right',text_rotation=8,progress_enabled=True,progress_text='В КОНЦЕ ПРОМО',texts=['Текст HDR'],hashtags=['один','два','три','четыре','пять'])
    config=call('/settings',{**config,'export_profile':'sdr'})
    assert config['export_profile']=='hdr10'
    ok('HDR export is enforced even when a client requests SDR')
    first=call('/preview/layer',{'settings':config,'text':'Текст'})
    second=call('/preview/layer',{'settings':{**config,'font':'impact'},'text':'Текст'})
    third=call('/preview/layer',{'settings':{**config,'text_x':65,'text_y':65},'text':'Текст'})
    assert first['url']!=second['url'] and first['layout']['bbox']!=second['layout']['bbox']
    assert third['layout']['bbox'][0]>first['layout']['bbox'][0] and third['layout']['bbox'][1]>first['layout']['bbox'][1]
    image=Image.open(io.BytesIO(call(first['url'].replace('/api','',1),raw=True)))
    assert image.mode=='RGBA' and image.getpixel((0,0))[3]==0
    ok('real-font layers change with typography and position, with transparent backgrounds')
    track=call('/assets/path',{'kind':'music','path':str(music)})['id']
    for source,brightness in ((sdr,203),(hdr,1000)):
        asset=call('/assets/path',{'kind':'video','path':str(source)})['id']
        clip=call('/preview/clip',{'asset_id':asset,'start':0})
        clip_path=WORK/'output/previews'/('clip_'+clip['url'].split('/')[-1]+'.mp4')
        assert core.probe(clip_path)['codec']=='h264'
        assert not core.probe(clip_path)['hdr']
        batch_id=call('/batches',{'settings':config,'video_ids':[asset],'music_ids':[track]})['id']
        call('/batches/'+batch_id+'/start',{})
        def finished():
            batch=next(x for x in call('/state')['batches'] if x['id']==batch_id)
            job=batch['jobs'][0]
            return job if job['status'] in ('done','error') else None
        job=wait_for(finished)
        assert job['status']=='done',job['error']
        info=core.probe(job['output'])
        assert info['codec']=='hevc' and info['pix_fmt']=='yuv420p10le' and info['profile']=='Main 10'
        assert info['color_transfer']=='smpte2084' and info['color_primaries']=='bt2020' and info['color_space']=='bt2020nc'
        raw=subprocess.run([f,'-v','error','-i',job['output'],'-frames:v','1','-pix_fmt','yuv420p10le','-f','rawvideo','pipe:1'],capture_output=True,check=True,creationflags=core.CREATE_FLAGS).stdout
        luma=array('H');luma.frombytes(raw[:720*1280*2])
        background=luma[20*720+20]
        assert abs(background-pq_y(brightness))<10,(brightness,background,pq_y(brightness))
        overlay=Image.open(job['spec']['overlay'])
        white=next((x,y) for y in range(overlay.height) for x in range(overlay.width) if overlay.getpixel((x,y))==(255,255,255,255))
        text_value=luma[white[1]*720+white[0]]
        assert abs(text_value-pq_y(203))<25,(brightness,text_value,pq_y(203))
        metadata=json.loads(core.run([core.executable('ffprobe'),'-v','error','-select_streams','v:0','-read_intervals','%+#1','-show_frames','-of','json',job['output']]))
        assert any('Mastering display' in side.get('side_data_type','') for frame in metadata['frames'] for side in frame.get('side_data_list',[]))
        assert job['playback_available'] and call('/jobs/'+job['id']+'/playback',raw=True)
        ok(f'{brightness}-nit source: HEVC Main10/PQ/BT.2020, measured luminance preserved; text ~203 nits; browser copy available')
    call('/shutdown',{});server.wait(timeout=20);server=None;logs.close()
    (ROOT/'docs/hdr-check-results.json').write_text(json.dumps({'passed':passed,'count':len(passed),'checked_at':core.now()},ensure_ascii=False,indent=2),encoding='utf-8')
    assert WORK.resolve().is_relative_to((ROOT/'output/temp').resolve()) and WORK.name.startswith('check_hdr_')
    shutil.rmtree(WORK)
    print('All HDR and preview checks passed.',flush=True)

if __name__=='__main__':
    try:main()
    except Exception:
        print('FAILED. Retained workspace: '+str(WORK),flush=True)
        try:call('/shutdown',{});server.wait(timeout=20)
        except Exception:
            if server and server.poll() is None:server.terminate()
        raise
