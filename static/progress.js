'use strict';
let progressPreviewData, progressPreviewSignature = '', progressPreviewRevision = 0, progressPreviewController;
let progressDemoStarted = 0, progressDemoRunning = false, progressPreviewFraction = 0, progressLastDraw = '';
const progressCanvas = $('#preview-progress');
const progressContext = progressCanvas.getContext('2d');

async function syncProgressPreview() {
  const config = formSettings();
  progressCanvas.hidden = !config.progress_enabled;
  $('#progress-demo').disabled = !config.progress_enabled;
  $('#progress-preview-position').disabled = !config.progress_enabled;
  if (!config.progress_enabled) {
    progressPreviewController?.abort(); ++progressPreviewRevision; progressDemoRunning=false;
    progressPreviewSignature='';progressPreviewData=null;$('#progress-overlay-status').textContent='';
    return;
  }
  const style = {font:config.font,resolution:config.resolution};
  Object.entries(config).filter(([name])=>name.startsWith('progress_')).forEach(([name,value])=>style[name]=value);
  const signature = JSON.stringify(style);
  if (signature === progressPreviewSignature) return;
  progressPreviewSignature=signature;
  progressPreviewController?.abort();progressPreviewController=new AbortController();
  const revision=++progressPreviewRevision;
  try {
    const data=await api('/preview/progress',{method:'POST',body:JSON.stringify({settings:config}),signal:progressPreviewController.signal});
    if(revision!==progressPreviewRevision)return;
    const base=new Image(),fill=new Image();base.src=data.base_url;fill.src=data.fill_url;
    await Promise.all([base.decode(),fill.decode()]);
    if(revision!==progressPreviewRevision)return;
    progressPreviewData={...data,base,fill};progressLastDraw='';
    progressCanvas.width=data.geometry.frame_width;progressCanvas.height=data.geometry.frame_height;
    $('#progress-overlay-status').classList.remove('error');
    $('#progress-overlay-status').textContent='Полная шкала на 80% времени ролика. Шрифт подписи совпадает со шрифтом основной надписи.';
    drawProgressPreview();
  } catch(error) {
    if(error.name!=='AbortError'&&revision===progressPreviewRevision){progressPreviewSignature='';$('#progress-overlay-status').textContent=error.message;$('#progress-overlay-status').classList.add('error');}
  }
}

function drawProgressPreview() {
  if (!progressPreviewData || progressCanvas.hidden) return;
  const g=progressPreviewData.geometry;
  const fraction=Math.max(0,Math.min(1,progressPreviewFraction));
  const t=Math.min(1,fraction/g.finish_fraction);
  const pixels=fraction>=g.finish_fraction?g.width:Math.min(g.width-1,g.width*(1-Math.pow(1-t,g.easing)));
  const signature=`${progressPreviewRevision}:${pixels.toFixed(2)}`;
  if(signature!==progressLastDraw){
    progressContext.clearRect(0,0,progressCanvas.width,progressCanvas.height);
    progressContext.drawImage(progressPreviewData.base,g.x,g.y);
    progressContext.save();progressContext.beginPath();progressContext.rect(g.x,g.y,pixels,g.bar_height);progressContext.clip();
    progressContext.drawImage(progressPreviewData.fill,g.x,g.y);progressContext.restore();progressLastDraw=signature;
  }
  $('#progress-preview-position').value=(fraction*100).toFixed(1);
  $('#progress-preview-status').textContent=`${Math.round(fraction*100)}% времени · ${Math.floor(pixels/g.width*100)}% шкалы`;
}

$('#progress-demo').onclick=()=>{
  if(progressDemoRunning){progressDemoRunning=false;$('#progress-demo').textContent='▶ Продолжить анимацию';return;}
  $('#preview-video').pause();
  const config=formSettings(),seconds=(Number(config.duration_min)+Number(config.duration_max))/2;
  if(progressPreviewFraction>=1)progressPreviewFraction=0;
  progressDemoStarted=performance.now()-progressPreviewFraction*seconds*1000;
  progressDemoRunning=true;$('#progress-demo').textContent='❚❚ Приостановить анимацию';
};
$('#progress-preview-position').oninput=event=>{
  progressDemoRunning=false;$('#progress-demo').textContent='▶ Показать анимацию';
  progressPreviewFraction=Number(event.target.value)/100;
  const video=$('#preview-video');video.pause();
  if(!video.hidden&&Number.isFinite(video.duration))video.currentTime=progressPreviewFraction*video.duration;
  drawProgressPreview();
};
$('#preview-video').addEventListener('play',()=>{progressDemoRunning=false;$('#progress-demo').textContent='▶ Показать анимацию';});
$('#preview-video').addEventListener('timeupdate',()=>{
  if(progressDemoRunning)return;
  const video=$('#preview-video');
  if(Number.isFinite(video.duration)&&video.duration>0){progressPreviewFraction=video.currentTime/video.duration;drawProgressPreview();}
});
document.addEventListener('click',event=>{
  const preset=event.target.closest('[data-progress-text]');
  if(preset){$('[data-field="progress_text"]').value=preset.dataset.progressText;$('[data-field="progress_enabled"]').checked=true;markChanged();}
});
function animateProgressPreview(now) {
  if(progressDemoRunning&&page==='create'&&!progressCanvas.hidden){
    const seconds=(Number($('[data-field="duration_min"]').value)+Number($('[data-field="duration_max"]').value))/2 || 14;
    progressPreviewFraction=Math.min(1,(now-progressDemoStarted)/(seconds*1000));
    if(progressPreviewFraction>=1){progressDemoRunning=false;$('#progress-demo').textContent='↻ Повторить анимацию';}
    drawProgressPreview();
  } else if(page==='create'&&!progressCanvas.hidden){
    const video=$('#preview-video');
    if(!video.paused&&Number.isFinite(video.duration)&&video.duration>0){progressPreviewFraction=video.currentTime/video.duration;drawProgressPreview();}
  }
  requestAnimationFrame(animateProgressPreview);
}
requestAnimationFrame(animateProgressPreview);
if(state)syncProgressPreview();
