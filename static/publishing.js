'use strict';
let publishingState, publishingBusy = false, publishingSyncing = false, publishingMediaSignature = '', publishingAccountSignature = '', publishingTaskSignature = '', publishingEditingId = null;
let publishingDrafts = {};
try { publishingDrafts = JSON.parse(localStorage.getItem('studio-publishing-drafts') || '{}'); } catch {}
const publishingRecipients = new Set();
const publishStatus = {queued:'В очереди',preparing:'Подготовка',initiating:'Создаём публикацию',uploading:'Передача видео',processing:'TikTok обрабатывает',published:'Опубликовано',failed:'Ошибка',uncertain:'Результат не подтверждён',cancelled:'Отменено'};
const privacyNames = {PUBLIC_TO_EVERYONE:'Все',MUTUAL_FOLLOW_FRIENDS:'Взаимные подписки',FOLLOWER_OF_CREATOR:'Подписчики',SELF_ONLY:'Только я'};
function publishingSaveDrafts() {try {localStorage.setItem('studio-publishing-drafts',JSON.stringify(publishingDrafts));} catch {}}
function publishingDraft(media) {return publishingDrafts[media.id] ||= {selected:false,account_id:'',privacy:'',caption:media.caption || '',brand:'none',is_aigc:false,allow_comment:false,allow_duet:false,allow_stitch:false};}
function publishingProfile(id) {return publishingState?.accounts.find(x=>x.id===id);}
function publishingOptions(choices, chosen, empty) {return `<option value="">${empty}</option>`+choices.map(([value,label])=>`<option value="${esc(value)}"${value===chosen?' selected':''}>${esc(label)}</option>`).join('');}
function publishingChosen() {return (publishingState?.media || []).filter(x=>!x.blocked && x.exists && publishingDraft(x).selected);}

async function refreshPublishing(initial = false) {
  if (publishingBusy) return;
  publishingBusy = true;
  try {
    publishingState = await api('/publishing/state');
    const info = publishingState.config;
    $('#publish-config-state').textContent = info.ready ? 'Настроено' : 'Нужно настроить';
    if (initial) {
      $('#publish-client-key').value = info.client_key;
      $('#publish-callback').value = info.callback;
      $('#publish-client-secret').placeholder = info.secret_saved ? 'Уже сохранён; оставь пустым, чтобы сохранить' : 'Client secret приложения TikTok';
      $('#publish-setup').open = !info.ready;
    }
    const accounts = JSON.stringify(publishingState.accounts);
    if (accounts !== publishingAccountSignature || initial) {
      publishingState.accounts.filter(x=>x.connected).forEach(x=>{if(!publishingAccountSignature)publishingRecipients.add(x.id);});
      renderPublishingAccounts(); publishingAccountSignature = accounts;
    }
    const media = JSON.stringify([publishingState.media,accounts,$('#publish-show-used').checked]);
    if (media !== publishingMediaSignature || initial) {renderPublishingMedia();publishingMediaSignature = media;}
    const tasks = JSON.stringify(publishingState.tasks);
    if (tasks !== publishingTaskSignature || initial) {renderPublishingTasks();publishingTaskSignature = tasks;}
    updatePublishingControls();
  } catch (error) {
    if (page === 'publishing') toast('Залив: '+error.message,true);
  } finally {publishingBusy = false;}
}

function renderPublishingAccounts() {
  $('#publish-account-count').textContent = `${publishingState.accounts.length} / 5`;
  $('#publish-add-account').disabled = publishingState.accounts.length >= 5;
  $('#publish-accounts').innerHTML = publishingState.accounts.length ? publishingState.accounts.map(profile=>`<article class="publish-account"><header><input type="checkbox" data-publish-recipient="${esc(profile.id)}"${publishingRecipients.has(profile.id)?' checked':''} aria-label="Распределять на ${esc(profile.label)}"><strong>${esc(profile.label)}</strong><span class="status-dot${profile.connected?'':' bad'}"></span></header><div class="publish-account-meta">${profile.connected?`@${esc(profile.username || profile.nickname || 'TikTok')} · подключён`:'TikTok ещё не подключён'}<br>${esc(profile.proxy_label)}<br>${profile.quick?.enabled?'Быстрый залив: '+esc(privacyNames[profile.quick.privacy] || profile.quick.privacy):'Быстрый залив выключен'}</div>${profile.error?`<p class="job-error">${esc(profile.error)}</p>`:''}<div class="job-actions"><button class="button" data-publish-connect="${esc(profile.id)}">${profile.connected?'Переподключить':'Подключить TikTok'}</button><button class="button subtle" data-publish-edit-account="${esc(profile.id)}">Настроить</button>${profile.connected?`<button class="button subtle" data-publish-refresh-account="${esc(profile.id)}">Обновить</button><button class="button subtle" data-publish-disconnect="${esc(profile.id)}">Отключить</button>`:`<button class="button subtle" data-publish-delete-account="${esc(profile.id)}">Удалить</button>`}</div></article>`).join('') : '<div class="publish-empty">Добавь аккаунты магазина, затем подключи каждый через вход TikTok.<br>У каждого аккаунта может быть свой постоянный прокси.</div>';
}

function renderPublishingMedia() {
  const media = publishingState.media.filter(x=>$('#publish-show-used').checked || !x.blocked);
  $('#publish-media').innerHTML = media.length ? media.map(item=>{
    const draft = publishingDraft(item);
    if (item.blocked || !item.exists) draft.selected = false;
    const profile = publishingProfile(draft.account_id);
    const choices = profile?.creator.privacy_level_options || [];
    const used = !!item.blocked || !item.exists;
    return `<article class="publish-media-row${used?' is-used':''}" data-publish-row="${esc(item.id)}"><input type="checkbox" data-publish-select="${esc(item.id)}"${draft.selected?' checked':''}${used?' disabled':''} aria-label="Выбрать ${esc(item.name)}"><button class="publish-thumb" data-publish-preview="${esc(item.id)}" aria-label="Посмотреть ${esc(item.name)}">${item.job_id?`<img loading="lazy" src="/api/previews/${encodeURIComponent(item.job_id)}" alt="">`:icon('play')}</button><div><strong class="publish-media-name">${esc(item.name)}</strong><p class="publish-media-meta">${duration(item.duration)} · ${size(item.size)}${used?` · ${!item.exists?'Файл недоступен':publishStatus[item.blocked] || item.blocked}`:''}</p><button class="text-button" data-publish-caption="${esc(item.id)}">Описание и параметры</button>${!used?`<button class="text-button" data-publish-already="${esc(item.id)}">Уже выкладывалось</button>`:''}</div><div class="publish-destination"><select aria-label="Аккаунт для ${esc(item.name)}" data-publish-destination="${esc(item.id)}"${used?' disabled':''}>${publishingOptions(publishingState.accounts.map(x=>[x.id,`${x.label}${x.username?' · @'+x.username:''}${x.connected?'':' · не подключён'}`]),draft.account_id,'Выбрать аккаунт')}</select></div><div class="publish-privacy"><select aria-label="Видимость для ${esc(item.name)}" data-publish-privacy="${esc(item.id)}"${used || !choices.length?' disabled':''}>${publishingOptions(choices.map(x=>[x,privacyNames[x] || x]),draft.privacy,'Видимость')}</select></div></article>`;
  }).join('') : '<div class="publish-empty">Здесь появятся готовые видео из Studio и добавленные MP4.<br>Отправленные ролики скрываются из списка новых.</div>';
  updatePublishingControls();
}

function renderPublishingTasks() {
  $('#publish-tasks').innerHTML = publishingState.tasks.length ? publishingState.tasks.slice().reverse().map(task=>`<article class="publish-task"><div><strong>${esc(task.media_name)}</strong><p>${new Date(task.created).toLocaleString('ru-RU')}</p></div><div><strong>${esc(task.account_label)}</strong><p>${task.username?'@'+esc(task.username):''}</p>${task.post_info.privacy_level?`<p>${esc(privacyNames[task.post_info.privacy_level] || task.post_info.privacy_level)}</p>`:''}</div><div><span class="publish-task-state ${esc(task.status)}">${task.remote.manual?'Отмечено вручную':publishStatus[task.status] || esc(task.status)}${task.status==='uploading'?` · ${Math.floor(task.progress)}%`:''}</span>${task.publish_id?`<p>ID: ${esc(task.publish_id)}</p>`:''}${task.error?`<p class="job-error">${esc(task.error)}</p>`:''}<div class="job-actions">${task.status==='queued'?`<button class="button subtle" data-publish-task="${esc(task.id)}" data-publish-action="cancel">Убрать из очереди</button>`:''}${task.status==='failed'?`<button class="button" data-publish-task="${esc(task.id)}" data-publish-action="retry">Повторить</button>`:''}${['processing','uncertain'].includes(task.status)&&task.publish_id?`<button class="button subtle" data-publish-task="${esc(task.id)}" data-publish-action="check">Проверить статус</button>`:''}</div></div></article>`).join('') : '<div class="publish-empty">После запуска здесь будет состояние каждого ролика.<br>«Опубликовано» появляется после подтверждения TikTok.</div>';
}

function updatePublishingControls() {
  if (!publishingState) return;
  const chosen = publishingChosen();
  const invalid = chosen.filter(media=>{const draft=publishingDraft(media), profile=publishingProfile(draft.account_id);return !profile?.connected || !(profile.creator.privacy_level_options || []).includes(draft.privacy);});
  $('#publish-selected').textContent = `Выбрано: ${chosen.length}`;
  const button = $('#publish-start');
  button.disabled = !chosen.length || !!invalid.length || !publishingState.config.ready || !!button.dataset.busy;
  if (!button.dataset.busy) button.innerHTML = `${icon('play')}Начать залив${chosen.length?' · '+chosen.length:''}`;
  $('#publish-start-hint').textContent = !publishingState.config.ready ? 'Настрой подключение приложения TikTok и авторизуй аккаунты.' : invalid.length ? `Для ${invalid.length} видео нужно подключить аккаунт и выбрать видимость.` : chosen.length ? `${chosen.length} видео будут отправлены по одному в назначенные аккаунты, с интервалом от 40 секунд.` : 'Выбери новые видео и назначь каждому аккаунт и видимость.';
  const selectable = publishingState.media.filter(x=>!x.blocked && x.exists);
  $('#publish-select-all').checked = !!selectable.length && chosen.length===selectable.length;
  $('#publish-select-all').indeterminate = chosen.length>0 && chosen.length<selectable.length;
  const queued = publishingState.tasks.filter(x=>x.status==='queued').length;
  const active = publishingState.tasks.filter(x=>['preparing','initiating','uploading','processing'].includes(x.status)).length;
  const posted = publishingState.tasks.filter(x=>x.status==='published').length;
  $('#publish-queue-summary').textContent = `${queued} ждёт · ${active} отправляется или проверяется · ${posted} в истории публикаций${publishingState.paused&&queued?' · очередь на паузе после запуска Studio':''}`;
  $('#publishing-count').textContent = queued+active;
  $('#publish-toggle').textContent = publishingState.paused ? 'Продолжить очередь' : 'Приостановить';
  $('#publish-toggle').disabled = !queued || !!$('#publish-toggle').dataset.busy;
  const quick = publishingState.quick || {ready:false,count:0,reason:'Настрой аккаунты для быстрого залива.'};
  $$('[data-publish-quick]').forEach(button=>{
    if(button.dataset.busy)return;
    const active = queued || publishingState.tasks.some(x=>['preparing','initiating','uploading','processing'].includes(x.status));
    const resume = queued && publishingState.paused;
    button.disabled = quick.ready && !quick.count && !resume;
    button.innerHTML = `${icon('upload')}${!quick.ready?'Настроить залив':quick.count?'Залить новые видео · '+quick.count:resume?'Продолжить залив · '+queued:active?'Залив идёт':'Новых видео нет'}`;
  });
  $('#publish-quick-hint').textContent = !quick.ready ? quick.reason : `Studio сама выберет ${quick.count} новых видео и распределит их по кругу. Получатели: ${quick.recipients.map(x=>x.label+' ('+(privacyNames[x.privacy] || x.privacy)+')').join(', ')}. Отправка с интервалом от 40 секунд.${quick.skipped?' Не подходят по описанию или длительности: '+quick.skipped+'.':''}`;
  updatePublishingTimer();
}

function updatePublishingTimer() {
  if (!publishingState) return;
  const queued = publishingState.tasks.some(x=>x.status==='queued');
  const seconds = Math.max(0,Math.ceil(publishingState.next_at-Date.now()/1000));
  $('#publish-timer').textContent = publishingState.paused&&queued ? 'Пауза' : queued ? seconds ? `Следующая отправка через ${seconds} сек` : 'Следующая отправка готовится' : publishingState.tasks.some(x=>['processing','initiating','uploading','preparing'].includes(x.status)) ? 'Ждём подтверждение TikTok' : 'Нет ожидающих видео';
}

async function syncPublishingMedia(notify = false) {
  if (publishingSyncing) return;
  publishingSyncing = true;
  try {const result=await post('/publishing/library/sync');if(notify)toast(`Библиотека обновлена. Добавлено: ${result.imported}.`);await refreshPublishing();}
  catch(error) {toast(error.message,true);}
  finally {publishingSyncing=false;}
}

function onPublishingNavigate(target) {
  if (target === 'publishing') refreshPublishing(!publishingState).then(()=>syncPublishingMedia());
}

function openPublishingAccount(id = '') {
  const profile=publishingProfile(id);
  $('#publish-account-id').value=id;
  $('#publish-account-title').textContent=profile?'Настроить аккаунт':'Добавить аккаунт';
  $('#publish-account-label').value=profile?.label || '';
  $('#publish-account-proxy').value='';$('#publish-clear-proxy').checked=false;
  $('#publish-account-proxy').placeholder=profile&&profile.proxy_label!=='Прямое соединение'?`Сохранён ${profile.proxy_label}; пустое поле сохраняет его`:'http://логин:пароль@адрес:порт';
  $('#publish-account-quick').checked=!!profile?.connected&&!!profile?.quick?.enabled;
  $('#publish-account-quick').disabled=!profile?.connected;
  $('#publish-account-quick-privacy').innerHTML=publishingOptions((profile?.creator.privacy_level_options || []).map(x=>[x,privacyNames[x] || x]),profile?.quick?.privacy || '','Выбрать видимость');
  $('#publish-account-quick-privacy').required=!!profile?.connected&&!!profile?.quick?.enabled;
  $('#publish-account-quick-brand').value=profile?.quick?.brand || 'none';
  $('#publish-account-quick-aigc').checked=!!profile?.quick?.is_aigc;
  $('#publish-account-quick-help').textContent=profile?.connected?'Эти параметры сохраняются: при каждом запуске выбирать их снова не нужно.':'Сначала подключи TikTok. После входа включи быстрый залив и выбери видимость один раз.';
  $('#publish-account-dialog').showModal();
}

$('#publish-add-account').onclick=()=>openPublishingAccount();
$('#publish-account-form').onsubmit=event=>{event.preventDefault();const button=$('button[type=submit]',event.target);busy(button,'Сохраняем…',async()=>{await post('/publishing/accounts',{id:$('#publish-account-id').value || undefined,label:$('#publish-account-label').value,proxy:$('#publish-account-proxy').value,clear_proxy:$('#publish-clear-proxy').checked,quick:{enabled:$('#publish-account-quick').checked,privacy:$('#publish-account-quick-privacy').value,brand:$('#publish-account-quick-brand').value,is_aigc:$('#publish-account-quick-aigc').checked}});$('#publish-account-proxy').value='';$('#publish-account-dialog').close();await refreshPublishing();});};
$('#publish-config-form').onsubmit=event=>{event.preventDefault();const button=$('button[type=submit]',event.target);busy(button,'Сохраняем…',async()=>{await post('/publishing/config',{client_key:$('#publish-client-key').value,client_secret:$('#publish-client-secret').value});$('#publish-client-secret').value='';await refreshPublishing(true);toast('Подключение сохранено. Теперь подключи аккаунты.');});};
$('#publish-sync').onclick=()=>busy($('#publish-sync'),'Обновляем…',()=>syncPublishingMedia(true));
$('#publish-import').onclick=()=>$('#publish-file-input').click();
$('#publish-file-input').onchange=event=>{const files=[...event.target.files];event.target.value='';if(!files.length)return;busy($('#publish-import'),'Добавляем…',async()=>{const data=new FormData();files.forEach(file=>data.append('files',file));const response=await fetch('/api/publishing/library/upload',{method:'POST',body:data});const result=await response.json();if(!response.ok)throw new Error(result.detail || 'Ошибка импорта');if(result.errors.length)toast(result.errors.join('\n'),true);if(result.imported.length)toast(`Добавлено видео: ${result.imported.length}.`);await refreshPublishing();});};
$('#publish-show-used').onchange=()=>{publishingMediaSignature='';renderPublishingMedia();};
$('#publish-select-all').onchange=event=>{publishingState.media.filter(x=>!x.blocked&&x.exists).forEach(media=>publishingDraft(media).selected=event.target.checked);publishingSaveDrafts();renderPublishingMedia();};
$('#publish-distribute').onclick=()=>{
  const recipients=publishingState?.accounts.filter(x=>publishingRecipients.has(x.id)) || [];
  if(!recipients.length){toast('Выбери аккаунты галочками для распределения.',true);return;}
  const selected=publishingChosen();if(!selected.length){toast('Сначала выбери видео.',true);return;}
  selected.forEach((media,index)=>{const draft=publishingDraft(media);draft.account_id=recipients[index%recipients.length].id;draft.privacy='';});
  publishingSaveDrafts();renderPublishingMedia();toast('Видео распределены. Выбери видимость публикаций.');
};
$('#publish-start').onclick=()=>busy($('#publish-start'),'Запускаем…',async()=>{const assignments=publishingChosen().map(media=>({media_id:media.id,...publishingDraft(media)}));await post('/publishing/start',{assignments});assignments.forEach(x=>publishingDrafts[x.media_id].selected=false);publishingSaveDrafts();await refreshPublishing();toast('Очередь запущена. Studio должна оставаться открытой.');});
$('#publish-toggle').onclick=()=>busy($('#publish-toggle'),'Обновляем…',async()=>{await post(publishingState.paused?'/publishing/start':'/publishing/pause');await refreshPublishing();});

document.addEventListener('change',event=>{
  const target=event.target;
  if(target.matches('[data-publish-recipient]')){if(target.checked)publishingRecipients.add(target.dataset.publishRecipient);else publishingRecipients.delete(target.dataset.publishRecipient);}
  if(target.matches('[data-publish-select],[data-publish-destination],[data-publish-privacy]')) {
    const id=target.dataset.publishSelect || target.dataset.publishDestination || target.dataset.publishPrivacy;
    const media=publishingState.media.find(x=>x.id===id),draft=publishingDraft(media);
    if(target.dataset.publishSelect)draft.selected=target.checked;
    if(target.dataset.publishDestination){draft.account_id=target.value;draft.privacy='';renderPublishingMedia();}
    if(target.dataset.publishPrivacy)draft.privacy=target.value;
    publishingSaveDrafts();updatePublishingControls();
  }
});

document.addEventListener('click',async event=>{
  const quickButton=event.target.closest('[data-publish-quick]');
  if(quickButton){
    if(!publishingState?.quick?.ready){navigate('publishing');$('#publish-setup').open=!publishingState?.config.ready;toast(publishingState?.quick?.reason || 'Сначала подключи аккаунты и настрой быстрый залив.');return;}
    await busy(quickButton,'Запускаем залив…',async()=>{const result=await post('/publishing/quick-start');navigate('publishing');await refreshPublishing();toast(result.queued?`В очередь добавлено: ${result.queued}. Всё остальное Studio сделает сама.`:result.resumed?'Очередь продолжена.':'Новых подходящих видео нет.');});return;
  }
  const target=event.target.closest('[data-publish-connect],[data-publish-edit-account],[data-publish-refresh-account],[data-publish-disconnect],[data-publish-delete-account],[data-publish-caption],[data-publish-task],[data-publish-preview],[data-publish-already]');
  if(!target)return;
  if(target.dataset.publishEditAccount){openPublishingAccount(target.dataset.publishEditAccount);return;}
  if(target.dataset.publishConnect){
    const tab=window.open('about:blank','studio-tiktok-oauth');
    await busy(target,'Подключаем…',async()=>{try{const result=await post(`/publishing/accounts/${target.dataset.publishConnect}/connect`);if(tab)tab.location=result.url;else location.href=result.url;}catch(error){tab?.close();throw error;}});return;
  }
  if(target.dataset.publishRefreshAccount){await busy(target,'Обновляем…',async()=>{await post(`/publishing/accounts/${target.dataset.publishRefreshAccount}/refresh`);await refreshPublishing();});return;}
  if(target.dataset.publishDisconnect){await busy(target,'Отключаем…',async()=>{await post(`/publishing/accounts/${target.dataset.publishDisconnect}/disconnect`);await refreshPublishing();});return;}
  if(target.dataset.publishDeleteAccount){await busy(target,'Удаляем…',async()=>{await api(`/publishing/accounts/${target.dataset.publishDeleteAccount}`,{method:'DELETE'});await refreshPublishing();});return;}
  if(target.dataset.publishTask){await busy(target,'Обновляем…',async()=>{await post(`/publishing/tasks/${target.dataset.publishTask}/${target.dataset.publishAction}`);await refreshPublishing();});return;}
  if(target.dataset.publishPreview){const media=publishingState.media.find(x=>x.id===target.dataset.publishPreview);const job=state?.batches.flatMap(x=>x.jobs).find(x=>x.id===media.job_id);$('#publish-preview-video').src=job?.playback_available?`/api/jobs/${job.id}/playback`:`/api/publishing/media/${media.id}`;$('#publish-preview-dialog').showModal();return;}
  if(target.dataset.publishAlready){const media=publishingState.media.find(x=>x.id===target.dataset.publishAlready),draft=publishingDraft(media);if(!draft.account_id){toast('Выбери аккаунт, где видео уже было опубликовано.',true);return;}await busy(target,'Сохраняем…',async()=>{await post(`/publishing/media/${media.id}/already-posted`,{account_id:draft.account_id});await refreshPublishing();});return;}
  if(target.dataset.publishCaption){
    publishingEditingId=target.dataset.publishCaption;
    const media=publishingState.media.find(x=>x.id===publishingEditingId),draft=publishingDraft(media),creator=publishingProfile(draft.account_id)?.creator || {};
    $('#publish-caption').value=draft.caption;$('#publish-brand').value=draft.brand;$('#publish-aigc').checked=draft.is_aigc;
    for(const permission of ['comment','duet','stitch']){const field=$('#publish-'+permission);field.disabled=!!creator[permission+'_disabled'];field.checked=!field.disabled&&!!draft['allow_'+permission];}
    $('#publish-caption-dialog').showModal();
  }
});
$('#publish-caption-form').onsubmit=event=>{event.preventDefault();const draft=publishingDrafts[publishingEditingId];draft.caption=$('#publish-caption').value;draft.brand=$('#publish-brand').value;draft.is_aigc=$('#publish-aigc').checked;for(const permission of ['comment','duet','stitch'])draft['allow_'+permission]=$('#publish-'+permission).checked;publishingSaveDrafts();$('#publish-caption-dialog').close();};
$('#publish-preview-dialog').addEventListener('close',()=>{$('#publish-preview-video').pause();$('#publish-preview-video').removeAttribute('src');$('#publish-preview-video').load();});
refreshPublishing(true).then(()=>{if(page==='publishing')syncPublishingMedia();});
setInterval(()=>{if(['publishing','results'].includes(page))refreshPublishing();},2200);
setInterval(updatePublishingTimer,1000);

$('#publish-account-quick').onchange=event=>{$('#publish-account-quick-privacy').required=event.target.checked;};
