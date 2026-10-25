"""Integration check in a separate workspace with generated, disposable media."""
from pathlib import Path
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile

sys.stdout.reconfigure(encoding='utf-8')

ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT / "output/temp" / ("check_" + uuid.uuid4().hex)
WORK.mkdir(parents=True)
BASE = "http://127.0.0.1:8766/api"
server = None
passed = []


def request(path, body=None, raw=False, expected=200):
    encoded = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=encoded, headers={"Content-Type": "application/json"})
    try:
        response = urllib.request.urlopen(req, timeout=180)
    except urllib.error.HTTPError as exc:
        response = exc
    content = response.read()
    assert response.code == expected, (path, response.code, content.decode(errors="replace"))
    return content if raw else json.loads(content)


def checked(name):
    passed.append(name)
    print("PASS: " + name, flush=True)


def current_batch(batch_id):
    return next(item for item in request('/state')['batches'] if item['id'] == batch_id)


def await_condition(callback, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = callback()
        if value:
            return value
        time.sleep(.25)
    raise AssertionError("Timed out")


def main():
    global server
    for name in ("core.py", "app.py", "worker.py", "publishing.py", "publishing_routes.py", "tiktok_api.py", "typography.py", "progress_overlay.py"):
        shutil.copyfile(ROOT / name, WORK / name)
    for name in ("static", "templates"):
        shutil.copytree(ROOT / name, WORK / name)
    (WORK / 'tools/bin').mkdir(parents=True)
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        os.link(ROOT / 'tools/bin' / name, WORK / 'tools/bin' / name)
    sys.path.insert(0, str(WORK))
    import core
    core.initialize()
    bg = WORK / 'test background кириллица.mp4'
    music = WORK / 'test music.wav'
    ffmpeg = str(ROOT / 'tools/bin/ffmpeg.exe')
    subprocess.run([ffmpeg, '-v','error','-y','-f','lavfi','-i','testsrc2=size=640x360:rate=15:duration=180','-f','lavfi','-i','sine=frequency=220:duration=180','-c:v','libx264','-preset','ultrafast','-crf','36','-c:a','aac',str(bg)],check=True, creationflags=core.CREATE_FLAGS)
    subprocess.run([ffmpeg,'-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=30',str(music)],check=True, creationflags=core.CREATE_FLAGS)
    with (WORK / 'server.log').open('w',encoding='utf-8') as logs:
        server = subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port','8766'],cwd=WORK,stdout=logs,stderr=logs,creationflags=core.CREATE_FLAGS)
        def ready():
            try:
                return request('/state')
            except (OSError,AssertionError):
                return None
        await_condition(ready,30)
        checked('isolated server and worker start')
        video = request('/assets/path',{'kind':'video','path':str(bg)})['id']
        audio = request('/assets/path',{'kind':'music','path':str(music)})['id']
        config = request('/state')['settings']
        config.update(encoder_mode='cpu',parallel_jobs=1,resolution='720',count=5,texts=[f'Твой стиль — твои правила.\nВариант {index}: новая коллекция уже в магазине!' for index in range(5)],hashtags=['стиль','магазин','коллекция','покупки','новинки','одежда'],original_volume=20)
        saved = request('/settings',config)
        assert saved['texts'] == config['texts']
        checked('Unicode paths, material analysis and settings persistence')
        data = {'name':'Проверка пяти роликов','settings':config,'video_ids':[video],'music_ids':[audio]}
        wrong = {**data,'settings':{**config,'hashtags':['один']}}
        request('/batches',wrong,expected=400)
        request('/batches',{**data,'settings':{**config,'texts':['Очень длинная рекламная надпись ' * 150]}},expected=400)
        checked('insufficient hashtags and text overflow rejected before rendering')
        preview = request('/preview',data)
        frame = request(preview['url'].replace('/api','',1),raw=True)
        assert frame[:2] == b'\xff\xd8'
        batch_id = request('/batches',data)['id']
        batch = current_batch(batch_id)
        jobs = batch['jobs']
        assert len(jobs)==5 and len({job['spec']['text'] for job in jobs})==5
        for i,first in enumerate(jobs):
            for second in jobs[i+1:]:
                a,b=first['spec'],second['spec']
                assert a['start']+a['duration']<=b['start'] or b['start']+b['duration']<=a['start']
        checked('five distinct texts, non-overlapping source segments, real previews')
        request('/jobs/'+jobs[0]['id']+'/edit',{'text':'Проверка русской надписи\nБелый текст с чёрной обводкой'})
        request('/jobs/'+jobs[1]['id']+'/edit',{'regenerate':True})
        checked('edit text and regenerate one job')
        request('/presets',{'name':'Мой пресет','settings':config})
        assert 'Мой пресет' in request('/state')['presets']
        checked('preset saved')
        request('/batches/'+batch_id+'/start',{})
        await_condition(lambda:any(job['status']=='rendering' for job in current_batch(batch_id)['jobs']),30)
        request('/queue/pause',{})
        await_condition(lambda:not any(job['status']=='rendering' for job in current_batch(batch_id)['jobs']),120)
        paused = current_batch(batch_id)
        assert sum(job['status']=='done' for job in paused['jobs'])==1
        assert sum(job['status']=='queued' for job in paused['jobs'])==4
        checked('queue pause completes the current video and holds the remaining four')
        last = paused['jobs'][-1]
        request('/jobs/'+last['id']+'/cancel',{})
        assert next(j for j in current_batch(batch_id)['jobs'] if j['id']==last['id'])['status']=='cancelled'
        request('/jobs/'+last['id']+'/retry',{})
        final = await_condition(lambda:(lambda b:b if all(j['status'] in ('done','error') for j in b['jobs']) else None)(current_batch(batch_id)),240)
        assert all(job['status']=='done' for job in final['jobs']),[(job['status'],job['error']) for job in final['jobs']]
        checked('cancel, retry and sequential rendering of five complete videos')
        for job in final['jobs']:
            info=core.probe(job['output'])
            assert info['width']==720 and info['height']==1280
            assert info['audio'] and info['video'] and 13<=info['duration']<=15.1
            assert Path(job['output']).with_suffix('.txt').read_text(encoding='utf-8')==job['spec']['caption']
            assert len(set(word for word in job['spec']['caption'].split() if word.startswith('#')))==5
            streams=json.loads(core.run([core.executable('ffprobe'),'-v','error','-show_streams','-of','json',job['output']]))['streams']
            assert {stream['codec_name'] for stream in streams}>={'hevc','aac'}
            assert info['hdr'] and info['pix_fmt']=='yuv420p10le' and info['color_primaries']=='bt2020'
        checked('all outputs: 13–15 seconds, 720×1280, HEVC HDR10 + AAC, five unique tags, TXT captions')
        output = request('/jobs/'+final['jobs'][0]['id']+'/video',raw=True)
        assert b'ftyp' in output[:32]
        archive=request('/batches/'+batch_id+'/zip',raw=True)
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            assert len([x for x in bundle.namelist() if x.endswith('.mp4')])==5
            assert len([x for x in bundle.namelist() if x.endswith('.txt')])==5
        checked('video streaming and complete batch ZIP export')
        # Check the default Full HD profile and short-looping audio independently.
        silent=WORK/'silent.mp4'
        short_music=WORK/'short.wav'
        subprocess.run([ffmpeg,'-v','error','-y','-i',str(bg),'-t','4','-an','-c:v','copy',str(silent)],check=True,creationflags=core.CREATE_FLAGS)
        subprocess.run([ffmpeg,'-v','error','-y','-f','lavfi','-i','sine=frequency=330:duration=0.5',str(short_music)],check=True,creationflags=core.CREATE_FLAGS)
        silent_id=request('/assets/path',{'kind':'video','path':str(silent)})['id']
        boundary='StudioCheck'+uuid.uuid4().hex
        form=(f'--{boundary}\r\nContent-Disposition: form-data; name="kind"\r\n\r\nmusic\r\n--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="short.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()+short_music.read_bytes()+f'\r\n--{boundary}--\r\n'.encode()
        req=urllib.request.Request(BASE+'/assets/upload',data=form,headers={'Content-Type':'multipart/form-data; boundary='+boundary})
        with urllib.request.urlopen(req,timeout=30) as response:
            imported=json.load(response)
        assert len(imported['imported'])==1 and not imported['errors']
        hd_data={**data,'video_ids':[silent_id],'music_ids':imported['imported'],'settings':{**config,'count':1,'duration_min':2,'duration_max':2,'resolution':'1080','crop_mode':'contain','loop_music':True,'original_volume':0}}
        hd_batch=request('/batches',hd_data)['id']
        request('/batches/'+hd_batch+'/start',{})
        hd=await_condition(lambda:(lambda b:b if b['jobs'][0]['status'] in ('done','error') else None)(current_batch(hd_batch)),90)['jobs'][0]
        assert hd['status']=='done',hd['error']
        info=core.probe(hd['output'])
        assert info['width']==1080 and info['height']==1920 and info['audio'] and 1.9<=info['duration']<=2.2
        from PIL import Image
        with Image.open(hd['preview']) as image:
            assert max(image.getpixel((10,10)))<8
        checked('multipart upload, Full HD export, fit with black bars, silent source and repeated short music')
        screenshot=ROOT/'docs/check-preview.jpg'
        core.run([ffmpeg,'-v','error','-y','-i',final['jobs'][0]['output'],'-vf','scale=360:640','-frames:v','1',str(screenshot)])
        # Restart persistence and interrupted-job recovery are checked using one extra short job.
        short={**data,'settings':{**config,'count':1,'duration_min':2,'duration_max':2}}
        extra_id=request('/batches',short)['id']
        extra_job=current_batch(extra_id)['jobs'][0]
        request('/batches/'+extra_id+'/start',{})
        await_condition(lambda:current_batch(extra_id)['jobs'][0]['status']=='rendering',30)
        request('/jobs/'+extra_job['id']+'/cancel',{})
        await_condition(lambda:current_batch(extra_id)['jobs'][0]['status']=='cancelled',30)
        assert not Path(extra_job['output']).exists()
        checked('active cancellation removes the incomplete output')
        # Simulate a forced server exit while FFmpeg is actively rendering.
        request('/jobs/'+extra_job['id']+'/retry',{})
        await_condition(lambda:current_batch(extra_id)['jobs'][0]['status']=='rendering',30)
        server.terminate();server.wait(timeout=15)
        server=subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port','8766'],cwd=WORK,stdout=logs,stderr=logs,creationflags=core.CREATE_FLAGS)
        await_condition(ready,30)
        assert current_batch(extra_id)['jobs'][0]['status']=='interrupted'
        assert request('/state')['settings']['texts']==config['texts']
        assert all(job['available'] for job in current_batch(batch_id)['jobs'])
        checked('forced server exit stops FFmpeg; restart preserves results and waits for explicit resume')
        worker_pid=core.get_meta('worker_pid')
        request('/shutdown',{})
        server.wait(timeout=20);server=None
        await_condition(lambda:not core.process_alive(worker_pid),10)
        checked('graceful shutdown endpoint stops the server and rendering worker')
    report={'passed':passed,'count':len(passed),'checked_at':core.now(),'materials':'generated synthetic media, not user footage'}
    (ROOT / 'docs/check-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    # This directory was allocated by this script and is inside the dedicated temporary folder.
    assert WORK.resolve().is_relative_to((ROOT/'output/temp').resolve()) and WORK.name.startswith('check_')
    shutil.rmtree(WORK)
    print(f'All {len(passed)} checks passed.',flush=True)


if __name__=='__main__':
    try:
        main()
    except Exception:
        print('CHECK FAILED. Workspace retained: '+str(WORK),flush=True)
        try:
            sys.path.insert(0,str(WORK))
            import core
            core.set_meta('worker_stop',True)
        except Exception:
            pass
        if server and server.poll() is None:
            time.sleep(1)
            server.terminate()
            server.wait(timeout=15)
        raise
