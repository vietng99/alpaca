(function(root,factory){if(typeof module==='object'&&module.exports)module.exports=factory();else root.ArthurGeometry=factory();})(globalThis,function(){
'use strict';
function sourceSize(a,w,h){return a.sourceSize||{width:a.frames.length*w,height:h};}
function frameRect(a,i,w,h){return a.frameRects?a.frameRects[i]:{x:i*w,y:0,width:w,height:h};}
function destinationRect(a,w,h){return a.destinationRect||{x:0,y:0,width:w,height:h};}
function valid(r){return r&&['x','y','width','height'].every(k=>Number.isFinite(r[k]))&&r.x>=0&&r.y>=0&&r.width>0&&r.height>0;}
function validate(a,w,h){
const size=sourceSize(a,w,h);
if(!Number.isInteger(size.width)||!Number.isInteger(size.height)||size.width<=0||size.height<=0)throw new Error('Invalid source size');
if(a.frameRects&&a.frameRects.length!==a.frames.length)throw new Error('Frame rect count must match playback frames');
for(let i=0;i<a.frames.length;i++){
const r=frameRect(a,i,w,h);if(!valid(r))throw new Error('Invalid source rect');
if(r.x+r.width>size.width||r.y+r.height>size.height)throw new Error('Source rect exceeds image bounds');
}
const d=destinationRect(a,w,h);
if(!valid(d)||d.x+d.width>w+1e-8||d.y+d.height>h+1e-8)throw new Error('Invalid destination rectangle');
return true;
}
return {sourceSize,frameRect,destinationRect,validate};
});