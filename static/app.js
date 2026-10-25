'use strict';
const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const icon = name => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const duration = seconds => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2,'0')}`;
const size = bytes => bytes > 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(1)} ГБ` : `${(bytes / 1024 ** 2).toFixed(1)} МБ`;
let state, page = 'create', saveTimer, editJobId, assetSignature = '', batchSignature = '', presetSignature = '', polling = false;
const excluded = new Set();
const statusNames = {ready:'Готово к сборке',queued:'В очереди',rendering:'Собирается',done:'Готово',error:'Ошибка',cancelled:'Отменено',interrupted:'Сборка прервана'};

function toast(message, error = false) {
  const item = document.createElement('div');
  item.className = `toast${error ? ' error' : ''}`;
  const close = document.createElement('button'); close.textContent = '×'; close.setAttribute('aria-label','Закрыть'); close.onclick = () => item.remove();
  item.append(close, document.createTextNode(message)); $('#toasts').append(item);
  setTimeout(() => item.remove(), error ? 16000 : 6000);
}

async function api(path, options = {}) {
  const response = await fetch('/api' + path, {headers:{'Content-Type':'application/json'},...options});
  if (!response.ok) {
    let message = 'Не удалось выполнить действие.';
    try { const value = await response.json(); message = typeof value.detail === 'string' ? value.detail : 'Проверь заполнение полей.'; } catch {}
    throw new Error(message);
  }
  return response.json();
}
const post = (path, body = {}) => api(path, {method:'POST', body:JSON.stringify(body)});

async function busy(button, title, callback) {
  if (button.disabled) return;
  const html = button.innerHTML;
  button.disabled = true; button.dataset.busy = 'true'; button.textContent = title;
  try { return await callback(); }
  catch (error) { toast(error.message, true); }
  finally { delete button.dataset.busy; button.disabled = false; button.innerHTML = html; updateReadiness(); if (typeof updatePublishingControls === 'function') updatePublishingControls(); }
}

function navigate(target, updateHash = true) {
  if (!$(`#page-${target}`)) target = 'create';
  page = target;
  $$('.page').forEach(item => item.hidden = item.id !== `page-${target}`);
  $$('.nav-item').forEach(item => item.classList.toggle('active', item.dataset.page === target));
  $('#breadcrumb-title').textContent = $(`.nav-item[data-page="${target}"]`).childNodes[1].textContent.trim();
  if (updateHash) history.replaceState(null, '', '#' + target);
  window.scrollTo({top:0, behavior:'instant'});
  if (typeof onPublishingNavigate === 'function') onPublishingNavigate(target);
}

function formSettings() {
  const result = {...(state?.settings || {})};
  $$('[data-field]').forEach(input => {
    result[input.dataset.field] = input.type === 'checkbox' ? input.checked : ['number','range'].includes(input.type) || input.dataset.field === 'parallel_jobs' ? Number(input.value) : input.value;
  });
  result.texts = $('#texts-editor').value.split(/\n\s*\n/).map(x => x.trim()).filter(Boolean);
  result.hashtags = [...new Set($('#tags-editor').value.split(/[\s,;]+/).map(x => x.replace(/^#+/, '').toLocaleLowerCase()).filter(Boolean))];
  return result;
}

function fillSettings(config) {
  $$('[data-field]').forEach(input => {
    const value = config[input.dataset.field];
    if (input.type === 'checkbox') input.checked = !!value;
    else input.value = value ?? '';
  });
  $('#texts-editor').value = (config.texts || []).join('\n\n');
  $('#tags-editor').value = (config.hashtags || []).map(x => '#' + x).join(' ');
  updateSummaries();
}

function paintRanges(root=document) {$$('input[type=range]',root).forEach(r=>{const min=Number(r.min||0),max=Number(r.max||100),v=Number(r.value);r.style.setProperty('--fill',max>min?((v-min)/(max-min)*100)+'%':'0%');});}
function updateSummaries() {
  const config = formSettings();
  $$('[data-number-for]').forEach(input=>{if(document.activeElement!==input)input.value=config[input.dataset.numberFor];});
  $$('output[data-value]').forEach(item => item.textContent = `${config[item.dataset.value]}${item.dataset.suffix || ''}`);
  paintRanges();
  $('#focus-controls').hidden = config.crop_mode !== 'cover';
  $('#progress-options').hidden = !config.progress_enabled;
  $('#texts-count').textContent = `${config.texts.length} вариантов`;
  $('#tags-count').textContent = `${config.hashtags.length} / 5`;
  $('#text-summary').innerHTML = config.texts.length ? `<strong>${config.texts.length} вариантов текста</strong><br>${esc(config.texts[0]).slice(0,220)}${config.texts[0].length>220?'…':''}` : 'Добавь рекламные тексты в разделе «Тексты и описания».';
  $('#caption-example').textContent = `${config.description || 'Описание магазина'}\n\n${config.hashtags.slice(0,5).map(x=>'#'+x).join(' ') || 'Здесь появятся пять хештегов'}`;
  updateReadiness();
  if (typeof syncPreviewTexts === 'function') syncPreviewTexts();
}

function updateReadiness() {
  const config = formSettings();
  const missing = [];
  if (!selected('video').length) missing.push('фоновое видео');
  if (!selected('music').length) missing.push('музыку');
  if (!config.texts.length) missing.push('рекламный текст');
  if (config.hashtags.length < 5) missing.push('пять разных хештегов');
  $('#prepare-hint').textContent = missing.length ? 'Для начала добавь: ' + missing.join(', ') + '.' : 'Проверь состав партии перед запуском сборки.';
  $('#prepare-button').disabled = missing.length > 0 || !!$('#prepare-button').dataset.busy;
}

function markChanged() {
  updateSummaries();
  $('#save-state').textContent = 'Сохраняем изменения…';
  if (typeof queueLivePreview === 'function') queueLivePreview();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => saveSettings(false), 1000);
}

async function saveSettings(notify = false) {
  clearTimeout(saveTimer);
  try {
    const config = formSettings();
    const saved = await post('/settings', config);
    state.settings = saved;
    $('#save-state').textContent = 'Все настройки сохранены';
    if (notify) toast('Настройки сохранены.');
    return saved;
  } catch(error) {
    $('#save-state').textContent = 'Проверь настройки';
    if (notify) toast(error.message, true);
    throw error;
  }
}

function selected(kind) {
  return (state?.assets || []).filter(asset => asset.kind === kind && asset.exists && !excluded.has(asset.id)).map(asset => asset.id);
}

function payload() {
  return {settings:formSettings(), video_ids:selected('video'), music_ids:selected('music'), name:$('#batch-name').value};
}

function renderAssets() {
  const items = state.assets;
  $('#asset-count').textContent = items.length;
  $('#library-total').textContent = `${items.length} файлов`;
  for (const kind of ['video','music']) {
    const entries = items.filter(x => x.kind === kind && x.exists);
    $(`#${kind}-selector`).innerHTML = entries.length ? entries.map(asset => `<label class="source-chip"><input type="checkbox" data-source="${asset.id}" ${excluded.has(asset.id)?'':'checked'}><span title="${esc(asset.name)}">${esc(asset.name)}</span><small>${duration(asset.info.duration)}</small></label>`).join('') : `<div class="source-empty">${kind==='video'?'Пока нет фоновых видео. Добавь длинный исходник.':'Добавь дорожки для музыкального сопровождения.'}</div>`;
    $(`#selected-${kind}-count`).textContent = `· выбрано ${selected(kind).length}`;
  }
  const fontValue = $('[data-field="font"]').value || state.settings.font;
  $('#font-select').innerHTML = '<option value="arial">Arial Bold</option><option value="segoe">Segoe UI Bold</option><option value="impact">Impact</option>' + items.filter(x=>x.kind==='font' && x.exists).map(x=>`<option value="${x.id}">${esc(x.name)}</option>`).join('');
  $('#font-select').value = fontValue;
  $('#asset-library').innerHTML = items.length ? items.map(asset => `<div class="library-row"><span class="drop-icon ${asset.kind==='video'?'violet':asset.kind==='music'?'mint':'peach'}">${icon(asset.kind==='video'?'film':asset.kind==='music'?'music':'text')}</span><div class="asset-info"><strong>${esc(asset.name)}${!asset.exists?' <span class="accent">· файл не найден</span>':''}</strong><small>${asset.managed?'Импортированная копия':'Подключён по пути'} · ${size(asset.info.size)}${asset.info.width?` · ${asset.info.width} × ${asset.info.height}`:''}${asset.info.hdr?' · HDR':''}<br>${esc(asset.path)}</small></div><span class="asset-duration">${asset.info.duration?duration(asset.info.duration):'Шрифт'}</span><button class="icon-button" data-remove="${asset.id}" title="Убрать из библиотеки; файл сохранится" aria-label="Убрать ${esc(asset.name)}">×</button></div>`).join('') : `<div class="empty-state">${icon('folder')}<h3>Здесь будут твои материалы</h3><p>Добавь видео, музыку или шрифт с помощью карточек выше.</p></div>`;
  updateReadiness();
  if (typeof syncPreviewSources === 'function') syncPreviewSources();
}

function renderPresets() {
  const value = $('#preset-select').value;
  $('#preset-select').innerHTML = '<option value="">Текущие настройки</option>' + Object.keys(state.presets).map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
  $('#preset-select').value = value;
}

function jobCard(job) {
  const s = job.spec;
  let actions = '';
  if (job.status === 'ready') actions += `<button class="button" data-edit="${job.id}">Изменить текст</button><button class="button" data-job="${job.id}" data-job-action="edit">Пересоздать выбор</button>`;
  if (['error','interrupted','cancelled'].includes(job.status)) actions += `<button class="button" data-job="${job.id}" data-job-action="retry">Повторить</button>`;
  if (['ready','queued','rendering','interrupted'].includes(job.status)) actions += `<button class="button subtle" data-job="${job.id}" data-job-action="cancel">${job.cancel_requested?'Отменяется…':'Отменить'}</button>`;
  if (job.available) actions += `<a class="button" href="/api/jobs/${job.id}/video?download=true">Скачать ${job.hdr?'HDR10':'MP4'}</a><button class="button subtle" data-copy="${job.id}">Описание</button><a class="button subtle" href="/api/jobs/${job.id}/caption">Скачать TXT</a>`;
  const canPlay = job.available && (!job.hdr || job.playback_available);
  const visual = canPlay ? `<video controls preload="none" poster="/api/previews/${job.id}?v=${encodeURIComponent(job.updated)}" src="/api/jobs/${job.id}/${job.hdr?'playback':'video'}"></video>` : `<img loading="lazy" src="/api/previews/${job.id}?v=${encodeURIComponent(job.updated)}" alt="Предпросмотр видео ${job.ordinal}">`;
  return `<article class="job-card" id="job-${job.id}"><div class="job-visual">${visual}<span class="job-number">ВИДЕО ${String(job.ordinal).padStart(2,'0')}</span></div><div class="job-body"><div class="job-status ${job.status}"><span class="status-dot${job.status==='error'?' bad':''}"></span>${job.status==='rendering'&&s.rendering?.phase==='preview'?'Готовим просмотр':statusNames[job.status]}${job.status==='rendering'?` · ${Math.floor(job.progress)}%`:''}</div>${job.status==='rendering'?`<div class="job-progress"><span style="width:${job.progress}%"></span></div>`:''}<p>${esc(s.text)}</p><div class="job-meta" title="${esc(s.background.name)}">${esc(s.background.name)} · с ${s.start.toFixed(1)} сек</div><div class="job-meta">${s.duration.toFixed(1)} сек · ${s.settings.resolution}p${job.hdr?' · HDR10 · просмотр SDR':''} · ${esc(s.music.name)}</div>${s.rendering?`<div class="job-meta">${s.rendering.encoder==='nvidia'?'NVIDIA NVENC':'CPU'}${s.rendering.elapsed_seconds?` · ${s.rendering.elapsed_seconds} сек сборки`:''}</div>${s.rendering.fallback?`<div class="job-meta">${esc(s.rendering.fallback)}</div>`:''}`:''}${job.status==='done'&&!job.available?'<div class="job-error">Готовый файл перемещён или удалён.</div>':''}${job.error?`<div class="job-error">${esc(job.error)}</div>`:''}<div class="job-actions">${actions}</div><details class="job-caption"><summary>Описание и хештеги</summary><pre>${esc(s.caption)}</pre></details></div></article>`;
}

function renderBatches() {
  // Avoid replacing a playing video or an expanded description on every poll.
  const openDetails = new Set($$('.job-caption[open]').map(item=>item.closest('.job-card').id));
  const playback = new Map($$('.job-visual video').map(video=>[video.closest('.job-card').id,{time:video.currentTime,playing:!video.paused}]));
  $('#batch-list').innerHTML = state.batches.length ? state.batches.map(batch=>{
    const done = batch.jobs.filter(x=>x.available).length;
    const runnable = batch.jobs.filter(x=>['ready','interrupted','error'].includes(x.status)).length;
    const active = batch.jobs.some(x=>['queued','rendering'].includes(x.status));
    return `<section class="batch-section" id="batch-${batch.id}"><div class="batch-heading"><div><h2>${esc(batch.name)}</h2><small>${new Date(batch.created).toLocaleString('ru-RU')} · ${done} из ${batch.jobs.length} готово${active?' · сборка в очереди':''}</small></div><div class="batch-actions">${runnable?`<button class="button primary" data-start="${batch.id}">${icon('play')}${batch.jobs.some(x=>x.status!=='ready')?'Возобновить':'Создать'} ${runnable} видео</button>`:''}${done?`<a class="button" href="/api/batches/${batch.id}/zip">${icon('download')}Скачать партию</a>`:''}<button class="button subtle" data-folder="${batch.id}">${icon('folder')}Папка</button></div></div>${batch.warnings.map(warning=>`<div class="batch-warning">${esc(warning)}</div>`).join('')}<div class="job-grid">${batch.jobs.map(jobCard).join('')}</div></section>`;
  }).join('') : `<div class="panel empty-state">${icon('spark')}<h3>Первая партия ждёт твоей идеи</h3><p>Добавь материалы и подготовь ролики на экране создания.</p><button class="button primary" data-go="create">Создать первую партию</button></div>`;
  openDetails.forEach(id=>{const details = $(`#${id} .job-caption`);if(details) details.open = true;});
  playback.forEach((value,id)=>{const video=$(`#${id} video`);if(video&&value.time>0){video.addEventListener('loadedmetadata',()=>{video.currentTime=value.time;if(value.playing) video.play().catch(()=>{});},{once:true});video.preload='metadata';}});
}

function renderSystem() {
  const system = state.system;
  const hardware = system.hardware || {};
  const encoder = state.settings.encoder_mode === 'cpu' ? 'CPU' : hardware.nvenc ? 'NVIDIA NVENC · HEVC 10 бит' : 'CPU (NVIDIA недоступна)';
  $('#encoder-hardware').textContent = hardware.name ? `${hardware.name}${hardware.memory_mb ? ` · ${Math.round(hardware.memory_mb / 1024)} ГБ` : ''} · ${hardware.nvenc ? 'NVENC 10 бит доступен' : 'NVENC недоступен'}` : hardware.nvenc ? 'NVIDIA NVENC 10 бит доступен' : 'NVIDIA NVENC недоступен: будет использоваться CPU.';
  const healthy = system.ffmpeg && system.ffprobe && system.worker;
  $('#system-dot').classList.toggle('bad', !healthy);
  $('#system-label').textContent = healthy ? 'Студия готова к работе' : 'Нужна настройка';
  $('#system-warning').hidden = healthy;
  $('#system-warning').textContent = !system.ffmpeg || !system.ffprobe ? 'Для сборки нужен FFmpeg. Закрой Studio и запусти «Установить.bat» в папке программы.' : 'Процесс сборки остановился. Перезапусти Studio, чтобы продолжить.';
  $('#system-info').innerHTML = [['FFmpeg',system.ffmpeg?'Установлен':'Не найден',system.ffmpeg],['FFprobe',system.ffprobe?'Установлен':'Не найден',system.ffprobe],['Обработка видео',system.worker?'Готова':'Остановлена',system.worker],['Свободное место',`${system.free_gb} ГБ`,system.free_gb>1],['Кодирование',encoder,true],['Параллельно',`${state.settings.parallel_jobs || 2} ролика`,true],['Доступ к панели','Этот компьютер',true]].map(([label,value,ok])=>`<div><span>${label}</span><strong class="${ok?'':'bad'}">${esc(value)}</strong></div>`).join('');
  const jobs = state.batches.flatMap(x=>x.jobs);
  const rendering = jobs.filter(x=>x.status==='rendering').length;
  const queued = jobs.filter(x=>x.status==='queued').length;
  $('#done-count').textContent = jobs.filter(x=>x.available).length;
  $('#queue-bar').hidden = !rendering && !queued && !state.paused;
  $('#queue-label').textContent = state.paused ? rendering ? 'Пауза после текущих роликов' : 'Очередь на паузе' : 'Очередь работает';
  $('#queue-detail').textContent = `${rendering} собирается · ${queued} ожидает · лимит ${state.settings.parallel_jobs || 2}`;
  $('#queue-toggle').textContent = state.paused ? 'Продолжить очередь' : 'Приостановить очередь';
  $('#step-preview').classList.toggle('active',state.batches.length>0);
  $('#step-export').classList.toggle('active',jobs.some(x=>x.status==='done'));
}

async function refresh(initial = false) {
  if (polling) return;
  polling = true;
  try {
    const next = await api('/state'); state = next;
    $('#connection-warning').hidden = true;
    const a = JSON.stringify(state.assets);
    const p = JSON.stringify(state.presets);
    const b = JSON.stringify(state.batches.map(batch=>({id:batch.id,name:batch.name,warnings:batch.warnings,jobs:batch.jobs.map(job=>[job.id,job.status,Math.floor(job.progress),job.updated,job.cancel_requested,job.available,job.playback_available])})));
    if (a !== assetSignature || initial) {renderAssets();assetSignature = a;}
    if (p !== presetSignature || initial) {renderPresets();presetSignature = p;}
    if (initial) {fillSettings(state.settings); if (typeof queueLivePreview === 'function') queueLivePreview();}
    if (b !== batchSignature || initial) {renderBatches();batchSignature = b;}
    renderSystem();
  } catch(error) {
    $('#connection-warning').hidden = false;
    if (initial) toast('Не удалось открыть Studio. ' + error.message, true);
  } finally {polling = false;}
}

async function uploadFiles(kind, files) {
  if (!files.length) return;
  const data = new FormData(); data.append('kind',kind);
  [...files].forEach(file=>data.append('files',file));
  const box = $('#upload-progress'); box.hidden = false;
  $('span',box).textContent='Загружаем материалы…'; $('progress',box).value=0;
  try {
    const result = await new Promise((resolve,reject)=>{
      const xhr = new XMLHttpRequest();xhr.open('POST','/api/assets/upload');
      xhr.upload.onprogress = event=>{if(event.lengthComputable){$('progress',box).value=event.loaded/event.total*100;if(event.loaded===event.total)$('span',box).textContent='Проверяем файлы…';}};
      xhr.onload=()=>{try{const result=JSON.parse(xhr.responseText);if(xhr.status>=400)reject(new Error(result.detail||'Ошибка импорта.'));else resolve(result);}catch{reject(new Error('Некорректный ответ программы.'));}};
      xhr.onerror=()=>reject(new Error('Соединение прервано при загрузке.'));
      xhr.send(data);
    });
    if(result.imported.length) toast(`Добавлено файлов: ${result.imported.length}.`);
    if(result.errors.length) toast(result.errors.join('\n'),true);
    await refresh();
  } catch(error) {toast(error.message,true);}
  finally {box.hidden=true;}
}

document.addEventListener('click', async event=>{
  const nav=event.target.closest('[data-page],[data-go]');
  if(nav){navigate(nav.dataset.page||nav.dataset.go);return;}
  const add=event.target.closest('[data-upload]');
  if(add){const input=document.createElement('input');input.type='file';input.multiple=true;input.accept={video:'.mp4,.mov,.mkv,.webm,.avi,.m4v,.mts',music:'.mp3,.wav,.m4a,.flac,.ogg,.aac,.opus',font:'.ttf,.otf'}[add.dataset.upload];input.onchange=()=>uploadFiles(add.dataset.upload,input.files);input.click();return;}
  const remove=event.target.closest('[data-remove]');
  if(remove){if(!confirm('Убрать материал из библиотеки? Сам файл останется на диске.'))return;await busy(remove,'…',async()=>{await api('/assets/'+remove.dataset.remove,{method:'DELETE'});await refresh();});return;}
  const start=event.target.closest('[data-start]');
  if(start){await busy(start,'Запускаем…',async()=>{await post('/batches/'+start.dataset.start+'/start');toast('Партия добавлена в очередь.');await refresh();});return;}
  const action=event.target.closest('[data-job]');
  if(action){await busy(action,'Подожди…',async()=>{await post(`/jobs/${action.dataset.job}/${action.dataset.jobAction}`,action.dataset.jobAction==='edit'?{regenerate:true}:{});await refresh();});return;}
  const edit=event.target.closest('[data-edit]');
  if(edit){editJobId=edit.dataset.edit;const job=state.batches.flatMap(x=>x.jobs).find(x=>x.id===editJobId);$('#edit-text').value=job.spec.text;$('#edit-dialog').showModal();return;}
  const copy=event.target.closest('[data-copy]');
  if(copy){try{const job=state.batches.flatMap(x=>x.jobs).find(x=>x.id===copy.dataset.copy);await navigator.clipboard.writeText(job.spec.caption);toast('Описание скопировано.');}catch{toast('Не удалось скопировать. Можно скачать описание в TXT.',true);}return;}
  const folder=event.target.closest('[data-folder]');
  if(folder){await busy(folder,'Открываем…',()=>post('/folder',{batch_id:folder.dataset.folder}));return;}
  if(event.target.closest('.close-dialog'))event.target.closest('dialog').close();
});

document.addEventListener('input',event=>{if(event.target.matches('input[type=range]'))paintRanges(document);if(event.target.matches('[data-field],#texts-editor,#tags-editor')){if(event.target.dataset.field==='font_size' && Number($('[data-field="min_font_size"]').value)>Number(event.target.value))$('[data-field="min_font_size"]').value=event.target.value;markChanged();}});
document.addEventListener('change',event=>{
  if(event.target.matches('[data-source]')){if(event.target.checked)excluded.delete(event.target.dataset.source);else excluded.add(event.target.dataset.source);for(const kind of ['video','music'])$(`#selected-${kind}-count`).textContent=`· выбрано ${selected(kind).length}`;updateReadiness();if(typeof syncPreviewSources==='function')syncPreviewSources();}
});
$$('.save-settings').forEach(button=>button.onclick=()=>busy(button,'Сохраняем…',()=>saveSettings(true)));
$('#prepare-button').onclick=()=>busy($('#prepare-button'),'Подбираем фрагменты…',async()=>{const data=payload();await saveSettings();const result=await post('/batches',data);await refresh();navigate('results');$(`#batch-${result.id}`)?.scrollIntoView({behavior:'smooth',block:'start'});toast('Партия подготовлена. Проверь ролики и нажми «Создать видео».');});
$('#show-guides').onchange=event=>$('#safe-guides').hidden=!event.target.checked;
$('#import-path-button').onclick=()=>$('#path-dialog').showModal();
$('#path-form').onsubmit=event=>{event.preventDefault();const button=$('button[type=submit]',event.target);busy(button,'Проверяем файл…',async()=>{await post('/assets/path',{kind:$('#path-kind').value,path:$('#path-value').value.trim().replace(/^"|"$/g,'')});$('#path-dialog').close();$('#path-value').value='';await refresh();toast('Материал подключён.');});};
$('#edit-form').onsubmit=event=>{event.preventDefault();const button=$('button[type=submit]',event.target);busy(button,'Обновляем кадр…',async()=>{await post(`/jobs/${editJobId}/edit`,{text:$('#edit-text').value});$('#edit-dialog').close();await refresh();});};
$('#queue-toggle').onclick=()=>busy($('#queue-toggle'),'Обновляем…',async()=>{await post('/queue/'+(state.paused?'resume':'pause'));await refresh();});
$('#open-output').onclick=()=>busy($('#open-output'),'Открываем…',async()=>{await saveSettings();await post('/folder');});
$('#cleanup-button').onclick=()=>busy($('#cleanup-button'),'Очищаем…',async()=>{const result=await post('/cleanup');toast(`Удалено временных файлов: ${result.removed}.`);await refresh();});
$('#save-preset').onclick=()=>$('#preset-dialog').showModal();
$('#preset-form').onsubmit=event=>{event.preventDefault();const button=$('button[type=submit]',event.target);busy(button,'Сохраняем…',async()=>{await post('/presets',{name:$('#preset-name').value,settings:formSettings()});$('#preset-dialog').close();await refresh();$('#preset-select').value=$('#preset-name').value.trim();toast('Пресет сохранён.');});};
$('#preset-select').onchange=event=>{const preset=state.presets[event.target.value];if(preset){fillSettings({...formSettings(),...preset});markChanged();toast('Оформление применено.');}};
$('#shutdown-button').onclick=()=>busy($('#shutdown-button'),'Закрываем…',async()=>{await saveSettings();await post('/shutdown');toast('Studio завершает работу. Для следующего запуска открой «Запустить Studio.bat».');});
$$('[data-drop]').forEach(zone=>{
  zone.addEventListener('dragover',event=>{event.preventDefault();zone.classList.add('dragover');});
  zone.addEventListener('dragleave',()=>zone.classList.remove('dragover'));
  zone.addEventListener('drop',event=>{event.preventDefault();zone.classList.remove('dragover');uploadFiles(zone.dataset.drop,event.dataTransfer.files);});
});
window.addEventListener('hashchange',()=>navigate(location.hash.slice(1),false));
window.addEventListener('unhandledrejection',event=>{event.preventDefault();console.error(event.reason);});
navigate(location.hash.slice(1)||'create',false);
refresh(true).then(()=>setInterval(()=>refresh(),1800));
