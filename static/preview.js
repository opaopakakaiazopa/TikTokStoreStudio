'use strict';
let layerTimer, layerController, layerRevision = 0, currentLayout, dragState;
let previewTextSignature = '', previewSourceSignature = '', clipRevision = 0, loadedClipKey = '';

function previewStatus(text, error = false) {
  $('#preview-status').textContent = text;
  $('#preview-status').classList.toggle('error', error);
}

function syncPreviewTexts() {
  const texts = formSettings().texts;
  const signature = JSON.stringify(texts);
  if (signature === previewTextSignature) {syncPreviewEditor();return;}
  previewTextSignature = signature;
  const previous = Number($('#preview-text-select').value || 0);
  $('#preview-text-select').innerHTML = texts.length ? texts.map((text,index)=>`<option value="${index}">${index+1}. ${esc(text.slice(0,65))}</option>`).join('') : '<option value="0">Образец оформления</option>';
  $('#preview-text-select').value = String(Math.min(previous, Math.max(0,texts.length-1)));
  syncPreviewEditor();
}

function syncPreviewEditor() {
  const editor=$('#preview-text-editor');
  const value=formSettings().texts[Number($('#preview-text-select').value || 0)] || '';
  if(document.activeElement!==editor&&editor.value!==value)editor.value=value;
}

function syncPreviewSources() {
  const videos = (state?.assets || []).filter(asset=>selected('video').includes(asset.id));
  const signature = JSON.stringify(videos.map(asset=>[asset.id,asset.info.size]));
  if (signature === previewSourceSignature) return;
  previewSourceSignature = signature;
  const previous = $('#preview-source-select').value;
  $('#preview-source-select').innerHTML = videos.length ? videos.map(asset=>`<option value="${asset.id}">${esc(asset.name)}</option>`).join('') : '<option value="">Добавь фоновое видео</option>';
  $('#preview-source-select').value = videos.some(asset=>asset.id===previous) ? previous : videos[0]?.id || '';
  if ($('#preview-source-select').value !== previous || !loadedClipKey) loadPreviewClip();
}

function applyBackgroundStyle() {
  const config = formSettings();
  for (const media of [$('#preview-video'), $('#live-preview')]) {
    media.style.objectFit = config.crop_mode === 'contain' ? 'contain' : 'cover';
    media.style.objectPosition = `${config.focus_x}% ${config.focus_y}%`;
  }
}

function queueLivePreview() {
  applyBackgroundStyle(); syncPreviewTexts();
  if(typeof syncProgressPreview==='function')syncProgressPreview();
  clearTimeout(layerTimer);
  layerController?.abort();
  const revision = ++layerRevision;
  layerTimer = setTimeout(()=>updateLayer(revision), 100);
}

async function updateLayer(revision) {
  layerController = new AbortController();
  const config = formSettings();
  const text = document.activeElement === $('#preview-text-editor') ? $('#preview-text-editor').value : config.texts[Number($('#preview-text-select').value || 0)] || '';
  try {
    const result = await api('/preview/layer',{method:'POST',body:JSON.stringify({settings:config,text}),signal:layerController.signal});
    if (revision !== layerRevision) return;
    const loaded = new Image(); loaded.src = result.url; await loaded.decode();
    if (revision !== layerRevision || dragState) return;
    currentLayout = result.layout;
    const layer = $('#preview-layer'); layer.src = result.url; layer.hidden = false; layer.style.transform = '';
    const handle = $('#text-drag-handle');
    const [left,top,right,bottom] = currentLayout.bbox || [0,0,0,0];
    Object.assign(handle.style,{left:`${left/currentLayout.width*100}%`,top:`${top/currentLayout.height*100}%`,width:`${(right-left)/currentLayout.width*100}%`,height:`${(bottom-top)/currentLayout.height*100}%`,transform:''});
    handle.hidden = !currentLayout.bbox;
    let clamped = false;
    if(config.keep_text_in_frame){
      for(const [name,axis] of [['text_x','x'],['text_y','y']]){
        const actual=currentLayout.position[axis];
        if(Math.abs(actual-config[name])>.15){$(`[data-field="${name}"]`).value=actual.toFixed(2);clamped=true;}
      }
      if(clamped){updateSummaries();clearTimeout(saveTimer);saveTimer=setTimeout(()=>saveSettings(false),1000);}
    }
    previewStatus(`${result.sample?'Образец · ':''}Шрифт ${result.layout.font_size} px · ${result.layout.lines} строк${clamped?' · достигнута граница кадра':' · можно перетаскивать'}`);
  } catch(error) {
    if (error.name !== 'AbortError' && revision === layerRevision) previewStatus(error.message,true);
  }
}

async function loadPreviewClip(force = false) {
  const assetId = $('#preview-source-select').value;
  const revision = ++clipRevision;
  const video = $('#preview-video'); video.pause();
  $('#preview-play').disabled = true; $('#preview-timeline').disabled = true;
  if (!assetId) {
    video.hidden = true; video.removeAttribute('src'); video.load();
    $('#live-preview').hidden = true; $('#preview-empty').hidden = false;
    loadedClipKey = ''; return;
  }
  const start = Number($('#preview-start').value || 0);
  const key = assetId + ':' + start;
  if (!force && key === loadedClipKey) {$('#preview-play').disabled=false;$('#preview-timeline').disabled=false;return;}
  const button = $('#preview-button'); button.disabled = true; button.textContent = 'Готовим фон…';
  try {
    const result = await post('/preview/clip',{asset_id:assetId,start});
    if (revision !== clipRevision) return;
    loadedClipKey = key;
    $('#preview-start').value=result.start; $('#preview-start').max=Math.max(0,result.source_duration-.1).toFixed(1);
    video.poster = result.poster; video.src=result.url; video.hidden=false;
    $('#live-preview').hidden=true; $('#live-preview').src=result.poster;
    $('#preview-empty').hidden=true;
    $('#preview-timeline').max=result.duration; $('#preview-timeline').value=0;
    $('#preview-play').disabled=false; $('#preview-timeline').disabled=false;
    applyBackgroundStyle();
  } catch(error) {
    if (revision===clipRevision) {loadedClipKey='';previewStatus(error.message,true);}
  } finally {
    if (revision===clipRevision) {button.disabled=false;button.textContent='Обновить фон';}
  }
}

$('#preview-button').onclick=()=>loadPreviewClip(true);
$('#preview-source-select').onchange=()=>{ $('#preview-start').value=0;loadPreviewClip(); };
$('#preview-start').onchange=()=>loadPreviewClip();
$('#preview-text-select').onchange=()=>{syncPreviewEditor();queueLivePreview();};
$('#preview-text-editor').oninput=event=>{if(event.target.value.trim()){const texts=formSettings().texts;const index=Number($('#preview-text-select').value || 0);texts[index]=event.target.value;$('#texts-editor').value=texts.join('\n\n');}markChanged();};
$('#preview-text-editor').onblur=syncPreviewEditor;
$('#preview-play').onclick=async()=>{const video=$('#preview-video');try{if(video.paused)await video.play();else video.pause();}catch{previewStatus('Не удалось включить видеофон. Попробуй обновить фон.',true);}};
$('#preview-video').addEventListener('play',()=>{$('#preview-play').textContent='❚❚';$('#preview-play').setAttribute('aria-label','Приостановить видеофон');});
$('#preview-video').addEventListener('pause',()=>{$('#preview-play').textContent='▶';$('#preview-play').setAttribute('aria-label','Воспроизвести видеофон');});
$('#preview-video').addEventListener('timeupdate',event=>{$('#preview-timeline').value=event.target.currentTime;$('#preview-time').textContent=duration(event.target.currentTime);});
$('#preview-video').addEventListener('error',()=>{if($('#preview-video').getAttribute('src')){$('#preview-video').hidden=true;$('#live-preview').hidden=false;previewStatus('Показан статичный кадр: браузер не воспроизводит видеофон.',true);}});
$('#preview-timeline').oninput=event=>$('#preview-video').currentTime=Number(event.target.value);
function setTextPosition(x, y) {
  $('[data-field="text_x"]').value=Number(x).toFixed(2);
  $('[data-field="text_y"]').value=Number(y).toFixed(2);
  markChanged();
}
$('#preview-center').onclick=()=>setTextPosition(50,50);
$('#preview-center-x').onclick=()=>setTextPosition(50,$('[data-field="text_y"]').value);
$('#preview-center-y').onclick=()=>setTextPosition($('[data-field="text_x"]').value,50);

function limitTextPosition(value, axis) {
  const config=formSettings();
  const limits=config.keep_text_in_frame ? currentLayout?.limits?.[axis] || [0,100] : [0,100];
  return Math.max(limits[0],Math.min(limits[1],value));
}
function nudgeText(direction, multiplier=1) {
  const width=currentLayout?.width || Number(formSettings().resolution),height=currentLayout?.height || width*16/9;
  const step=Math.max(.1,Math.min(100,Number($('#preview-nudge-step').value) || 1))*multiplier;
  let x=Number($('[data-field="text_x"]').value),y=Number($('[data-field="text_y"]').value);
  if(direction==='left')x-=step/width*100;
  if(direction==='right')x+=step/width*100;
  if(direction==='up')y-=step/height*100;
  if(direction==='down')y+=step/height*100;
  setTextPosition(limitTextPosition(x,'x'),limitTextPosition(y,'y'));
}

const dragHandle = $('#text-drag-handle');
dragHandle.addEventListener('pointerdown',event=>{
  if (!currentLayout || event.button!==0) return;
  event.preventDefault(); clearTimeout(layerTimer); layerController?.abort(); ++layerRevision;
  const stage = $('#preview-stage');
  dragState={id:event.pointerId,x:event.clientX,y:event.clientY,width:stage.clientWidth,height:stage.clientHeight,startX:currentLayout.position.x,startY:currentLayout.position.y};
  dragHandle.focus();dragHandle.setPointerCapture(event.pointerId);dragHandle.classList.add('dragging');
});
dragHandle.addEventListener('pointermove',event=>{
  if (!dragState || event.pointerId!==dragState.id) return;
  const x=limitTextPosition(dragState.startX+(event.clientX-dragState.x)/dragState.width*100,'x');
  const y=limitTextPosition(dragState.startY+(event.clientY-dragState.y)/dragState.height*100,'y');
  $('[data-field="text_x"]').value=x.toFixed(2);$('[data-field="text_y"]').value=y.toFixed(2);
  const dx=(x-dragState.startX)/100*dragState.width,dy=(y-dragState.startY)/100*dragState.height;
  $('#preview-layer').style.transform=`translate(${dx}px,${dy}px)`;dragHandle.style.transform=`translate(${dx}px,${dy}px)`;
  $$('[data-number-for="text_x"],[data-number-for="text_y"]').forEach(input=>input.value=$(`[data-field="${input.dataset.numberFor}"]`).value);
});
function finishDrag(event) {
  if (!dragState || event.pointerId!==dragState.id) return;
  dragState=null;dragHandle.classList.remove('dragging');markChanged();
}
dragHandle.addEventListener('pointerup',finishDrag);dragHandle.addEventListener('pointercancel',finishDrag);dragHandle.addEventListener('lostpointercapture',finishDrag);
dragHandle.addEventListener('keydown',event=>{
  const direction={ArrowLeft:'left',ArrowRight:'right',ArrowUp:'up',ArrowDown:'down'}[event.key];
  if(!direction)return;
  event.preventDefault();nudgeText(direction,(event.shiftKey?10:1)*(event.altKey ? 0.1 : 1));
});
document.addEventListener('click',event=>{
  const button=event.target.closest('[data-text-nudge]');
  if(button)nudgeText(button.dataset.textNudge,(event.shiftKey?10:1)*(event.altKey ? 0.1 : 1));
});
document.addEventListener('input',event=>{
  const field=event.target;
  if(!field.matches('[data-number-for]') || field.value.trim()==='')return;
  const value=Number(field.value);
  if(!Number.isFinite(value)||value<Number(field.min)||value>Number(field.max))return;
  $(`[data-field="${field.dataset.numberFor}"]`).value=value;
  markChanged();
});
document.addEventListener('focusout',event=>{
  const field=event.target;
  if(field.matches('[data-number-for]'))field.value=$(`[data-field="${field.dataset.numberFor}"]`).value;
});
$('#text-style-reset').onclick=()=>{
  const defaults={font:'arial',font_size:64,min_font_size:32,text_width:76,text_x:50,text_y:50,stroke:4,text_color:'#ffffff',stroke_color:'#000000',line_spacing:1.12,letter_spacing:0,paragraph_spacing:0,text_align:'center',text_opacity:100,text_rotation:0,keep_text_in_frame:true};
  for(const [name,value] of Object.entries(defaults)){const field=$(`[data-field="${name}"]`);if(field.type==='checkbox')field.checked=value;else field.value=value;}
  markChanged();
};
if (state) {syncPreviewSources();queueLivePreview();}
