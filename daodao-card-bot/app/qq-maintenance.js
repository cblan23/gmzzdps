(() => {
  'use strict';
  if (window.daodaoQqMaintenanceLoaded) return;
  window.daodaoQqMaintenanceLoaded = true;
  const nav = document.querySelector('nav'), main = document.querySelector('main');
  if (!nav || !main || typeof window.switchTab !== 'function') return;
  const style = document.createElement('style');
  style.textContent = `#qqPanel .qq-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}#qqPanel .qq-box{padding:20px;background:var(--surface);border:1px solid var(--line);border-radius:6px}#qqPanel h3{margin:0 0 12px;font-size:16px}#qqPanel .qq-state{font-size:23px;font-weight:700;margin:12px 0}#qqPanel .qq-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}#qqPanel p{color:var(--muted);line-height:1.8}#qqPanel .qq-meta{font-size:13px;white-space:pre-line}#qqPanel pre{font:13px/1.8 Consolas,monospace;max-height:330px;overflow:auto;white-space:pre-wrap;word-break:break-word}#qqPanel .qq-log{margin-top:16px}#qqMessage{min-height:24px;color:var(--amber);margin:10px 0}#qqLoginModal img{width:260px;height:260px;object-fit:contain;image-rendering:pixelated;background:white;padding:8px}#qqLoginModal .qq-qr{text-align:center}#qqLoginModal p{line-height:1.8;white-space:pre-wrap}#qqConfirmModal p{line-height:1.8}@media(max-width:1000px){#qqPanel .qq-grid{grid-template-columns:1fr}}`;
  document.head.append(style);
  const tab = document.createElement('button');
  tab.id = 'qqTab'; tab.type = 'button'; tab.textContent = 'QQ 机器人'; nav.append(tab);
  const panel = document.createElement('section'); panel.id = 'qqPanel'; panel.hidden = true;
  panel.innerHTML = `<div class="panel-head"><h2>叨叨助手 · 机器人维护</h2><div class="panel-tools"><small id="qqUpdated">尚未检测</small><button id="qqRefresh">刷新检测</button></div></div>
  <div id="qqMessage" role="status" aria-live="polite"></div><div class="qq-grid">
  <div class="qq-box"><h3>QQ 登录</h3><div class="qq-state" id="qqOnline">检测中</div><div class="qq-meta" id="qqAccount"></div><p>机器人无响应时点击快速恢复。自动检测、恢复登录进程并等待新二维码；手机扫码后自动连接发卡。</p><div class="qq-actions"><button class="primary" data-qq-action="recover">快速恢复</button><button id="qqLogin">扫码登录</button></div></div>
  <div class="qq-box"><h3>消息接收与发卡进程</h3><div class="qq-state" id="qqWorker">检测中</div><div class="qq-meta" id="qqServices"></div><p>QQ 在线但没有响应时，可重启发卡进程。已有领取记录保留。</p><div class="qq-actions"><button data-qq-action="restart_bot">重启发卡进程</button><button class="danger" data-qq-action="restart_napcat">重启 QQ 容器</button></div></div>
  <div class="qq-box"><h3>发卡服务</h3><div class="qq-state" id="qqApi">检测中</div><div class="qq-meta" id="qqCards"></div><p id="qqGroups"></p><p>新卡首次登录激活后可用 8 小时；当天重复领取仍返回原卡。</p></div></div>
  <div class="qq-box qq-log"><h3>近期运行记录</h3><p>只显示运行事件，不展示卡号、密码或服务 Token。页面打开时每 10 秒刷新。</p><pre id="qqLogs">暂无记录</pre><div class="qq-actions"><button id="qqDiagnosticView">查看详细诊断</button><button id="qqDiagnosticExport">导出诊断日志</button></div><p>诊断包含 QQ 号、群号、请求编号和耗时，仅供管理员排查。确认发送成功不代表对方已读。</p><pre id="qqDiagnosticLogs" hidden></pre></div>`;
  const selector=document.createElement('select');selector.id='qqInstance';
  selector.setAttribute('aria-label','选择机器人实例');
  for(const [value,label] of [['legacy','3921054980 · 旧服务器'],['ecs','3035610294 · ECS'],['local','2864967431 · 本地电脑']]){
    const option=document.createElement('option');option.value=value;option.textContent=label;selector.append(option);
  }
  panel.querySelector('.panel-tools').prepend(selector);
  const instanceBar=document.createElement('div');instanceBar.className='qq-actions';instanceBar.style.marginBottom='16px';
  instanceBar.setAttribute('aria-label','三个机器人实例');
  const instanceButtons=[];
  for(const [value,label] of [['legacy','3921054980 · 旧服务器'],['ecs','3035610294 · ECS'],['local','2864967431 · 本地电脑']]){
    const button=document.createElement('button');button.type='button';button.textContent=label;
    button.setAttribute('aria-pressed',value==='legacy'?'true':'false');
    button.onclick=()=>{selector.value=value;selector.onchange();};
    instanceButtons.push({value,button});instanceBar.append(button);
  }
  panel.prepend(instanceBar);
  main.append(panel);
  const modal = document.createElement('div'); modal.className = 'modal'; modal.id = 'qqLoginModal'; modal.hidden = true;
  modal.innerHTML = `<div class="modal-body" role="dialog" aria-modal="true" aria-labelledby="qqLoginTitle"><div class="modal-head"><h2 id="qqLoginTitle">机器人扫码登录</h2><button id="qqLoginClose">关闭</button></div><p id="qqLoginHint">正在获取登录状态…</p><div class="qq-qr"><img id="qqQrImage" alt="机器人 QQ 登录二维码" hidden></div><div class="modal-actions"><button data-qq-action="recover">快速恢复登录进程</button><button id="qqQrRefresh">刷新二维码</button><button id="qqLoginCheck" class="primary">我已扫码，检测登录</button></div></div>`;
  document.body.append(modal);
  const confirmModal = document.createElement('div'); confirmModal.className='modal'; confirmModal.id='qqConfirmModal'; confirmModal.hidden=true;
  confirmModal.innerHTML='<div class="modal-body" role="dialog" aria-modal="true" aria-labelledby="qqConfirmTitle"><div class="modal-head"><h2 id="qqConfirmTitle">确认维护操作</h2></div><p id="qqConfirmText"></p><div class="modal-actions"><button id="qqConfirmCancel">取消</button><button id="qqConfirmYes" class="primary">确认操作</button></div></div>';
  document.body.append(confirmModal);
  const el = id => document.getElementById(id);
  const set = (id, text) => { el(id).textContent = text; };
  let csrf = '', loading = false, actionPending = false, pendingConfirmation = null;
  let loginLoading=false, modalGeneration=0, qrDeadline=0, currentOperation=null;
  let selectedInstance='legacy',instanceGeneration=0;
  function clearQr(){qrDeadline=0;el('qqQrImage').hidden=true;el('qqQrImage').removeAttribute('src');}
  function closeLogin(){modal.hidden=true;modalGeneration++;clearQr();}
  function lockActions(){document.querySelectorAll('[data-qq-action]').forEach(button=>{button.disabled=actionPending||selectedInstance==='local';});el('qqQrRefresh').disabled=actionPending||selectedInstance==='local';el('qqLogin').disabled=selectedInstance==='local';}
  function confirmAction(text) {
    set('qqConfirmText', text); confirmModal.hidden=false;
    return new Promise(resolve => { pendingConfirmation=resolve; el('qqConfirmCancel').focus(); });
  }
  function finishConfirmation(answer) { confirmModal.hidden=true; if(pendingConfirmation)pendingConfirmation(answer); pendingConfirmation=null; }
  el('qqConfirmCancel').onclick=()=>finishConfirmation(false); el('qqConfirmYes').onclick=()=>finishConfirmation(true);
  async function api(path, options={}) {
    const generation=instanceGeneration;
    const prefix=selectedInstance==='ecs'?'/api/v1/dps/admin/qq-instances/ecs/':'/api/v1/dps/admin/qq/';
    if(selectedInstance==='local')throw new Error('本地维护通道尚未接通，请在本地登录页操作');
    const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),15000);
    try {
      const response=await fetch(prefix+path,{cache:'no-store',credentials:'same-origin',...options,signal:controller.signal});
      if(response.status===401)throw new Error('管理登录已失效，请刷新管理页面重新登录');
      const value=await response.json();
      if(generation!==instanceGeneration)throw new Error('实例已切换，请刷新当前实例');
      if(!response.ok)throw new Error(value.error||'维护服务暂不可用');
      return value;
    } finally {clearTimeout(timeout);}
  }
  const serviceActive = row => row && row.ActiveState==='active';
  async function refresh() {
    if(loading)return; loading=true; el('qqRefresh').disabled=true;
    try {
      if(selectedInstance==='local'){
        set('qqOnline','远程状态待接入');set('qqAccount','机器人 QQ：2864967431 · 本地电脑');
        set('qqWorker','远程状态未接通');set('qqServices','本地维护通道尚未接通');
        set('qqApi','未检查');set('qqCards','共用远程卡池；这里不代表后台故障');
        set('qqGroups','');set('qqLogs','');set('qqUpdated','本地实例');
        set('qqMessage','本地已完成首次登录，但网页维护通道尚未接通，这里不能实时确认在线状态。暂请在本地电脑 http://127.0.0.1:6109 操作。');
        lockActions();return;
      }
      const data=await api('status'); csrf=data.csrf;
      const online=data.qq.online===true;
      const degraded=online&&data.qq_interface_health?.ok===false;
      set('qqOnline',degraded?'在线标志异常，接口无响应':online?'在线':data.qq.online===false?'离线':'连接暂不可用');
      el('qqOnline').style.color=online?'var(--green)':'var(--amber)';
      set('qqAccount','机器人 QQ：'+data.bot_qq+'\nOneBot：'+(data.qq.connected?'已连接':'未连接'));
      const worker=serviceActive(data.services['daodao-card-bot.service']);
      set('qqWorker',data.worker?.connected?'已连接':worker?'进程运行，等待连接':'未运行');
      set('qqServices','QQ 容器：'+(data.services.napcat?.Running?'运行中':'未运行或状态未知')+'\n发卡 API：'+(data.card_api_remote?'新 ECS 统一管理':serviceActive(data.services['daodao-card-api.service'])?'运行中':'未运行或状态未知'));
      set('qqApi',data.cards.error?'检测失败':'连接正常');
      set('qqCards',data.cards.error||('累计领取：'+(data.cards.claimed??'--')+'\n今日领取：'+(data.cards.today_claimed??'--')));
      set('qqGroups','允许领卡群：'+data.groups.join('、'));
      const loginEvents=(data.login_events||[]).map(item=>new Date(item.at*1000).toLocaleString()+' 登录状态：'+(item.online===true?'在线':item.online===false?'离线':'未知')+(item.error?'；QQ 提示：'+item.error:''));
      set('qqLogs',loginEvents.concat(data.logs).join('\n')||'暂无运行事件');
      set('qqUpdated','最近检测 '+new Date(data.server_time*1000).toLocaleTimeString());
      if(degraded)set('qqMessage','QQ心跳在线但实时接口检测失败，请点击快速恢复；此状态不能确认可以收发消息。');
      else if(data.operation?.state==='done'&&(!online||!data.worker?.connected))set('qqMessage','上次维护操作已结束，但当前连接已失效；请查看实时状态并恢复登录。');
      else if(data.operation)set('qqMessage',data.operation.message);
      else set('qqMessage',online?'QQ 已在线，群成员可 @叨叨助手 领卡':'QQ 未在线时不能发卡，请使用恢复登录或扫码登录');
      actionPending=data.operation?.state==='running';
      currentOperation=data.operation;
      lockActions();
      if(!modal.hidden){
        if(actionPending){clearQr();set('qqLoginHint',data.operation.message);}
        else if(online){clearQr();set('qqLoginHint',data.worker?.connected?'QQ 与发卡进程已连接，可在群内领卡。':'QQ 已上线，正在等待发卡连接…');}
        else if(data.operation?.state==='failed'){clearQr();set('qqLoginHint',data.operation.message+'。可点击“快速恢复登录进程”。');}
        else await readLogin();
      }
    } catch(error) {set('qqMessage',error.name==='AbortError'?'检测超时，请稍后刷新':error.message);}
    finally {loading=false;el('qqRefresh').disabled=false;}
  }
  async function readLogin() {
    if(loginLoading||modal.hidden||actionPending)return;
    loginLoading=true;const generation=modalGeneration;
    try {
      const data=await api('login');
      if(modal.hidden||generation!==modalGeneration||actionPending)return;
      set('qqLoginHint',data.online?'QQ 已在线，无需重新扫码。':data.error||'使用机器人 QQ 扫码，并在手机上确认登录。');
      if(data.qr&&data.qr.startsWith('data:image/png;base64,')){
        el('qqQrImage').src=data.qr;el('qqQrImage').hidden=false;
        qrDeadline=Date.now()+Math.max(0,Number(data.refresh_in_seconds)||0)*1000;
      } else {clearQr();if(!data.online&&!data.error)set('qqLoginHint','没有已确认的新二维码，请刷新二维码；失败时点击快速恢复。');}
    } catch(error){clearQr();if(!modal.hidden)set('qqLoginHint',error.name==='AbortError'?'登录状态读取超时，请稍后再试':error.message);}
    finally{loginLoading=false;}
  }
  async function showLogin(){
    modalGeneration++;modal.hidden=false;clearQr();set('qqLoginHint','正在检测登录状态…');
    await refresh();
    if(!actionPending)await readLogin();
  }
  async function action(name) {
    const targetGeneration=instanceGeneration;
    if(actionPending)return;
    if(name==='restart_napcat'&&!await confirmAction('重启 QQ 容器会暂时中断机器人消息，保留 Session 和领取记录；可能需要重新扫码。继续吗？'))return;
    if(name==='restart_bot'&&!await confirmAction('重启发卡进程会短暂暂停消息处理，不更换已领取的卡。继续吗？'))return;
    if(targetGeneration!==instanceGeneration)return;
    if(actionPending)return;
    if(!csrf)await refresh();
    if(targetGeneration!==instanceGeneration)return;
    if(!csrf){set('qqMessage','请先完成管理状态检测');return;}
    actionPending=true;lockActions();
    if(['recover','restart_napcat','refresh_qr','quick_login'].includes(name)){
      modalGeneration++;modal.hidden=false;clearQr();set('qqLoginHint','正在处理，请稍候；只在确认新二维码后显示。');
    }
    try {
      const value=await api('action',{method:'POST',headers:{'Content-Type':'application/json','X-Daodao-CSRF':csrf},body:JSON.stringify({action:name,request_id:crypto.randomUUID()})});
      set('qqMessage',value.operation.message);
      currentOperation=value.operation;
      setTimeout(refresh,2000);
    } catch(error){actionPending=false;lockActions();clearQr();set('qqMessage',error.message);if(!modal.hidden)set('qqLoginHint',error.message);setTimeout(refresh,2000);}
  }
  document.querySelectorAll('[data-qq-action]').forEach(button=>{button.onclick=()=>action(button.dataset.qqAction);});
  el('qqRefresh').onclick=refresh;el('qqLogin').onclick=showLogin;
  selector.onchange=()=>{
    selectedInstance=selector.value;instanceGeneration++;csrf='';actionPending=false;currentOperation=null;
    for(const item of instanceButtons){item.button.classList.toggle('primary',item.value===selectedInstance);item.button.setAttribute('aria-pressed',item.value===selectedInstance?'true':'false');}
    closeLogin();finishConfirmation(false);clearQr();lockActions();
    el('qqDiagnosticLogs').hidden=true;
    if(!loading)refresh();else setTimeout(refresh,1000);
  };
  async function diagnostics(download) {
    el('qqDiagnosticView').disabled=true;el('qqDiagnosticExport').disabled=true;
    try {
      const data=await api('diagnostics');
      if(download){
        const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));
        const link=document.createElement('a');link.href=url;link.download='qq-diagnostics-'+Date.now()+'.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      } else {
        el('qqDiagnosticLogs').hidden=false;
        set('qqDiagnosticLogs',(data.events||[]).slice(-150).map(row=>new Date(row.at*1000).toLocaleString()+' '+JSON.stringify(row)).join('\n')||'暂无详细记录');
      }
    } catch(error){set('qqMessage','诊断读取失败：'+error.message);}
    finally {el('qqDiagnosticView').disabled=false;el('qqDiagnosticExport').disabled=false;}
  }
  el('qqDiagnosticView').onclick=()=>diagnostics(false);
  el('qqDiagnosticExport').onclick=()=>diagnostics(true);
  el('qqQrRefresh').onclick=()=>action('refresh_qr');el('qqLoginCheck').onclick=refresh;
  el('qqLoginClose').onclick=closeLogin;
  document.addEventListener('keydown',event=>{if(event.key==='Escape'){closeLogin();finishConfirmation(false);}});
  const originalSwitch=window.switchTab;
  window.switchTab=function(name){originalSwitch(name);panel.hidden=name!=='qq';tab.classList.toggle('active',name==='qq');const metrics=main.querySelector('.metrics');if(metrics)metrics.hidden=name==='qq';if(name==='qq')refresh();else closeLogin();};
  tab.onclick=()=>window.switchTab('qq');
  setInterval(()=>{if(!panel.hidden&&!document.hidden)refresh();},10000);
  setInterval(()=>{
    if(modal.hidden)return;
    if(qrDeadline&&Date.now()>=qrDeadline){clearQr();set('qqLoginHint','二维码显示时间已到，请刷新后再扫码。');}
    if(!document.hidden)refresh();
  },3000);
})();
