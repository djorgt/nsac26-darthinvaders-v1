import test from 'node:test';
import assert from 'node:assert/strict';
import {createSimulation,parseCoordinate,PRESETS} from './sky-engine.js';
test('coordinates accept supported notation and reject malformed minutes/seconds',()=>{
 assert.equal(parseCoordinate('180.5',true),180.5);
 assert.ok(Math.abs(parseCoordinate('17h57m48.5s',true)-269.4520833)<.0001);
 assert.ok(Math.abs(parseCoordinate('-7d14m42.53s')+7.2451472)<.0001);
 for(const s of ['','wrong','17h61m0s','1:2:65','1:2:3:4','Infinity'])assert.equal(parseCoordinate(s,true),null);
 for(const p of Object.values(PRESETS)){assert.notEqual(parseCoordinate(p.ra,true),null);assert.notEqual(parseCoordinate(p.dec),null);}
});
test('simulation is deterministic and field of view changes pixel grid',()=>{
 const a=createSimulation(),b=createSimulation();a.load(269.452,4.693,6);b.load(269.452,4.693,6);
 assert.deepEqual(a.image(1,'all','flip'),b.image(1,'all','flip'));
 const small=a.image(0,'all','flip').grid;a.load(269.452,4.693,10);assert.ok(a.image(0,'all','flip').grid>small);
});
test('difference subtracts the first visit and stack averages all visits',()=>{
 const a=createSimulation();a.load(269.452,4.693,6);
 assert.ok(a.image(0,'all','diff').pixels.every(x=>x===0));
 const first=a.image(0,'all','flip').pixels,next=a.image(1,'all','flip').pixels,diff=a.image(1,'all','diff').pixels;
 assert.ok(diff.some(x=>x!==0));for(let i=0;i<diff.length;i++)assert.ok(Math.abs(diff[i]-(next[i]-first[i]))<.00001);
 const frames=[0,1,2,3].map(e=>a.image(e,'all','flip').pixels),stack=a.image(0,'all','stack').pixels;
 for(let i=0;i<stack.length;i++)assert.ok(Math.abs(stack[i]-frames.reduce((s,f)=>s+f[i]/4,0))<.00001);
 assert.ok(a.spectrum(0,'diff').every(x=>x===0));
});
