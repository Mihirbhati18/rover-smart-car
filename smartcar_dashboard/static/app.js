const $ = id => document.getElementById(id);
let state = {}, overlay = false, lastEvents = '', recognition = null, clientError = '';
let routeRecognition = null, previewedRoute = '';
const busyButtons = ['connectCamera','connectSerial','connectBluetooth','disconnect','mode4','clear','analyze','replay'];
async function api(action, data={}) {
  try {
    const response = await fetch('/api/'+action, {method:'POST',headers:{'Content-Type':'application/json','X-Session':window.SESSION_TOKEN},body:JSON.stringify(data)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Request failed');
    if(action!=='heartbeat')clientError='';
    return result;
  } catch(error) { clientError=error.message; $('error').hidden=false; $('error').textContent=clientError; return null; }
}
function text(id, value){$(id).textContent=value;}
function render(s){
  state=s;
  const replay=s.source==='replay';
  text('sourceBadge',replay?'RECORDED DEMO':'LIVE HARDWARE');
  text('mode',s.mode==='REPLAY'?'Recorded demo':s.mode);
  text('cameraStatus',s.camera_connected?'Live frames arriving':'Not connected');
  text('serialStatus',s.serial_connected?s.port+' connected':'Disconnected');
  $('connectBluetooth').classList.toggle('active',s.serial_connected&&s.transport==='ble');
  text('connectBluetooth',s.serial_connected&&s.transport==='ble'?'✓ HC-08 connected':'Connect HC-08 Bluetooth');
  text('frameAge',replay?'RECORDED FRAME':s.frame_age!=null?s.frame_age+'s FRAME AGE':'NO LIVE FRAME');
  text('feedLabel',replay?'● RECORDED REFERENCE':s.camera_connected?'● LIVE PHONE CAMERA':'○ CAMERA OFFLINE');
  text('imageCaption',overlay?'Image similarity overlay · experimental':'Phone camera · '+(replay?'saved reference image':'live view'));
  text('rangeSource',replay?'SAMPLE VALUES':s.analysis?'MEASURED '+s.analysis.time:'MEASURED BY SENSOR');
  if(s.manual_only)text('rangeSource','FRONT ONLY · ADVISORY');
  text('notice',replay?'Recorded image + sample sensor readings. Commands are simulated; hardware is disconnected.':s.armed?'Controls ON. Camera actions and voice commands can move the car in short pulses. STOP / Esc is always available.':'Controls OFF. Connect camera and Arduino, select mode 4, then click Enable controls when ready.');
  $('notice').classList.toggle('live',!replay);
  $('error').hidden=!(s.error||clientError); if(s.error||clientError) text('error',s.error||clientError);
  $('sensorWarning').hidden=!(s.armed&&s.manual_only&&s.warning);
  text('sensorWarning',s.warning||'');
  const modeSelected=s.mode==='PRESENTATION';
  text('arm','Enable controls'); $('arm').classList.toggle('active',s.armed&&!s.manual_only);
  $('arm').setAttribute('aria-pressed',String(s.armed&&!s.manual_only));
  $('armManual').classList.toggle('active',s.armed&&s.manual_only);
  $('armManual').setAttribute('aria-pressed',String(s.armed&&s.manual_only));
  $('mode4').classList.toggle('selected-mode',modeSelected);
  $('mode4').setAttribute('aria-pressed',String(modeSelected));
  text('mode4',modeSelected?'✓ Mode 4 selected':'Select mode 4');
  text('controlsState',s.emergency?'■ STOP active — controls OFF':s.armed?(replay?'● Demo controls ON — simulated movement only':'● Controls ON — car can move'):'○ Controls OFF — movement disabled');
  $('controlsState').classList.toggle('on',s.armed);
  if(s.armed&&s.manual_only){text('controlsState','● Manual controls ON — sensor warnings only');text('notice','Manual short steps ON. Use the direction buttons below the first microphone. Each click stops automatically. Ultrasonic warnings do not block your manual commands.');}
  text('controlsHelp',s.emergency?'Click Clear emergency stop first.':s.armed?'Green glow means enabled. Disable controls or STOP switches movement off.':!replay&&!modeSelected?'Click Select mode 4, then Enable controls.':'Click Enable controls when ready. Previewing a route never moves the car.');
  if(!s.armed&&!s.emergency&&modeSelected&&!s.camera_connected)text('controlsHelp','No camera? Click Enable manual (no camera), then use the short direction buttons below the first microphone.');
  text('stage',s.stage);text('lastCommand',s.last_command==='—'?'NO COMMAND':s.last_command);
  text('routeStatus',s.route_status || 'Preview a route first');
  if(s.error && s.route_status==='Parsing route')text('routeStatus','Route was not understood — edit the text and click Preview route again. No movement sent.');
  $('routeRun').disabled=!s.armed||s.busy||s.emergency||!previewedRoute||s.route_text!==previewedRoute||(!replay&&!(s.manual_only?s.manual_firmware:s.route_firmware));
  text('routeSafety',s.manual_only?'MANUAL ROUTE · Following your instructions. Camera is optional, ultrasonic is advisory only, and there are no left/right clearance checks. STOP cancels the route.':'CAMERA CONTROLS · Front ≤20 cm stops route pulses; turns require the chosen side over 20 cm. Manual mode uses warnings only and does not require the camera.');
  text('routeLimits',s.manual_only?'Manual routes support forward/reverse distances and left/right angles. No 6-step or camera-mode distance/time limits. Whole positive cm/degrees; left/right alone means 90°. Movement is estimated from timing. Each pulse stops automatically; controls remain ON after the route.':'Distances and angles are timed estimates. Camera-mode routes support up to 6 forward/left/right instructions and finish with controls OFF.');
  if(!replay&&!s.route_firmware) text('routeStatus','New route firmware must be uploaded before running. Preview is available.');
  text('passStatus',s.pass_status||'Not running');drawTrack(s.pass_track);
  $('passRun').disabled=!s.armed||s.busy||s.emergency||replay||!s.camera_connected;
  if(document.activeElement!==$('cropBottom'))$('cropBottom').value=String(s.crop_bottom);
  text('boxes',s.boxes?'Boxes ON':'Boxes OFF');$('boxes').setAttribute('aria-pressed',String(s.boxes));
  text('auto',s.auto?'Loop running…':'Run up to 5 steps');
  for(const id of busyButtons) $(id).disabled=s.busy;
  $('arm').disabled=s.armed||s.busy||s.emergency||(!replay&&(!modeSelected||!s.serial_connected||!s.camera_connected));
  $('armManual').disabled=s.armed||s.busy||s.emergency||(!replay&&(!modeSelected||!s.serial_connected));
  $('disarm').disabled=!s.armed;
  for(const id of ['step','auto']) $(id).disabled=!s.armed||s.manual_only||s.busy||s.emergency;
  document.querySelectorAll('[data-direction]').forEach(b=>b.disabled=!s.armed||s.busy||s.emergency);
  if(s.analysis){
    const a=s.analysis;
    text('scoreL',a.scores.LEFT+'%');text('scoreC',a.scores.CENTER+'%');text('scoreR',a.scores.RIGHT+'%');
    text('direction',a.direction);text('reason',a.reason);text('decisionIcon',({LEFT:'↰',RIGHT:'↱',FORWARD:'↑',STOP:'■'})[a.direction]||'◎');
  } else {text('decisionIcon','◎');text('direction','Awaiting analysis');text('reason','Capture a frame to see the image hint and clearance check together.');for(const id of ['scoreL','scoreC','scoreR'])text(id,'—');}
  ['left','centre','right'].forEach((k,i)=>text(['rangeL','rangeC','rangeR'][i],s.manual_only&&k!=='centre'?'—':s.scan?(s.scan[k]??'Invalid'):'—'));
  if(s.voice_text)text('transcript','Heard / received: “'+s.voice_text+'”');
  const eventKey=s.events.map(e=>e.id).join(',');
  if(eventKey!==lastEvents){
    lastEvents=eventKey;$('timeline').replaceChildren();
    for(const e of s.events.slice().reverse()){
      const row=document.createElement('div');row.className='event '+e.kind.toLowerCase();
      const when=document.createElement('time');when.textContent=e.time;
      const kind=document.createElement('b');kind.textContent=e.kind;
      const message=document.createElement('p');message.textContent=e.message;
      row.append(when,kind,message);$('timeline').append(row);
    }
  }
}
let polling=false;
async function poll(){if(polling)return;polling=true;try{const r=await fetch('/api/state');if(!r.ok)throw new Error('Server offline');render(await r.json());}catch(e){text('stage','Local server unavailable');$('error').hidden=false;text('error','Local server disconnected. Arduino pulses stop automatically. Restart the launcher to reconnect.');}finally{polling=false;}}
$('connectCamera').onclick=()=>{localStorage.setItem('roverCamera',$('cameraUrl').value);api('camera',{url:$('cameraUrl').value,rotation:Number($('rotation').value)});};
$('cameraUrl').value=localStorage.getItem('roverCamera')||'';
$('connectSerial').onclick=()=>api('connect',{port:$('port').value});
$('connectBluetooth').onclick=()=>api('connect_ble');
$('disconnect').onclick=()=>api('disconnect');
$('mode4').onclick=()=>api('mode');
$('arm').onclick=()=>api('arm');
$('armManual').onclick=()=>api('arm',{manual:true});
$('disarm').onclick=()=>api('disarm');
$('stop').onclick=()=>api('stop');
$('clear').onclick=()=>api('clear');
$('analyze').onclick=()=>api('analyze');
$('step').onclick=()=>api('step');
$('auto').onclick=()=>api('auto');
$('replay').onclick=()=>api('replay');
$('overlay').onclick=()=>{overlay=!overlay;text('overlay',overlay?'Show camera':'Show analysis');refreshImage();};
$('present').onclick=()=>{document.body.classList.toggle('focus');text('present',document.body.classList.contains('focus')?'Exit presentation ↙':'Presentation view ↗');};
$('helpToggle').onclick=e=>{e.preventDefault();$('guide').hidden=!$('guide').hidden;if(!$('guide').hidden)$('guide').scrollIntoView({behavior:'smooth'});};
$('voiceForm').onsubmit=e=>{e.preventDefault();api('voice',{text:$('voiceText').value});};
$('routeForm').onsubmit=e=>{e.preventDefault();previewedRoute=$('routeText').value;api('route_preview',{text:previewedRoute});};
$('routeText').oninput=()=>{previewedRoute='';$('routeRun').disabled=true;};
$('routeRun').onclick=()=>{if(previewedRoute&&$('routeText').value===previewedRoute)api('route_run',{text:previewedRoute});};
$('routeCancel').onclick=()=>api('stop');
function passRun(text){api('pass_run',{text,turn_deg:Number($('passTurn').value),safety_cm:Number($('passSafety').value)});}
$('passForm').onsubmit=e=>{e.preventDefault();passRun($('passText').value);};
let lastTrack='';
function drawTrack(t){
  const key=JSON.stringify(t);if(key===lastTrack)return;lastTrack=key;
  const svg=$('passTrack'),ns='http://www.w3.org/2000/svg';svg.replaceChildren();
  if(!t){svg.setAttribute('viewBox','-60 -110 120 120');return;}
  // Top-down view in cm: forward is up (svg y = -x), right is right (svg x = y).
  const all=t.points.concat(t.obstacles);
  const side=Math.max(40,...all.map(p=>Math.abs(p[1])+20));
  svg.setAttribute('viewBox',`${-side} ${-(t.target+20)} ${2*side} ${t.target+40}`);
  const el=(name,attrs)=>{const e=document.createElementNS(ns,name);for(const k in attrs)e.setAttribute(k,attrs[k]);e.setAttribute('vector-effect','non-scaling-stroke');svg.append(e);return e;};
  el('line',{x1:0,y1:0,x2:0,y2:-t.target,class:'trk-plan'});
  el('circle',{cx:0,cy:-t.target,r:4,class:'trk-goal'});
  el('circle',{cx:0,cy:0,r:2.5,class:'trk-start'});
  el('polyline',{points:t.points.map(p=>`${p[1]},${-p[0]}`).join(' '),class:'trk-path'});
  for(const o of t.obstacles)el('circle',{cx:o[1],cy:-o[0],r:3.5,class:'trk-obst'});
  const last=t.points[t.points.length-1];
  el('polygon',{points:'0,-7 5,5 -5,5',class:'trk-car',transform:`translate(${last[1]},${-last[0]}) rotate(${t.heading})`});
}
$('passCancel').onclick=()=>api('stop');
$('cropBottom').onchange=()=>api('view',{crop_bottom:Number($('cropBottom').value),boxes:state.boxes});
$('boxes').onclick=()=>api('view',{crop_bottom:state.crop_bottom,boxes:!state.boxes});
document.querySelectorAll('[data-direction]').forEach(b=>b.onclick=()=>api('voice',{text:b.dataset.direction}));
document.addEventListener('keydown',e=>{if(e.key==='Escape'){e.preventDefault();api('stop');}});
const SpeechRecognition=window.SpeechRecognition||window.webkitSpeechRecognition;
if(!SpeechRecognition){$('routeMic').disabled=true;text('routeHelp','Use Edge/Chrome for route speech, or type the route below.');}
$('routeMic').onclick=()=>{
  if(!SpeechRecognition)return;
  if(routeRecognition){routeRecognition.abort();return;}
  if(recognition)recognition.abort();
  routeRecognition=new SpeechRecognition();routeRecognition.lang='en-IN';routeRecognition.continuous=false;routeRecognition.interimResults=true;
  routeRecognition.onstart=()=>{text('routeMicState','LISTENING TO ROUTE');$('routeMic').classList.add('listening');};
  routeRecognition.onresult=e=>{const r=e.results[e.resultIndex];text('routeTranscript',r[0].transcript);if(r.isFinal){
    if(/\b(emergency|halt|freeze)\b/i.test(r[0].transcript)||/^\s*stop[.!]?\s*$/i.test(r[0].transcript)){api('stop');return;}
    $('routeText').value=r[0].transcript;previewedRoute=r[0].transcript;api('route_preview',{text:previewedRoute});
  }};
  routeRecognition.onerror=e=>text('routeHelp','Route microphone: '+e.error+'. Type a route as backup.');
  routeRecognition.onend=()=>{text('routeMicState','ROUTE MIC IDLE');$('routeMic').classList.remove('listening');routeRecognition=null;};
  try{routeRecognition.start();}catch(e){text('routeHelp',e.message);routeRecognition=null;}
};
let passRecognition=null;
if(!SpeechRecognition){$('passMic').disabled=true;text('passHelp','Use Edge/Chrome for speech, or type the distance.');}
$('passMic').onclick=()=>{
  if(!SpeechRecognition)return;
  if(passRecognition){passRecognition.abort();return;}
  if(recognition)recognition.abort();
  if(routeRecognition)routeRecognition.abort();
  passRecognition=new SpeechRecognition();passRecognition.lang='en-IN';passRecognition.continuous=false;passRecognition.interimResults=true;
  passRecognition.onstart=()=>{text('passMicState','LISTENING');$('passMic').classList.add('listening');};
  passRecognition.onresult=e=>{const r=e.results[e.resultIndex];text('passTranscript',r[0].transcript);if(r.isFinal){
    if(/\b(stop|emergency|halt|freeze)\b/i.test(r[0].transcript)){api('stop');return;}
    $('passText').value=r[0].transcript;passRun(r[0].transcript);
  }};
  passRecognition.onerror=e=>text('passHelp','Pass microphone: '+e.error+'. Type the distance as backup.');
  passRecognition.onend=()=>{text('passMicState','PASS MIC IDLE');$('passMic').classList.remove('listening');passRecognition=null;};
  try{passRecognition.start();}catch(e){text('passHelp',e.message);passRecognition=null;}
};
if(!SpeechRecognition){$('mic').disabled=true;text('voiceHelp','This browser has no speech recognition. Open this localhost address in Chrome or Edge, or use typed commands.');text('micState','TEXT BACKUP READY');}
$('mic').onclick=()=>{
  if(!SpeechRecognition)return;
  if(routeRecognition)routeRecognition.abort();
  if(recognition){recognition.abort();return;}
  recognition=new SpeechRecognition();recognition.lang='en-IN';recognition.continuous=false;recognition.interimResults=true;
  recognition.onstart=()=>{$('mic').classList.add('listening');text('micState','LISTENING');};
  recognition.onresult=e=>{const result=e.results[e.resultIndex];text('transcript',result[0].transcript);if(result.isFinal){$('voiceText').value=result[0].transcript;api('voice',{text:result[0].transcript});}};
  recognition.onerror=e=>{text('voiceHelp','Microphone: '+e.error+'. Typed commands are available below.');};
  recognition.onend=()=>{$('mic').classList.remove('listening');text('micState','MIC IDLE');recognition=null;};
  try{recognition.start();}catch(e){text('voiceHelp',e.message);recognition=null;}
};
let imageLoading=false;
function refreshImage(){if(imageLoading)return;imageLoading=true;$('cameraImage').src=(overlay?'/analysis.jpg':'/frame.jpg')+'?t='+Date.now();}
$('cameraImage').onload=()=>{imageLoading=false;$('imagePlaceholder').hidden=true;};
$('cameraImage').onerror=()=>{imageLoading=false;$('imagePlaceholder').hidden=false;};
setInterval(poll,600);setInterval(refreshImage,450);
setInterval(()=>{if(!document.hidden)api('heartbeat');},900);
document.addEventListener('visibilitychange',()=>{if(document.hidden&&state.armed)api('disarm');});
window.addEventListener('pagehide',()=>fetch('/api/disarm',{method:'POST',headers:{'Content-Type':'application/json','X-Session':window.SESSION_TOKEN},body:'{}',keepalive:true}));
poll();
