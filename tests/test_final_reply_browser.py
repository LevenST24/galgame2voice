"""Completed SSE replies must reach the real stage once, including old format."""

import json

import pytest

from test_engine_status_browser import render_engine_case


SCRIPT = """
import {mountAdv} from '/frontend/src/adv/adv_app.js';
import {createConversation} from '/frontend/src/conversation.js';
import {DEFAULT_SESSION_SETTINGS} from '/frontend/src/store.js';
const mode=__MODE__,plays=[];
const session={id:'synthetic-final-reply',title:'测试',messages:[],settings:{...DEFAULT_SESSION_SETTINGS,
  advTextIntervalMs:0,advReplyFormat:mode==='legacy'?'legacy':'lines'}};
const state={activeId:session.id,global:{voiceMode:true}};
const line=index=>({index,zh:`终稿第${index}句。`,ja:`最終${index}。`,emotion:'neutral',act:{},
  audio:{status:'ready',chunks:[{index:0,audio_url:`/audio/final${index}.wav`}]}});
let calls=0,requestBody;
const frames=[];
if(mode==='missing-final-audio'||mode==='already-streamed')frames.push(['line',line(0)]);
if(mode==='already-streamed')frames.push(['line_audio',{line:0,...line(0).audio}]);
frames.push(['done',mode==='legacy'?{chinese:'整段终稿已显示。',japanese:'全体の回答。',total_audio_url:'/audio/whole.wav'}
  :mode==='japanese-only'?{chinese:'',japanese:'日本語だけの回答。'}
  :{chinese:'终稿第0句。'+(mode==='terminal-lines'?'终稿第1句。':''),japanese:'最終0。',
    lines:mode==='terminal-lines'?[line(0),line(1)]:[line(0)]}]);
globalThis.fetch=async(url,options={})=>{
  if(url==='/api/chat/stream'){
    calls++;requestBody=JSON.parse(options.body);
    return new Response(frames.map(([event,data])=>`event: ${event}\\ndata: ${JSON.stringify(data)}\\n\\n`).join(''),
      {headers:{'Content-Type':'text/event-stream'}});
  }
  return Response.json({});
};
let serial=0;
const controller=createConversation({state,getActive:()=>session,getSession:()=>session,
  addMessage(role,content){const msg={id:'m'+(++serial),role,content,ts:Date.now()};session.messages.push(msg);return msg;},
  uid:prefix=>prefix+(++serial),saveState(){},persistSession:async()=>{},isSessionDeleting:()=>false,
  audioStore:{set(){}},putCachedAudioBlob(){},fetchAndCacheAudio:async()=>{},audioPlayer:{isPlaying:false},
  getCurrentVoice:()=>null,stopCurrentVoice(){},fetch:globalThis.fetch,
});
const host=document.createElement('div');document.body.replaceChildren(host);
const dom={root:host};
for(const key of ['sidebar','messages','input','micBtn'])dom[key]=document.createElement(key==='input'?'textarea':'div');
const audio={supportsStartNotification:true,playLine(index,chunks,options){
  return new Promise(resolve=>plays.push({index,chunks,options,resolve}));
},stopLine(){plays.at(-1)?.resolve('interrupted');}};
const app=mountAdv(dom,{controller,audio,getSession:()=>session,voiceEnabled:()=>true,
  getCharacter:()=>({id:1,name:'测试角色'}),loadPortrait:async()=>null,
  saveSettings:patch=>Object.assign(session.settings,patch),subscribe:()=>()=>{},
});
const result={};
try{
  controller.send('模拟请求');
  for(let i=0;i<100&&controller.busy;i++)await new Promise(resolve=>setTimeout(resolve,10));
  if(controller.busy)throw new Error('Completion did not settle');
  const primary=host.querySelector('.adv-primary-text');
  const stored=session.messages.at(-1);
  if(stored.role!=='assistant')throw new Error('Completed reply was not stored');
  if(mode==='japanese-only'){
    if(!primary.textContent.includes('日本語だけの回答'))throw new Error('Japanese-only reply is absent');
  }else{
    if(plays.length!==1)throw new Error('First sentence played '+plays.length+' times');
    if(primary.textContent)throw new Error('Text appeared before playback start');
    plays[0].options.onStart();
    if(!primary.textContent.includes(mode==='legacy'?'整段终稿':'终稿第0句'))throw new Error('First completed sentence is absent');
  }
  if(mode==='terminal-lines'){
    app.action('advance');
    if(plays.length!==2||plays[1].index!==1)throw new Error('Second final sentence is absent');
    plays[1].options.onStart();
    if(!primary.textContent.includes('终稿第1句'))throw new Error('Second final sentence was not revealed');
  }
  if(mode==='legacy'&&stored.lines!==undefined)throw new Error('Synthetic history line identity was persisted');
  if(calls!==1)throw new Error('A request was resubmitted');
  result.completeReplyVisible=true;result.requestCalls=calls;result.playCalls=plays.length;
  result.replyFormat=requestBody.reply_format;result.storedJapanese=stored.japanese;
  const rect=app.stage.stage.getBoundingClientRect();
  result.left=rect.left;result.right=rect.right;result.viewport=innerWidth;
  result.overflow=document.documentElement.scrollWidth-innerWidth;
}catch(error){result.error=String(error.stack||error);}
// Settle synthetic playback while retaining the final stage for screenshot QA.
finally{for(const play of plays)play.resolve('ended');}
const output=document.createElement('pre');output.id='engine-result';output.hidden=true;
output.textContent=JSON.stringify(result);parent.document.body.append(output);
"""


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
@pytest.mark.parametrize("mode", ["legacy", "terminal-lines", "missing-final-audio", "japanese-only", "already-streamed"])
def test_completed_reply_reaches_the_stage_once(tmp_path, width, height, mode):
    data = render_engine_case(tmp_path, width, height, SCRIPT.replace("__MODE__", json.dumps(mode)), 3000)
    assert "error" not in data, data
    assert data["completeReplyVisible"] is True
    assert data["requestCalls"] == 1
    assert data["playCalls"] == (2 if mode == "terminal-lines" else 0 if mode == "japanese-only" else 1)
    assert data["replyFormat"] == ("old" if mode == "legacy" else "lines")


@pytest.mark.requires_piped_subprocess
@pytest.mark.parametrize("width,height", [(1280, 900)])
def test_listener_failure_does_not_interrupt_reply_or_stage(tmp_path, width, height):
    script = SCRIPT.replace("__MODE__", json.dumps("already-streamed")).replace(
        "const host=document.createElement('div');",
        "for(const type of ['turnstart','text','line','line_audio','done']) "
        "controller.on(type,()=>{throw new Error('synthetic listener failure');});\n"
        "const host=document.createElement('div');",
    )
    data = render_engine_case(tmp_path, width, height, script, 3000)
    assert "error" not in data, data
    assert data["completeReplyVisible"] is True
    assert data["requestCalls"] == 1
    assert data["playCalls"] == 1
