// Orthogonal routes through free space between task cards and sector headers.
// One visibility grid is shared by all edges in a draw, including folded groups.
const clearance=8, padding=4;
const distance=(a,b)=>Math.abs(a.x-b.x)+Math.abs(a.y-b.y);
function ports(r){
 const side=(x,y,dx,dy)=>({x:x+dx*clearance,y:y+dy*clearance,point:[x,y],axis:dx?0:1});
 return r.folded?[side(r.left,r.y,-1,0)]:[
  side(r.right,r.y,1,0),side(r.left,r.y,-1,0),side(r.x,r.bottom,0,1),side(r.x,r.top,0,-1)
 ];
}
class Queue{
 items=[];
 push(item){let i=this.items.length;this.items.push(item);while(i){const parent=(i-1)>>1;if(this.items[parent].score<=item.score)break;this.items[i]=this.items[parent];i=parent;}this.items[i]=item;}
 pop(){const first=this.items[0],last=this.items.pop();if(this.items.length){let i=0;while(i*2+1<this.items.length){let child=i*2+1;if(child+1<this.items.length&&this.items[child+1].score<this.items[child].score)child++;if(this.items[child].score>=last.score)break;this.items[i]=this.items[child];i=child;}this.items[i]=last;}return first;}
}
function simplify(points){
 const result=[];
 for(const p of points){
  const last=result.at(-1);if(last&&last[0]===p[0]&&last[1]===p[1])continue;
  while(result.length>1){const a=result.at(-2),b=result.at(-1);if((a[0]===b[0]&&b[0]===p[0])||(a[1]===b[1]&&b[1]===p[1]))result.pop();else break;}
  result.push(p);
 }
 return result;
}
export function createRouter(rectangles,endpoints,width,height){
 const obstacles=rectangles.map(r=>({left:r.left-padding,right:r.right+padding,top:r.top-padding,bottom:r.bottom+padding}));
 const endpointPorts=new Map(endpoints.map(e=>[e.key,ports(e)]));
 const xs=new Set([1,width-1]),ys=new Set([1,height-1]);
 for(const r of rectangles){xs.add(r.left-clearance);xs.add(r.right+clearance);ys.add(r.top-clearance);ys.add(r.bottom+clearance);}
 for(const list of endpointPorts.values())for(const p of list){xs.add(p.x);ys.add(p.y);}
 const x=[...xs].filter(v=>v>=0&&v<=width).sort((a,b)=>a-b),y=[...ys].filter(v=>v>=0&&v<=height).sort((a,b)=>a-b);
 const xIndex=new Map(x.map((v,i)=>[v,i])),yIndex=new Map(y.map((v,i)=>[v,i])),columns=x.length;
 const id=p=>yIndex.has(p.y)&&xIndex.has(p.x)?yIndex.get(p.y)*columns+xIndex.get(p.x):-1;
 const point=i=>({x:x[i%columns],y:y[Math.floor(i/columns)]});
 const clear=(a,b)=>!obstacles.some(r=>a.y===b.y?
  a.y>r.top&&a.y<r.bottom&&Math.max(a.x,b.x)>r.left&&Math.min(a.x,b.x)<r.right:
  a.x>r.left&&a.x<r.right&&Math.max(a.y,b.y)>r.top&&Math.min(a.y,b.y)<r.bottom);
 const neighbors=new Map();
 function adjacent(i){
  if(neighbors.has(i))return neighbors.get(i);
  const row=Math.floor(i/columns),column=i%columns,a=point(i),links=[];
  for(const [j,axis] of [[column?i-1:-1,0],[column+1<columns?i+1:-1,0],[row?i-columns:-1,1],[row+1<y.length?i+columns:-1,1]]){
   if(j<0)continue;const b=point(j);if(clear(a,b))links.push({id:j,axis,length:distance(a,b)});
  }
  neighbors.set(i,links);return links;
 }
 return (from,to)=>{
  const starts=endpointPorts.get(from.key).filter(p=>id(p)>=0),ends=endpointPorts.get(to.key).filter(p=>id(p)>=0);
  const goals=new Map(ends.map(p=>[id(p),p]));
  const heuristic=p=>Math.min(...ends.map(q=>distance(p,q)));
  const queue=new Queue(),cost=new Map(),previous=new Map(),origins=new Map();
  for(const start of starts){const state=id(start)*2+start.axis;cost.set(state,0);origins.set(state,start);queue.push({state,g:0,score:heuristic(start)});}
  while(queue.items.length){
   const current=queue.pop(),state=current.state;if(current.g!==cost.get(state))continue;
   const i=Math.floor(state/2),end=goals.get(i);
   if(end){
    let cursor=state;const route=[];
    while(true){const p=point(Math.floor(cursor/2));route.push([p.x,p.y]);if(!previous.has(cursor))break;cursor=previous.get(cursor);}
    route.reverse();return simplify([origins.get(cursor).point,...route,end.point]);
   }
   for(const next of adjacent(i)){
    const nextState=next.id*2+next.axis,g=current.g+next.length+(state%2===next.axis?0:12);
    if(g>=(cost.get(nextState)??Infinity))continue;
    cost.set(nextState,g);previous.set(nextState,state);queue.push({state:nextState,g,score:g+heuristic(point(next.id))});
   }
  }
  return null;
 };
}
