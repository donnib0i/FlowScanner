// PIN never injected into HTML — stored in localStorage only
let PIN = localStorage.getItem('scanner_pin') || '';
const _pa = p => PIN ? (p.includes('?') ? p+'&pin='+encodeURIComponent(PIN) : p+'?pin='+encodeURIComponent(PIN)) : p;

function _promptPin(msg){
  const p = prompt(msg || 'Enter access PIN:');
  if(p !== null){
    PIN = p.trim();
    localStorage.setItem('scanner_pin', PIN);
  }
}

// On 401, prompt for PIN and reload
function _handleAuth(resp){
  if(resp.status === 401){
    localStorage.removeItem('scanner_pin');
    PIN = '';
    _promptPin('PIN required. Enter access PIN:');
    return true;
  }
  return false;
}

function _isMarketOpen(){
  try{
    const et=new Date(new Date().toLocaleString('en-US',{timeZone:'America/New_York'}));
    const d=et.getDay();
    if(d===0||d===6) return false;
    const m=et.getHours()*60+et.getMinutes();
    return m>=570&&m<=960;
  }catch{return true}
}

const S={
  dte:_isMarketOpen()?'0dte':'all',
  whale:false,full:false,
  dir:'up',dteMode:'0dte',
  scanning:false,scanRunning:false,
  callFlow:0,putFlow:0,
  signals:[],hotContracts:[],
  view:'signals',
  qt:[],ft:[],
  scanData:[],scanFilter:'any',scanSort:'setup',
  flowSort:'premium',flowDir:-1,
};
const FLOW_SORT_KEYS={
  premium:function(s){return s.total||0},
  score:function(s){return s.score||0},
  vol:function(s){return s.vol||0},
  oi:function(s){return s.oi||0},
  voloi:function(s){return s.vol_oi||0},
  dte:function(s){return s.dte<0?9999:s.dte},
  pc:function(s){return s.pc_ratio||0},
};

(function(){
  const lbl={'0dte':'0DTE','7dte':'7 DTE','all':'ALL DTE'};
  const el=document.getElementById('c-dte');
  el.textContent=lbl[S.dte]||'0DTE';
  el.className='chip'+(S.dte!=='all'?' on':'');
})();

function _loadVix(attempt){
  attempt=attempt||0;
  fetch(_pa('/api/vix')).then(r=>{if(_handleAuth(r))return Promise.reject('auth');return r.ok?r.json():Promise.reject()}).then(d=>{
    renderVix(d);
    if(d.vix<=0&&attempt<6) setTimeout(()=>_loadVix(attempt+1),15000);
    else setTimeout(()=>_loadVix(0),90000);
  }).catch(()=>{
    if(attempt<8) setTimeout(()=>_loadVix(attempt+1),attempt<2?5000:10000);
  });
}
function _loadUniverse(attempt){
  attempt=attempt||0;
  fetch(_pa('/api/universe')).then(r=>r.ok?r.json():Promise.reject()).then(d=>{
    S.qt=d.quick;S.ft=d.full;
    if(S.full) document.getElementById('c-scope').textContent='FULL ('+S.ft.length+')';
  }).catch(()=>{
    if(attempt<8) setTimeout(()=>_loadUniverse(attempt+1),attempt<2?5000:10000);
  });
}
_loadVix(0);
_loadUniverse(0);
function _refreshSourceBadge(){
 fetch(_pa('/api/status')).then(r=>r.ok?r.json():null).then(d=>{
  if(!d) return;
  const badge=document.getElementById('source-badge');
  if(d.live){
    badge.textContent='● LIVE — TastyTrade OPRA feed';
    badge.style.color='#00ff88';
  } else if(d.flow_source==='unknown'){
    badge.textContent='○ source unknown — no flow scan yet';
    badge.style.color='#555';
  } else {
    badge.textContent='○ DELAYED — yfinance 15min';
    badge.style.color='#555';
    if(d.flow_source_reason) badge.title=d.flow_source_reason;
  }
 }).catch(()=>{});
}
_refreshSourceBadge();

function renderVix(d){
  const el=document.getElementById('vix-chip');
  const closed=!_isMarketOpen();
  if(d.vix<=0){
    el.textContent=closed?'CLOSED':'VIX -';
    el.className=closed?'vix-pill elevated':'vix-pill';
    return;
  }
  el.textContent=closed?'VIX '+d.vix.toFixed(1)+' CLOSED':'VIX '+d.vix.toFixed(1)+' '+d.regime.toUpperCase();
  el.className='vix-pill '+(d.vix>=30?'fear':d.vix>=24?'elevated':d.vix<16?'calm':'');
}

function showTab(n,btn){
  document.querySelectorAll('.tab-pane').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+n).classList.add('active');
  btn.classList.add('active');
  if(n!=='flow') document.getElementById('flow-bar').classList.remove('on');
}

const dteOpts=['0dte','7dte','all'];
const dteLbls={'0dte':'0DTE','7dte':'7 DTE','all':'ALL DTE'};
function tDte(){
  S.dte=dteOpts[(dteOpts.indexOf(S.dte)+1)%3];
  const el=document.getElementById('c-dte');
  el.textContent=dteLbls[S.dte];
  el.className='chip'+(S.dte!=='all'?' on':'');
}
function tWhale(){
  S.whale=!S.whale;
  document.getElementById('c-whale').className='chip'+(S.whale?' on':'');
}
function tScope(){
  S.full=!S.full;
  const el=document.getElementById('c-scope');
  el.textContent=S.full?'FULL ('+(S.ft.length||'?')+')':'QUICK';
  el.className='chip'+(S.full?' on':'');
}

function doFlowScan(retryCount){
  if(S.scanning&&!retryCount) return;
  retryCount=retryCount||0;
  if(!retryCount){
    S.scanning=true;S.callFlow=0;S.putFlow=0;S.signals=[];S.hotContracts=[];
    setView('signals');
    document.getElementById('view-toggle').style.display='none';
    document.getElementById('flow-sort-bar').style.display='none';
    document.getElementById('flow-feed').textContent='';
    document.getElementById('hot-feed').textContent='';
    document.getElementById('flow-bar').classList.remove('on');
    document.getElementById('bc').textContent='CALLS -';
    document.getElementById('bp').textContent='PUTS -';
    const bd=document.getElementById('bd');bd.textContent='-';bd.className='bias-dir neut';
    document.getElementById('pw').style.display='block';
    document.getElementById('pl').style.display='block';
  }
  // Single stocks only — no ETFs. SPX is a cash index, not an ETF.
  const tickers=(S.full?S.ft:S.qt).join(',')
    ||'SPX,NVDA,AMD,AAPL,MSFT,META,AMZN,TSLA';
  const n=tickers.split(',').length;
  const btn=document.getElementById('scan-btn');
  btn.textContent='SCANNING '+n+'...';btn.className='scan-btn loading';
  const minScore=S.whale?60:40;
  const url=_pa('/api/flow?tickers='+tickers+'&dte='+S.dte+'&min_score='+minScore);
  const es=new EventSource(url);
  let gotData=false;
  es.onmessage=function(e){
    gotData=true;
    const m=JSON.parse(e.data);
    if(m.__ping__) return;
    if(m.__progress__){
      document.getElementById('pb').style.width=(m.i/m.n*100)+'%';
      document.getElementById('pl').textContent=m.ticker+' . '+m.i+' of '+m.n;
      return;
    }
    if(m.__done__||m.__error__){es.close();endFlowScan(m.__error__);_refreshSourceBadge();return}
    if(m.__signal__){
      const s=m.data;
      S.signals.push(s);
      S.callFlow+=s.call_flow||0;S.putFlow+=s.put_flow||0;
      (s.top_calls||[]).concat(s.top_puts||[]).forEach(function(c){S.hotContracts.push(Object.assign({},c,{ticker:s.ticker,badge:s.badge,cls:s.cls}))});
      renderFlowCard(s);
      updateFlowBias();
      document.getElementById('flow-sort-bar').style.display='flex';
    }
  };
  es.onerror=function(){
    es.close();
    if(!gotData&&retryCount<2){
      const wait=retryCount===0?4:8;
      document.getElementById('pl').textContent='Waking up... retry '+(retryCount+1)+'/2';
      setTimeout(function(){doFlowScan(retryCount+1)},wait*1000);
    } else {
      endFlowScan(gotData?null:'Server unavailable - try again');
    }
  };
}
function endFlowScan(err){
  S.scanning=false;
  const btn=document.getElementById('scan-btn');
  btn.textContent=S.full?'FULL':'SCAN';btn.className='scan-btn';
  document.getElementById('pw').style.display='none';
  document.getElementById('pl').style.display='none';
  document.getElementById('pb').style.width='0%';
  if(err){toast('Error: '+err,'err');return}
  if(!S.signals.length){
    const hint=!_isMarketOpen()&&S.dte==='0dte'
      ?'Market closed - 0DTE expired. Switch to ALL DTE.'
      :S.dte!=='all'?'Try ALL DTE or FULL scan.':'No unusual institutional flow detected.';
    const feed=document.getElementById('flow-feed');
    feed.textContent='';
    const wrap=document.createElement('div');wrap.className='empty-st';
    const icon=document.createElement('div');icon.className='icon';icon.textContent='?';
    const h=document.createElement('h3');h.textContent='No signals';
    const p=document.createElement('p');p.textContent=hint;
    wrap.appendChild(icon);wrap.appendChild(h);wrap.appendChild(p);
    feed.appendChild(wrap);
  } else {
    toast(S.signals.length+' signal'+(S.signals.length>1?'s':'')+' - institutional only');
    if(S.hotContracts.length) document.getElementById('view-toggle').style.display='flex';
    sortFlowFeed();   // apply the selected sort so order matches the dropdown
  }
}
function updateFlowBias(){
  const cf=S.callFlow,pf=S.putFlow;
  document.getElementById('bc').textContent='CALLS '+fmt(cf);
  document.getElementById('bp').textContent='PUTS '+fmt(pf);
  const bd=document.getElementById('bd');
  if(cf>pf*1.2){bd.textContent='BULL';bd.className='bias-dir bull'}
  else if(pf>cf*1.2){bd.textContent='BEAR';bd.className='bias-dir bear'}
  else{bd.textContent='EVEN';bd.className='bias-dir neut'}
  const total=cf+pf;
  if(total>0){
    document.getElementById('flow-fill').style.width=Math.round(cf/total*100)+'%';
    document.getElementById('flow-bar').classList.add('on');
  }
}

function fmt(v){
  if(v>=1e6) return '$'+(v/1e6).toFixed(1)+'M';
  if(v>=1e3) return '$'+(v/1e3).toFixed(0)+'K';
  return '$'+v.toFixed(0);
}
function badgeCls(b){
  var m={'GOLDEN SWEEP':'golden','WHALE':'whale','STACKED':'stacked','SWEEP':'sweep','BLOCK':'block'};
  return m[b]||'flow';
}
function voiCls(v){return v>=10?'hot':v>=3?'warm':'cool'}
function fmtVol(v){v=v||0;return v>=1000?(v/1000).toFixed(v>=10000?0:1)+'k':String(v)}
function scoreCls(s){return s>=70?'whale':s>=50?'inst':'retail'}

function renderFlowCard(s){
  const feed=document.getElementById('flow-feed');
  const card=document.createElement('div');
  card.className='flow-card '+(s.cls||'');

  // head
  const head=document.createElement('div');
  head.className='card-head';
  head.onclick=function(){toggleDetail(head)};

  const badgeEl=document.createElement('span');
  badgeEl.className='badge '+badgeCls(s.badge);
  badgeEl.textContent=s.badge;

  const titleDiv=document.createElement('div');
  titleDiv.className='card-title';
  const tickEl=document.createElement('div');
  tickEl.className='card-ticker';
  tickEl.textContent=s.ticker+(s.hits>1?' x'+s.hits:'');
  const subEl=document.createElement('div');
  subEl.className='card-sub';
  subEl.style.color=s.bias==='call'?'var(--green)':'var(--red)';
  const dtePart=s.dte===0?' 0DTE':s.dte>=0?' '+s.dte+'DTE':'';
  subEl.textContent=s.ts+' . '+s.bias.toUpperCase()+' FLOW'+dtePart;
  titleDiv.appendChild(tickEl);titleDiv.appendChild(subEl);

  const premDiv=document.createElement('div');
  premDiv.className='card-premium';
  const amtEl=document.createElement('div');
  amtEl.className='card-amount '+s.bias;
  amtEl.textContent=s.total_fmt;
  const lblEl=document.createElement('div');
  lblEl.className='card-alabel';
  lblEl.style.color=s.bias==='call'?'var(--green)':'var(--red)';
  lblEl.textContent=s.bias==='call'?'CALLS':'PUTS';
  premDiv.appendChild(amtEl);premDiv.appendChild(lblEl);

  head.appendChild(badgeEl);head.appendChild(titleDiv);head.appendChild(premDiv);

  // score bar
  const sbWrap=document.createElement('div');sbWrap.className='score-bar-wrap';
  const sbTrack=document.createElement('div');sbTrack.className='score-bar-track';
  const sbFill=document.createElement('div');
  sbFill.className='score-bar-fill '+scoreCls(s.score);
  sbFill.style.width=s.score+'%';
  sbTrack.appendChild(sbFill);
  const sbNum=document.createElement('span');
  sbNum.className='score-num '+scoreCls(s.score);
  sbNum.textContent=s.score;
  sbWrap.appendChild(sbTrack);sbWrap.appendChild(sbNum);

  // stats row
  const statsDiv=document.createElement('div');statsDiv.className='card-stats';
  const stats=[
    ['Vol/OI', s.vol_oi>0?'x'+s.vol_oi.toFixed(1):'-', voiCls(s.vol_oi)],
    ['Side', s.side==='ask'?'AT ASK':s.side==='bid'?'AT BID':'MIXED', s.side],
    ['P/C', s.pc_ratio.toFixed(2), ''],
    ['Tier', (s.tier||'-').toUpperCase(), ''],
  ];
  stats.forEach(function(st){
    const cs=document.createElement('div');cs.className='cstat';
    const lbl=document.createElement('label');lbl.textContent=st[0];
    const val=document.createElement('div');val.className='v '+(st[2]||'');val.textContent=st[1];
    cs.appendChild(lbl);cs.appendChild(val);statsDiv.appendChild(cs);
  });

  // contracts — both sides, each chip carrying what it costs and what it needs
  const contractsWrap=document.createElement('div');
  contractsWrap.className='contracts-wrap';

  function buildChip(c,i){
    const cc=document.createElement('div');
    cc.className='cc '+(i===0?(c.golden?'golden-c':'top1'):'');

    const top=document.createElement('div');top.className='cc-top';
    const strike=document.createElement('div');
    strike.className='cc-strike '+c.type;
    strike.textContent='$'+c.strike.toFixed(0)+(c.type==='call'?'C':'P');
    top.appendChild(strike);
    if(c.golden||c.sweep){
      const mk=document.createElement('span');
      mk.className='cc-mk '+(c.golden?'gold':'swp');
      mk.textContent=c.golden?'G':'S';
      mk.title=c.golden?'Golden sweep':'Sweep';
      top.appendChild(mk);
    }
    cc.appendChild(top);

    const meta=document.createElement('div');meta.className='cc-meta';
    const dLbl=c.dte===0?'0DTE':c.dte>=0?c.dte+'DTE':'-';
    meta.textContent=dLbl+' . '+c.exp;
    cc.appendChild(meta);

    const price=document.createElement('div');price.className='cc-price';
    price.textContent=c.mid>0?'$'+c.mid.toFixed(2):'-';
    if(c.spread_pct!==null&&c.spread_pct!==undefined){
      const sp=document.createElement('span');
      sp.className='cc-spread'+(c.wide_spread?' wide':'');
      sp.textContent=' '+c.spread_pct.toFixed(0)+'%';
      sp.title='Bid '+c.bid+' / Ask '+c.ask+(c.wide_spread?' — wide, expect slippage':'');
      price.appendChild(sp);
    }
    cc.appendChild(price);

    if(c.breakeven){
      const be=document.createElement('div');be.className='cc-be';
      let mv='';
      if(c.pct_to_breakeven!==null&&c.pct_to_breakeven!==undefined){
        mv=c.pct_to_breakeven<=0
          ? ' (in)'
          : ' ('+(c.type==='call'?'+':'-')+Math.abs(c.pct_to_breakeven).toFixed(1)+'%)';
      }
      be.textContent='BE '+c.breakeven.toFixed(2)+mv;
      if(c.pct_to_breakeven!==null&&c.pct_to_breakeven<=0) be.classList.add('through');
      be.title='Breakeven at expiry; move from spot needed to reach it';
      cc.appendChild(be);
    }

    const voi=document.createElement('div');voi.className='cc-voi '+voiCls(c.vol_oi);
    voi.textContent=(c.vol_oi>0?'x'+c.vol_oi.toFixed(1):'-')+' . '+fmtVol(c.vol);
    voi.title='Volume / open interest';
    cc.appendChild(voi);
    return cc;
  }

  function buildSide(list,label,clr){
    if(!list||!list.length) return;
    const hdr=document.createElement('div');
    hdr.className='cc-side-lbl';hdr.style.color=clr;hdr.textContent=label;
    contractsWrap.appendChild(hdr);
    const row=document.createElement('div');row.className='contracts-row';
    list.forEach(function(c,i){row.appendChild(buildChip(c,i))});
    contractsWrap.appendChild(row);
  }
  buildSide(s.top_calls,'CALLS','var(--green)');
  buildSide(s.top_puts,'PUTS','var(--red)');

  if(s.filtered_n>0){
    const fn=document.createElement('div');fn.className='cc-filtered';
    fn.textContent=s.filtered_n+' contract'+(s.filtered_n>1?'s':'')+' hidden . '+s.filtered_fmt;
    if(s.filtered_reasons&&s.filtered_reasons.length) fn.title=s.filtered_reasons.join('; ');
    contractsWrap.appendChild(fn);
  }

  // detail
  const det=document.createElement('div');det.className='card-detail';
  const dg=document.createElement('div');dg.className='dg';
  const dgItems=[
    ['Calls', s.call_fmt, 'var(--green)'],
    ['Puts', s.put_fmt, 'var(--red)'],
    ['IV Skew', s.iv_skew?(s.iv_skew>0?'+':'')+(s.iv_skew*100).toFixed(2)+'%':'-', ''],
    ['Stacked', s.stacked?'YES':'NO', ''],
    ['Golden', s.golden?'YES':'NO', s.golden?'var(--gold)':'var(--sub)'],
    ['Strike', s.strike?'$'+s.strike:'-', ''],
  ];
  dgItems.forEach(function(it){
    const item=document.createElement('div');item.className='dg-item';
    const lbl=document.createElement('label');lbl.textContent=it[0];
    const sp=document.createElement('span');
    if(it[2]) sp.style.color=it[2];
    sp.textContent=it[1];
    item.appendChild(lbl);item.appendChild(sp);dg.appendChild(item);
  });
  const segs=document.createElement('div');segs.className='dte-segs';
  [['0DTE',s.dte0||'$0','var(--cyan)'],['1-7 DTE',s.dte1_7||'$0','var(--amber)'],['8+ DTE',s.dte8p||'$0','var(--sub)']].forEach(function(sg){
    const seg=document.createElement('div');seg.className='dte-seg';
    const lbl=document.createElement('label');lbl.textContent=sg[0];
    const sp=document.createElement('span');sp.style.color=sg[2];sp.textContent=sg[1];
    seg.appendChild(lbl);seg.appendChild(sp);segs.appendChild(seg);
  });
  det.appendChild(dg);det.appendChild(segs);

  card.appendChild(head);card.appendChild(sbWrap);card.appendChild(statsDiv);
  if(contractsWrap.childNodes.length) card.appendChild(contractsWrap);
  card.appendChild(det);
  feed.appendChild(card);
}
function setFlowSort(v){S.flowSort=v;sortFlowFeed()}
function toggleFlowDir(){
  S.flowDir=-S.flowDir;
  document.getElementById('flow-dir').innerHTML=S.flowDir<0?'&#9660;':'&#9650;';
  sortFlowFeed();
}
function sortFlowFeed(){
  if(!S.signals.length) return;
  const key=FLOW_SORT_KEYS[S.flowSort]||FLOW_SORT_KEYS.premium;
  S.signals.sort(function(a,b){return (key(a)-key(b))*S.flowDir});
  const feed=document.getElementById('flow-feed');
  feed.textContent='';
  S.signals.forEach(renderFlowCard);
}
function toggleDetail(head){
  head.closest('.flow-card').querySelector('.card-detail').classList.toggle('open');
}

function setView(v){
  S.view=v;
  document.getElementById('vt-sig').className='vt-btn'+(v==='signals'?' on':'');
  document.getElementById('vt-hot').className='vt-btn'+(v==='hot'?' on':'');
  document.getElementById('flow-feed').style.display=v==='signals'?'':'none';
  document.getElementById('hot-feed').style.display=v==='hot'?'':'none';
  if(v==='hot') renderHot();
}
function renderHot(){
  const feed=document.getElementById('hot-feed');
  feed.textContent='';
  if(!S.hotContracts.length){
    const wrap=document.createElement('div');wrap.className='empty-st';
    const icon=document.createElement('div');icon.className='icon';icon.textContent='!';
    const h=document.createElement('h3');h.textContent='No hot contracts yet';
    const p=document.createElement('p');p.textContent='Run a scan first.';
    wrap.appendChild(icon);wrap.appendChild(h);wrap.appendChild(p);
    feed.appendChild(wrap);return;
  }
  const seen=new Set();
  const deduped=S.hotContracts.filter(function(c){
    const k=c.ticker+'-'+c.strike+'-'+c.type+'-'+c.exp;
    if(seen.has(k))return false;seen.add(k);return true;
  });
  const calls=deduped.filter(function(c){return c.type==='call'})
    .sort(function(a,b){return(b.vol_oi||0)-(a.vol_oi||0)}).slice(0,12);
  const puts=deduped.filter(function(c){return c.type==='put'})
    .sort(function(a,b){return(b.vol_oi||0)-(a.vol_oi||0)}).slice(0,12);
  function buildSec(lbl,clr,list){
    if(!list.length) return;
    const hdr=document.createElement('div');
    hdr.style.cssText='padding:6px 12px 4px;font-size:9px;font-weight:700;letter-spacing:1px;color:'+clr+';text-transform:uppercase';
    hdr.textContent=lbl;feed.appendChild(hdr);
    list.forEach(function(c,i){
      const rc=i===0?'t1':i===1?'t2':i===2?'t3':'';
      const vo=c.vol_oi||0;
      const vc=vo>=10?'fire':vo>=5?'hot':'warm';
      const dLbl=c.dte===0?'0DTE':c.dte>=0?c.dte+'DTE':'-';
      const el=document.createElement('div');el.className='hot-card';
      const rank=document.createElement('div');rank.className='hot-rank '+rc;rank.textContent=i+1;
      const info=document.createElement('div');info.className='hot-info';
      const sym=document.createElement('div');sym.className='hot-sym';
      const sp1=document.createElement('span');sp1.className=c.type;sp1.textContent=c.ticker;
      const sp2=document.createElement('span');
      sp2.style.cssText='color:var(--sub);font-size:12px';
      sp2.textContent=' $'+c.strike.toFixed(0)+' '+(c.type==='call'?'C':'P');
      sym.appendChild(sp1);sym.appendChild(sp2);
      const meta=document.createElement('div');meta.className='hot-meta';
      meta.textContent=dLbl+' . '+c.exp+' . '+(c.mid>0?'$'+c.mid.toFixed(2):'-');
      info.appendChild(sym);info.appendChild(meta);
      const right=document.createElement('div');right.className='hot-right';
      const voiEl=document.createElement('div');voiEl.className='hot-voi '+vc;
      voiEl.textContent='x'+vo.toFixed(1);
      const flowEl=document.createElement('div');flowEl.className='hot-flow';
      flowEl.textContent=(c.flow||'-')+' flow';
      right.appendChild(voiEl);right.appendChild(flowEl);
      el.appendChild(rank);el.appendChild(info);el.appendChild(right);
      feed.appendChild(el);
    });
  }
  buildSec('CALLS','var(--green)',calls);
  buildSec('PUTS','var(--red)',puts);
}

// Full scan
function updateScanFilter(){S.scanFilter=document.getElementById('scan-filter').value}
function updateScanSort(){
  S.scanSort=document.getElementById('scan-sort').value;
  if(S.scanData.length) renderScanTable(S.scanData);
}

async function runFullScan(){
  if(S.scanRunning) return;
  S.scanRunning=true;
  const btn=document.getElementById('scan-run-btn');
  btn.textContent='SCANNING...';btn.style.opacity='.6';
  const wrap=document.getElementById('scan-table-wrap');
  wrap.textContent='';
  const skelWrap=document.createElement('div');skelWrap.style.padding='16px';
  for(let i=0;i<5;i++){
    const s=document.createElement('div');
    s.className='skel';s.style.cssText='height:'+(i===0?'32':'28')+'px;margin-bottom:6px';
    skelWrap.appendChild(s);
  }
  wrap.appendChild(skelWrap);
  const filter=document.getElementById('scan-filter').value;
  const sort=document.getElementById('scan-sort').value;
  const dteMode=document.getElementById('scan-dte').value;
  try{
    const r=await fetch(_pa('/api/scan?filter='+filter+'&sort='+sort+'&dte_mode='+dteMode));
    if(_handleAuth(r))return;
    if(!r.ok){const e=await r.json();throw new Error(e.detail||'Scan failed');}
    const d=await r.json();
    S.scanData=d.results||[];
    document.getElementById('scan-stat').textContent=d.filtered+' / '+d.total+' . '+d.last_updated;
    renderScanTable(S.scanData);
    toast(d.filtered+' setups found . '+d.last_updated);
  }catch(e){
    wrap.textContent='';
    const empt=document.createElement('div');empt.className='empty-st';
    const icon=document.createElement('div');icon.className='icon';icon.textContent='!';
    const h=document.createElement('h3');h.textContent='Scan failed';
    const p=document.createElement('p');p.textContent=e.message;
    empt.appendChild(icon);empt.appendChild(h);empt.appendChild(p);
    wrap.appendChild(empt);
    toast('Scan failed: '+e.message,'err');
  }finally{
    S.scanRunning=false;
    btn.textContent='RUN FULL SCAN (232 tickers)';btn.style.opacity='1';
  }
}

function renderScanTable(data){
  const wrap=document.getElementById('scan-table-wrap');
  wrap.textContent='';
  if(!data||!data.length){
    const empt=document.createElement('div');empt.className='empty-st';
    const icon=document.createElement('div');icon.className='icon';icon.textContent='?';
    const h=document.createElement('h3');h.textContent='No setups matched';
    const p=document.createElement('p');p.textContent='Try a different filter.';
    empt.appendChild(icon);empt.appendChild(h);empt.appendChild(p);
    wrap.appendChild(empt);return;
  }
  const tbl=document.createElement('table');tbl.className='scan-table';
  const thead=document.createElement('thead');
  const hr=document.createElement('tr');
  ['TICKER','PRICE . CHG%','SETUP','OPT SCORE','CONTRACT','REL VOL'].forEach(function(col){
    const th=document.createElement('th');th.textContent=col;hr.appendChild(th);
  });
  thead.appendChild(hr);tbl.appendChild(thead);
  const tbody=document.createElement('tbody');
  data.forEach(function(r){
    const tr=document.createElement('tr');
    // ticker
    const td1=document.createElement('td');
    const tc=document.createElement('div');tc.className='ticker-cell';tc.textContent=r.ticker;
    const sc=document.createElement('div');sc.className='sector-cell';sc.textContent=r.sector;
    td1.appendChild(tc);td1.appendChild(sc);
    // price/chg
    const td2=document.createElement('td');
    const pr=document.createElement('div');pr.textContent='$'+r.price.toFixed(2);
    const ch=document.createElement('span');
    ch.className=r.change_pct>=0?'chg-up':'chg-dn';
    ch.textContent=(r.change_pct>=0?'+':'')+r.change_pct.toFixed(2)+'%';
    td2.appendChild(pr);td2.appendChild(ch);
    // setup
    const td3=document.createElement('td');
    const sb=document.createElement('span');sb.className='setup-badge '+r.grade;
    sb.textContent=r.grade+' . '+(r.setup||r.direction.toUpperCase());
    td3.appendChild(sb);
    // opt score
    const td4=document.createElement('td');
    if(r.contract){
      const bw=document.createElement('span');bw.className='opt-bar-wrap';
      const bf=document.createElement('span');bf.className='opt-bar-fill';
      bf.style.width=Math.min(r.contract.score||0,100)+'%';
      bw.appendChild(bf);td4.appendChild(bw);
    }
    const sn=document.createTextNode(r.contract?r.contract.score||0:0);
    td4.appendChild(sn);
    // contract
    const td5=document.createElement('td');td5.className='contract-cell';
    if(r.contract){
      const c=r.contract;
      const sk=document.createElement('span');
      sk.className='strike '+(r.direction==='up'?'call':'put');
      sk.textContent='$'+c.strike+' '+(r.direction==='up'?'C':'P');
      const ex=document.createElement('span');
      ex.style.cssText='font-size:9px;color:var(--sub)';
      ex.textContent=' '+c.exp+' . '+(c.dte===0?'0DTE':c.dte+'DTE');
      td5.appendChild(sk);td5.appendChild(document.createElement('br'));td5.appendChild(ex);
    } else {
      td5.textContent='-';
    }
    // rel vol
    const td6=document.createElement('td');
    if(r.rel_vol>=2){
      const sp=document.createElement('span');sp.style.color='var(--amber)';
      sp.textContent=r.rel_vol.toFixed(1)+'x';td6.appendChild(sp);
    } else {
      td6.textContent=r.rel_vol.toFixed(1)+'x';
    }
    tr.appendChild(td1);tr.appendChild(td2);tr.appendChild(td3);
    tr.appendChild(td4);tr.appendChild(td5);tr.appendChild(td6);
    tbody.appendChild(tr);
  });
  tbl.appendChild(tbody);wrap.appendChild(tbl);
}

// Sectors
async function loadSectors(){
  const feed=document.getElementById('sectors-feed');
  feed.textContent='';
  const skelWrap=document.createElement('div');skelWrap.style.padding='12px';
  for(let i=0;i<3;i++){
    const s=document.createElement('div');
    s.className='skel';s.style.cssText='height:100px;margin-bottom:8px';
    skelWrap.appendChild(s);
  }
  feed.appendChild(skelWrap);
  try{
    const r=await fetch(_pa('/api/sectors'));
    if(!r.ok){const e=await r.json();throw new Error(e.detail||'Failed');}
    const d=await r.json();
    const sectors=d.sectors||[];
    if(!sectors.length) throw new Error('No sector data');
    const max=Math.max.apply(null,sectors.map(function(s){return Math.abs(s.change)}));
    const maxVal=max||0.01;
    feed.textContent='';
    const hdr=document.createElement('div');hdr.className='sec-head';
    const hdrLeft=document.createTextNode('SECTORS');
    const ts=document.createElement('span');ts.className='ts';
    ts.textContent='UPDATED '+(d.last_updated||'');
    hdr.appendChild(hdrLeft);hdr.appendChild(ts);
    feed.appendChild(hdr);
    if(d.laggard){
      const lg=d.laggard;
      const lb=document.createElement('div');lb.className='laggard-box';
      const lbl=document.createElement('div');lbl.className='laggard-label';lbl.textContent='TOP LAGGARD';
      const lt=document.createElement('div');lt.className='laggard-ticker';
      lt.textContent=lg.ticker+'  ·  '+lg.sector;
      const ld=document.createElement('div');ld.className='laggard-desc';
      const scs=(lg.sector_change>0?'+':'')+lg.sector_change+'%';
      const tcs=(lg.stock_change>0?'+':'')+lg.stock_change+'%';
      ld.textContent=lg.sector+' '+scs+'  ·  '+lg.ticker+' '+tcs+'   (diverges '+lg.divergence+'%)';
      lb.appendChild(lbl);lb.appendChild(lt);lb.appendChild(ld);feed.appendChild(lb);
    }
    const grid=document.createElement('div');grid.className='sector-grid';
    sectors.forEach(function(s){
      const up=s.change>=0;
      const pct=Math.abs(s.change/maxVal*100).toFixed(0);
      const biasClr=s.bias==='bull'?'var(--green)':s.bias==='bear'?'var(--red)':'var(--sub)';
      const card=document.createElement('div');
      card.className='sector-card '+(up?'up':'dn');
      card.dataset.sector=s.name;
      const changeStr=(up?'+':'')+s.change.toFixed(2)+'%';
      card.title='Tap to see '+s.name+' stocks';
      card.onclick=function(){toggleHeatmap(card,s.name,grid)};
      const nm=document.createElement('div');nm.className='sc-name';
      const bk=s.breakout==='up'?' 🚀':s.breakout==='down'?' 🔻':'';
      nm.textContent=s.name+bk;
      const chg=document.createElement('div');chg.className='sc-chg '+(up?'up':'dn');
      chg.textContent=changeStr;
      const track=document.createElement('div');track.className='sc-bar-track';
      const fill=document.createElement('div');
      fill.className='sc-bar-fill '+(up?'up':'dn');fill.style.width=pct+'%';
      track.appendChild(fill);
      const meta=document.createElement('div');meta.className='sc-meta';
      const biasEl=document.createElement('span');biasEl.style.color=biasClr;
      biasEl.textContent=(s.bias||'NEUT').toUpperCase();
      const volEl=document.createElement('span');
      volEl.textContent=s.rel_vol.toFixed(1)+'x vol';
      meta.appendChild(biasEl);meta.appendChild(volEl);
      card.appendChild(nm);card.appendChild(chg);
      card.appendChild(track);card.appendChild(meta);
      grid.appendChild(card);
    });
    feed.appendChild(grid);
  }catch(e){
    feed.textContent='';
    const empt=document.createElement('div');empt.className='empty-st';
    const icon=document.createElement('div');icon.className='icon';icon.textContent='!';
    const h=document.createElement('h3');h.textContent='Failed to load sectors';
    const p=document.createElement('p');p.textContent=e.message;
    empt.appendChild(icon);empt.appendChild(h);empt.appendChild(p);
    feed.appendChild(empt);
    const btn=document.createElement('button');btn.className='load-btn';
    btn.textContent='TRY AGAIN';btn.onclick=loadSectors;feed.appendChild(btn);
    toast('Sectors failed: '+e.message,'err');
  }
}

// ── Sector heatmap (tap a sector card) ──────────────────────────────────────
function toggleHeatmap(card,sector,grid){
  const existing=grid.querySelector('.heat-panel');
  const wasMine=existing && existing.dataset.sector===sector;
  if(existing) existing.remove();
  grid.querySelectorAll('.sector-card.open').forEach(function(c){c.classList.remove('open')});
  if(wasMine) return;                          // tapping the open one closes it
  card.classList.add('open');
  const panel=document.createElement('div');
  panel.className='heat-panel';panel.dataset.sector=sector;
  const head=document.createElement('div');head.className='heat-head';
  const title=document.createElement('div');title.className='heat-title';title.textContent=sector;
  const sub=document.createElement('span');sub.className='heat-sub';sub.textContent='loading…';
  title.appendChild(sub);
  const close=document.createElement('div');close.className='heat-close';close.textContent='✕';
  close.onclick=function(ev){ev.stopPropagation();panel.remove();card.classList.remove('open')};
  head.appendChild(title);head.appendChild(close);panel.appendChild(head);
  const map=document.createElement('div');map.className='heat-map';
  const sk=document.createElement('div');sk.className='skel';sk.style.cssText='height:200px;width:100%';
  map.appendChild(sk);panel.appendChild(map);
  const plays=document.createElement('div');plays.className='plays-panel';
  panel.appendChild(plays);
  card.insertAdjacentElement('afterend',panel);
  loadHeatmap(sector,map,sub);
  loadPlays(sector,plays);
}

async function loadPlays(sector,box){
  try{
    const r=await fetch(_pa('/api/sector/'+encodeURIComponent(sector)+'/plays'));
    if(!r.ok) return;                          // plays are best-effort, never block heatmap
    const d=await r.json();
    const plays=d.plays||[];
    box.textContent='';
    if(d.breakout==='none'||!plays.length){
      const em=document.createElement('div');em.className='plays-empty';
      em.textContent='No RS breakout right now.';box.appendChild(em);return;
    }
    const dir=d.breakout==='up'?'CALLS':'PUTS';
    const hd=document.createElement('div');hd.className='plays-head';
    hd.textContent='BREAKOUT PLAYS · '+dir;box.appendChild(hd);
    plays.forEach(function(p){
      const c=p.contract||{};
      const cd=document.createElement('div');cd.className='play-card '+p.role;
      const top=document.createElement('div');top.className='play-top';
      const chg=(p.change>=0?'+':'')+p.change+'%';
      top.textContent=p.ticker+'  ·  '+p.role.toUpperCase()+'  ·  '+chg;
      const bot=document.createElement('div');bot.className='play-con';
      const bits=[];
      if(c.label) bits.push(c.label);
      else{ if(c.strike) bits.push(c.strike+(c.type?' '+c.type:'')); }
      if(c.mid!=null) bits.push('@'+c.mid);
      if(c.delta!=null) bits.push('Δ'+c.delta);
      if(c.dte!=null&&c.dte>=0) bits.push(c.dte+'DTE');
      bot.textContent=bits.join('  ');
      cd.appendChild(top);cd.appendChild(bot);box.appendChild(cd);
    });
  }catch(e){ /* best-effort */ }
}

async function loadHeatmap(sector,map,sub){
  try{
    const r=await fetch(_pa('/api/sector/'+encodeURIComponent(sector)+'/heatmap'));
    if(!r.ok){const e=await r.json();throw new Error(e.detail||'Failed');}
    const d=await r.json();
    const stocks=(d.stocks||[]).filter(function(s){return s.weight>0});
    map.textContent='';
    if(!stocks.length){
      const em=document.createElement('div');em.className='heat-empty';
      em.textContent='No stock data — market may be closed';map.appendChild(em);
      sub.textContent='';return;
    }
    sub.textContent=stocks.length+' stocks';
    const W=map.clientWidth||map.offsetWidth||320;
    // taller canvas when there are more names so even small tiles stay tappable
    const H=Math.max(220,Math.min(640,Math.round(W*0.55+stocks.length*5)));
    map.style.position='relative';map.style.height=H+'px';
    const rects=squarify(stocks.map(function(s){return {w:Math.max(s.weight,1),it:s}}),W,H);
    rects.forEach(function(rc){
      const s=rc.it;
      const t=document.createElement('div');t.className='heat-tile';
      t.style.cssText='position:absolute;left:'+rc.x+'px;top:'+rc.y+'px;width:'+
        Math.max(rc.w-2,1)+'px;height:'+Math.max(rc.h-2,1)+'px;background:'+heatColor(s.change);
      if(rc.h>=14&&rc.w>=22){
        const tk=document.createElement('div');tk.className='ht-tk';
        if(rc.w<40)tk.style.fontSize='9px';
        tk.textContent=s.ticker;t.appendChild(tk);
      }
      if(rc.h>30&&rc.w>40){
        const ch=document.createElement('div');ch.className='ht-ch';
        ch.textContent=(s.change>0?'+':'')+s.change+'%';t.appendChild(ch);
      }
      t.onclick=function(ev){ev.stopPropagation();toast(s.ticker+'  '+(s.change>0?'+':'')+s.change+'%')};
      map.appendChild(t);
    });
  }catch(e){
    map.textContent='';
    const em=document.createElement('div');em.className='heat-empty';
    em.textContent=e.message||'Failed to load';map.appendChild(em);
    sub.textContent='';
  }
}

// Squarified treemap (Bruls et al.) — returns absolute rects {it,x,y,w,h}.
function squarify(items,W,H){
  const totalArea=W*H;
  let totalW=0;items.forEach(function(i){totalW+=i.w});if(totalW<=0)totalW=1;
  const data=items.map(function(i){return {it:i.it,area:i.w/totalW*totalArea}});
  const out=[];let X=0,Y=0,Wc=W,Hc=H;
  function worst(row,side){
    let s=0,mx=-Infinity,mn=Infinity;
    row.forEach(function(r){s+=r.area;if(r.area>mx)mx=r.area;if(r.area<mn)mn=r.area});
    return Math.max(side*side*mx/(s*s),s*s/(side*side*mn));
  }
  function layout(row){
    let s=0;row.forEach(function(r){s+=r.area});
    if(Wc>=Hc){const colW=s/Hc;let oy=Y;
      row.forEach(function(r){const th=r.area/colW;out.push({it:r.it,x:X,y:oy,w:colW,h:th});oy+=th});
      X+=colW;Wc-=colW;
    }else{const rowH=s/Wc;let ox=X;
      row.forEach(function(r){const tw=r.area/rowH;out.push({it:r.it,x:ox,y:Y,w:tw,h:rowH});ox+=tw});
      Y+=rowH;Hc-=rowH;}
  }
  let row=[];
  data.forEach(function(d){
    const side=Math.min(Wc,Hc);
    if(row.length===0){row=[d];return;}
    if(worst(row.concat([d]),side)<=worst(row,side)){row.push(d);}
    else{layout(row);row=[d];}
  });
  if(row.length)layout(row);
  return out;
}

function heatColor(ch){
  const a=Math.min(Math.abs(ch)/3,1);
  const lerp=function(x,y){return Math.round(x+(y-x)*a)};
  if(ch>=0){return 'rgb('+lerp(28,21)+','+lerp(46,194)+','+lerp(40,101)+')';}
  return 'rgb('+lerp(48,255)+','+lerp(34,51)+','+lerp(40,85)+')';
}

// Contract finder
function setDir(d){
  S.dir=d;
  document.getElementById('d-up').className='dir-btn up'+(d==='up'?' on':'');
  document.getElementById('d-dn').className='dir-btn dn'+(d==='down'?' on':'');
}
function setDteMode(m){
  S.dteMode=m;
  ['0dte','weekly','all'].forEach(function(k){
    document.getElementById('dt-'+k).className='dte-btn'+(k===m?' on':'');
  });
}

function _findError(msg){
  const res=document.getElementById('find-result');
  res.textContent='';
  const p=document.createElement('div');p.className='empty-st';p.style.padding='16px';
  p.textContent=msg;res.appendChild(p);
  const btn=document.getElementById('find-btn');
  btn.textContent='FIND TOP 3 CONTRACTS';btn.classList.remove('loading');
}

async function doFind(retry){
  const btn=document.getElementById('find-btn');
  if(!retry&&btn.classList.contains('loading')) return;
  const ticker=document.getElementById('ft').value.trim().toUpperCase()||'NVDA';
  btn.textContent=retry?'RETRYING...':'FINDING...';btn.classList.add('loading');
  const res=document.getElementById('find-result');
  if(!retry){
    res.textContent='';
    document.getElementById('both-result').textContent='';  // drop a stale ladder
    const skelWrap=document.createElement('div');skelWrap.style.cssText='margin:4px 0';
    [130,110,110].forEach(function(h){
      const s=document.createElement('div');
      s.className='skel';s.style.cssText='height:'+h+'px;border-radius:10px;margin-bottom:8px';
      skelWrap.appendChild(s);
    });
    res.appendChild(skelWrap);
  }
  try{
    const r=await fetch(_pa('/api/find?ticker='+ticker+'&direction='+S.dir+'&dte_mode='+S.dteMode));
    if(_handleAuth(r))return;
    if(!r.ok){
      // 4xx is a real answer (bad ticker / no chain) — show it now. Only a
      // network failure or 5xx means the server may still be cold.
      if(r.status<500){
        const e=await r.json().catch(function(){return {}});
        _findError(e.detail||e.error||'No contracts found for '+ticker);
        return;
      }
      throw new Error('Server error ('+r.status+')');
    }
    const d=await r.json();
    renderContracts(d.ticker,d.contracts,d.last_updated,d.dte_note);
    btn.textContent='FIND TOP 3 CONTRACTS';btn.classList.remove('loading');
  }catch(e){
    if(!retry){
      res.textContent='';
      const p=document.createElement('div');p.className='empty-st';p.style.padding='16px';
      p.textContent='Waking up server...';res.appendChild(p);
      setTimeout(function(){doFind(true)},5000);
    }else{
      _findError(e.message);
    }
  }
}

async function doFindBoth(){
  const ticker=(document.getElementById('ft').value.trim()||'NVDA').toUpperCase();
  const btn=document.getElementById('both-btn');
  const res=document.getElementById('both-result');
  btn.textContent='Loading...';btn.classList.add('loading');
  res.textContent='';
  document.getElementById('find-result').textContent='';  // drop stale contracts
  try{
    const r=await fetch(_pa('/api/find/both?ticker='+ticker+'&dte_mode='+S.dteMode));
    if(_handleAuth(r))return;
    if(!r.ok){const e=await r.json();throw new Error(e.detail||'Failed');}
    const d=await r.json();
    renderBothLadder(d);
  }catch(e){
    res.textContent='Error: '+e.message;
  }finally{
    btn.textContent='▶ CALLS vs PUTS LADDER';btn.classList.remove('loading');
  }
}

function renderBothLadder(d){
  const res=document.getElementById('both-result');
  res.textContent='';

  const fmtM=v=>v>=1e6?(v/1e6).toFixed(1)+'M':v>=1e3?(v/1e3).toFixed(0)+'K':v.toFixed(0);
  const fmtN=v=>v>=1e6?(v/1e6).toFixed(1)+'M':v>=1e3?(v/1e3).toFixed(0)+'K':String(v);

  const ct=d.call_totals, pt=d.put_totals;
  const cw='#00ff88', pw='#ff3355', neu='#888';

  if(d.dte_note){
    const warn=document.createElement('div');
    warn.style.cssText='background:rgba(255,176,32,.08);border:1px solid rgba(255,176,32,.3);'
      +'border-radius:8px;padding:9px 12px;margin-bottom:10px;font-size:11px;color:var(--amber)';
    warn.textContent='⚠ '+d.dte_note;
    res.appendChild(warn);
  }

  // ── best contract per side ──────────────────────────────────────────────
  if(d.best_call||d.best_put){
    const pickWrap=document.createElement('div');
    pickWrap.style.cssText='display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px';
    [['BEST CALL',d.best_call,cw,'C'],['BEST PUT',d.best_put,pw,'P']].forEach(function(p){
      const label=p[0], c=p[1], col=p[2], letter=p[3];
      const box=document.createElement('div');
      box.style.cssText='background:#0d0d16;border:1px solid '+col+'33;border-radius:10px;padding:12px';
      if(!c){
        box.innerHTML='<div style="font-size:9px;color:#555;letter-spacing:.8px;margin-bottom:6px">'
          +label+'</div><div style="font-size:11px;color:#555">no qualifying contract</div>';
        pickWrap.appendChild(box);return;
      }
      const roi=(c.roi!=null)?Number(c.roi).toFixed(0)+'%':'-';
      box.innerHTML='<div style="font-size:9px;color:#555;letter-spacing:.8px;margin-bottom:6px">'
        +label+'</div>'
        +'<div style="font-size:15px;font-weight:800;color:'+col+'">$'+Number(c.strike).toFixed(0)+' '+letter+'</div>'
        +'<div style="font-size:10px;color:var(--sub);margin-top:3px">'
        +(c.exp?String(c.exp).slice(5):'-')+' · '+(c.dte===0?'0DTE':c.dte+'DTE')+'</div>'
        +'<div style="display:flex;gap:10px;margin-top:8px;font-size:10px;color:#888">'
        +'<span>MID <b style="color:#ccc">$'+Number(c.mid||0).toFixed(2)+'</b></span>'
        +'<span>Δ <b style="color:#ccc">'+Number(c.delta||0).toFixed(2)+'</b></span></div>'
        +'<div style="display:flex;gap:10px;margin-top:3px;font-size:10px;color:#888">'
        +'<span>SCORE <b style="color:'+col+'">'+Number(c.score||0).toFixed(0)+'</b></span>'
        +'<span>ROI <b style="color:#ccc">'+roi+'</b></span></div>';
      pickWrap.appendChild(box);
    });
    res.appendChild(pickWrap);
  }

  // ── summary scoreboard ──────────────────────────────────────────────────
  const scoreEl=document.createElement('div');
  scoreEl.style.cssText='background:#0d0d16;border:1px solid #1a1a2e;border-radius:10px;padding:14px 16px;margin-bottom:12px';

  const hdr=document.createElement('div');
  hdr.style.cssText='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px';
  hdr.innerHTML=`<span style="font-size:11px;color:#555;letter-spacing:.8px">CALLS vs PUTS — ${d.ticker} ${d.exp} (${d.dte}DTE)</span><span style="font-size:10px;color:#444">${d.last_updated}</span>`;
  scoreEl.appendChild(hdr);

  const metrics=[
    {key:'dollar_flow', label:'$ FLOW',   fmt:v=>'$'+fmtM(v), winner:d.flow_winner},
    {key:'volume',      label:'VOLUME',   fmt:fmtN,            winner:d.vol_winner},
    {key:'oi',          label:'OI',       fmt:fmtN,            winner:d.oi_winner},
    {key:'ddoi',        label:'Δ OI',     fmt:fmtN,            winner:d.ddoi_winner},
  ];

  const grid=document.createElement('div');
  grid.style.cssText='display:grid;grid-template-columns:1fr 1fr;gap:8px';

  metrics.forEach(m=>{
    const cWin=m.winner==='call', pWin=m.winner==='put';
    const cell=document.createElement('div');
    cell.style.cssText='background:#111;border-radius:8px;padding:10px 12px';
    cell.innerHTML=`
      <div style="font-size:9px;color:#555;letter-spacing:.8px;margin-bottom:6px">${m.label}</div>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <span style="font-size:11px;color:#555">C </span>
          <span style="font-size:13px;font-weight:700;color:${cWin?cw:neu}">${m.fmt(ct[m.key])}</span>
          ${cWin?'<span style="font-size:9px;color:'+cw+';margin-left:4px">▲</span>':''}
        </div>
        <div>
          <span style="font-size:11px;color:#555">P </span>
          <span style="font-size:13px;font-weight:700;color:${pWin?pw:neu}">${m.fmt(pt[m.key])}</span>
          ${pWin?'<span style="font-size:9px;color:'+pw+';margin-left:4px">▲</span>':''}
        </div>
      </div>`;
    grid.appendChild(cell);
  });
  scoreEl.appendChild(grid);

  // Overall bias
  const cWins=[d.flow_winner,d.vol_winner,d.oi_winner,d.ddoi_winner].filter(x=>x==='call').length;
  const bias=cWins>=3?'CALL HEAVY':cWins<=1?'PUT HEAVY':'MIXED';
  const biasCol=cWins>=3?cw:cWins<=1?pw:neu;
  const biasEl=document.createElement('div');
  biasEl.style.cssText='margin-top:10px;text-align:center;font-size:14px;font-weight:800;letter-spacing:1px;color:'+biasCol;
  biasEl.textContent=bias+' ('+cWins+'/4 metrics call-dominant)';
  scoreEl.appendChild(biasEl);
  res.appendChild(scoreEl);

  // ── ladder table ────────────────────────────────────────────────────────
  const ladderEl=document.createElement('div');
  ladderEl.style.cssText='background:#0d0d16;border:1px solid #1a1a2e;border-radius:10px;padding:14px 16px;margin-bottom:60px';

  const ladderHdr=document.createElement('div');
  ladderHdr.style.cssText='font-size:9px;color:#555;letter-spacing:.8px;margin-bottom:10px';
  ladderHdr.textContent='TOP STRIKES BY $ FLOW';
  ladderEl.appendChild(ladderHdr);

  // header row
  const hrow=document.createElement('div');
  hrow.style.cssText='display:grid;grid-template-columns:60px 1fr 1fr 1fr 1fr;gap:4px;font-size:9px;color:#444;letter-spacing:.5px;margin-bottom:6px;padding:0 4px';
  hrow.innerHTML='<span>STRIKE</span><span style="text-align:right">$FLOW</span><span style="text-align:right">VOL</span><span style="text-align:right">OI</span><span style="text-align:right">ΔOI</span>';
  ladderEl.appendChild(hrow);

  // merge calls + puts, sort by dollar flow
  const allStrikes=[...d.calls.map(r=>({...r,side:'call'})),...d.puts.map(r=>({...r,side:'put'}))];
  allStrikes.sort((a,b)=>b.dollar_flow-a.dollar_flow);

  allStrikes.slice(0,12).forEach(r=>{
    const col=r.side==='call'?cw:pw;
    const row=document.createElement('div');
    row.style.cssText='display:grid;grid-template-columns:60px 1fr 1fr 1fr 1fr;gap:4px;font-size:11px;padding:5px 4px;border-bottom:1px solid #111';
    row.innerHTML=`
      <span style="font-weight:700;color:${col}">${r.side==='call'?'C':'P'} ${r.strike}</span>
      <span style="text-align:right;color:#ccc">$${fmtM(r.dollar_flow)}</span>
      <span style="text-align:right;color:#aaa">${fmtN(r.vol)}</span>
      <span style="text-align:right;color:#888">${fmtN(r.oi)}</span>
      <span style="text-align:right;color:#666">${fmtN(r.ddoi)}</span>`;
    ladderEl.appendChild(row);
  });

  res.appendChild(ladderEl);
}

function renderContracts(ticker,cs,ts,dteNote){
  if(!Array.isArray(cs)) cs=[cs];
  const isCall=S.dir==='up';
  const dteLbl={'0dte':'0DTE','weekly':'WEEKLY','all':'ALL'}[S.dteMode]||'';
  const res=document.getElementById('find-result');
  res.textContent='';
  if(dteNote){
    const warn=document.createElement('div');
    warn.style.cssText='background:rgba(255,176,32,.08);border:1px solid rgba(255,176,32,.3);'
      +'border-radius:8px;padding:9px 12px;margin-bottom:10px;font-size:11px;color:var(--amber)';
    warn.textContent='⚠ '+dteNote;
    res.appendChild(warn);
  }
  const wrap=document.createElement('div');wrap.className='cont-cards';
  cs.forEach(function(c,i){
    const card=document.createElement('div');
    card.className='cont-card'+(i===0?' best':'');
    // hero
    const hero=document.createElement('div');hero.className='cont-hero';
    const symDiv=document.createElement('div');
    const sym=document.createElement('div');sym.className='cont-sym';
    const sp1=document.createElement('span');sp1.className=isCall?'call':'put';sp1.textContent=ticker;
    const sp2=document.createElement('span');sp2.className='ks';
    sp2.textContent=' $'+c.strike.toFixed(0)+' '+(isCall?'C':'P');
    sym.appendChild(sp1);sym.appendChild(sp2);
    const exp=document.createElement('div');
    exp.style.cssText='font-size:10px;color:var(--sub);margin-top:4px';
    exp.textContent=(c.exp?c.exp.slice(5):'-')+' . '+(c.dte===0?'0DTE':c.dte+'DTE')+' . '+dteLbl;
    symDiv.appendChild(sym);symDiv.appendChild(exp);
    const rightDiv=document.createElement('div');
    const badge=document.createElement('div');
    badge.className='cont-badge '+(i===0?'best':'alt');
    badge.textContent=i===0?'BEST FIT':'ALT '+(i+1);
    rightDiv.appendChild(badge);
    if(c.stale){
      const st=document.createElement('div');
      st.style.cssText='font-size:9px;color:var(--amber);margin-top:4px';
      st.textContent='STALE';rightDiv.appendChild(st);
    }
    hero.appendChild(symDiv);hero.appendChild(rightDiv);
    // grid
    const grid=document.createElement('div');grid.className='cont-grid';
    const mid=c.mid?'$'+c.mid.toFixed(2):'-';
    const bidask=(c.bid>0&&c.ask>0)?'$'+c.bid.toFixed(2)+'/$'+c.ask.toFixed(2):'last '+mid;
    const dlt=c.delta!=null?(c.delta>=0?'+':'')+c.delta.toFixed(3):'-';
    const iv=c.iv?(c.iv*100).toFixed(1)+'%':'-';
    const voiN=c.oi>0?c.vol/c.oi:0;
    const voi=c.oi>0?voiN.toFixed(1)+'x':'-';
    const roiClr=c.roi>50?'g':c.roi>0?'cy':c.roi<0?'r':'';
    const roi=c.roi!=null?(c.roi>0?'+':'')+c.roi.toFixed(1)+'%':'-';
    [
      ['Mid',mid,isCall?'g':'r'],
      ['Bid/Ask',bidask,''],
      ['Delta',dlt,isCall?'g':'r'],
      ['IV',iv,'cy'],
      ['Vol/OI',voi,voiN>=10?'gd':voiN>=3?'cy':''],
      ['1s ROI',roi,roiClr],
    ].forEach(function(it){
      const cg=document.createElement('div');cg.className='cg';
      const lbl=document.createElement('label');lbl.textContent=it[0];
      const sp=document.createElement('span');if(it[2]) sp.className=it[2];sp.textContent=it[1];
      cg.appendChild(lbl);cg.appendChild(sp);grid.appendChild(cg);
    });
    // note
    const note=document.createElement('div');note.className='cont-note';
    note.textContent=(c.stale?'Stale quote - market closed':'Ranked: 1s ROI . delta . liquidity . spread')
      +(i===0?' . Score: '+(c.score||'-'):'')
      +(ts?' . '+ts:'');
    card.appendChild(hero);card.appendChild(grid);card.appendChild(note);
    wrap.appendChild(card);
  });
  res.appendChild(wrap);
}

function toast(msg,type){
  const el=document.getElementById('toast');
  el.textContent=msg;
  el.className='show'+(type==='err'?' err':'');
  setTimeout(function(){el.className=''},2800);
}

async function loadIntel(){
  const tickers=document.getElementById('intel-tickers').value.trim();
  const qs=tickers?'?tickers='+encodeURIComponent(tickers):'';
  document.getElementById('intel-macro').textContent='Loading macro regime…';
  document.getElementById('intel-dp').textContent='Loading dark pool data…';
  document.getElementById('intel-ins').textContent='Loading insider data…';
  try{
    const [macroRes,dpRes,insRes]=await Promise.all([
      fetch(_pa('/api/macro')),
      fetch(_pa('/api/darkpool'+qs)),
      fetch(_pa('/api/insider'+qs))
    ]);
    if(_handleAuth(macroRes)||_handleAuth(dpRes)||_handleAuth(insRes))return;
    const macro=macroRes.ok?await macroRes.json():{error:'unavailable'};
    const dp=dpRes.ok?await dpRes.json():{error:'unavailable'};
    const ins=insRes.ok?await insRes.json():{error:'unavailable'};
    renderIntelMacro(macro);
    renderIntelDP(dp);
    renderIntelIns(ins);
  }catch(e){
    document.getElementById('intel-macro').textContent='Error: '+e.message;
    document.getElementById('intel-dp').textContent='Error: '+e.message;
    document.getElementById('intel-ins').textContent='Error: '+e.message;
  }
}

function renderIntelMacro(data){
  const el=document.getElementById('intel-macro');
  if(data.error){el.textContent=data.error;return;}
  const col=data.regime==='RISK-ON'?'#00ff88':data.regime==='RISK-OFF'?'#ff3355':'#ffa500';
  const score=data.score>=0?'+'+data.score:String(data.score);
  let html=`<div style="display:flex;align-items:center;gap:16px;margin-bottom:8px">
    <span style="color:${col};font-size:16px;font-weight:800">${data.regime}</span>
    <span style="color:#888;font-size:12px">Score: ${score}</span>
    <span style="color:#444;font-size:10px">${data.source||''}</span>
  </div>`;
  if(data.signals&&data.signals.length){
    html+=data.signals.map(s=>`<div style="font-size:11px;color:#aaa;padding:2px 0">• ${s}</div>`).join('');
  }
  if(data.data&&Object.keys(data.data).length){
    html+=`<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px">`;
    for(const[k,v]of Object.entries(data.data)){
      if(v.value!=null){
        // Monthly series (CPI, unemployment, fed funds) lag by weeks while the
        // daily ones are current. Without the age they read as equally fresh.
        const age=v.stale_days;
        const aged=age!=null&&age>=30;
        const tag=age==null?'':` <span style="color:${aged?'#ffb020':'#555'}" title="${v.as_of||''}">${age}d</span>`;
        html+=`<span style="background:#111;border:1px solid ${aged?'#3a2c10':'#222'};border-radius:4px;padding:3px 8px;font-size:10px;color:#aaa">${v.label}: <span style="color:#ccc">${v.value.toFixed?v.value.toFixed(2):v.value}${v.unit}</span>${tag}</span>`;
      }
    }
    html+=`</div>`;
  }
  html+=`<div style="font-size:9px;color:#444;margin-top:6px">Updated: ${data.last_updated||'—'}</div>`;
  el.innerHTML=html;
}

function renderIntelDP(data){
  const el=document.getElementById('intel-dp');
  if(data.error){el.textContent=data.error;return;}
  const sigs=(data.signals||[]).filter(s=>s.score>15).slice(0,20);
  if(!sigs.length){el.textContent='No significant dark pool anomalies detected.';return;}
  const rows=sigs.map(s=>{
    const col=s.signal==='ACCUMULATION'?'#00ff88':s.signal==='DISTRIBUTION'?'#ff3355':'#888';
    const vol=s.vol_ratio!=null?s.vol_ratio.toFixed(1)+'x':'—';
    const impact=s.price_impact_pct!=null?s.price_impact_pct.toFixed(2)+'%':'—';
    return `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1a1a2e;font-size:12px">
      <span style="color:${col};font-weight:700;width:60px">${s.ticker}</span>
      <span style="color:${col};width:110px">${s.signal}</span>
      <span style="color:#aaa;width:60px">Vol: ${vol}</span>
      <span style="color:#666;width:70px">D${impact}</span>
      <span style="color:#888">Score: ${Math.round(s.score)}</span>
    </div>`;
  }).join('');
  el.innerHTML=`<div style="max-height:280px;overflow-y:auto">${rows}</div><div style="font-size:9px;color:#444;margin-top:6px">Source: yfinance vol-proxy · Updated: ${data.last_updated||'—'}</div>`;
}

function renderIntelIns(data){
  const el=document.getElementById('intel-ins');
  if(data.error){el.textContent=data.error;return;}
  const sigs=(data.signals||[]).filter(s=>s.score>20).slice(0,20);
  if(!sigs.length){el.textContent='No significant insider activity detected.';return;}
  const rows=sigs.map(s=>{
    const col=s.net_sentiment==='BUYING'?'#00ff88':s.net_sentiment==='SELLING'?'#ff3355':'#ffa500';
    const val=s.buy_value>1e6?(s.buy_value/1e6).toFixed(1)+'M':s.buy_value>1e3?(s.buy_value/1e3).toFixed(0)+'K':'—';
    return `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid #1a1a2e;font-size:12px">
      <span style="color:${col};font-weight:700;width:60px">${s.ticker}</span>
      <span style="color:${col};width:100px">${s.net_sentiment}</span>
      <span style="color:#aaa;width:80px">Buys: ${s.buy_count} ($${val})</span>
      <span style="color:#888">Score: ${Math.round(s.score)}</span>
    </div>`;
  }).join('');
  el.innerHTML=`<div style="max-height:280px;overflow-y:auto">${rows}</div><div style="font-size:9px;color:#444;margin-top:6px">Source: SEC EDGAR Form 4 · Updated: ${data.last_updated||'—'}</div>`;
}

// ── UOA: Unusual Options Activity tab ────────────────────────────────────────
function _e(v){return String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function _fN(n){if(n>=1e6)return'$'+(n/1e6).toFixed(1)+'M';if(n>=1e3)return'$'+(n/1e3).toFixed(0)+'K';return'$'+n;}

const _UOA_COLORS={'🔴 EXTREME':'#ff3355','🟠 UNUSUAL':'#ff8c00','🟡 NOTABLE':'#ffd700','⚪ NORMAL':'#666'};
let _uoaSignals=[]; let _uoaMeta=''; let _uoaSort={key:'score',dir:-1};
// {label, key, type:'n'umeric | 's'tring | 'x' non-sortable}
const _UOA_COLS=[
  {label:'SIGNAL',key:'score',type:'x'},
  {label:'TICKER',key:'ticker',type:'s'},
  {label:'SECTOR',key:'sector',type:'s'},
  {label:'TYPE',key:'type',type:'s'},
  {label:'STRIKE',key:'strike',type:'n'},
  {label:'EXPIRY',key:'dte',type:'x'},
  {label:'DTE',key:'dte',type:'n'},
  {label:'VOL',key:'volume',type:'n'},
  {label:'OI',key:'open_interest',type:'n'},
  {label:'V/OI',key:'vol_oi',type:'n'},
  {label:'NOTIONAL',key:'notional',type:'n'},
  {label:'SIDE',key:'trade_side',type:'s'},
  {label:'SCORE',key:'score',type:'n'},
];
function sortUOA(key){
  if(_uoaSort.key===key){_uoaSort.dir=-_uoaSort.dir;}
  else{_uoaSort.key=key;_uoaSort.dir=-1;}
  renderUOATable();
}
function renderUOATable(){
  const wrap=document.getElementById('uoa-table-wrap');
  wrap.innerHTML='';
  if(!_uoaSignals.length){
    const msg=document.createElement('div');
    msg.style.cssText='text-align:center;padding:30px;color:#555;font-size:12px';
    msg.textContent='No unusual flow detected right now.';
    wrap.appendChild(msg);return;
  }
  const sk=_uoaSort.key,dir=_uoaSort.dir;
  const sorted=_uoaSignals.slice().sort(function(a,b){
    let av=a[sk],bv=b[sk];
    if(typeof av==='string'||typeof bv==='string'){
      return String(av).localeCompare(String(bv))*dir;
    }
    return ((av||0)-(bv||0))*dir;
  });
  const rows=sorted.map(function(s){
    const lc=_UOA_COLORS[s.label]||'#666';
    const tc=s.type==='call'?'#00ff88':'#ff3355';
    const sc=s.trade_side==='ask'?'#ffd700':s.trade_side==='bid'?'#ff8c00':'#555';
    const vc=s.vol_oi>=5?'#ff3355':s.vol_oi>=1?'#ff8c00':s.vol_oi>=0.5?'#ffd700':'#aaa';
    return '<tr>'
      +'<td style="color:'+lc+';font-size:9px;white-space:nowrap">'+_e(s.label)+'</td>'
      +'<td style="font-weight:700;color:#eee">'+_e(s.ticker)+'</td>'
      +'<td style="font-size:9px;color:#666">'+_e(s.sector)+'</td>'
      +'<td style="color:'+tc+';font-weight:700">'+_e(s.type.toUpperCase())+'</td>'
      +'<td style="color:#aaa">$'+_e(s.strike)+'</td>'
      +'<td style="font-size:10px;color:#888">'+_e(s.expiry)+'</td>'
      +'<td style="color:#777">'+_e(s.dte)+'d</td>'
      +'<td style="color:#ccc">'+_e(Number(s.volume).toLocaleString())+'</td>'
      +'<td style="color:#666">'+_e(Number(s.open_interest).toLocaleString())+'</td>'
      +'<td style="color:'+vc+';font-weight:700">'+_e(s.vol_oi)+'x</td>'
      +'<td style="color:#00ff88;font-weight:700">'+_e(_fN(s.notional))+'</td>'
      +'<td style="color:'+sc+';font-size:10px">'+_e(s.trade_side)+'</td>'
      +'<td style="color:#aaa">'+_e(s.score)+'</td>'
      +'</tr>';
  }).join('');
  const meta=document.createElement('div');
  meta.style.cssText='font-size:9px;color:#444;margin-bottom:6px;text-align:right';
  meta.textContent=_uoaMeta;
  wrap.appendChild(meta);
  const ths=_UOA_COLS.map(function(c){
    if(c.type==='x') return '<th>'+c.label+'</th>';
    const active=c.key===sk;
    const arr=active?(dir<0?' <span class="arr">&#9660;</span>':' <span class="arr">&#9650;</span>'):'';
    return '<th class="sortable'+(active?' active':'')+'" data-key="'+c.key+'">'+c.label+arr+'</th>';
  }).join('');
  const tbl=document.createElement('table');
  tbl.className='scan-table';tbl.style.fontSize='11px';
  tbl.innerHTML='<thead><tr>'+ths+'</tr></thead><tbody>'+rows+'</tbody>';
  tbl.querySelectorAll('th.sortable').forEach(function(th){
    th.onclick=function(){sortUOA(th.dataset.key)};
  });
  wrap.appendChild(tbl);
}

async function loadUOA(force){
  const btn=document.getElementById('uoa-run-btn');
  const status=document.getElementById('uoa-status');
  const wrap=document.getElementById('uoa-table-wrap');
  const bar=document.getElementById('uoa-sector-bar');
  btn.disabled=true; btn.textContent='SCANNING…';
  status.textContent='Screening tickers → fetching options chains → scoring anomalies…';
  status.style.display='block';
  wrap.innerHTML='';
  bar.style.display='none';
  try{
    const r=await fetch(_pa('/api/unusual-flow?min_score=35'+(force?'&force=true':'')));
    if(!r.ok) throw new Error(await r.text());
    const d=await r.json();
    status.style.display='none';
    // Sector bar
    const sumKeys=Object.keys(d.summary||{});
    if(sumKeys.length){
      const maxN=Math.max(...sumKeys.map(k=>d.summary[k].notional));
      const bhtml=sumKeys.sort((a,b)=>d.summary[b].notional-d.summary[a].notional).map(sec=>{
        const s=d.summary[sec];
        const pct=Math.round(s.notional/maxN*100);
        const bias=s.calls>=s.puts?'#00ff88':'#ff3355';
        const cpct=s.count?(s.calls/s.count*100).toFixed(0):0;
        return '<div style="margin-bottom:5px">'
          +'<div style="display:flex;justify-content:space-between;font-size:9px;color:#aaa;margin-bottom:2px">'
          +'<span>'+_e(sec)+'</span>'
          +'<span style="color:'+bias+'">'+_e(cpct)+'% CALLS · '+_e(_fN(s.notional))+'</span>'
          +'</div>'
          +'<div style="height:4px;background:#1a1a2e;border-radius:2px">'
          +'<div style="height:4px;width:'+pct+'%;background:'+bias+';border-radius:2px"></div>'
          +'</div></div>';
      }).join('');
      bar.innerHTML='<div style="background:#0d0d1a;border:1px solid #222;border-radius:8px;padding:10px 12px">'
        +'<div style="font-size:9px;letter-spacing:.8px;color:#555;margin-bottom:8px">SECTOR FLOW BREAKDOWN</div>'
        +bhtml+'</div>';
      bar.style.display='block';
    }
    // Contracts table (sortable — tap a column header)
    _uoaSignals=d.signals||[];
    _uoaMeta=d.count+' contracts · '+(d.cached?'cached':'live')+' · '+(d.last_updated||'');
    renderUOATable();
  } catch(e){
    status.style.display='none';
    const err=document.createElement('div');
    err.style.cssText='text-align:center;padding:20px;color:#ff3355;font-size:11px';
    err.textContent='Error: '+e.message;
    wrap.appendChild(err);
  } finally {
    btn.disabled=false; btn.textContent='▶ SCAN UNUSUAL FLOW';
  }
}
