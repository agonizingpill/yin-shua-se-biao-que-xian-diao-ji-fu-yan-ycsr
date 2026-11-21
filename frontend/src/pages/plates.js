import {esc,formData,fixed} from '../utils/format.js';
import {notify,jobLabel} from '../components/press.js';
import {send} from '../api.js';
const plateLabels={registered:'待测套准',measured:'待主管认可',ready:'上机就绪'};
const swLabels={switching:'换版中·暂停放行',failed:'切换失败·旧版保留',succeeded:'首张签印·已换版'};
const swClass={switching:'fail',failed:'',succeeded:'pass'};
const cmyk=()=>`<div class="cmykfields">${['c','m','y','k'].map(k=>`<div class="tone"><label>${k.toUpperCase()} / 实地密度</label><input name="density_${k}" type="number" min="0" max="3" step=".01" value="1.4" required></div>`).join('')}</div><label>网点扩大率 / %</label><input name="dot_gain" type="number" min="0" max="50" step=".1" value="14" required>`;

export function render(s){
 const readonly=s.user.role==='viewer';
 const switchById=id=>s.switches.find(w=>w.id===id);
 const plateById=id=>s.plates.find(p=>p.id===id);
 return `<div class="head"><div><div class="eyebrow">PLATE VERSION / BACKUP & IN-USE / 05</div>
<h1>备用印版先登记，旧版照常开印。</h1>
<p>新版以备版身份登记、测量、认可，均不遮挡旧在用版量产。换版时记录累计印量分界，切换失败保留旧版；新版首张沿用训练判别，合格经主管签印才恢复放行。</p></div></div>
<div class="production-layout">
<section class="section">
<h2>登记备用印版版本</h2>
<form id="plate-form">
<label>归属工单</label><select name="job_id">${s.jobs.map(j=>`<option value="${j.id}">${esc(j.code)} · ${esc(j.title)}</option>`).join('')}</select>
<label>印版版本</label><input name="version" placeholder="V2" required>
<label>客户定稿与制版来源</label><textarea name="source_note" minlength="5" required placeholder="客户签字PDF版本、制版日期与校对人"></textarea>
<div class="actions"><button class="primary" ${readonly?'disabled':''}>登记备用印版</button></div>
</form>
<p class="note">备版期间旧在用版照常生产与封箱；已认可版次套准值与来源冻结，重制另建新版本。</p>
<h2>各工单在用版与待放行原因</h2>
${s.plateStatus.map(st=>`<article class="section plate-status"><div class="eyebrow">${esc(st.job_code)} · ${esc(st.job_title)} / ${esc(st.job_status)}</div>
<h3>在用版：${st.in_use_plate?esc(st.in_use_plate.version)+' <span class="status pass">上机就绪</span>':'<span class="status fail">尚未激活</span>'} ｜ 最新登记：${esc(st.latest_plate?.version||'—')}（${st.latest_plate?.role==='in_use'?'在用':'备用'}）</h3>
${st.pending_release_reasons.length?`<ul class="reasons">${st.pending_release_reasons.map(r=>`<li>${esc(r)}</li>`).join('')}</ul>`:'<p class="note">无拦截，可正常抽检、封箱与发货。</p>'}
${st.active_switch_id?`<p class="muted">进行中换版单 #${st.active_switch_id}</p>`:''}</article>`).join('')}
</section>
<section>
<h2>印版版本簿（备用 / 在用）</h2>
${s.plates.map(p=>{const sw=s.switches.find(w=>w.new_plate_id===p.id&&w.state==='switching');return `<article class="plate section" data-id="${p.id}">
<div class="eyebrow">${jobLabel(s,p.job_id)} / ${esc(p.version)}</div>
<h2>${plateLabels[p.state]} <span class="status ${p.state==='ready'?'pass':''}">${fixed(p.registration_error_mm,3)} mm</span> ${p.in_use?'<span class="status pass">在用版</span>':'<span class="status">备用版</span>'} ${sw?`<span class="status fail">换版中 #${sw.id}</span>`:''}</h2>
<p class="muted">${esc(p.source_note)}</p>
<form class="plate-measure" data-key="${p.id}">
<label>四色最大套准误差 / mm</label><input name="registration_error_mm" type="number" min="0" max="5" step=".01" value="${p.registration_error_mm??.08}" ${p.state==='ready'?'disabled':''}>
<div class="actions">
<button ${p.state==='ready'||readonly?'disabled':''}>记录套准测量</button>
<button type="button" class="magenta" data-approve="${p.id}" ${p.state!=='measured'||s.user.role!=='admin'?'disabled':''}>主管认可备版就绪</button>
<button type="button" data-activate="${p.id}" ${p.state!=='ready'||p.in_use||s.user.role!=='admin'?'disabled':''}>${p.in_use?'已是在用版':'主管激活为在用版'}</button>
</div></form>
<small>认可人 ${esc(p.approved_by||'尚未认可')} · 备版认可不改变在用版；首版认可自动激活</small>
${p.state==='ready'&&!p.in_use&&!sw?`<form class="switch-start" data-plate="${p.id}"><input type="hidden" name="job_id" value="${p.job_id}"><input type="hidden" name="new_plate_id" value="${p.id}"><div class="actions"><button class="primary" ${readonly?'disabled':''}>发起换版（分界后暂停放行）</button></div></form>`:''}
</article>`}).join('')}
<h2>换版单与首张送验</h2>
${s.switches.length?s.switches.map(w=>{const old=plateById(w.old_plate_id),np=plateById(w.new_plate_id);const fa=w.first_sample_id?s.samples.find(x=>x.id===w.first_sample_id):null;const proof=fa?s.proofs.find(p=>p.id===fa.proof_id):null;return `<article class="section switch ${w.state}">
<div class="eyebrow">SWITCH #${w.id} / ${jobLabel(s,w.job_id)}</div>
<h3>${esc(old?.version)} → ${esc(np?.version)} <span class="status ${swClass[w.state]}">${swLabels[w.state]}</span></h3>
<p class="muted">累计印量分界：${w.split_quantity} 张（旧版额度止于该分界，不转给新版）<br>
首张：${proof?esc(proof.code)+' · '+esc(proof.classification_state)+' · '+esc(proof.defect||''):'尚未送验'}${w.signed_by?`<br>主管签印：${esc(w.signed_by)}`:''}${w.note?`<br>回退依据：${esc(w.note)}`:''}</p>
${w.state==='switching'?`<form class="first-article" data-switch="${w.id}">
<input type="hidden" name="job_id" value="${w.job_id}">
<label>新版首张色标编号</label><input name="code" placeholder="PS-V2-FIRST" required ${fa?'disabled':''}>
<label>截至本次累计已印 / 张</label><input name="quantity" type="number" min="1" value="${w.split_quantity+100}" required ${fa?'disabled':''}>
${cmyk()}
<div class="actions"><button class="primary" ${readonly||fa?'disabled':''}>送验首张（沿用训练判别）</button></div>
</form>
<div class="actions">
<button class="magenta" data-sign="${w.id}" ${s.user.role!=='admin'||!fa||proof?.classification_state!=='classified'||!proof?.pass_flag?'disabled':''}>主管签印合格首张并恢复放行</button>
<button data-fail="${w.id}" ${readonly||(proof&&proof.classification_state==='classifying')?'disabled':''}>切换失败·保留旧版</button>
</div>
${fa&&proof&&proof.classification_state==='classified'&&!proof.pass_flag?'<p class="note">新版首张缺陷留在调机：不能签印，请登记失败回退旧版，旧版合格张不作新版依据。</p>':''}
${fa&&proof&&proof.classification_state==='classifying'?'<p class="note">判读迟回期间不放行，请等待结果。</p>':''}`:`<p class="note">${w.state==='succeeded'?'新版已转在用、旧版转备版，历史分界与首张证据保留。':'已回退旧版继续生产，新版缺陷留调机后可重新发起换版。'}</p>`}
</article>`}).join(''):'<p class="note">暂无换版单。备版认可后可由机长发起换版。</p>'}
</section></div>`;}

export function bind(s,reload){
 const save=async(path,body,msg,method='POST')=>{try{await send(path,body,method);await reload();notify(msg);}catch(e){notify(e.message);}};
 const numeric=d=>{for(const k of ['job_id','new_plate_id','quantity','density_c','density_m','density_y','density_k','dot_gain'])if(k in d)d[k]=Number(d[k]);return d;};
 document.querySelector('#plate-form').onsubmit=e=>{e.preventDefault();let d=formData(e.target);d.job_id=Number(d.job_id);save('/plates',d,'备用印版已登记，不影响在用版生产');};
 document.querySelectorAll('.plate-measure').forEach(f=>f.onsubmit=e=>{e.preventDefault();save('/plates/'+f.dataset.key+'/measure',{registration_error_mm:Number(formData(f).registration_error_mm)},'套准实测已记录');});
 document.querySelectorAll('[data-approve]').forEach(b=>b.onclick=()=>save('/plates/'+b.dataset.approve+'/approve',{},'主管已认可备版就绪'));
 document.querySelectorAll('[data-activate]').forEach(b=>b.onclick=()=>save('/plates/'+b.dataset.activate+'/activate',{},'在用版已切换激活'));
 document.querySelectorAll('.switch-start').forEach(f=>f.onsubmit=e=>{e.preventDefault();save('/plate-switches',numeric(formData(f)),'换版已发起，暂停放行并记录印量分界');});
 document.querySelectorAll('.first-article').forEach(f=>f.onsubmit=e=>{e.preventDefault();save('/plate-switches/'+f.dataset.switch+'/first-article',numeric(formData(f)),'新版首张已入队判读');});
 document.querySelectorAll('[data-sign]').forEach(b=>b.onclick=()=>save('/plate-switches/'+b.dataset.sign+'/sign',{},'首张合格已签印，新版转在用并恢复放行'));
 document.querySelectorAll('[data-fail]').forEach(b=>b.onclick=()=>{const note=prompt('请填写切换失败、保留旧版的处置依据（至少5字）');if(note&&note.trim().length>=5)save('/plate-switches/'+b.dataset.fail+'/fail',{note:note.trim()},'已回退，旧在用版保留并恢复生产');else if(note!==null)notify('处置依据至少5字');});
}
