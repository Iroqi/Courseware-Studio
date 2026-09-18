(() => {
  // 只负责交互接线：choice / hotspot / sequence / bucket。
  // 时间轴、音频时钟、场景、字幕、进度条全部归宿主页面所有。

  function parseConfig(el){try{return JSON.parse(el.getAttribute('data-interaction')||'{}')}catch{return {}}}
  function configError(kind, message){ throw new Error('Courseware interaction contract ('+kind+'): '+message); }
  function validateConfig(el, kind, config){
    const id = el.id ? '#'+el.id : kind;
    if(kind==='choice'){
      const opts=config.options||config.choices;
      if(!Array.isArray(opts)||!opts.length) configError(kind, id+' 缺少 options/choices');
      const domIds=[...el.querySelectorAll('[data-choice-id]')].map(n=>String(n.dataset.choiceId));
      const ids=opts.map(o=>String(o&&o.id));
      if(new Set(ids).size!==ids.length || new Set(domIds).size!==domIds.length) configError(kind, id+' 选项 id 必须唯一');
      if(ids.length!==domIds.length || ids.some(v=>!domIds.includes(v))) configError(kind, id+' 配置选项与 DOM 选项不一致');
      if(opts.filter(o=>o&&o.correct===true).length!==1) configError(kind, id+' 必须且只能有一个 correct=true');
    } else if(kind==='hotspot'){
      const opts=config.options||config.spots;
      if(!Array.isArray(opts)||!opts.length) configError(kind, id+' 缺少 options/spots');
      const domIds=[...el.querySelectorAll('[data-hotspot-id]')].map(n=>String(n.dataset.hotspotId));
      const ids=opts.map(o=>String(o&&o.id));
      if(new Set(ids).size!==ids.length || new Set(domIds).size!==domIds.length) configError(kind, id+' hotspot id 必须唯一');
      if(ids.length!==domIds.length || ids.some(v=>!domIds.includes(v))) configError(kind, id+' 配置 hotspot 与 DOM 不一致');
      if(opts.filter(o=>o&&o.correct===true).length!==1) configError(kind, id+' 必须且只能有一个 correct=true');
    } else if(kind==='sequence'){
      const expected=(config.correct_order||config.answer||[]).map(String);
      const domIds=[...el.querySelectorAll('.sequence-item')].map(n=>String(n.dataset.sequenceId));
      if(!expected.length) configError(kind, id+' 缺少 correct_order/answer');
      if(new Set(expected).size!==expected.length || new Set(domIds).size!==domIds.length) configError(kind, id+' sequence id 必须唯一');
      if(expected.length!==domIds.length || expected.some(v=>!domIds.includes(v))) configError(kind, id+' 正确顺序必须完整覆盖所有 sequence-item');
    } else if(kind==='bucket'){
      const answer=config.answer;
      if(!answer || typeof answer!=='object' || Array.isArray(answer) || !Object.keys(answer).length) configError(kind, id+' 缺少 answer');
      const itemIds=[...el.querySelectorAll('.bucket-item')].map(n=>String(n.dataset.bucketItem));
      const itemSet=new Set(itemIds), answerIds=Object.keys(answer).map(String), answerSet=new Set(answerIds);
      if(itemSet.size!==itemIds.length || answerSet.size!==answerIds.length) configError(kind, id+' bucket item id 必须唯一');
      if(itemSet.size!==answerSet.size || itemIds.some(v=>!answerSet.has(v))) configError(kind, id+' answer 必须覆盖全部 bucket-item');
      const bucketIds=new Set([...el.querySelectorAll('[data-drop][data-bucket-id]')].map(n=>String(n.dataset.bucketId)));
      answerIds.forEach(k=>{ if(!bucketIds.has(String(answer[k]))) configError(kind, id+' answer 引用了不存在的 bucket: '+answer[k]); });
    } else {
      configError(kind||'unknown', id+' 不支持的 interaction type');
    }
  }
  // 门禁答对的唯一放行信号：correct===true 时写 data-locked='1'（页面用
  // MutationObserver 观察它，不要轮询）。本运行时不记录作答、不计算掌握度。
  function finish(el,msg,opts){
    opts=opts||{};
    const fb=el.querySelector('.interaction-feedback');
    if(fb){
      fb.textContent=msg||'';
      fb.hidden=!msg;
      fb.classList.remove('is-correct','is-wrong');
      if(opts.correct===true) fb.classList.add('is-correct');
      else if(opts.correct===false) fb.classList.add('is-wrong');
    }
    el.dataset.completed=opts.correct===true?'1':'0';
    if(opts.correct!==true) return;
    el.classList.add('is-completed');
    el.dataset.locked='1';
    el.querySelectorAll('button').forEach(b=>{b.disabled=true;});
    const badge=el.querySelector('.interaction-badge');
    if(badge) badge.hidden=false;
  }
  // ── 拖拽 / 点选手势内核 ──────────────────────────────────────────────
  // sequence（列表内排序）与 bucket（跨筐搬运）共用这一份。一次按下之后解析成两种手势：
  // 位移 ≤ 3px 算**点选**（onTap），否则算**拖拽**（onDrop）。写成一个内核而不是两套，
  // 是为了让两种形态手感一致；顺带让"点一下"成为一等手势——拖拽在触屏上不稳，
  // 点选式（点条目 → 点筐）是那条兜底的路。
  // 无 PointerEvent 的旧环境用鼠标事件（这一路不支持触屏）。原生 DnD 那条路已删：
  // 鼠标适配器覆盖同样的环境，还多一个"点"的手势，严格更优。
  const GEST = (typeof window !== 'undefined' && window.PointerEvent)
    ? { down:'pointerdown', move:'pointermove', up:'pointerup', cancel:'pointercancel' }
    : { down:'mousedown',   move:'mousemove',   up:'mouseup',   cancel:null };

  // 元素级幂等绑定：交互块重建后会重新接线，块里那些**没被重建**的持久按钮
  // （提交键、提示键）不该跟着多挂一层监听——多一层的表现是"点一下触发两次"，很隐蔽。
  function once(node, ev, fn){ if(!node || node.dataset.bound === '1') return;
    node.dataset.bound = '1'; node.addEventListener(ev, fn); }

  function dropHolder(c){ return c.querySelector('[data-drop-slot]') || c; }

  function makeDraggable(o){
    const root = o.root, itemSel = o.itemSel, dropSel = o.dropSel;
    const containers = () => dropSel ? [...root.querySelectorAll(dropSel)] : [root];
    // 指针落在哪个容器里，以及该插到该容器内哪一项之前
    const locate = (x, y) => {
      const box = containers().find(c => { const r = c.getBoundingClientRect();
        return x >= r.left && x <= r.right && y >= r.top && y <= r.bottom; });
      if(!box) return null;
      const holder = dropHolder(box);
      const before = [...holder.querySelectorAll(itemSel)]
        .find(it => { const r = it.getBoundingClientRect(); return y < r.top + r.height/2; });
      return { holder: holder, before: before || null };
    };
    [...root.querySelectorAll(itemSel)].forEach(item => {
      if(item.dataset.gesture === '1') return;      // 幂等：接线可能被反复调用
      item.dataset.gesture = '1';
      item.addEventListener(GEST.down, e => {
        if(e.button !== undefined && e.button !== 0) return;
        if(o.enabled && !o.enabled()) return;
        e.preventDefault();
        const r = item.getBoundingClientRect(), dx = e.clientX - r.left, dy = e.clientY - r.top;
        let moved = false;
        const clone = item.cloneNode(true);
        clone.classList.add('drag-ghost');
        clone.style.cssText = 'position:fixed;left:'+r.left+'px;top:'+r.top+'px;width:'+r.width
          +'px;margin:0;z-index:9999;opacity:.97;pointer-events:none;';
        (document.body || item.ownerDocument.body).appendChild(clone);
        item.classList.add('drag-src');
        const move = ev => {
          if(!moved && Math.abs(ev.clientX - e.clientX) + Math.abs(ev.clientY - e.clientY) > 3)
            moved = true;
          if(!moved) return;
          clone.style.left = (ev.clientX - dx) + 'px';
          clone.style.top  = (ev.clientY - dy) + 'px';
          const at = locate(ev.clientX, ev.clientY);
          if(at){ if(at.before) at.holder.insertBefore(item, at.before); else at.holder.appendChild(item); }
        };
        const end = () => {
          document.removeEventListener(GEST.move, move);
          document.removeEventListener(GEST.up, end);
          if(GEST.cancel) document.removeEventListener(GEST.cancel, end);
          item.classList.remove('drag-src');
          if(clone.parentNode) clone.parentNode.removeChild(clone);
          if(!moved){ if(o.onTap) o.onTap(item); }        // 没动 = 点选
          else if(o.onDrop) o.onDrop(item);
        };
        document.addEventListener(GEST.move, move);
        document.addEventListener(GEST.up, end);
        if(GEST.cancel) document.addEventListener(GEST.cancel, end);
      });
    });
  }

  function wireInteractions(){
    document.querySelectorAll('[data-interaction]').forEach(el=>{
      // 动态 gate 的 shell 在首次调用时还没有题目节点；openGate() 填入题目后
      // 必须能再次扫描新增节点。不能在元素层直接 return：幂等性由 once() 和
      // makeDraggable() 的节点级标记保证，既不会重复监听，也不会漏掉新节点。
      const config=parseConfig(el), kind=el.dataset.interactionType;
      // 模板里的 gate shell 初始只有空的 data-interaction，占位阶段不校验；
      // buildChoice/buildHotspot/buildBucket/buildSequence 写入真实配置后，openGate() 会再次接线并校验。
      if((el.getAttribute('data-interaction') || '').trim()) validateConfig(el, kind, config);
      // ── choice：单选 ───────────────────
      if(kind==='choice'){
        const fb=el.querySelector('.interaction-feedback');
        el.querySelectorAll('[data-choice-id]').forEach(btn=>once(btn,'click',()=>{
          if(el.dataset.locked==='1')return;
          const cfg=parseConfig(el);                       // 点击时再读：页面改了配置不用重接线
          const opts=cfg.options||cfg.choices||[];
          const correctnessRequired=opts.some(o=>o&&typeof o.correct==='boolean');
          el.querySelectorAll('[data-choice-id]').forEach(b=>b.dataset.selected='0');
          btn.dataset.selected='1';
          const opt=opts.find(o=>String(o.id)===String(btn.dataset.choiceId));
          const correct=opt?.correct===true;
          const msg=opt?.feedback||(correct?'正确，继续。':correctnessRequired?'再想一步，再试一次。':'已记录。');
          if(fb){fb.textContent=msg;fb.hidden=false;fb.classList.remove('is-correct','is-wrong');fb.classList.add(correct?'is-correct':'is-wrong');}
          if(correct){ finish(el,msg,{correct:true,detail:String(btn.dataset.choiceId)}); }
          else { finish(el,msg,{correct:false,detail:String(btn.dataset.choiceId)}); }
        }));
      }

      // ── hotspot：在图上点部位 ──────────────────────────────────────────
      // 判定与单选同形（config.options[{id,correct,feedback}]），差别只在"答案不是一个句子，
      // 而是图上的一处"。可点区是任何带 [data-hotspot-id] 的元素：画布上的真图形、
      // 或门禁卡片里的示意图都行（画布被浮层盖着，把图放进卡片最稳，见 interactions.md §2.4）。
      if(kind==='hotspot'){
        const fb=el.querySelector('.interaction-feedback');
        el.querySelectorAll('[data-hotspot-id]').forEach(spot=>once(spot,'click',()=>{
          if(el.dataset.locked==='1')return;
          if(spot.dataset.hotspotState==='miss')return;    // 同一处点第二遍不重复反馈
          const cfg=parseConfig(el);
          const opts=cfg.options||cfg.spots||[];
          const needsOne=opts.some(o=>o&&o.correct===true);
          const id=String(spot.dataset.hotspotId);
          const opt=opts.find(o=>String(o.id)===id);
          const correct=opt?.correct===true;
          const msg=opt?.feedback||(correct?'就是这一处。':'不是这一处，再看看。');
          if(fb){fb.textContent=msg;fb.hidden=false;fb.classList.remove('is-correct','is-wrong');fb.classList.add(correct?'is-correct':'is-wrong');}
          if(correct){ spot.dataset.hotspotState='hit'; finish(el,msg,{correct:true,detail:id}); }
          else { spot.dataset.hotspotState='miss'; finish(el,msg,{correct:false,detail:id}); }
        }));
      }

      // ── sequence：列表内拖拽排序 ───────────────────────────────────────
      if(kind==='sequence'){
        const list=el.querySelector('.sequence-list');
        if(list) makeDraggable({ root:list, itemSel:'.sequence-item',
          enabled:()=>el.dataset.locked!=='1' });
        once(el.querySelector('[data-sequence-submit]'),'click',()=>{
          const cfg=parseConfig(el);
          const order=list?[...list.querySelectorAll('.sequence-item')].map(x=>String(x.dataset.sequenceId)):[];
          const expected=(cfg.correct_order||cfg.answer||[]).map(String);
          if(expected.length!==order.length || !order.every((v,i)=>v===expected[i])){
            const fb=el.querySelector('.interaction-feedback');
            if(fb){fb.hidden=false;fb.classList.remove('is-correct');fb.classList.add('is-wrong');
              fb.textContent=cfg.wrong_text||'顺序还不对，再调整一次。';}
            return;
          }
          // 排对之后**必须**走 finish(correct:true)：data-locked 是页面唯一的放行信号（runtime.md §4.1）
          finish(el,cfg.hit_text||'顺序正确。',{correct:true,detail:order.join(',')});
        });
      }

      // ── bucket：把条目放进筐（归类 / 边界） ─────────────────────────────
      // config.answer = {条目id: 筐id}；config.feedback = {条目id: 放错时的解释}。
      // 拖进筐，或"点条目 → 点筐"。未归完或有放错 → 不锁；全对才 finish(correct:true)。
      if(kind==='bucket'){
        const itemSel='.bucket-item', dropSel='[data-drop]';
        const fb=el.querySelector('.interaction-feedback');
        const placed=()=>[...el.querySelectorAll(itemSel)].reduce((acc,it)=>{
          const box=it.closest(dropSel);
          acc[String(it.dataset.bucketItem)]=box?String(box.dataset.bucketId||''):'';
          return acc; },{});
        const paint=()=>{ el.querySelectorAll(itemSel).forEach(it=>{
          it.dataset.placed=it.closest(dropSel)?'1':'0'; }); };
        paint();
        makeDraggable({ root:el, itemSel:itemSel, dropSel:dropSel,
          enabled:()=>el.dataset.locked!=='1',
          onTap:it=>{                                   // 点条目：选中 / 取消选中
            const same=it.dataset.picked==='1';
            el.querySelectorAll(itemSel).forEach(x=>{x.dataset.picked='0';});
            it.dataset.picked=same?'0':'1';
          },
          onDrop:()=>paint() });
        // 筐的点击处理**无状态**（"选中了哪个条目"只存在 DOM 的 data-picked 上），
        // 于是它可以只绑一次：页面重建条目、反复接线都不会把监听叠起来。
        el.querySelectorAll(dropSel).forEach(box=>once(box,'click',ev=>{
          if(el.dataset.locked==='1')return;
          // 点在**条目**上的那一下会冒泡到筐/待放区，不能当成"往这里放"：
          // 否则"点 A 再点 B"时，点 B 的冒泡会把刚选中的 B 立刻放回原处，看起来是选不中。
          if(ev.target && ev.target.closest && ev.target.closest(itemSel))return;
          const picked=el.querySelector(itemSel+'[data-picked="1"]');
          if(!picked)return;
          dropHolder(box).appendChild(picked);          // 认 [data-drop-slot]（bucket 的内层 ul）
          picked.dataset.picked='0'; paint();
        }));
        once(el.querySelector('[data-bucket-submit]'),'click',()=>{
          const cfg=parseConfig(el);
          const answer=cfg.answer||{}, why=cfg.feedback||{};
          const now=placed(), ids=Object.keys(answer);
          const domIds=[...el.querySelectorAll(itemSel)].map(it=>String(it.dataset.bucketItem));
          if(domIds.length!==ids.length || domIds.some(k=>!Object.prototype.hasOwnProperty.call(answer,k))){
            throw new Error('Courseware interaction contract (bucket): answer 与当前 bucket-item 不一致');
          }
          const empty=ids.filter(k=>!now[k]);
          if(empty.length){
            if(fb){fb.hidden=false;fb.classList.remove('is-correct','is-wrong');
              fb.classList.add('is-wrong');fb.textContent=cfg.unplaced_text||'还有没放进筐的。';}
            return;
          }
          const wrong=ids.filter(k=>String(answer[k])!==String(now[k]));
          if(wrong.length){
            if(fb){fb.hidden=false;fb.classList.remove('is-correct');fb.classList.add('is-wrong');
              fb.textContent=why[wrong[0]]||'有一处放错了，再想想。';}
            wrong.forEach(k=>{ const it=el.querySelector('[data-bucket-item="'+k+'"]');
              if(it)it.dataset.bucketState='miss'; });
            return;
          }
          el.querySelectorAll(itemSel).forEach(it=>{ it.dataset.bucketState='hit'; });
          finish(el,cfg.hit_text||'分对了。',{correct:true,detail:JSON.stringify(now)});
        });
      }
      once(el.querySelector('[data-hint-action]'),'click',()=>{
        const h=el.querySelector('.interaction-hint'); if(h)h.hidden=!h.hidden;});
    });
  }

  wireInteractions();
  window.coursewareStudioWire = wireInteractions;
})();
