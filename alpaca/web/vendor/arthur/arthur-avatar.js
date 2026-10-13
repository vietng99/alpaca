/* Arthur reusable canvas avatar. No imports or external runtime dependencies. */
(function(root,factory){
  if(typeof module==="object"&&module.exports) module.exports=factory(root,require("./arthur-core.js"),require("./arthur-geometry.js"));
  else root.ArthurAvatar=factory(root,root.ArthurCore,root.ArthurGeometry);
})(globalThis,function(root,Core,Geometry){
  "use strict";
  const scriptURL=root.document?.currentScript?.src;
  const defaultAssetBase=scriptURL?new URL("../assets/",scriptURL).href:null;

  class ArthurAvatar{
    static async create(container,options={}){
      const instance=new ArthurAvatar(container,options);
      await instance.ready;
      return instance;
    }
    constructor(container,options={}){
      if(!Core||!Geometry) throw new Error("Load arthur-core.js and arthur-geometry.js before arthur-avatar.js");
      if(!container?.ownerDocument||typeof container.appendChild!=="function")
        throw new Error("Pass a DOM container element");
      this.container=container;
      this.document=container.ownerDocument;
      this.window=this.document.defaultView;
      this.manifest=options.manifest??root.ArthurAssets;
      const m=this.manifest;
      if(!m||!m.actions||m.gaze?.frames?.length!==16||!Number.isInteger(m.width)||!Number.isInteger(m.height)||m.width<=0||m.height<=0)
        throw new Error("Invalid Arthur asset manifest");
      this._player=new Core.AnimationPlayer(m.actions,m.neutralAction);
      this._options={...m.gaze,...options.gaze,origin:{...m.gaze.origin,...options.gaze?.origin}};
      if(!(this._options.smoothingMs>0)||!(this._options.deadZone>=0)||
         !(this._options.hysteresisDegrees>=0&&this._options.hysteresisDegrees<11.25)||
         !["x","y"].every(k=>Number.isFinite(this._options.origin[k])&&this._options.origin[k]>=0&&this._options.origin[k]<=1))
        throw new Error("Invalid gaze options");
      this._gazeEnabled=options.gazeEnabled??true;
      if(typeof this._gazeEnabled!=="boolean") throw new Error("gazeEnabled must be a boolean");
      this._motionOverride=options.reducedMotion??"system";
      if(this._motionOverride!=="system"&&typeof this._motionOverride!=="boolean")
        throw new Error("reducedMotion must be true, false, or system");
      this._media=this.window.matchMedia("(prefers-reduced-motion: reduce)");
      this._reducedMotion=this._motionOverride==="system"?this._media.matches:this._motionOverride;
      this._images=new Map();this._pendingLoads=new Set();this._loads=new Map();
      this._cleanups=[];
      this._loaded=false;this._destroyed=false;this._paused=false;this._hidden=this.document.hidden;
      this._actionRevision=0;this._raf=0;this._lastTime=null;this._point=null;this._gaze=null;this._frameIndex=0;
      this._renderKey=null;this._renderedKind=null;this._renderedFrameIndex=null;
      let base=options.assetBaseURL??defaultAssetBase??new URL("assets/",this.document.baseURI).href;
      if(!String(base).endsWith("/")) base=String(base)+"/";
      this.assetBaseURL=new URL(base,this.document.baseURI).href;
      this.element=this.document.createElement("span");
      this.element.className="arthur-avatar";
      if(options.size!==undefined){
        if(!Number.isFinite(options.size)||options.size<=0) throw new Error("size must be positive");
        this.element.style.width=options.size+"px";
      }
      this.canvas=this.document.createElement("canvas");
      this.canvas.width=m.width;this.canvas.height=m.height;
      this.canvas.setAttribute("role","img");
      this.canvas.setAttribute("aria-label",options.label??"Arthur the alpaca");
      this.element.appendChild(this.canvas);
      this._ctx=this.canvas.getContext("2d");
      if(!this._ctx) throw new Error("Canvas 2D is unavailable");
      container.appendChild(this.element);
      this._tick=this._tick.bind(this);
      const pointerTarget=options.pointerTarget??this.document.documentElement;
      this._listen(pointerTarget,"pointermove",event=>{
        if(event.pointerType==="touch") return;
        this._point={x:event.clientX,y:event.clientY};
        this._wake();
      },{passive:true});
      this._listen(pointerTarget,"pointerleave",()=>this._neutral());
      this._listen(this.window,"blur",()=>this._neutral());
      this._listen(this.document,"visibilitychange",()=>{
        this._hidden=this.document.hidden;this._stopClock();
        if(this._hidden) this._neutral();else this._wake();
      });
      this._listen(this._media,"change",()=>{
        if(this._motionOverride==="system") this._applyMotion(this._media.matches);
      });
      this.ready=this._loadImages().then(()=>{
        if(this._destroyed) return this;
        this._loaded=true;this._draw();
        this._emit("ready",this.getState());
        this._wake();return this;
      }).catch(error=>{
        if(!this._destroyed) this._emit("error",{message:error.message});
        this.destroy();throw error;
      });
    }
    _listen(target,name,handler,options){
      target.addEventListener(name,handler,options);
      this._cleanups.push(()=>target.removeEventListener(name,handler,options));
    }
    async _loadImages(){
      const m=this.manifest;
      await this._load(m.actions[this._resolveAction(m.neutralAction)].image);
    }
    _load(path){
      if(this._loads.has(path)) return this._loads.get(path);
      const load=new Promise((resolve,reject)=>{
        const image=new this.window.Image();
        const finish=error=>{
          image.onload=null;image.onerror=null;this._pendingLoads.delete(cancel);
          if(error) reject(error);else resolve();
        };
        const cancel=()=>{
          image.onload=null;image.onerror=null;image.removeAttribute("src");
          this._pendingLoads.delete(cancel);resolve();
        };
        this._pendingLoads.add(cancel);
        image.onload=()=>{
          if(this._destroyed) return finish();
          try{this._check(path,image);}catch(error){return finish(error);}
          this._images.set(path,image);
          finish();
        };
        image.onerror=()=>finish(new Error("Could not load Arthur asset: "+path));
        image.src=new URL(path,this.assetBaseURL).href;
      });
      this._loads.set(path,load);
      return load;
    }
    _check(path,image){
      const m=this.manifest;
      if(path===m.atlas.image&&(image.naturalWidth!==m.atlas.width||image.naturalHeight!==m.atlas.height))
        throw new Error("Arthur atlas dimensions do not match its manifest");
      for(const [name,action] of Object.entries(m.actions)){
        if(action.image!==path) continue;
        Geometry.validate(action,m.width,m.height);
        const size=Geometry.sourceSize(action,m.width,m.height);
        if(image.naturalWidth!==size.width||image.naturalHeight!==size.height)
          throw new Error("Arthur strip dimensions do not match: "+name);
      }
    }
    _ready(path){
      if(this._images.has(path)) return true;
      if(!this._loads.has(path)) this._want(path);
      return false;
    }
    _want(path){
      this._load(path).then(()=>this._draw(),()=>{setTimeout(()=>this._loads.delete(path),5000);});
    }
    _assertAlive(){if(this._destroyed) throw new Error("Arthur avatar has been destroyed");}
    _resolveAction(name){return Object.hasOwn(this.manifest.aliases,name)?this.manifest.aliases[name]:name;}
    setAction(name,options={}){
      this._assertAlive();
      const previous=this._player.action;
      const target=this._resolveAction(name);
      const playback={...options};
      if(playback.returnTo!==undefined) playback.returnTo=this._resolveAction(playback.returnTo);
      this._player.setAction(target,playback);
      this._actionRevision++;
      this._frameIndex=0;this._gaze=null;this._lastTime=null;
      this._draw();
      this._emit("actionchange",{previous,action:target,requested:name,reason:"api"});
      this._wake();return this;
    }
    setGaze(enabled){
      this._assertAlive();
      if(typeof enabled!=="boolean") throw new Error("setGaze expects a boolean");
      this._gazeEnabled=enabled;this._gaze=null;
      this._draw();this._wake();return this;
    }
    setReducedMotion(value="system"){
      this._assertAlive();
      if(value!=="system"&&typeof value!=="boolean") throw new Error("Use true, false, or system");
      this._motionOverride=value;
      this._applyMotion(value==="system"?this._media.matches:value);
      return this;
    }
    _applyMotion(reduced){
      if(reduced===this._reducedMotion) return;
      this._reducedMotion=reduced;this._gaze=null;this._stopClock();
      this._frameIndex=this._player.advance(0,reduced);
      this._draw();this._emit("motionchange",{reducedMotion:reduced,mode:this._motionOverride});
      this._wake();
    }
    pause(){this._assertAlive();if(!this._paused){this._paused=true;this._stopClock();this._emit("playbackchange",{paused:true});}return this;}
    resume(){this._assertAlive();if(this._paused){this._paused=false;this._lastTime=null;this._emit("playbackchange",{paused:false});}this._wake();return this;}
    _neutral(){this._point=null;this._gaze=null;this._draw();}
    _stopClock(){
      if(this._raf) this.window.cancelAnimationFrame(this._raf);
      this._raf=0;this._lastTime=null;
    }
    _wake(){
      if(this._loaded&&!this._destroyed&&!this._paused&&!this._hidden&&!this._reducedMotion&&!this._raf)
        this._raf=this.window.requestAnimationFrame(this._tick);
    }
    _tick(time){
      this._raf=0;
      if(this._destroyed||this._paused||this._hidden) return;
      const elapsed=this._lastTime===null?0:Math.max(0,time-this._lastTime);
      this._lastTime=time;
      const previous=this._player.action;
      this._frameIndex=this._player.advance(elapsed,this._reducedMotion);
      const completion=this._player.completion;
      if(this._gazeEnabled&&this._point&&!this._reducedMotion&&Core.canUseGaze(this._player.action,this.manifest.neutralAction)){
        const rect=this.canvas.getBoundingClientRect();
        this._gaze=Core.updateGaze(this._gaze,{
          x:this._point.x-rect.left-rect.width*this._options.origin.x,
          y:this._point.y-rect.top-rect.height*this._options.origin.y
        },elapsed,this._options);
      }else this._gaze=null;
      const transitionAction=this._player.action,transitionRevision=this._actionRevision;
      this._draw();
      if(completion){
        if(!this._destroyed&&this._actionRevision===transitionRevision&&previous!==transitionAction)
          this._emit("actionchange",{previous,action:transitionAction,reason:"complete"});
        this._emit("complete",{...completion,revision:transitionRevision});
      }
      this._wake();
    }
    _draw(){
      if(!this._loaded||this._destroyed) return;
      const m=this.manifest;
      const useGaze=this._gazeEnabled&&!this._reducedMotion&&this._gaze&&Core.canUseGaze(this._player.action,m.neutralAction)&&this._ready(m.atlas.image);
      const kind=useGaze?"gaze":"action";
      const index=useGaze?this._gaze.index:this._frameIndex;
      const key=kind+":"+this._player.action+":"+index;
      if(key===this._renderKey) return;
      const previousGaze=this._renderedKind==="gaze"?this._renderedFrameIndex:null;
      let image,sx,sy,sw=m.width,sh=m.height;
      let destination={x:0,y:0,width:m.width,height:m.height};
      if(useGaze){
        const frame=m.gaze.frames[index];
        image=this._images.get(m.atlas.image);sx=frame.x;sy=frame.y;
      }else{
        const action=m.actions[this._player.action];
        image=this._images.get(action.image);
        if(!image){this._ready(action.image);return;}
        const rect=Geometry.frameRect(action,index,m.width,m.height);
        sx=rect.x;sy=rect.y;sw=rect.width;sh=rect.height;
        destination=Geometry.destinationRect(action,m.width,m.height);
      }
      this._ctx.clearRect(0,0,m.width,m.height);
      this._ctx.drawImage(image,sx,sy,sw,sh,destination.x,destination.y,destination.width,destination.height);
      this._renderKey=key;this._renderedKind=kind;this._renderedFrameIndex=index;
      const nextGaze=kind==="gaze"?index:null;
      if(previousGaze!==nextGaze) this._emit("gazechange",{index:nextGaze,angle:nextGaze===null?null:m.gaze.frames[nextGaze].angle});
    }
    _emit(name,detail){
      if(this._destroyed&&name!=="destroy") return;
      this.container.dispatchEvent(new this.window.CustomEvent("arthur:"+name,{detail,bubbles:true}));
    }
    getState(){
      return {
        ready:this._loaded,destroyed:this._destroyed,action:this._player.action,actionRevision:this._actionRevision,
        frameIndex:this._frameIndex,loop:this._player.loop,returnTo:this._player.returnTo,
        paused:this._paused,gazeEnabled:this._gazeEnabled,reducedMotion:this._reducedMotion,
        gazeIndex:this._renderedKind==="gaze"?this._renderedFrameIndex:null,
        gazeAngle:this._renderedKind==="gaze"?this._gaze?.angle??null:null,
        renderedKind:this._renderedKind,renderedFrameIndex:this._renderedFrameIndex
      };
    }
    destroy(){
      if(this._destroyed) return;
      this._destroyed=true;this._paused=true;this._stopClock();
      for(const cleanup of this._cleanups.splice(0)) cleanup();
      for(const cancel of [...this._pendingLoads]) cancel();
      this.element.remove();this._images.clear();
      this._emit("destroy",this.getState());
    }
  }
  return ArthurAvatar;
});
