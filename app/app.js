function duration(s){s=Math.max(0,Math.round(s));return s<60?s+'秒':Math.floor(s/60)+'分'+s%60+'秒'}
const token='__TOKEN__', $=id=>document.getElementById(id);
let nodes=[],running=false,lastRows='',fileKey='',configKey='',resultRows=[];
const escape=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function get(url){const r=await fetch(url);const x=await r.json();if(!r.ok)throw Error(x.error||'请求失败');return x}
async function post(url,obj={}){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Local-Token':token},body:JSON.stringify(obj)});const x=await r.json();if(!r.ok)throw Error(x.error||'请求失败');return x}
function selected(){return [...document.querySelectorAll('.node-check:checked')].map(c=>c.value)}
function estimate(){
 const f=$('filter').value.trim().toLowerCase(), chosen=new Set(selected());
 const pool=nodes.filter(n=>chosen.has(n.name)&&(!f||n.name.toLowerCase().includes(f)));
 const count=$('dedupe').checked?new Set(pool.map(n=>n.group_key)).size:pool.length;
 const rounds=$('mode').value==='both'?2:1,mb=Number($('volume').value),up=$('speed').checked?Number($('upload').value):0;
 $('volume').disabled=running||!$('speed').checked;$('upload').disabled=running||!$('speed').checked;
 const jobs=count*rounds,c=Number($('concurrency').value||16);
 const fast=20+Math.ceil(jobs/c)*4+Math.ceil(jobs/4)*15+($('speed').checked?jobs*(3+mb*8/100+(up?3+up*8/50:0)):0);
 const bound=20+Math.ceil(jobs/c)*25+Math.ceil(jobs/4)*40+($('speed').checked?jobs*(12+mb/2+(up?12+Math.max(10,up):0)):0);
 $('timeEstimate').textContent=`预计约 ${duration(fast)} ～ ${duration(bound)}；按全部节点通过估算，淘汰节点会缩短时长，报告导出另需少量时间。`;
 const scope=$('dedupe').checked?`筛选后 ${pool.length} 个节点 → ${count} 个分类，每类取首个选中节点。`:`筛选后测 ${count} 个节点。`;
 $('estimate').textContent=scope+($('speed').checked?`预计测速流量 ${count*rounds*(mb+up+2)} MB（${count} × ${rounds} 种链路 × (下载 ${mb} + 上传 ${up} + 连通测试 2) MB 上限），机场倍率另计。`:`1MB预测速上限 ${count*rounds*2} MB（含一次失败补测）；不执行大文件下载测速。`);
}
async function loadNodes(){
 if(!$('profile').value){nodes=[];$('nodes').innerHTML='';$('nodeCount').textContent='多选要测的节点（请先输入订阅链接）';estimate();return}
 $('nodeCount').textContent='正在读取节点…';
 const x=await get('/api/nodes?profile='+encodeURIComponent($('profile').value));nodes=x.nodes;
 $('nodes').innerHTML=(x.groups||[]).map(g=>`<details class="node-group"><summary>${escape(g.group_label)} <span class="muted">（${g.members.length} 个）</span></summary><div class="group-members">${g.members.map(name=>`<label><input class="node-check" type="checkbox" checked value="${escape(name)}"> ${escape(name)}</label>`).join('')}</div></details>`).join('');
 $('nodeCount').textContent=`多选要测的节点（${nodes.length} 个 / ${(x.groups||[]).length} 类）`;
 $('providers').textContent=x.has_providers?'此订阅含 provider，其节点加载后也按相同规则分类。若要测全部 provider，请清空节点选择；预计流量不包含尚未加载的 provider 节点。':'';
 document.querySelectorAll('.node-check').forEach(c=>c.onchange=estimate);estimate();
}
function tag(x){const status=x?.status||'未测';const cls=/限制|不可用|失败/.test(status)?'bad':/风控|待确认|超时|未测|仅/.test(status)?'warn':/可访问|支持|解锁|可用/.test(status)?'good':'';return `<span class="tag ${cls}" title="${escape(x?.detail||'')}">${escape(status)}</span>`}
function category(r){const parts=(r.group_label||'').split(' / '),n=nodes.find(n=>n.name===r.node);return {country:parts[1]||n?.country||r.exit_country||'ZZ',multiplier:Number((parts[2]||n?.multiplier||'1').replace('x',''))||1}}
function ordered(rows){return [...rows].sort((a,b)=>{const x=category(a),y=category(b);return x.country.localeCompare(y.country)||x.multiplier-y.multiplier||(a.chain||'').localeCompare(b.chain||'')||(a.latency_ms??1e9)-(b.latency_ms??1e9)||(a.node||'').localeCompare(b.node||'')})}
function trafficSummary(rows){let raw=0,charged=0;for(const r of rows){const mb=((r.traffic_download_bytes??Math.round(((r.download_mb||0)+(r.small_download_mb||0))*1e6))+(r.traffic_upload_bytes||0)+(r.previous_test_traffic_bytes||0))/1e6;raw+=mb;charged+=mb*category(r).multiplier}return `测速流量合计 ${raw.toFixed(2)} MB（下载+上传）；按倍率预计扣费 ${charged.toFixed(2)} MB（${(charged/1000).toFixed(4)} GB）。包含报告中已记录的补测与重测；不含TLS等协议开销及平台检测的小请求。旧记录只能按已保存下载量估算。`}
function networkQuality(r){const s=r.speed_mbps,d=r.latency_ms,l=r.loss_pct,j=r.jitter_ms;
 if(s==null)return d!=null?['待确认','未完成下载测速；可单独重测']:['暂不可用','本轮连接探测失败，可重测确认'];
 if(s<10||(l!=null&&l>=66))return ['较差','加载慢或连接失败较多，不作首选'];
 if(d==null)return ['待确认','有下载速度，缺少响应时间数据'];
 if(d>600)return ['响应偏慢','下载可用；网页与交互响应偏慢'];
 if(s<20)return ['备用','速度不足20 Mbps，轻量使用或备用'];
 if((l||0)>0||(j||0)>150)return ['有波动','速度达标，响应有波动；建议重测确认'];
 if(s>=50&&d<=250&&j!=null&&j<=60)return ['优秀','适合网页、AI交互及大文件下载'];
 if(d<=400)return ['良好','适合网页和AI交互；另看平台检测'];
 return ['可用','适合下载与视频，交互响应一般'];
}
function speedLevel(r){return !Number.isFinite(r.speed_mbps)?'未测':r.speed_mbps<10?'低速':r.speed_mbps<20?'备用':'达标'}
function recommendations(rows){return [...new Set(rows.map(r=>r.chain))].sort().map(chain=>{
 const valid=rows.filter(r=>r.chain===chain&&Number.isFinite(r.speed_mbps)&&r.speed_mbps>=20&&!r.error&&(r.loss_pct==null||r.loss_pct<=34));
 const top=[...valid].sort((a,b)=>b.speed_mbps-a.speed_mbps||(a.latency_ms??1e9)-(b.latency_ms??1e9)).slice(0,5);
 const countries=[...new Set(valid.map(r=>category(r).country))].filter(c=>!['ZZ','未知',''].includes(c)).sort();
 const choices=countries.map(country=>valid.filter(r=>category(r).country===country).sort((a,b)=>category(a).multiplier-category(b).multiplier||(a.loss_pct??100)-(b.loss_pct??100)||(a.latency_ms??1e9)-(b.latency_ms??1e9)||b.speed_mbps-a.speed_mbps)[0]);
 const item=r=>`${escape(r.node)} <strong>↓${r.speed_mbps}${r.upload_mbps!=null?' ↑'+r.upload_mbps:''} Mbps</strong> · ${category(r).multiplier}x · ${r.latency_ms??'—'} ms ${r.download_complete===false?'（限时采样）':''}`;
 return `<h3>${escape(chain)} · 达标速度前5</h3>`+(top.length?`<ol>${top.map(r=>`<li>${item(r)}</li>`).join('')}</ol>`:'<p class="muted">暂无达到20 Mbps的有效节点推荐。</p>')+(choices.length?`<h3>各国低倍率候选</h3><ul>${choices.map(r=>`<li>${escape(category(r).country)}：${item(r)}</li>`).join('')}</ul>`:'');
 }).join('')}
function draw(rows){
 rows=ordered(rows);resultRows=rows;$('trafficSummary').textContent=trafficSummary(rows);$('recommendations').innerHTML=recommendations(rows);
 $('rows').innerHTML=rows.length?rows.map((r,i)=>`<tr data-row="${i}"><td title="${escape(r.node)}">${escape(r.node)}<small>${escape(r.chain)} · ${escape(r.stage||r.connectivity||'')}</small>${r.group_label?`<small title="${escape((r.group_members||[]).join('\n'))}">${escape(r.group_label)} · 同组 ${r.group_size||1} 个，仅此节点实测</small>`:''}<div class="retest-tools"><select class="retest-volume" aria-label="${escape(r.node)} 重测下载量" ${running?'disabled':''}>${[20,50,100].map(v=>`<option value="${v}" ${(r.speed_requested_mb||20)===v?'selected':''}>${v} MB</option>`).join('')}</select><button class="retest-button" data-kind="speed" ${running?'disabled':''}>重测速</button><button class="retest-button" data-kind="full" ${running?'disabled':''}>完整重测</button></div><small>${r.measured_at?escape(r.measured_at.slice(11,19))+' 测量':''}</small></td><td>${r.latency_ms??'—'}<small>失败 ${r.loss_pct??'—'}%</small></td><td>${r.speed_mbps??'未测'}<small>${speedLevel(r)}</small>${r.download_complete===false&&r.speed_mbps!=null?'（限时）':''}<small>${r.download_mb?'下载 '+r.download_mb+' MB':''}${r.download_streams?' · '+r.download_streams+'线程':''}${r.speed_source?' · '+escape(r.speed_source):''}</small></td><td>${r.upload_mbps??'未测'}${r.upload_mbps!=null&&r.upload_complete===false?'（限时）':''}<small>${r.upload_mb?'上传 '+r.upload_mb+' MB':''}${r.upload_source?' · '+escape(r.upload_source):''}</small>${r.upload_error?`<small title="${escape(r.upload_error)}">上传失败</small>`:''}</td><td><strong>${escape(networkQuality(r)[0])}</strong><small style="white-space:normal;max-width:240px">${escape(networkQuality(r)[1])}</small>${r.download_complete===false&&r.speed_mbps!=null?'<small>限时采样</small>':''}</td>${['ChatGPT','Claude','Gemini','Netflix','Disney+','YouTube Premium'].map(k=>`<td>${tag(r.checks?.[k])}</td>`).join('')}</tr>`).join(''):'<tr><td colspan="11" class="muted">等待节点结果。</td></tr>';
}
async function poll(){
 try{
  const s=await get('/api/state');
  if(s.run_key&&s.run_key!==configKey&&s.options){
   configKey=s.run_key;const o=s.options;
   if(o.profile&&$('profile').value!==o.profile){$('profile').value=o.profile;await loadNodes()}
   $('endpointLookup').checked=o.endpoint_lookup===true;$('mode').value=o.mode;$('speed').checked=!!o.speed_mb;$('volume').value=String(o.speed_mb||20);if(o.upload_mb!=null)$('upload').value=String(o.upload_mb);if(o.streams)$('streams').value=String(o.streams);$('dedupe').checked=o.dedupe!==false;$('filter').value=o.filter||'';
   document.querySelectorAll('.node-check').forEach(c=>c.checked=!o.nodes?.length||o.nodes.includes(c.value));
  }
  const changed=running!==s.running;running=s.running;
  $('start').disabled=running;$('stop').disabled=!running;
  for(const id of ['profile','mode','filter','speed','dedupe','concurrency','streams','selectAll','selectNone','endpointLookup','subUrl','loadSub'])$(id).disabled=running;
  document.querySelectorAll('.node-check').forEach(c=>c.disabled=running);estimate();
  $('phase').textContent=s.phase;$('progress').textContent=s.total?`${s.completed} / ${s.total}`:'';
  $('elapsed').textContent=s.running&&s.run_started_epoch?`已运行 ${duration(Date.now()/1000-s.run_started_epoch)}；参考上限 ${duration(s.eta_upper_s||0)}（非倒计时）`:'';
  $('liveDownloads').innerHTML=Object.values(s.downloads||{}).filter(d=>d.target_mb>1).map(d=>`<p><strong>${escape(d.kind||'下载')} · ${escape(d.node||'')} · ${escape(d.source||'')}</strong><br>${d.download_mb} / ${d.target_mb} MB（${d.percent}%） · 当前 ${d.current_mbps} Mbps · 平均 ${d.average_mbps} Mbps · ${d.elapsed_s}秒</p><div class="bar"><span style="width:${d.percent}%"></span></div>`).join('');
  $('bar').style.width=(s.total?100*s.completed/s.total:0)+'%';$('error').textContent=s.error||'';
  const key=JSON.stringify(s.rows);if(changed||key!==lastRows){draw(s.rows);lastRows=key}
  const fk=JSON.stringify(s.files);if(fk!==fileKey){fileKey=fk;let reportFiles=s.files;if(s.files.json){try{const parent=s.files.json.slice(0,s.files.json.lastIndexOf('/')+1);const manifest=await get(parent+'报告文件.json');reportFiles=Object.fromEntries(Object.entries(manifest).map(([k,v])=>[k,parent+encodeURIComponent(v)]))}catch(e){}}const names={xlsx:'下载 Excel',png:'下载彩色长图',csv:'下载 CSV',json:'下载原始数据'};
   $('downloads').innerHTML=Object.entries(reportFiles).map(([k,v])=>`<a class="download" href="${escape(v)}" download>${names[k]||escape(k)}</a>`).join('');
   $('imageSection').hidden=!reportFiles.png;if(reportFiles.png)$('preview').src=reportFiles.png;
  }
 }catch(e){$('error').textContent='本地服务连接中断。重新双击启动入口即可。'}
}
$('profile').onchange=()=>loadNodes().catch(e=>$('error').textContent=e.message);
for(const id of ['filter','speed','mode','volume','upload','streams','dedupe','concurrency'])$(id).oninput=estimate;
$('selectAll').onclick=()=>{document.querySelectorAll('.node-check').forEach(c=>c.checked=true);estimate()};
$('selectNone').onclick=()=>{document.querySelectorAll('.node-check').forEach(c=>c.checked=false);estimate()};
$('start').onclick=async()=>{try{
 const chosen=selected();if(!chosen.length&&nodes.length&&!$('providers').textContent){$('error').textContent='请至少选择一个节点。';return}
 $('start').disabled=true;
 await post('/api/start',{profile:$('profile').value,mode:$('mode').value,filter:$('filter').value,nodes:chosen,speed_mb:$('speed').checked?Number($('volume').value):0,upload_mb:$('speed').checked?Number($('upload').value):0,streams:Number($('streams').value),dedupe:$('dedupe').checked,concurrency:Number($('concurrency').value),endpoint_lookup:$('endpointLookup').checked});await poll();
 }catch(e){$('error').textContent=e.message;$('start').disabled=false}};
$('rows').onclick=async e=>{if(!e.target.classList.contains('retest-button'))return;const tr=e.target.closest('tr'),r=resultRows[Number(tr.dataset.row)];try{
 e.target.disabled=true;await post('/api/retest',{node:r.node,chain:r.chain,kind:e.target.dataset.kind,endpoint_lookup:$('endpointLookup').checked,speed_mb:Number(tr.querySelector('.retest-volume').value),upload_mb:Number($('upload').value)});await poll();
 }catch(err){$('error').textContent=err.message;e.target.disabled=false}};
$('stop').onclick=()=>post('/api/stop').catch(e=>$('error').textContent=e.message);
async function loadProfiles(select){const x=await get('/api/profiles');$('profile').innerHTML=x.profiles.map(p=>`<option value="${escape(p.id)}" ${(select?p.id===select:p.current)?'selected':''}>${escape(p.name)}${p.current?'（当前）':''}</option>`).join('');
 if(!x.clash_verge)$('subNote').textContent='没有检测到 Clash Verge 的订阅，请在上方粘贴订阅链接。'+(x.core?'':'');return x}
$('loadSub').onclick=async()=>{const url=$('subUrl').value.trim();if(!url){$('subNote').textContent='请先粘贴订阅链接。';return}
 $('loadSub').disabled=true;$('subNote').textContent='正在下载订阅（先直连，失败再经本机 Clash 端口）…';
 try{const r=await post('/api/subscription',{url});await loadProfiles(r.id);await loadNodes();$('subNote').textContent=`已加载：${r.name}，共 ${nodes.length} 个节点。`;$('subUrl').value=''}
 catch(e){$('subNote').textContent=e.message}finally{$('loadSub').disabled=running}};
(async()=>{try{await loadProfiles();await loadNodes();await poll();setInterval(poll,1500)}catch(e){$('error').textContent=e.message}})();




