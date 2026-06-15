
/* Adobe-style signature overlays: signature images placed ON the PDF preview
   page (never in the editable text). Persisted as fractional page coordinates
   and flattened into the final PDF server-side (see stamp_overlays). Shares the
   editor's global scope: BASE/OID/DT/TOK/SIG_OVERLAYS/togglePreview. */
(function(){
  'use strict';
  var overlays = (typeof SIG_OVERLAYS !== 'undefined' && Array.isArray(SIG_OVERLAYS)) ? SIG_OVERLAYS.slice() : [];
  var saveT = null;
  var _sigData = overlays.length ? overlays[overlays.length - 1].image : '';   // reuse the last-placed signature
  var bar = null, modal = null, drawing = false, dctx = null, dlast = null;

  function _save(){
    clearTimeout(saveT);
    saveT = setTimeout(function(){
      var el = document.getElementById('saved');
      if(el){ el.textContent = 'Saving\u2026'; el.style.color = '#fbbf24'; }
      fetch(BASE + '/save/' + OID + '/' + DT + '/' + TOK, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ signatures_overlay: overlays })
      }).then(function(r){
        if(el){ el.textContent = (r && r.ok) ? 'Saved \u2713' : 'Save failed';
                el.style.color = (r && r.ok) ? '#86efac' : '#fca5a5'; }
      }).catch(function(){ if(el){ el.textContent = 'Save failed'; el.style.color = '#fca5a5'; } });
    }, 350);
  }

  function _pageImg(wrap){ return wrap.querySelector('img.pg'); }

  // Rebuild overlays[] from the live DOM (fractions of each page), then persist.
  function _commit(){
    var out = [], wraps = document.querySelectorAll('#pv-pages .pg-wrap');
    for(var i=0;i<wraps.length;i++){
      var pr = wraps[i].getBoundingClientRect();   // measure the WRAP (always sized via aspect-ratio) so a not-yet-loaded image never drops the overlay
      if(!pr.width || !pr.height) continue;
      var ovs = wraps[i].querySelectorAll('.sig-ov');
      for(var j=0;j<ovs.length;j++){
        var er = ovs[j].getBoundingClientRect();
        out.push({
          page: i,
          x: (er.left - pr.left) / pr.width,
          y: (er.top  - pr.top ) / pr.height,
          w: er.width / pr.width,
          h: er.height / pr.height,
          image: ovs[j].getAttribute('data-img')
        });
      }
    }
    overlays = out;
    _save();
  }

  // which page is under the pointer (so a signature can be dragged page-to-page)
  function _pageUnder(x, y){
    var wraps = document.querySelectorAll('#pv-pages .pg-wrap');
    for(var i=0;i<wraps.length;i++){
      var r = wraps[i].getBoundingClientRect();
      if(x>=r.left && x<=r.right && y>=r.top && y<=r.bottom) return wraps[i];
    }
    return null;
  }

  function _bindDrag(el){
    var d = null;
    el.addEventListener('pointerdown', function(e){
      if(e.target.classList.contains('sig-ov-grip') || e.target.classList.contains('sig-ov-del')) return;
      e.preventDefault();
      try{ el.setPointerCapture(e.pointerId); }catch(x){}
      var er = el.getBoundingClientRect();
      d = { dx: e.clientX - er.left, dy: e.clientY - er.top };
      el.classList.add('drag');
    });
    el.addEventListener('pointermove', function(e){
      if(!d) return;
      var page = _pageUnder(e.clientX, e.clientY) || el.parentNode;
      if(page !== el.parentNode) page.appendChild(el);          // move across pages
      var pr = page.getBoundingClientRect();
      var left = (e.clientX - d.dx - pr.left) / pr.width;
      var top  = (e.clientY - d.dy - pr.top ) / pr.height;
      left = Math.max(0, Math.min(1 - el.offsetWidth  / pr.width,  left));
      top  = Math.max(0, Math.min(1 - el.offsetHeight / pr.height, top));
      el.style.left = (left*100) + '%';
      el.style.top  = (top*100) + '%';
    });
    function up(e){ if(!d) return; d = null; el.classList.remove('drag');
      try{ el.releasePointerCapture(e.pointerId); }catch(x){} _commit(); }
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  }

  function _bindResize(el, grip){
    var r = null;
    grip.addEventListener('pointerdown', function(e){
      e.preventDefault(); e.stopPropagation();
      try{ grip.setPointerCapture(e.pointerId); }catch(x){}
      r = { x: e.clientX, w: el.offsetWidth, pr: el.parentNode.getBoundingClientRect() };
    });
    grip.addEventListener('pointermove', function(e){
      if(!r) return;
      var nw = Math.max(24, Math.min(r.pr.width, r.w + (e.clientX - r.x)));
      el.style.width = (nw / r.pr.width * 100) + '%';   // height follows the image aspect ratio
    });
    function up(e){ if(!r) return; r = null;
      try{ grip.releasePointerCapture(e.pointerId); }catch(x){} _commit(); }
    grip.addEventListener('pointerup', up);
    grip.addEventListener('pointercancel', up);
  }

  function _makeOverlay(wrap, ov){
    var el = document.createElement('div');
    el.className = 'sig-ov';
    el.setAttribute('data-img', ov.image);
    el.style.left  = (ov.x*100) + '%';
    el.style.top   = (ov.y*100) + '%';
    el.style.width = (ov.w*100) + '%';
    var im = document.createElement('img'); im.src = ov.image; im.alt = 'signature';
    el.appendChild(im);
    var del = document.createElement('button');
    del.className = 'sig-ov-del'; del.type = 'button'; del.textContent = '\u2715';
    del.title = 'Remove signature';
    del.addEventListener('click', function(e){ e.stopPropagation(); el.remove(); _commit(); });
    el.appendChild(del);
    var grip = document.createElement('div'); grip.className = 'sig-ov-grip';
    el.appendChild(grip);
    _bindDrag(el);
    _bindResize(el, grip);
    wrap.appendChild(el);
    return el;
  }

  // Re-paint overlay DOM from overlays[] (called after each preview refresh).
  window.renderSigOverlays = function(){
    var wraps = document.querySelectorAll('#pv-pages .pg-wrap');
    if(!wraps.length) return;
    for(var i=0;i<wraps.length;i++){
      var old = wraps[i].querySelectorAll('.sig-ov');
      for(var k=0;k<old.length;k++) old[k].remove();
    }
    for(var j=0;j<overlays.length;j++){
      var o = overlays[j];
      var w = (o.page >= 0 && o.page < wraps.length) ? wraps[o.page] : wraps[0];
      if(w) _makeOverlay(w, o);
    }
  };

  // ── full-screen placement stage ──────────────────────────────────────────
  window.placeSignature = function(){
    var pv = document.querySelector('.pv');
    if(pv && !pv.classList.contains('open') && typeof togglePreview === 'function') togglePreview();
    if(!pv) return;
    pv.classList.add('sigmode');
    _ensureBar();
    _refreshPalette();
    if(!_sigData) _openCreate();          // no signature yet -> ask for one
  };
  function _exitPlacement(){
    var pv = document.querySelector('.pv'); if(pv) pv.classList.remove('sigmode');
    if(bar) bar.style.display = 'none';
    _commit();
  }
  function _ensureBar(){
    var pv = document.querySelector('.pv'); if(!pv) return;
    if(bar){ bar.style.display = 'flex'; return; }
    bar = document.createElement('div'); bar.className = 'sig-bar';
    bar.innerHTML =
      '<span class="sig-bar-t">Place your signature</span>' +
      '<div id="sig-palette" class="sig-pal"></div>' +
      '<button type="button" class="sig-bar-btn" id="sig-create-btn">＋ Create / change</button>' +
      '<span class="sig-bar-hint">Click your signature to drop it, then drag it onto any page</span>' +
      '<button type="button" class="sig-bar-done" id="sig-done-btn">Done</button>';
    pv.insertBefore(bar, pv.firstChild);
    document.getElementById('sig-create-btn').addEventListener('click', _openCreate);
    document.getElementById('sig-done-btn').addEventListener('click', _exitPlacement);
    bar.style.display = 'flex';
  }
  function _refreshPalette(){
    var p = document.getElementById('sig-palette'); if(!p) return;
    if(_sigData){
      p.innerHTML = '<img class="sig-chip" id="sig-chip" src="' + _sigData + '" alt="signature" title="Click to drop on the page">';
      document.getElementById('sig-chip').addEventListener('click', _placeFromPalette);
    } else {
      p.innerHTML = '<span class="sig-pal-empty">No signature yet — click Create</span>';
    }
  }
  function _visiblePage(){
    var pv = document.querySelector('.pv'), wraps = document.querySelectorAll('#pv-pages .pg-wrap');
    if(!wraps.length) return null;
    var mid = pv.getBoundingClientRect().top + pv.clientHeight/2, best = wraps[0], bd = 1e9;
    for(var i=0;i<wraps.length;i++){ var r = wraps[i].getBoundingClientRect();
      var dd = Math.abs((r.top + r.bottom)/2 - mid); if(dd < bd){ bd = dd; best = wraps[i]; } }
    return best;
  }
  function _placeFromPalette(){
    if(!_sigData) return;
    var wrap = _visiblePage(); if(!wrap) return;
    var im = new Image();
    im.onload = function(){
      var pr = wrap.getBoundingClientRect();
      var ar = (im.width/im.height) || 3, pw = pr.width||600, ph = pr.height||850;
      var wFrac = Math.min(0.28, 180/pw), hFrac = (wFrac*pw/ar)/ph;
      if(hFrac > 0.11){ hFrac = 0.11; wFrac = (hFrac*ph*ar)/pw; }   // cap height so it never covers the cell
      _makeOverlay(wrap, { x: 0.5 - wFrac/2, y: 0.42, w: wFrac, h: hFrac, image: _sigData });
      _commit();
    };
    im.src = _sigData;
  }

  // ── create-signature modal: draw / type / upload ─────────────────────────
  function _openCreate(){
    if(modal){ modal.style.display = 'flex'; _showTab('draw'); _clearCanvas(); return; }
    modal = document.createElement('div'); modal.className = 'sig-modal';
    modal.innerHTML =
      '<div class="sig-modal-box">' +
        '<h3>Add your signature</h3>' +
        '<div class="sig-tabs">' +
          '<div class="sig-tab on" data-tab="draw">Draw</div>' +
          '<div class="sig-tab" data-tab="type">Type</div>' +
          '<div class="sig-tab" data-tab="upload">Upload</div>' +
        '</div>' +
        '<div class="sig-panel on" data-panel="draw"><canvas id="sig-canvas"></canvas>' +
          '<div style="text-align:right;margin-top:6px;"><button type="button" class="sig-bar-btn" id="sig-clear" ' +
          'style="background:#eef1f6;color:#374151;border-color:#d4dae6;">Clear</button></div></div>' +
        '<div class="sig-panel" data-panel="type"><input id="sig-type" placeholder="Type your name"><div id="sig-type-prev"></div></div>' +
        '<div class="sig-panel" data-panel="upload"><input type="file" id="sig-upload" accept=".png,.jpg,.jpeg">' +
          '<p style="font-size:11.5px;color:#64748b;margin-top:8px;">PNG or JPG, max 5 MB.</p></div>' +
        '<div class="sig-modal-actions"><button type="button" class="sig-mbtn x" id="sig-cancel">Cancel</button>' +
          '<button type="button" class="sig-mbtn ok" id="sig-use">Use signature</button></div>' +
      '</div>';
    document.body.appendChild(modal);
    var tabs = modal.querySelectorAll('.sig-tab');
    for(var i=0;i<tabs.length;i++) tabs[i].addEventListener('click', function(){ _showTab(this.getAttribute('data-tab')); });
    var cv = document.getElementById('sig-canvas');
    cv.width = cv.clientWidth || 430; cv.height = 150; dctx = cv.getContext('2d');
    dctx.lineWidth = 2.4; dctx.lineCap = 'round'; dctx.lineJoin = 'round'; dctx.strokeStyle = '#111';
    cv.addEventListener('pointerdown', function(e){ drawing = true; dlast = _cpos(cv,e); try{cv.setPointerCapture(e.pointerId);}catch(x){} });
    cv.addEventListener('pointermove', function(e){ if(!drawing) return; var p = _cpos(cv,e);
      dctx.beginPath(); dctx.moveTo(dlast.x,dlast.y); dctx.lineTo(p.x,p.y); dctx.stroke(); dlast = p; });
    cv.addEventListener('pointerup', function(){ drawing = false; });
    cv.addEventListener('pointercancel', function(){ drawing = false; });
    document.getElementById('sig-clear').addEventListener('click', _clearCanvas);
    var ti = document.getElementById('sig-type');
    ti.addEventListener('input', function(){ document.getElementById('sig-type-prev').textContent = ti.value; });
    document.getElementById('sig-cancel').addEventListener('click', function(){ modal.style.display = 'none'; });
    document.getElementById('sig-use').addEventListener('click', _useSignature);
    modal.addEventListener('click', function(e){ if(e.target === modal) modal.style.display = 'none'; });
    _showTab('draw');
  }
  function _cpos(cv,e){ var r = cv.getBoundingClientRect();
    return { x: (e.clientX-r.left)*(cv.width/r.width), y: (e.clientY-r.top)*(cv.height/r.height) }; }
  function _clearCanvas(){ if(dctx){ var c = dctx.canvas; dctx.clearRect(0,0,c.width,c.height); } }
  function _showTab(name){
    var tabs = modal.querySelectorAll('.sig-tab'), pans = modal.querySelectorAll('.sig-panel');
    for(var i=0;i<tabs.length;i++) tabs[i].classList.toggle('on', tabs[i].getAttribute('data-tab')===name);
    for(var j=0;j<pans.length;j++) pans[j].classList.toggle('on', pans[j].getAttribute('data-panel')===name);
  }
  function _canvasNonBlank(c){
    try{ var d = c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      for(var i=3;i<d.length;i+=4){ if(d[i]!==0) return true; } }catch(e){ return true; } return false;
  }
  function _typeToData(text){
    var c = document.createElement('canvas'); c.width = 520; c.height = 150;
    var x = c.getContext('2d'); x.fillStyle = '#111'; x.textBaseline = 'middle';
    x.font = '64px "Brush Script MT","Segoe Script","Comic Sans MS",cursive';
    x.fillText(text, 16, 82); return c.toDataURL('image/png');
  }
  // Compress to a small data URL so it ALWAYS clears the server's size cap
  // (oversized images were being silently dropped -> "some signatures missing").
  function _compress(img){
    var maxSide = 380, sc = Math.min(1, maxSide / Math.max(img.width || 1, img.height || 1));
    var c = document.createElement('canvas');
    c.width = Math.max(1, Math.round((img.width||1)*sc));
    c.height = Math.max(1, Math.round((img.height||1)*sc));
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    var data = c.toDataURL('image/png');
    if(data.length > 520000){                       // big photo -> flatten to JPEG
      var c2 = document.createElement('canvas'); c2.width = c.width; c2.height = c.height;
      var x = c2.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0,0,c2.width,c2.height);
      x.drawImage(c, 0, 0); data = c2.toDataURL('image/jpeg', 0.82);
    }
    return data;
  }
  function _useSignature(){
    var active = modal.querySelector('.sig-tab.on').getAttribute('data-tab');
    if(active === 'draw'){
      var c = document.getElementById('sig-canvas');
      if(!_canvasNonBlank(c)){ alert('Please draw your signature first.'); return; }
      _setSig(c.toDataURL('image/png'));
    } else if(active === 'type'){
      var t = (document.getElementById('sig-type').value || '').trim();
      if(!t){ alert('Please type your name.'); return; }
      _setSig(_typeToData(t));
    } else {
      var f = document.getElementById('sig-upload').files && document.getElementById('sig-upload').files[0];
      if(!f){ alert('Please choose an image file.'); return; }
      if(f.size > 5*1024*1024){ alert('Image too large — max 5 MB.'); return; }
      var img = new Image();
      img.onload = function(){ _setSig(_compress(img)); };
      img.onerror = function(){ alert('Could not read that image.'); };
      img.src = URL.createObjectURL(f);
    }
  }
  function _setSig(data){ _sigData = data; if(modal) modal.style.display = 'none'; _refreshPalette(); _placeFromPalette(); }

  // kept for the topbar file input (legacy entry point)
  window._sigOverlayFromFile = function(input){
    var f = input.files && input.files[0]; if(!f) return;
    if(f.size > 5*1024*1024){ alert('Signature image too large — max 5 MB.'); input.value = ''; return; }
    var img = new Image();
    img.onload = function(){
      var pv = document.querySelector('.pv');
      if(pv && !pv.classList.contains('sigmode')) window.placeSignature();
      _setSig(_compress(img)); input.value = ''; };
    img.onerror = function(){ alert('Could not read that image file.'); input.value = ''; };
    img.src = URL.createObjectURL(f);
  };
})();
