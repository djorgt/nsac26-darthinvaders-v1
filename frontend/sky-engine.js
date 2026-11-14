
export const EPOCHS=["2025-05-20","2025-11-18","2026-05-19","2026-11-03"];
export const BANDS=[[0.75,1.11],[1.11,1.64],[1.64,2.42],[2.42,3.82],[3.82,4.42],[4.42,5.00]];
const PIX=6.2;
export const PRESETS={
  barnard:{ra:"17h57m48.5s",dec:"+4d41m36s",raD:269.452,decD:4.693,f:45,pm:[-0.13,1.67],prof:[0.7,1,0.8,0.45,0.3,0.25]},
  epsindi:{ra:"22h04m10.5s",dec:"-56d46m58s",raD:331.044,decD:-56.783,f:22,pm:[0.64,-0.40],prof:[0.05,0.3,0.35,0.2,0.6,0.9],companion:true},
  wise0855:{ra:"08h55m10.831s",dec:"-7d14m42.53s",raD:133.795,decD:-7.245,f:20,pm:[-1.31,0.11],prof:[0,0.01,0.02,0.05,0.4,1]},
  rwaur:{ra:"05h07m49.5s",dec:"+30d24m05s",raD:76.956,decD:30.401,f:35,pm:[0,0],prof:[0.6,0.8,0.9,1,1,0.9],vary:[1,0.45,1.25,0.7]},
  rdra:{ra:"16h32m40.2s",dec:"+66d45m18s",raD:248.168,decD:66.755,f:40,pm:[0,0],prof:[0.4,0.9,1,0.8,0.6,0.5],vary:[1.5,0.45,1.1,0.35]}
};
export function parseCoordinate(value,isRA=false){
 const s=value.trim(); if(!s) return null;
 if(/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(s)) { const n=Number(s); return Number.isFinite(n)?n:null; }
 if(!/^[+-]?\d+(?:\.\d+)?(?:[hd:°\s]+\d+(?:\.\d+)?)(?:[m:′'\s]+\d+(?:\.\d+)?)?[s″"]?$/.test(s)) return null;
 const p=s.replace(/^[+-]/,'').split(/[hdms:°′'″"\s]+/).filter(Boolean).map(Number);
 if(p.length<2||p.length>3||p.some(n=>!Number.isFinite(n))||p[1]>=60||(p[2]||0)>=60)return null;
 const n=(p[0]+p[1]/60+(p[2]||0)/3600)*(isRA?15:1)*(s.startsWith('-')?-1:1);return n;
}

export function createSimulation(){
const S={band:"all",mode:"flip",epoch:0,playing:false,timer:null,cache:new Map(),stars:[],grid:58,seed:1,target:null};

function rng32(a){return function(){a|=0;a=a+0x6D2B79F5|0;let t=Math.imul(a^a>>>15,1|a);t=t+Math.imul(t^t>>>7,61|t)^t;return((t^t>>>14)>>>0)/4294967296}}
function gauss(r){let u=0;while(u===0)u=r();return Math.sqrt(-2*Math.log(u))*Math.cos(2*Math.PI*r())}
function years(e){return(new Date(EPOCHS[e])-new Date(EPOCHS[0]))/(365.25*864e5)}

function buildField(ra,dec,fov){
  S.seed=(Math.round(ra*1000)*73856093)^(Math.round(dec*1000)*19349663);
  const r=rng32(S.seed);
  const g=Math.round(fov*60/PIX);S.grid=g;
  const stars=[];const n=Math.round(g*g*0.025);
  for(let i=0;i<n;i++)stars.push({x:r()*g,y:r()*g,f:0.4*Math.exp(r()*3.4),s:(r()*2-1)*1.2,pm:[0,0]});
  stars.push({x:r()*g,y:r()*g,f:9,prof:[0.5,0.6,0.7,0.8,1,1],pm:[0,0],only:1+Math.floor(r()*3)});
  S.target=null;
  for(const k in PRESETS){
    const p=PRESETS[k];
    if(Math.abs(p.decD-dec)<0.05&&Math.abs((p.raD-ra)*Math.cos(dec*Math.PI/180))<0.05){
      S.target={x:g/2,y:g/2,f:p.f,prof:p.prof,pm:p.pm,vary:p.vary,companion:p.companion};
      stars.push(S.target);break;
    }
  }
  S.stars=stars;S.cache.clear();
}

function bandFlux(st,b,e){
  let v=st.prof?st.f*st.prof[b]:st.f*Math.exp(st.s*(b-2.5)/2.5);
  if(st.vary)v*=st.vary[e];
  return v;
}
function starPos(st,e){const t=years(e);return[st.x-st.pm[0]*t,st.y-st.pm[1]*t]}
function addPSF(img,g,px,py,flux){
  const sg=0.9,amp=flux/(2*Math.PI*sg*sg);
  for(let y=Math.floor(py)-3;y<=Math.floor(py)+3;y++){
    if(y<0||y>=g)continue;
    for(let x=Math.floor(px)-3;x<=Math.floor(px)+3;x++){
      if(x<0||x>=g)continue;
      const dx=x+0.5-px,dy=y+0.5-py;
      img[y*g+x]+=amp*Math.exp(-(dx*dx+dy*dy)/(2*sg*sg));
    }
  }
}
function frame(e,bk){
  const key=e+"|"+bk;if(S.cache.has(key))return S.cache.get(key);
  const g=S.grid,img=new Float32Array(g*g);
  const bands=bk==="all"?[0,1,2,3,4,5]:[+bk];
  for(const st of S.stars){
    if(st.only!=null&&st.only!==e)continue;
    let f=0;for(const b of bands)f+=bandFlux(st,b,e);f/=bands.length;
    const[px,py]=starPos(st,e);
    addPSF(img,g,px,py,f);
    if(st.companion)addPSF(img,g,px+0.6,py+0.35,f*0.6);
  }
  const r=rng32(S.seed^(e*7919+(bk==="all"?97:(+bk+1)*131)));
  const sig=1/Math.sqrt(bands.length);
  for(let i=0;i<img.length;i++)img[i]+=sig*gauss(r);
  S.cache.set(key,img);return img;
}
function currentImage(){
  const bk=S.band,g=S.grid;
  if(S.mode==="stack"){
    const out=new Float32Array(g*g);
    for(let e=0;e<4;e++){const f=frame(e,bk);for(let i=0;i<out.length;i++)out[i]+=f[i]/4}
    return out;
  }
  if(S.mode==="diff"){
    const a=frame(S.epoch,bk),b=frame(0,bk),out=new Float32Array(g*g);
    for(let i=0;i<out.length;i++)out[i]=a[i]-b[i];
    return out;
  }
  return frame(S.epoch,bk);
}


 return {
   load(ra,dec,fov){buildField(ra,dec,fov)},
   image(epoch,band,mode){S.epoch=epoch;S.band=band;S.mode=mode;return {pixels:currentImage(),grid:S.grid}},
   spectrum(epoch,mode){
     const epochs=mode==='stack'?[0,1,2,3]:[epoch];
     return BANDS.map((_,b)=>{
       const sample=(e)=>{const f=frame(e,String(b));const [cx,cy]=S.target?starPos(S.target,e):[S.grid/2,S.grid/2];let total=0;
       for(let y=Math.floor(cy)-2;y<=Math.floor(cy)+2;y++)for(let x=Math.floor(cx)-2;x<=Math.floor(cx)+2;x++)if(x>=0&&y>=0&&x<S.grid&&y<S.grid)total+=f[y*S.grid+x];return total;};
       const sum=epochs.reduce((a,e)=>a+sample(e),0)/epochs.length;
       return mode==='diff'?sum-sample(0):sum;
     });
   }
 };
}
