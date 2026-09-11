// PIN never injected into HTML — stored in localStorage only
let PIN = localStorage.getItem('scanner_pin') || '';

// The PIN travels in a header, never the query string. As ?pin= it was written
// into Railway's access logs, the CDN edge's logs, the Referer of any outbound
// request and the browser's own history — none of which are places a shared
// secret survives. _pa is kept as a no-op so every call site stays unchanged.
const _pa = p => p;

const _origFetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  init = init || {};
  if (PIN && typeof input === 'string' && input.startsWith('/api/')) {
    init.headers = Object.assign({}, init.headers || {}, {'X-Pin': PIN});
  }
  return _origFetch(input, init);
};

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
  // Post-scan chips. The bar above SCAN decides what gets scanned and costs a
  // rescan to change; these narrow a scan already paid for, in place.
  fSweeps:false,fDte:'all',fMin:0,fWhale:false,
};
const FLOW_FILTERS={
  // Sweep only became worth filtering on once it stopped firing on 72% of
  // contracts. A golden sweep is a sweep that also cleared the size bar.
  sweeps:function(s){return !!(s.has_sweep||s.golden)},
  dte0:function(s){return s.dte===0},
  swing:function(s){return s.dte>0},
  whale:function(s){return s.score>=70||s.tier==='whale'||s.tier==='block'},
  minPrem:function(s,v){return (s.total||0)>=v},
};
const FLOW_MIN_STEPS=[0,500000,1000000,5000000];
const FLOW_MIN_LBLS=['MIN $','MIN $500K','MIN $1M','MIN $5M'];
const FLOW_DTE_OPTS=['all','0dte','swing'];
const FLOW_DTE_LBLS={'all':'ANY DTE','0dte':'0DTE','swing':'SWING'};
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
    badge.style.color='var(--up)';
  } else if(d.flow_source==='unknown'){
    badge.textContent='○ source unknown — no flow scan yet';
    badge.style.color='var(--t3)';
  } else {
    badge.textContent='○ DELAYED — yfinance 15min';
    badge.style.color='var(--t3)';
    if(d.flow_source_reason) badge.title=d.flow_source_reason;
  }
  _updateFlowFreshness(d);
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

async function doFlowScan(retryCount){
  if(S.scanning&&!retryCount) return;
  retryCount=retryCount||0;
  if(!retryCount){
    S.scanning=true;S.callFlow=0;S.putFlow=0;S.signals=[];S.hotContracts=[];
    setView('signals');
    document.getElementById('view-toggle').style.display='none';
    document.getElementById('flow-sort-bar').style.display='none';
    document.getElementById('flow-filter-bar').style.display='none';
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
  btn.textContent='Scanning '+n+'…';btn.className='scan-btn loading';
  const minScore=S.whale?60:40;
  const url=_pa('/api/flow?tickers='+tickers+'&dte='+S.dte+'&min_score='+minScore);
  // EventSource cannot send headers, so when a PIN is set the stream
  // authenticates with a single-use ticket instead. The PIN itself stays out of
  // the URL, the access log and browser history.
  if(PIN){
    try{
      const tr=await fetch('/api/sse-ticket');
      if(_handleAuth(tr)) return;
      if(tr.ok){
        const tj=await tr.json();
        if(tj.ticket) url += (url.includes('?')?'&':'?')+'ticket='+encodeURIComponent(tj.ticket);
      }
    }catch(e){ /* fall through: the stream will surface its own failure */ }
  }
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
      // Cards stream in one at a time, so the chips have to gate them here
      // too — otherwise a filter set mid-scan leaks every later signal in.
      if(flowPasses(s)) renderFlowCard(s);
      updateFlowFilterCount(S.signals.filter(flowPasses).length);
      updateFlowBias();
      document.getElementById('flow-sort-bar').style.display='flex';
      document.getElementById('flow-filter-bar').style.display='flex';
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
  btn.textContent=S.full?'Full':'Scan';btn.className='scan-btn';
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
  document.getElementById('bc').textContent=fmt(cf);
  document.getElementById('bp').textContent=fmt(pf);
  const bd=document.getElementById('bd');
  if(cf>pf*1.2){bd.textContent='Bull';bd.className='bias-dir bull'}
  else if(pf>cf*1.2){bd.textContent='Bear';bd.className='bias-dir bear'}
  else{bd.textContent='Even';bd.className='bias-dir neut'}
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

function renderFlowLadder(d){
  // Where the premium is stacked, by strike. The four contract chips are
  // ranked and capped, so they cannot show shape: six strikes bought in a row
  // around spot and one lotto strike far out look identical there. Highest
  // strike sits on top, the way a price ladder reads, with spot ruled across
  // it so the stack's side of the money is obvious at a glance.
  if(!d||!d.rows||!d.rows.length) return null;
  const rows=d.rows.slice().sort((a,b)=>b.strike-a.strike);
  const rowH=14,padT=6,padB=6,labelW=42,barX=46,rightPad=52;
  const w=300,barW=w-barX-rightPad;
  const h=padT+padB+rows.length*rowH;
  const yOf=i=>padT+i*rowH+rowH/2;

  let svg='<svg width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'" style="display:block;width:100%;max-width:'+w+'px">';
  rows.forEach(function(r,i){
    const y=yOf(i),len=Math.max(r.pct/100*barW,0.8);
    // Calls and puts at one strike are a straddle, not conviction — keep the
    // split visible instead of summing them into a single anonymous bar.
    const cw=r.total?len*(r.call/r.total):0;
    svg+='<text x="'+labelW+'" y="'+(y+3.2)+'" text-anchor="end" font-size="8.5" fill="var(--t3)" font-family="var(--font)">'+r.strike+'</text>';
    if(cw>0) svg+='<rect x="'+barX+'" y="'+(y-4.5)+'" width="'+cw.toFixed(1)+'" height="9" rx="1.5" fill="var(--up)" opacity=".8"/>';
    if(len-cw>0) svg+='<rect x="'+(barX+cw).toFixed(1)+'" y="'+(y-4.5)+'" width="'+(len-cw).toFixed(1)+'" height="9" rx="1.5" fill="var(--dn)" opacity=".8"/>';
    // The longest bar's amount would sit under the spot label in the right
    // gutter, so on a long bar the amount moves inside it.
    const inside=len>barW*0.62;
    svg+='<text x="'+(inside?barX+len-5:barX+len+5).toFixed(1)+'" y="'+(y+3.2)+
         '" text-anchor="'+(inside?'end':'start')+'" font-size="8" fill="'+
         (inside?'var(--ink)':'var(--t3)')+'" font-family="var(--font)">'+r.fmt+'</text>';
  });

  if(d.spot>0){
    // Interpolate spot onto the strike axis, same as the GEX profile does, so
    // the rule lands between strikes rather than snapping to the nearest row.
    const hi=rows[0].strike,lo=rows[rows.length-1].strike;
    let y;
    if(d.spot>=hi) y=padT;
    else if(d.spot<=lo) y=h-padB;
    else{
      y=yOf(0);
      for(let i=0;i<rows.length-1;i++){
        const a=rows[i].strike,b=rows[i+1].strike;
        if(d.spot<=a&&d.spot>=b){const t=(a-d.spot)/(a-b);y=yOf(i)+(yOf(i+1)-yOf(i))*t;break}
      }
    }
    const sy=y.toFixed(1);
    svg+='<line x1="4" y1="'+sy+'" x2="'+(barX+barW)+'" y2="'+sy+'" stroke="var(--m4)" stroke-width="1" stroke-dasharray="3 3" opacity=".85"/>';
    svg+='<text x="'+(barX+barW+4)+'" y="'+(y+3.2).toFixed(1)+'" font-size="8" fill="var(--m4)" font-family="var(--font)">'+d.spot.toFixed(2)+'</text>';
  }
  svg+='</svg>';

  const wrap=document.createElement('div');
  wrap.className='ladder-wrap';
  const hdr=document.createElement('div');
  hdr.className='cc-side-lbl';hdr.style.color='var(--sub)';
  hdr.textContent='Premium by strike';
  wrap.appendChild(hdr);
  const chart=document.createElement('div');
  chart.className='ladder-chart';
  chart.innerHTML=svg;   // static SVG built from numbers only — no user text
  wrap.appendChild(chart);
  return wrap;
}

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
  const ladder=renderFlowLadder(s.ladder);
  if(ladder) card.appendChild(ladder);
  card.appendChild(det);
  feed.appendChild(card);
}
function updateFlowFilterCount(shown){
  // Say what was hidden. A chip that empties the feed in silence is
  // indistinguishable from a scan that found nothing.
  const cnt=document.getElementById('flow-filter-count');
  cnt.textContent=shown<S.signals.length?shown+' of '+S.signals.length:'';
}
function flowPasses(s){
  // Chips stack: every one that is on must pass. OR-ing them would put back
  // exactly the noise the reader turned a chip on to remove.
  if(S.fSweeps&&!FLOW_FILTERS.sweeps(s)) return false;
  if(S.fDte==='0dte'&&!FLOW_FILTERS.dte0(s)) return false;
  if(S.fDte==='swing'&&!FLOW_FILTERS.swing(s)) return false;
  if(S.fWhale&&!FLOW_FILTERS.whale(s)) return false;
  if(S.fMin>0&&!FLOW_FILTERS.minPrem(s,S.fMin)) return false;
  return true;
}
function tFlowSweeps(){
  S.fSweeps=!S.fSweeps;
  document.getElementById('f-sweeps').className='chip'+(S.fSweeps?' on':'');
  sortFlowFeed();
}
function tFlowDte(){
  S.fDte=FLOW_DTE_OPTS[(FLOW_DTE_OPTS.indexOf(S.fDte)+1)%FLOW_DTE_OPTS.length];
  const el=document.getElementById('f-dte');
  el.textContent=FLOW_DTE_LBLS[S.fDte];
  el.className='chip'+(S.fDte!=='all'?' on':'');
  sortFlowFeed();
}
function tFlowMin(){
  const i=(FLOW_MIN_STEPS.indexOf(S.fMin)+1)%FLOW_MIN_STEPS.length;
  S.fMin=FLOW_MIN_STEPS[i];
  const el=document.getElementById('f-min');
  el.textContent=FLOW_MIN_LBLS[i];
  el.className='chip'+(S.fMin>0?' on':'');
  sortFlowFeed();
}
function tFlowWhale(){
  S.fWhale=!S.fWhale;
  document.getElementById('f-whale').className='chip'+(S.fWhale?' on':'');
  sortFlowFeed();
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
  const shown=S.signals.filter(flowPasses);
  shown.forEach(renderFlowCard);
  updateFlowFilterCount(shown.length);
  if(!shown.length){
    const wrap=document.createElement('div');wrap.className='empty-st';
    const h=document.createElement('h3');h.textContent='Nothing matches';
    const p=document.createElement('p');
    p.textContent=S.signals.length+' signal'+(S.signals.length>1?'s':'')+' hidden by the filters.';
    wrap.appendChild(h);wrap.appendChild(p);feed.appendChild(wrap);
  }
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
  btn.textContent='Scanning…';btn.style.opacity='.6';
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
    ts.textContent='Updated '+(d.last_updated||'');
    hdr.appendChild(hdrLeft);hdr.appendChild(ts);
    feed.appendChild(hdr);
    if(d.laggard){
      const lg=d.laggard;
      const lb=document.createElement('div');lb.className='laggard-box';
      const lbl=document.createElement('div');lbl.className='laggard-label';lbl.textContent='Top laggard';
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
    btn.textContent='Try again';btn.onclick=loadSectors;feed.appendChild(btn);
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
        Math.max(rc.w-2,1)+'px;height:'+Math.max(rc.h-2,1)+'px;background:'+heatColor(s.change)+
        ';color:'+heatInk(s.change);
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

// Monochrome treemap: magnitude is luminance, direction is which way it runs
// from the neutral mid-grey -- gainers lighten toward white, losers darken
// toward the ground. A 3% move saturates the ramp, as it did with the colours.
function heatLevel(ch){
  const a=Math.min(Math.abs(ch)/3,1);
  const mid=66;
  return Math.round(ch>=0 ? mid+(255-mid)*a : mid-(mid-12)*a);
}
function heatColor(ch){
  const v=heatLevel(ch);
  return 'rgb('+v+','+v+','+v+')';
}
// Ink that survives its own tile: the crossover sits where grey stops carrying
// white text, so no tile is ever a light-on-light or dark-on-dark label.
function heatInk(ch){
  return heatLevel(ch)>=128 ? '#000' : '#fff';
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
  const cw='var(--up)', pw='var(--dn)', neu='var(--t2)';

  if(d.dte_note){
    const warn=document.createElement('div');
    warn.style.cssText='background:var(--s1);border:1px solid var(--line2);'
      +'border-radius:10px;padding:9px 12px;margin-bottom:10px;font-size:11px;color:var(--t2)';
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
      box.style.cssText='background:var(--s1);border:1px solid '+col+'33;border-radius:10px;padding:12px';
      if(!c){
        box.innerHTML='<div style="font-size:9px;color:var(--t3);letter-spacing:.8px;margin-bottom:6px">'
          +label+'</div><div style="font-size:11px;color:var(--t3)">no qualifying contract</div>';
        pickWrap.appendChild(box);return;
      }
      const roi=(c.roi!=null)?Number(c.roi).toFixed(0)+'%':'-';
      box.innerHTML='<div style="font-size:9px;color:var(--t3);letter-spacing:.8px;margin-bottom:6px">'
        +label+'</div>'
        +'<div style="font-size:15px;font-weight:800;color:'+col+'">$'+Number(c.strike).toFixed(0)+' '+letter+'</div>'
        +'<div style="font-size:10px;color:var(--sub);margin-top:3px">'
        +(c.exp?String(c.exp).slice(5):'-')+' · '+(c.dte===0?'0DTE':c.dte+'DTE')+'</div>'
        +'<div style="display:flex;gap:10px;margin-top:8px;font-size:10px;color:var(--t2)">'
        +'<span>MID <b style="color:var(--t1)">$'+Number(c.mid||0).toFixed(2)+'</b></span>'
        +'<span>Δ <b style="color:var(--t1)">'+Number(c.delta||0).toFixed(2)+'</b></span></div>'
        +'<div style="display:flex;gap:10px;margin-top:3px;font-size:10px;color:var(--t2)">'
        +'<span>SCORE <b style="color:'+col+'">'+Number(c.score||0).toFixed(0)+'</b></span>'
        +'<span>ROI <b style="color:var(--t1)">'+roi+'</b></span></div>';
      pickWrap.appendChild(box);
    });
    res.appendChild(pickWrap);
  }

  // ── summary scoreboard ──────────────────────────────────────────────────
  const scoreEl=document.createElement('div');
  scoreEl.style.cssText='background:var(--s1);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:12px';

  const hdr=document.createElement('div');
  hdr.style.cssText='display:flex;justify-content:space-between;align-items:center;margin-bottom:12px';
  hdr.innerHTML=`<span style="font-size:11px;color:var(--t3);letter-spacing:.8px">CALLS vs PUTS — ${d.ticker} ${d.exp} (${d.dte}DTE)</span><span style="font-size:10px;color:var(--t3)">${d.last_updated}</span>`;
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
    cell.style.cssText='background:var(--line);border-radius:8px;padding:10px 12px';
    cell.innerHTML=`
      <div style="font-size:9px;color:var(--t3);letter-spacing:.8px;margin-bottom:6px">${m.label}</div>
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div>
          <span style="font-size:11px;color:var(--t3)">C </span>
          <span style="font-size:13px;font-weight:700;color:${cWin?cw:neu}">${m.fmt(ct[m.key])}</span>
          ${cWin?'<span style="font-size:9px;color:'+cw+';margin-left:4px">▲</span>':''}
        </div>
        <div>
          <span style="font-size:11px;color:var(--t3)">P </span>
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
  ladderEl.style.cssText='background:var(--s1);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:60px';

  const ladderHdr=document.createElement('div');
  ladderHdr.style.cssText='font-size:9px;color:var(--t3);letter-spacing:.8px;margin-bottom:10px';
  ladderHdr.textContent='TOP STRIKES BY $ FLOW';
  ladderEl.appendChild(ladderHdr);

  // header row
  const hrow=document.createElement('div');
  hrow.style.cssText='display:grid;grid-template-columns:60px 1fr 1fr 1fr 1fr;gap:4px;font-size:9px;color:var(--t3);letter-spacing:.5px;margin-bottom:6px;padding:0 4px';
  hrow.innerHTML='<span>STRIKE</span><span style="text-align:right">$FLOW</span><span style="text-align:right">VOL</span><span style="text-align:right">OI</span><span style="text-align:right">ΔOI</span>';
  ladderEl.appendChild(hrow);

  // merge calls + puts, sort by dollar flow
  const allStrikes=[...d.calls.map(r=>({...r,side:'call'})),...d.puts.map(r=>({...r,side:'put'}))];
  allStrikes.sort((a,b)=>b.dollar_flow-a.dollar_flow);

  allStrikes.slice(0,12).forEach(r=>{
    const col=r.side==='call'?cw:pw;
    const row=document.createElement('div');
    row.style.cssText='display:grid;grid-template-columns:60px 1fr 1fr 1fr 1fr;gap:4px;font-size:11px;padding:5px 4px;border-bottom:1px solid var(--line)';
    row.innerHTML=`
      <span style="font-weight:700;color:${col}">${r.side==='call'?'C':'P'} ${r.strike}</span>
      <span style="text-align:right;color:var(--t1)">$${fmtM(r.dollar_flow)}</span>
      <span style="text-align:right;color:var(--t2)">${fmtN(r.vol)}</span>
      <span style="text-align:right;color:var(--t2)">${fmtN(r.oi)}</span>
      <span style="text-align:right;color:var(--t3)">${fmtN(r.ddoi)}</span>`;
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
    warn.style.cssText='background:var(--s1);border:1px solid var(--line2);'
      +'border-radius:10px;padding:9px 12px;margin-bottom:10px;font-size:11px;color:var(--t2)';
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
  const col=data.regime==='RISK-ON'?'var(--up)':data.regime==='RISK-OFF'?'var(--dn)':'var(--m4)';
  const score=data.score>=0?'+'+data.score:String(data.score);
  let html=`<div style="display:flex;align-items:center;gap:16px;margin-bottom:8px">
    <span style="color:${col};font-size:16px;font-weight:800">${data.regime}</span>
    <span style="color:var(--t2);font-size:12px">Score: ${score}</span>
    <span style="color:var(--t3);font-size:10px">${data.source||''}</span>
  </div>`;
  if(data.signals&&data.signals.length){
    html+=data.signals.map(s=>`<div style="font-size:11px;color:var(--t2);padding:2px 0">• ${s}</div>`).join('');
  }
  if(data.data&&Object.keys(data.data).length){
    html+=`<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:8px">`;
    for(const[k,v]of Object.entries(data.data)){
      if(v.value!=null){
        // Monthly series (CPI, unemployment, fed funds) lag by weeks while the
        // daily ones are current. Without the age they read as equally fresh.
        const age=v.stale_days;
        const aged=age!=null&&age>=30;
        const tag=age==null?'':` <span style="color:${aged?'var(--m4)':'var(--t3)'}" title="${v.as_of||''}">${age}d</span>`;
        html+=`<span style="background:var(--line);border:1px solid ${aged?'var(--line2)':'var(--line)'};border-radius:4px;padding:3px 8px;font-size:10px;color:var(--t2)">${v.label}: <span style="color:var(--t1)">${v.value.toFixed?v.value.toFixed(2):v.value}${v.unit}</span>${tag}</span>`;
      }
    }
    html+=`</div>`;
  }
  html+=`<div style="font-size:9px;color:var(--t3);margin-top:6px">Updated: ${data.last_updated||'—'}</div>`;
  el.innerHTML=html;
}

function renderIntelDP(data){
  const el=document.getElementById('intel-dp');
  if(data.error){el.textContent=data.error;return;}
  const sigs=(data.signals||[]).filter(s=>s.score>15).slice(0,20);
  if(!sigs.length){el.textContent='No significant dark pool anomalies detected.';return;}
  const rows=sigs.map(s=>{
    const col=s.signal==='ACCUMULATION'?'var(--up)':s.signal==='DISTRIBUTION'?'var(--dn)':'var(--t2)';
    const vol=s.vol_ratio!=null?s.vol_ratio.toFixed(1)+'x':'—';
    const impact=s.price_impact_pct!=null?s.price_impact_pct.toFixed(2)+'%':'—';
    return `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--line);font-size:12px">
      <span style="color:${col};font-weight:700;width:60px">${s.ticker}</span>
      <span style="color:${col};width:110px">${s.signal}</span>
      <span style="color:var(--t2);width:60px">Vol: ${vol}</span>
      <span style="color:var(--t3);width:70px">D${impact}</span>
      <span style="color:var(--t2)">Score: ${Math.round(s.score)}</span>
    </div>`;
  }).join('');
  el.innerHTML=`<div style="max-height:280px;overflow-y:auto">${rows}</div><div style="font-size:9px;color:var(--t3);margin-top:6px">Source: yfinance vol-proxy · Updated: ${data.last_updated||'—'}</div>`;
}

function renderIntelIns(data){
  const el=document.getElementById('intel-ins');
  if(data.error){el.textContent=data.error;return;}
  const sigs=(data.signals||[]).filter(s=>s.score>20).slice(0,20);
  if(!sigs.length){el.textContent='No significant insider activity detected.';return;}
  const rows=sigs.map(s=>{
    const col=s.net_sentiment==='BUYING'?'var(--up)':s.net_sentiment==='SELLING'?'var(--dn)':'var(--m4)';
    const val=s.buy_value>1e6?(s.buy_value/1e6).toFixed(1)+'M':s.buy_value>1e3?(s.buy_value/1e3).toFixed(0)+'K':'—';
    return `<div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--line);font-size:12px">
      <span style="color:${col};font-weight:700;width:60px">${s.ticker}</span>
      <span style="color:${col};width:100px">${s.net_sentiment}</span>
      <span style="color:var(--t2);width:80px">Buys: ${s.buy_count} ($${val})</span>
      <span style="color:var(--t2)">Score: ${Math.round(s.score)}</span>
    </div>`;
  }).join('');
  el.innerHTML=`<div style="max-height:280px;overflow-y:auto">${rows}</div><div style="font-size:9px;color:var(--t3);margin-top:6px">Source: SEC EDGAR Form 4 · Updated: ${data.last_updated||'—'}</div>`;
}

// ── UOA: Unusual Options Activity tab ────────────────────────────────────────
function _e(v){return String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function _fN(n){if(n>=1e6)return'$'+(n/1e6).toFixed(1)+'M';if(n>=1e3)return'$'+(n/1e3).toFixed(0)+'K';return'$'+n;}

const _UOA_COLORS={'🔴 EXTREME':'var(--dn)','🟠 UNUSUAL':'var(--m4)','🟡 NOTABLE':'var(--t1)','⚪ NORMAL':'var(--t3)'};
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
    msg.style.cssText='text-align:center;padding:30px;color:var(--t3);font-size:12px';
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
    const lc=_UOA_COLORS[s.label]||'var(--t3)';
    const tc=s.type==='call'?'var(--up)':'var(--dn)';
    const sc=s.trade_side==='ask'?'var(--t1)':s.trade_side==='bid'?'var(--m4)':'var(--t3)';
    const vc=s.vol_oi>=5?'var(--dn)':s.vol_oi>=1?'var(--m4)':s.vol_oi>=0.5?'var(--t1)':'var(--t2)';
    return '<tr>'
      +'<td style="color:'+lc+';font-size:9px;white-space:nowrap">'+_e(s.label)+'</td>'
      +'<td style="font-weight:700;color:var(--t1)">'+_e(s.ticker)+'</td>'
      +'<td style="font-size:9px;color:var(--t3)">'+_e(s.sector)+'</td>'
      +'<td style="color:'+tc+';font-weight:700">'+_e(s.type.toUpperCase())+'</td>'
      +'<td style="color:var(--t2)">$'+_e(s.strike)+'</td>'
      +'<td style="font-size:10px;color:var(--t2)">'+_e(s.expiry)+'</td>'
      +'<td style="color:var(--t3)">'+_e(s.dte)+'d</td>'
      +'<td style="color:var(--t1)">'+_e(Number(s.volume).toLocaleString())+'</td>'
      +'<td style="color:var(--t3)">'+_e(Number(s.open_interest).toLocaleString())+'</td>'
      +'<td style="color:'+vc+';font-weight:700">'+_e(s.vol_oi)+'x</td>'
      +'<td style="color:var(--up);font-weight:700">'+_e(_fN(s.notional))+'</td>'
      +'<td style="color:'+sc+';font-size:10px">'+_e(s.trade_side)+'</td>'
      +'<td style="color:var(--t2)">'+_e(s.score)+'</td>'
      +'</tr>';
  }).join('');
  const meta=document.createElement('div');
  meta.style.cssText='font-size:9px;color:var(--t3);margin-bottom:6px;text-align:right';
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
  btn.disabled=true; btn.textContent='Scanning…';
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
        const bias=s.calls>=s.puts?'var(--up)':'var(--dn)';
        const cpct=s.count?(s.calls/s.count*100).toFixed(0):0;
        return '<div style="margin-bottom:5px">'
          +'<div style="display:flex;justify-content:space-between;font-size:9px;color:var(--t2);margin-bottom:2px">'
          +'<span>'+_e(sec)+'</span>'
          +'<span style="color:'+bias+'">'+_e(cpct)+'% CALLS · '+_e(_fN(s.notional))+'</span>'
          +'</div>'
          +'<div style="height:4px;background:var(--line);border-radius:2px">'
          +'<div style="height:4px;width:'+pct+'%;background:'+bias+';border-radius:2px"></div>'
          +'</div></div>';
      }).join('');
      bar.innerHTML='<div style="background:var(--s1);border:1px solid var(--line);border-radius:8px;padding:10px 12px">'
        +'<div style="font-size:9px;letter-spacing:.8px;color:var(--t3);margin-bottom:8px">SECTOR FLOW BREAKDOWN</div>'
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
    err.style.cssText='text-align:center;padding:20px;color:var(--dn);font-size:11px';
    err.textContent='Error: '+e.message;
    wrap.appendChild(err);
  } finally {
    btn.disabled=false; btn.textContent='▶ SCAN UNUSUAL FLOW';
  }
}

// ─── GEX: dealer gamma exposure ─────────────────────────────────────────────
// Measured quantities only. Nothing here says where price is going, because
// the endpoint deliberately does not report that.
function gexMoney(v){
  const a=Math.abs(v), sign=v<0?'-':'';
  if(a>=1e9) return sign+'$'+(a/1e9).toFixed(2)+'B';
  if(a>=1e6) return sign+'$'+(a/1e6).toFixed(1)+'M';
  if(a>=1e3) return sign+'$'+(a/1e3).toFixed(0)+'K';
  return sign+'$'+a.toFixed(0);
}

function gexStat(k,v,cls,unit){
  return '<div class="gex-stat"><div class="k">'+k+'</div><div class="v '+(cls||'')+'">'+
         v+(unit?'<span class="u">'+unit+'</span>':'')+'</div></div>';
}

// The gamma surface is measured on index strikes, but dealers hedge it in the
// futures and out of hours the future is the only leg still printing. Holding
// the payload lets the unit toggle redraw without paying for another chain.
let _gexData=null, _gexUnit='under';

let _gexContract=null;   // contract code; null = the family's full-size one

// The full contract and its micro quote the same index, so which one is
// selected never moves a level -- it only changes what reaching that level is
// worth. On a four-figure account that is the difference between a tradeable
// contract and one you cannot carry.
function gexContract(){
  const f=_gexData&&_gexData.futures;
  if(!f||!f.contracts||!f.contracts.length) return null;
  return f.contracts.find(c=>c.code===_gexContract)||f.contracts[0];
}

function gexUnit(){
  const f=_gexData&&_gexData.futures, c=gexContract();
  return (_gexUnit==='fut'&&f)
    ? {ratio:f.ratio, label:(c?c.code:f.future), fut:true}
    : {ratio:1, label:(_gexData&&_gexData.symbol)||'', fut:false};
}

// Distances: whole dollars. Nobody sizes a trade off the cents on a $4,932 move.
function gexDollars(v){
  return '$'+String(Math.round(Math.abs(v))).replace(/\B(?=(\d{3})+(?!\d))/g,',');
}

// Contract specs: exact. ES ticks at $12.50 and MNQ at $0.50 -- these are the
// numbers traders know by heart, and rounding them printed "$13" and "$1".
function gexSpec(v){
  const a=Math.abs(v);
  return '$'+(a%1===0?String(a):a.toFixed(2));
}

// A strike in the active unit. Index strikes are round by construction; the
// futures prices they map to are not, and rounding them to look tidy would be
// inventing precision the basis does not have.
function gexLevel(v,u){
  if(v==null) return '—';
  const x=v*u.ratio;
  return u.fut ? x.toFixed(2) : String(Math.round(x*100)/100);
}

function gexAxisMoney(v){
  const a=Math.abs(v);
  if(a>=1e9) return (a/1e9).toFixed(1)+'B';
  if(a>=1e6) return (a/1e6).toFixed(0)+'M';
  if(a>=1e3) return (a/1e3).toFixed(0)+'K';
  return a.toFixed(0);
}

// The band worth drawing. A full SPX chain runs thousands of points wide and
// nearly all of it is far-OTM strikes carrying no gamma -- drawing every one
// produced a 12,000px ladder that was 80% blank rows. Walk outward from spot
// until the band holds COVER of the total magnitude, then keep that contiguous
// range so the ladder has no holes in it. Walls and the flip are pulled in even
// if they sit outside, because a chart that hides the level it names is worse
// than a tall one.
function gexWindow(strikes, mag, spot, marks){
  const COVER=0.94, MAXROWS=64;
  const total=strikes.reduce((a,k)=>a+mag[k],0);
  if(!total) return strikes.slice(0,MAXROWS);
  const nearest=v=>strikes.reduce((best,k,idx)=>
        Math.abs(k-v)<Math.abs(strikes[best]-v)?idx:best,0);

  // Grow outward from spot, always toward the heavier side, until the band
  // holds COVER of the surface.
  let lo=nearest(spot), hi=lo, acc=mag[strikes[lo]];
  while(acc/total<COVER && (lo>0||hi<strikes.length-1)){
    const dLo=lo>0?mag[strikes[lo-1]]:-1, dHi=hi<strikes.length-1?mag[strikes[hi+1]]:-1;
    if(dHi>dLo){ hi++; acc+=dHi; } else { lo--; acc+=dLo; }
  }

  // Then take in anything the header names. A chart that hides the level it
  // prints above itself is worse than a tall one, so these are pinned: the
  // row trim below will not cross them.
  const pins=new Set();
  marks.filter(v=>v!=null).forEach(v=>{
    const i=nearest(v); pins.add(i);
    if(i<lo) lo=i;
    if(i>hi) hi=i;
  });

  // Trim back to a readable height from whichever end carries less, stopping
  // at a pinned row. This runs after the pins are in, which is the whole
  // point -- recentring on spot here is what used to drop a far wall.
  while(hi-lo+1>MAXROWS){
    const canLo=!pins.has(lo), canHi=!pins.has(hi);
    if(!canLo&&!canHi) break;
    if(canHi&&(!canLo||mag[strikes[hi]]<=mag[strikes[lo]])) hi--; else lo++;
  }
  return strikes.slice(lo,hi+1);
}

function renderGexChart(d,containerW,u){
  u=u||{ratio:1,fut:false};
  // Aggregate both option types into one bar per strike: the reader is asking
  // where dealers are long or short gamma, not how it splits by contract type.
  const byStrike={};
  d.profile.forEach(r=>{
    const e=byStrike[r.strike]||(byStrike[r.strike]={n:0,inferred:0,total:0});
    e.n+=r.gamma_notional;
    e.total+=Math.abs(r.gamma_notional);
    if(r.src==='inferred') e.inferred+=Math.abs(r.gamma_notional);
  });
  let strikes=Object.keys(byStrike).map(Number).sort((a,b)=>a-b);
  if(!strikes.length) return '<div style="font-size:11px;color:var(--sub);text-align:center;padding:16px">No strikes with usable data.</div>';

  const mag={}; strikes.forEach(k=>mag[k]=Math.abs(byStrike[k].n));
  const shown=gexWindow(strikes,mag,d.spot,[d.flip,d.call_wall,d.put_wall]);
  const clipped=strikes.length-shown.length;
  strikes=shown;

  // Lay the chart out against the real container so nothing is cut off and no
  // dead strip is left over. The right gutter has to hold "SPOT 7599.64" and
  // the left one a five-digit strike, both in the 8.5px mono face.
  const W=Math.max(300,Math.round(containerW||360));
  // Both gutters are sized from the text that actually goes in them. They were
  // fixed at 44/68, which fits a four-digit SPX strike and silently cropped the
  // leading digit off every five-digit one -- an NQ ladder read "0005.19".
  const CH_STRIKE=5.1, CH_RULE=4.8;   // mono advance at 8.5px and 8px
  const strikeChars=strikes.reduce((m,k)=>Math.max(m,gexLevel(k,u).length),4);
  const ruleChars=Math.max(
    ('SPOT '+(d.spot*u.ratio).toFixed(2)).length,
    d.flip!=null?('FLIP '+(d.flip*u.ratio).toFixed(2)).length:0,
    d.futures?(d.futures.future+' '+d.futures.last.toFixed(2)).length:0);
  const padL=Math.ceil(strikeChars*CH_STRIKE)+14;
  const padR=Math.ceil(ruleChars*CH_RULE)+10;
  const rowH=14, padT=12, padB=26;
  const plotL=padL, plotR=W-padR;
  const midX=Math.round((plotL+plotR)/2), halfW=(plotR-plotL)/2-2;
  const h=padT+padB+strikes.length*rowH;

  const idx={}; strikes.forEach((k,i)=>idx[k]=i);
  const yOf=k=>padT+(strikes.length-1-idx[k])*rowH+rowH/2;

  const maxMag=Math.max(...strikes.map(k=>mag[k]))||1;
  // Label round strikes, not every Nth row. Stepping by row position lands the
  // axis on values like 7715 and 7665, which nobody thinks in; stepping by
  // price keeps it on the 7700s and 7650s a trader actually reads.
  const span=strikes[strikes.length-1]-strikes[0];
  const step=[0.5,1,2.5,5,10,25,50,100,250,500,1000]
             .find(x=>span/x<=13)||1000;
  const isRound=k=>Math.abs(k/step-Math.round(k/step))<1e-6;
  const wall=k=>k===d.call_wall?'call':(k===d.put_wall?'put':null);

  let svg='<svg width="'+W+'" height="'+h+'" viewBox="0 0 '+W+' '+h+
          '" style="display:block" role="img" aria-label="Dealer gamma by strike">';
  svg+='<line x1="'+midX+'" y1="'+(padT-4)+'" x2="'+midX+'" y2="'+(h-padB+4)+
       '" stroke="rgba(255,255,255,.14)" stroke-width="1"/>';

  strikes.forEach((k,i)=>{
    const e=byStrike[k], y=yOf(k), w=wall(k);
    const len=mag[k]/maxMag*halfW;
    const pos=e.n>=0;
    const x=pos?midX:midX-len;
    // Sign observed from today's flow is drawn solid; sign assumed by
    // convention is drawn faint, so the reader sees how much is inference.
    const share=e.total?e.inferred/e.total:0;
    const col=pos?'var(--up)':'var(--dn)';
    // The wall is marked by a caret and a coloured strike, not a tinted row:
    // a band spanning the plot reads as a bar the width of the chart.
    if(w){
      const wc=w==='call'?'var(--up)':'var(--dn)';
      svg+='<path d="M'+(plotL-1)+' '+(y-4)+'L'+(plotL+4)+' '+y+'L'+(plotL-1)+' '+(y+4)+'Z" fill="'+wc+'"/>';
    }
    svg+='<rect x="'+x.toFixed(1)+'" y="'+(y-4.5)+'" width="'+Math.max(len,0.75).toFixed(1)+
         '" height="9" rx="1.5" fill="'+col+'" opacity="'+(0.30+0.65*share).toFixed(2)+'"/>';
    // Strikes live in a fixed left gutter. They used to flip sides with the
    // sign of the bar, which made the axis zigzag and unreadable.
    if(isRound(k)||w){
      svg+='<text x="'+(padL-9)+'" y="'+(y+3)+'" text-anchor="end" font-size="8.5" fill="'+
           (w?(w==='call'?'var(--up)':'var(--dn)'):'var(--t3)')+'" font-family="var(--font)">'+
           gexLevel(k,u)+'</text>';
    }
    svg+='<line x1="'+(padL-5)+'" y1="'+y+'" x2="'+(padL-2)+'" y2="'+y+
         '" stroke="rgba(255,255,255,.16)" stroke-width="1"/>';
  });

  const priceToY=p=>{
    // Position the spot/flip rule against the strike axis by interpolation.
    if(p<=strikes[0]) return yOf(strikes[0]);
    if(p>=strikes[strikes.length-1]) return yOf(strikes[strikes.length-1]);
    for(let i=0;i<strikes.length-1;i++){
      const a=strikes[i], b=strikes[i+1];
      if(p>=a&&p<=b){const t=(p-a)/(b-a);return yOf(a)+(yOf(b)-yOf(a))*t;}
    }
    return yOf(strikes[0]);
  };

  // Rule labels are anchored to the right edge rather than offset from the
  // plot, which is what used to push "SPOT 7599.64" past the viewBox and cut
  // the last characters off. The lines stay on their true price; only the
  // labels are nudged apart, because spot and the flip are routinely a couple
  // of points from each other and the two captions landed on top of each other.
  // Captions on the rules are prices, so they keep both decimals; the strike
  // labels in the gutter are strikes and do not.
  const px=v=>(v*u.ratio).toFixed(2);
  const rules=[{p:d.spot,col:'var(--m4)',lbl:'SPOT '+px(d.spot),dash:false}];
  if(d.flip!=null)
    rules.push({p:d.flip,col:'var(--t1)',lbl:'FLIP '+px(d.flip),dash:true});
  // Out of hours the index print is frozen at its close and this is the only
  // line on the chart that is still moving.
  if(d.futures&&d.futures.implied_underlying>0)
    rules.push({p:d.futures.implied_underlying,col:'var(--m4)',
                lbl:d.futures.future+' '+d.futures.last.toFixed(2),dash:true});
  rules.forEach(r=>r.y=priceToY(r.p));
  rules.sort((a,b)=>a.y-b.y);
  const MINGAP=9.5;
  rules.forEach((r,i)=>{
    r.ly=r.y;
    if(i>0 && r.ly-rules[i-1].ly<MINGAP) r.ly=rules[i-1].ly+MINGAP;
  });
  // Keep the nudged stack inside the plot.
  const spill=rules.length?rules[rules.length-1].ly-(h-padB):0;
  if(spill>0) rules.forEach(r=>r.ly-=spill);
  rules.forEach(r=>{
    svg+='<line x1="'+plotL+'" y1="'+r.y+'" x2="'+plotR+'" y2="'+r.y+'" stroke="'+r.col+
         '" stroke-width="1"'+(r.dash?' stroke-dasharray="3 3"':'')+' opacity=".85"/>';
    // When a label has been pushed off its line, a leader keeps them tied.
    if(Math.abs(r.ly-r.y)>0.5)
      svg+='<line x1="'+plotR+'" y1="'+r.y+'" x2="'+(plotR+5)+'" y2="'+r.ly+
           '" stroke="'+r.col+'" stroke-width="1" opacity=".45"/>';
    svg+='<text x="'+(W-4)+'" y="'+(r.ly+3)+'" text-anchor="end" font-size="8" fill="'+r.col+
         '" font-family="var(--font)">'+r.lbl+'</text>';
  });

  // A bar meant nothing without a scale to read it against.
  const base=h-padB+16;
  svg+='<text x="'+plotL+'" y="'+base+'" font-size="7.5" fill="var(--t3)" font-family="var(--font)">'+
       '&#8722;$'+gexAxisMoney(maxMag)+'</text>'+
       '<text x="'+midX+'" y="'+base+'" text-anchor="middle" font-size="7.5" fill="var(--t3)" '+
       'font-family="var(--font)">per 1% move</text>'+
       '<text x="'+plotR+'" y="'+base+'" text-anchor="end" font-size="7.5" fill="var(--t3)" '+
       'font-family="var(--font)">+$'+gexAxisMoney(maxMag)+'</text>';
  svg+='</svg>';

  let note='';
  if(clipped>0){
    note='<div class="gex-axis-note">'+gexLevel(strikes[0],u)+'&ndash;'+
         gexLevel(strikes[strikes.length-1],u)+
         ' &middot; '+clipped+' further strike'+(clipped===1?'':'s')+
         ' hold almost no gamma and are not drawn</div>';
  }
  return '<div class="gex-chart-wrap">'+svg+'</div>'+note;
}

function gexPct(v){return (v>=0?'+':'')+(v*100).toFixed(2)+'%';}

// The stat block and the unit switch. Rebuilt rather than patched so the two
// can never disagree about which unit is showing.
// What a move from here to each measured level is worth on one contract.
// Distance only -- it says nothing about direction, position or whether the
// level gets reached, because none of that is measured here.
function renderGexDist(d){
  const f=d.futures, c=gexContract();
  if(!f||!c||!(c.last>0)) return '';
  const rows=[['CALL WALL',d.call_wall,'gex-pos'],
              ['ZERO-GAMMA FLIP',d.flip,''],
              ['PUT WALL',d.put_wall,'gex-neg']]
             .filter(r=>r[1]!=null);
  if(!rows.length) return '';
  let out='<div class="gex-dist"><div class="gex-dist-head">'+
          'FROM '+c.code+' '+c.last.toFixed(2)+
          '<span>'+gexSpec(c.multiplier)+'/pt &middot; 1 tick '+
          gexSpec(c.tick_value)+'</span></div>';
  rows.forEach(([lbl,lvl,cls])=>{
    const price=lvl*f.ratio, pts=price-c.last;
    out+='<div class="gex-dist-row"><span class="l">'+lbl+'</span>'+
         '<span class="p">'+price.toFixed(2)+'</span>'+
         '<span class="d '+(pts>=0?'gex-pos':'gex-neg')+'">'+
         (pts>=0?'+':'&minus;')+Math.abs(pts).toFixed(2)+'</span>'+
         '<span class="v">'+gexDollars(Math.abs(pts)*c.multiplier)+'</span></div>';
  });
  out+='</div>';
  return out;
}

function renderGexHead(d){
  const u=gexUnit(), f=d.futures;
  let out='';
  // There is no option chain on a future. Asking for MNQ gets the Nasdaq
  // surface priced in MNQ, and the screen says so rather than quietly
  // answering a different question than the one that was typed.
  if(d.resolved_from_future)
    out+='<div class="gex-resolved"><b>'+d.requested+'</b> &rarr; gamma measured on '+
         d.symbol+' options, priced in '+(gexContract()||{code:d.requested}).code+
         '. There is no option chain on a future.</div>';
  out+='<div class="gex-head">'+
    gexStat('NET GAMMA',gexMoney(d.net_gex),d.net_gex>=0?'gex-pos':'gex-neg','/1%')+
    gexStat('ZERO-GAMMA FLIP',d.flip!=null?gexLevel(d.flip,u):'none in range','')+
    gexStat('CALL WALL',gexLevel(d.call_wall,u),'')+
    gexStat('PUT WALL',gexLevel(d.put_wall,u),'');
  if(f){
    // The live print follows the selected size -- MNQ and NQ quote within a
    // tick of each other, and a stat headed NQ beside a table headed MNQ reads
    // like the two disagree.
    const sel=gexContract()||{code:f.future,last:f.last};
    out+=gexStat(sel.code+' LAST',(sel.last||f.last).toFixed(2),
                 f.change>=0?'gex-pos':'gex-neg',
                 ' '+(f.change>=0?'+':'')+f.change.toFixed(2))+
         gexStat(f.future+(f.basis!=null?' BASIS':' RATIO'),
                 f.basis!=null?(f.basis>=0?'+':'')+f.basis.toFixed(2)
                              :'&times;'+f.ratio.toFixed(3),
                 '',f.basis!=null?' pts':'');
  }
  out+='</div>';
  if(f){
    const c=gexContract();
    out+='<div class="gex-unit"><span class="lbl">LEVELS IN</span>'+
         '<button class="gex-unit-btn'+(u.fut?'':' on')+'" onclick="setGexUnit(\'under\')">'+
         (d.symbol||'INDEX')+'</button>'+
         '<button class="gex-unit-btn'+(u.fut?' on':'')+'" onclick="setGexUnit(\'fut\')">'+
         f.future+'</button>';
    if(f.contracts&&f.contracts.length>1){
      out+='<span class="lbl sz">SIZE</span>';
      f.contracts.forEach(x=>{
        out+='<button class="gex-unit-btn'+(c&&c.code===x.code?' on':'')+
             '" title="'+x.name+' &mdash; '+gexSpec(x.multiplier)+' per point"'+
             ' onclick="setGexContract(\''+x.code+'\')">'+x.code+'</button>';
      });
    }
    out+='</div>';
  }
  out+=renderGexDist(d);
  return out;
}

function setGexUnit(which){
  if(_gexUnit===which||!_gexData) return;
  _gexUnit=which;
  drawGex();
}

function setGexContract(code){
  const c=gexContract();
  if(!_gexData||(c&&c.code===code)) return;
  _gexContract=code;
  drawGex();
}

function drawGex(){
  const d=_gexData; if(!d) return;
  document.getElementById('gex-head').innerHTML=renderGexHead(d);
  const c=document.getElementById('gex-chart');
  c.innerHTML=renderGexChart(d,c.clientWidth,gexUnit());
  document.getElementById('gex-prov').innerHTML=renderGexProv(d);
}

function renderGexProv(d){
  const p=d.provenance;
  const pct=x=>(x*100).toFixed(0)+'%';
  let out='<div class="gex-prov">';
  const SRC={'yfinance':'yfinance chain',
             'dxfeed':'dxFeed Summary (TastyTrade)',
             'yfinance+dxfeed':'yfinance chain, backfilled from dxFeed Summary'};
  out+='<b>Open interest is '+p.oi_asof+'</b> — not intraday. The surface is least '+
       'accurate on days with heavy overnight repositioning.<br>';
  out+='OI source: '+(SRC[p.oi_source]||p.oi_source)+' — '+pct(p.oi_coverage)+
       ' of strikes carry a reading.<br>';
  out+='Sign: '+pct(p.inferred_pct)+' observed from today\'s flow, '+
       pct(p.assumed_pct)+' assumed (dealers long calls / short puts).<br>';
  out+='Strikes: '+p.strikes_total+' total, '+p.strikes_dropped+' dropped for no usable IV. ';
  out+='Expiries: '+(p.expiries||[]).join(', ')+'.';
  const f=d.futures;
  if(f){
    out+='<br><b>'+f.future+' conversion</b> &times;'+f.ratio.toFixed(5)+
         (f.basis!=null?' ('+(f.basis>=0?'+':'')+f.basis.toFixed(2)+' pts)':'')+
         ', measured from the '+f.ratio_asof+' closes of '+f.name+' and the index '+
         'printed in the same session. It is a measured basis, not a fair-value '+
         'model, and it drifts as carry does.<br>'+
         f.future+' is '+f.last.toFixed(2)+' ('+(f.change>=0?'+':'')+
         f.change.toFixed(2)+', '+gexPct(f.change_pct)+' from its close) &mdash; '+
         'the surface itself has not repriced since the last settle.';
  }
  if(!p.flip_stable && p.flip_roots>1){
    out+='<br><span class="warn">Net gamma crosses zero '+p.flip_roots+
         '&times; within &plusmn;5% — the flip is an artifact of where spot '+
         'sits, not a level.</span>';
  }
  if(p.concentrated){
    out+='<br><span class="warn">One strike holds '+pct(p.max_strike_share)+
         ' of the surface — near expiry the flip means less.</span>';
  }
  out+='</div>';
  return out;
}

async function loadGEX(){
  const btn=document.getElementById('gex-run-btn');
  const st=document.getElementById('gex-status');
  const sym=(document.getElementById('gex-sym').value||'SPX').trim().toUpperCase();
  btn.disabled=true; btn.textContent='Building…';
  st.style.display='block'; st.textContent='Fetching chains for '+sym+'...';
  document.getElementById('gex-head').innerHTML='';
  document.getElementById('gex-chart').innerHTML='';
  document.getElementById('gex-prov').innerHTML='';
  try{
    const r=await fetch(_pa('/api/gex?symbol='+encodeURIComponent(sym)));
    if(_handleAuth(r)) return;
    if(!r.ok){
      const msg=await r.json().catch(()=>({}));
      st.textContent=(msg.detail||('Request failed ('+r.status+')'));
      return;
    }
    const d=await r.json();
    if(!d.provenance.oi_usable){
      st.style.display='block';
      // There is no surface to convert, so nothing claims there is one. The
      // futures print still goes up: out of hours it is the only live number
      // on this screen, and it is the reason to come back at the open.
      if(d.futures){
        const f=d.futures;
        const codes=(f.contracts||[]).map(x=>x.code);
        if(d.preselect_contract&&codes.indexOf(d.preselect_contract)>=0)
          _gexContract=d.preselect_contract;
        document.getElementById('gex-head').innerHTML='<div class="gex-head">'+
          gexStat(f.future+' LAST',f.last.toFixed(2),
                  f.change>=0?'gex-pos':'gex-neg',
                  ' '+(f.change>=0?'+':'')+f.change.toFixed(2))+
          gexStat(f.future+' FROM CLOSE',gexPct(f.change_pct),
                  f.change>=0?'gex-pos':'gex-neg','')+
          gexStat('IMPLIES '+(d.symbol||'INDEX'),f.implied_underlying.toFixed(2),'')+
          '</div>';
      }
      st.innerHTML='<b style="color:var(--gold)">No open interest in this chain.</b><br>'+
        'The feed returned none, so there is no surface to draw. This is a missing '+
        'reading, not a zero one — nothing here is safe to trade off.<br>'+
        'Outside market hours yfinance reports no OI; the fallback reading comes '+
        'from dxFeed and needs TastyTrade credentials (or an OAuth grant) on this '+
        'deployment. During market hours the chain itself carries OI.';
      return;
    }
    st.style.display='none';
    _gexData=d;
    // A symbol with no futures counterpart cannot stay switched to a unit it
    // does not have, and a contract code from the previous symbol's family
    // (MNQ held over onto SPX) would silently fall back to the wrong size.
    if(!d.futures) _gexUnit='under';
    const codes=(d.futures&&d.futures.contracts||[]).map(x=>x.code);
    if(codes.indexOf(_gexContract)<0) _gexContract=null;
    // Typing a contract is a request to see it in that contract's prices, so
    // the unit and the size are already set when the surface arrives.
    if(d.preselect_contract&&codes.indexOf(d.preselect_contract)>=0){
      _gexUnit='fut'; _gexContract=d.preselect_contract;
    }
    drawGex();
  }catch(e){
    st.textContent='Could not build the surface.';
  }finally{
    btn.disabled=false; btn.innerHTML='Build gamma surface';
  }
}

// ─── Per-tab data freshness ─────────────────────────────────────────────────
// Every tab says what its data actually is. Nothing is called real-time unless
// it is -- on the deployed app the quote feed is delayed and the flow feed is a
// 15-minute snapshot, so a badge claiming otherwise would be a lie the user
// sizes positions against.
const FRESHNESS = {
  flow:    {t:'Quotes delayed ~15m (yfinance). This deployment cannot stream OPRA — run locally with TastyTrade for that.', c:'is-lagged'},
  scan:    {t:'Quotes and history delayed ~15m (yfinance).', c:'is-lagged'},
  sectors: {t:'Sector quotes delayed ~15m (yfinance).', c:'is-lagged'},
  find:    {t:'Chains and quotes delayed ~15m (yfinance).', c:'is-lagged'},
  uoa:     {t:'Built on delayed chains (~15m). Not a live tape.', c:'is-lagged'},
  gex:     {t:'Open interest is prior-session settle. Spot delayed ~15m. Not intraday OI.', c:'is-lagged'},
  intel:   {t:'Dark pool is a volume-based proxy, not off-exchange prints. Insider = SEC filings (days behind). Macro = FRED (monthly series lag weeks).', c:'is-lagged'},
};

function _mountFreshness(){
  Object.keys(FRESHNESS).forEach(k=>{
    const pane=document.getElementById('tab-'+k);
    if(!pane || pane.querySelector('.freshness')) return;
    const f=FRESHNESS[k];
    const el=document.createElement('div');
    el.className='freshness '+f.c;
    el.id='freshness-'+k;
    el.innerHTML='<span class="dot"></span><span class="txt">'+f.t+'</span>';
    pane.insertBefore(el, pane.firstChild);
  });
}

// The FLOW tab is the one that can genuinely be live, so it reflects the real
// provenance rather than a fixed string.
function _updateFlowFreshness(d){
  const el=document.getElementById('freshness-flow');
  if(!el) return;
  const txt=el.querySelector('.txt');
  if(d && d.live){
    el.className='freshness is-live';
    txt.textContent='LIVE — TastyTrade OPRA. Real-time prints with exchange-reported side.';
  } else {
    el.className='freshness is-lagged';
    txt.textContent=FRESHNESS.flow.t;
  }
}

_mountFreshness();
