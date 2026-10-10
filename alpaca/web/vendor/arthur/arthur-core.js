/* Arthur avatar mechanics. Browser global + CommonJS, no runtime dependencies. */
(function(root,factory){
  if(typeof module==="object"&&module.exports) module.exports=factory();
  else root.ArthurCore=factory();
})(globalThis,function(){
  "use strict";
  const normalizeAngle=a=>((a%360)+360)%360;
  const shortestDelta=(from,to)=>((to-from+540)%360+360)%360-180;
  function angleFromPoint(x,y){
    if(x===0&&y===0) return null;
    return normalizeAngle(Math.atan2(x,-y)*180/Math.PI);
  }
  const angleToIndex=a=>Math.round(normalizeAngle(a)/22.5)%16;
  function chooseSector(angle,previous,margin=3){
    if(previous!==null&&previous!==undefined&&Math.abs(shortestDelta(previous*22.5,angle))<=11.25+margin)
      return previous;
    return angleToIndex(angle);
  }
  function updateGaze(previous,point,elapsedMs,options={}){
    if(!point||options.reducedMotion||Math.hypot(point.x,point.y)<(options.deadZone??12)) return null;
    const target=angleFromPoint(point.x,point.y);
    const angle=previous ? normalizeAngle(previous.angle+shortestDelta(previous.angle,target)*
      (1-Math.exp(-Math.max(0,elapsedMs)/(options.smoothingMs??90)))) : target;
    return {angle,index:chooseSector(angle,previous?.index,options.hysteresisDegrees??3)};
  }
  const canUseGaze=(action,neutralAction="idle")=>action===neutralAction;

  class AnimationPlayer{
    constructor(actions,neutralAction="idle"){
      this.actions=actions;
      this.neutralAction=neutralAction;
      this.setAction(neutralAction);
    }
    setAction(name,options={}){
      const action=Object.hasOwn(this.actions,name)?this.actions[name]:null;
      if(!action) throw new Error("Unknown action: "+name);
      if(!Array.isArray(action.frames)||action.frames.length===0)
        throw new Error("Invalid animation frames: "+name);
      const durations=action.durationsMs??action.frames.map(()=>1000/action.fps);
      if(durations.length!==action.frames.length||!durations.every(d=>Number.isFinite(d)&&d>0))
        throw new Error("Invalid frame duration: "+name);
      const returnTo=options.returnTo??this.neutralAction;
      if(!Object.hasOwn(this.actions,returnTo)) throw new Error("Unknown action: "+returnTo);
      if(options.loop!==undefined&&typeof options.loop!=="boolean")
        throw new Error("loop must be a boolean");
      this.action=name;
      this.elapsedMs=0;
      this.durations=durations;
      this.durationMs=durations.reduce((sum,d)=>sum+d,0);
      this.loop=options.loop??Boolean(action.loop);
      this.returnTo=returnTo;
      this.finished=false;
      this.completion=null;
    }
    advance(elapsedMs,reducedMotion=false){
      if(!Number.isFinite(elapsedMs)) throw new Error("Playback delta must be finite");
      this.completion=null;
      if(reducedMotion) return this.actions[this.action].frames[0];
      this.elapsedMs+=Math.max(0,elapsedMs);
      if(!this.loop&&!this.finished&&this.elapsedMs>=this.durationMs){
        const oldAction=this.action,returnTo=this.returnTo;
        const remainder=this.elapsedMs-this.durationMs;
        if(returnTo!==oldAction){
          this.setAction(returnTo);
          this.elapsedMs=remainder;
        }else{
          this.finished=true;
          this.elapsedMs=this.durationMs;
        }
        this.completion={action:oldAction,returnTo};
      }
      const frames=this.actions[this.action].frames;
      let time=this.loop?this.elapsedMs%this.durationMs:this.elapsedMs;
      for(let i=0;i<frames.length;i++){
        if(time<this.durations[i]) return frames[i];
        time-=this.durations[i];
      }
      return frames[frames.length-1];
    }
  }
  return {normalizeAngle,shortestDelta,angleFromPoint,angleToIndex,chooseSector,updateGaze,canUseGaze,AnimationPlayer};
});
