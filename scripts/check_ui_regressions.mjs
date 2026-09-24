import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root=path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const source=fs.readFileSync(path.join(root,'template.html'),'utf8');

// Pull the browser's real function declarations into a small VM rather than copying their
// logic into tests. The selected helpers contain no regex literals or braces inside strings.
function functionSource(name){
  const start=source.indexOf(`function ${name}(`);
  assert.notEqual(start,-1,`function ${name} is present`);
  const open=source.indexOf('{',start);let depth=0;
  for(let i=open;i<source.length;i++){
    if(source[i]==='{')depth++;
    else if(source[i]==='}'&&!--depth)return source.slice(start,i+1);
  }
  throw new Error(`could not parse function ${name}`);
}
function load(name,context){
  const box=vm.createContext(context);
  vm.runInContext(`${functionSource(name)};this.result=${name};`,box);
  return box.result;
}

// A date can be inside the embedded price window and still lack enough preceding bars for
// RRG/Strength warm-up. That was the fresh-link bug described in F1.
{
  const dates=Array.from({length:320},(_,i)=>String(i).padStart(3,'0'));
  const D={dates,full_len:752};
  const needHistory=()=>D.full_len>D.dates.length,STR=[130,20];
  const requestedDateIndex=load('requestedDateIndex',{D});
  const requestedDateNeedsHistory=load('requestedDateNeedsHistory',{D,needHistory,STR,requestedDateIndex});
  assert.deepEqual({...requestedDateNeedsHistory('050',Array.from({length:320},(_,i)=>i),47)}, {index:50,needed:true});
  assert.deepEqual({...requestedDateNeedsHistory('200',Array.from({length:320},(_,i)=>i),47)}, {index:200,needed:false});
  assert.deepEqual({...requestedDateNeedsHistory('zzz',[0,5,10,15,20,25,30],5)}, {index:6,needed:true});
}

// Compare must inherit the same dated membership windows as the named basket. An explicitly
// selected ticker remains an intentional all-history series.
{
  const D={px:{A:[1],SANM:[1]}},ENTRIES=[1],NODES={compute:{kind:'domain'}};
  const windows=()=>new Map([['A',[[null,null]]],['SANM',[['2026-09-23',null]]]]);
  const tokTickers=()=>['A','SANM'];
  const sideWindows=load('sideWindows',{D,ENTRIES,NODES,windows,tokTickers,Map});
  const basket=sideWindows(['compute']);
  assert.equal(JSON.stringify(basket.get('SANM')),JSON.stringify([['2026-09-23',null]]));
  assert.equal(JSON.stringify(sideWindows(['tk:SANM']).get('SANM')),JSON.stringify([[null,null]]));
}

// Strength is intentionally independent of the Daily/Weekly display frame.
{
  const px=Array.from({length:160},(_,i)=>100+i),bx=Array(160).fill(100),D={px:{QQQ:bx}};
  const strengthAt=load('strengthAt',{STR:[130,20],series:()=>px,D});
  const expected=(px[130]/bx[130])/(px[20]/bx[20])*100-100;
  assert.equal(strengthAt({},'QQQ',150),expected);
}

console.log('UI regression checks passed');
