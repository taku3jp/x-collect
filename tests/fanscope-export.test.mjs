import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const source=readFileSync('gas/Code.js','utf8');
const companion=readFileSync('gas/FanScopeExport.js','utf8');
function harness(){
 const effects=[];
 const blocked=name=>()=>{effects.push(name);throw Error('unexpected '+name);};
 const context=vm.createContext({
  PropertiesService:{getScriptProperties:()=>({getProperty:()=> 'test-token'})},
  SpreadsheetApp:{getActiveSpreadsheet:()=>({getSheetByName:()=>({
   getLastRow:()=>2,getRange:()=>({getValues:()=>[[1,'2026/09/20 12:34','https://x.com/example/status/1234567890123456789','PRIVATE TEXT','PRIVATE MEDIA','','',300000,0,7,9]]})
  })})},
  LockService:{getScriptLock:blocked('lock')},
  ContentService:{MimeType:{JSON:'application/json'},createTextOutput:text=>({setMimeType:()=>({text})})}
 });
 vm.runInContext(source+'\n'+companion,context);
 return {context,effects};
}
test('capability route responds before auth/init and returns no data',()=>{
 const {context,effects}=harness();
 const result=context.doGet({parameter:{action:'fanscope_export_capability'}});
 assert.deepEqual(JSON.parse(result.text),{version:1,source:'taku3jp/x-collect',capability:'fanscope_export'});
 assert.deepEqual(effects,[]);
});
test('export routes before locks/writes and omits content',()=>{
 const {context,effects}=harness();
 const result=context.doPost({postData:{contents:JSON.stringify({action:'fanscope_export',token:'test-token'})}});
 assert.deepEqual(JSON.parse(result.text),{version:1,source:'taku3jp/x-collect',rows:[{url:'https://x.com/example/status/1234567890123456789',date:'2026/09/20 12:34',impressions:300000,likes:null,reposts:7,bookmarks:9}]});
 assert.deepEqual(effects,[]);
});
test('export rejects an invalid token without mutations',()=>{
 const {context,effects}=harness();
 const result=context.doPost({postData:{contents:JSON.stringify({action:'fanscope_export',token:'wrong'})}});
 assert.deepEqual(JSON.parse(result.text),{error:'invalid token'});
 assert.deepEqual(effects,[]);
});
